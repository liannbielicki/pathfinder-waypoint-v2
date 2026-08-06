"""A persistent snapshot of Riley's persona cards, pinned until we release it.

Why this exists
---------------
A panel is a pure function of ``(segment, panel_size, seed)`` *within* one of
Riley's generations, and nothing in Pathfinder varies personas by org or by run.
So a long campaign is already persona-consistent -- until Riley regenerates her
subtype set mid-flight, at which point some passes score against generation A
and the remainder against generation B, inside a single experiment.

The request-level ``subtype_version`` pin cannot solve that: her service hard-409s
a retired version instead of serving it (spec section 6), so pinning detects drift
rather than preventing it. Here we keep our own copy of the raw payloads and read
from disk, which is the only thing that actually holds a persona set still.

Two modes, one class:
  * strict (default) -- read-only. A miss raises ``PersonaCardsError`` so the
    evaluator abstains, rather than silently pulling a fresh generation.
  * fill (``allow_fetch=True``) -- used by the refresh CLI: a miss is fetched live
    once and recorded. An existing entry is never re-fetched, so filling gaps can
    never disturb panels a campaign is already running against.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from pathfinder.persona_cards_client import PersonaCardsError
from pathfinder.persona_cards_contract import PersonaCardPanel, panel_from_dict
from pathfinder.segment_vocab import segment_code_to_persona_key

_SCHEMA_VERSION = 1


class MixedGenerationError(RuntimeError):
    """Raised when one snapshot spans more than one subtype_version.

    That is precisely the inconsistency the snapshot exists to prevent, so it is
    an error rather than a warning.
    """


def snapshot_key(segment: str, panel_size: int, seed: int) -> str:
    """The full identity of a panel draw within one generation.

    The segment is normalized so a cell code ("2A", the audience vocabulary)
    and its persona key ("service_a", what the evaluator's candidate carries)
    name the same draw — capture and runtime lookup use different vocabularies.
    """
    normalized = segment_code_to_persona_key(str(segment))
    return f"{normalized}|{int(panel_size)}|{int(seed)}"


def payload_from_panel(panel: PersonaCardPanel) -> dict:
    """Serialize a parsed panel back to its wire shape.

    Lossless for everything the system reads: ``PersonaCard.fields`` holds each
    card's complete dict, and the panel-level keys are fully modeled.
    """
    return {
        "panel_id": panel.panel_id,
        "subtype_version": panel.subtype_version,
        "segment": panel.segment,
        "n_personas": panel.n_personas,
        "personas": [dict(card.fields) for card in panel.personas],
    }


class PersonaCardSnapshot:
    """Disk-backed store of raw panel payloads keyed by ``snapshot_key``."""

    def __init__(self, path):
        self._path = Path(path)
        self._panels: dict = {}
        if self._path.exists():
            raw = json.loads(self._path.read_text())
            # Re-key on load: snapshots captured before segment normalization
            # hold cell-code keys ("2A|24|0") that runtime lookups would miss.
            for key, entry in dict(raw.get("panels", {})).items():
                try:
                    segment, panel_size, seed = key.rsplit("|", 2)
                    key = snapshot_key(segment, int(panel_size), int(seed))
                except ValueError:
                    pass
                self._panels[key] = entry

    @property
    def path(self) -> Path:
        return self._path

    def keys(self) -> list:
        return sorted(self._panels)

    def put(self, *, segment: str, panel_size: int, seed: int, payload: dict) -> None:
        self._panels[snapshot_key(segment, panel_size, seed)] = {
            "subtype_version": str(payload.get("subtype_version", "") or ""),
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "payload": payload,
        }

    def get_payload(self, *, segment: str, panel_size: int, seed: int) -> Optional[dict]:
        entry = self._panels.get(snapshot_key(segment, panel_size, seed))
        return entry["payload"] if entry else None

    def get_panel(self, *, segment: str, panel_size: int,
                  seed: int) -> Optional[PersonaCardPanel]:
        payload = self.get_payload(segment=segment, panel_size=panel_size, seed=seed)
        if payload is None:
            return None
        try:
            return panel_from_dict(payload)
        except Exception as exc:
            raise PersonaCardsError(
                f"snapshot entry for {snapshot_key(segment, panel_size, seed)!r} "
                f"is malformed: {exc}") from exc

    def subtype_versions(self) -> set:
        return {e.get("subtype_version", "") for e in self._panels.values()}

    def verify_single_generation(self) -> str:
        """Return the one generation present, or raise if the snapshot is mixed."""
        versions = self.subtype_versions()
        if not versions:
            raise ValueError(f"snapshot {self._path} is empty; nothing to verify")
        if len(versions) > 1:
            listed = ", ".join(sorted(versions))
            raise MixedGenerationError(
                f"snapshot {self._path} spans {len(versions)} persona generations "
                f"({listed}); a campaign must run against exactly one. Rebuild it "
                f"from scratch rather than refreshing part of it.")
        return next(iter(versions))

    def save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        body = {"schema_version": _SCHEMA_VERSION, "panels": self._panels}
        self._path.write_text(json.dumps(body, indent=2, sort_keys=True) + "\n")


#: The 16-box cells. A campaign draws from these and nothing else.
ALL_SEGMENTS = tuple(
    f"{row}{col}" for row in ("1", "2", "3", "4") for col in ("A", "B", "C", "D")
)

#: Riley's service floor. LocalReactionClient always requests at least this many
#: cards and slices locally, so one entry per segment covers every panel_size
#: at or below the floor -- i.e. both the search and confirm panels.
RILEY_MIN_PANEL = 24


def capture_panels(*, snapshot: PersonaCardSnapshot, live,
                   segments=ALL_SEGMENTS, panel_sizes=(RILEY_MIN_PANEL,),
                   seed: int = 0) -> dict:
    """Fill any missing panels in *snapshot* from the *live* service.

    Existing entries are never re-fetched, so this is safe to re-run against a
    snapshot a campaign is already using. Raises ``MixedGenerationError`` if the
    result spans generations -- which happens if Riley regenerates mid-capture.
    """
    filler = SnapshotCardsClient(snapshot=snapshot, live=live, allow_fetch=True)
    captured = skipped = 0
    for segment in segments:
        for panel_size in panel_sizes:
            if snapshot.get_payload(
                    segment=segment, panel_size=panel_size, seed=seed) is not None:
                skipped += 1
                continue
            filler.fetch_panel(segment=segment, panel_size=panel_size, seed=seed)
            captured += 1
    return {
        "captured": captured,
        "skipped": skipped,
        "subtype_version": snapshot.verify_single_generation(),
    }


class SnapshotCardsClient:
    """Drop-in for ``PersonaCardsClient``, served from a snapshot.

    Exposes the same ``fetch_panel``/``refetch`` surface, so it substitutes
    directly into ``LocalReactionClient`` with no other changes.
    """

    def __init__(self, *, snapshot: PersonaCardSnapshot, live=None,
                 allow_fetch: bool = False):
        if allow_fetch and live is None:
            raise ValueError("allow_fetch=True requires a live client to fill from")
        self._snapshot = snapshot
        self._live = live
        self._allow_fetch = allow_fetch

    @property
    def snapshot(self) -> PersonaCardSnapshot:
        return self._snapshot

    def fetch_panel(self, *, segment: str, panel_size: int, seed: int,
                    subtype_ids: Optional[list] = None,
                    subtype_version: Optional[str] = None) -> PersonaCardPanel:
        # A requested subtype_version is deliberately ignored on a hit: the
        # snapshot IS the pin, and honoring a stale request-level pin is what
        # 409-bricked live scoring before.
        panel = self._snapshot.get_panel(
            segment=segment, panel_size=panel_size, seed=seed)
        if panel is not None:
            return panel

        key = snapshot_key(segment, panel_size, seed)
        if not self._allow_fetch:
            raise PersonaCardsError(
                f"no snapshot entry for {key!r} in {self._snapshot.path}; refusing to "
                f"fetch a possibly-newer persona generation mid-campaign. Capture it "
                f"with scripts/snapshot_persona_cards.py, or unset "
                f"PATHFINDER_PERSONA_CARDS_SNAPSHOT to score against live cards.")

        fetched = self._live.fetch_panel(
            segment=segment, panel_size=panel_size, seed=seed,
            subtype_ids=subtype_ids, subtype_version=subtype_version)
        self._snapshot.put(segment=segment, panel_size=panel_size, seed=seed,
                           payload=payload_from_panel(fetched))
        return fetched

    def refetch(self, panel_id: str) -> PersonaCardPanel:
        if self._live is None:
            raise PersonaCardsError("refetch requires a live persona-cards client")
        return self._live.refetch(panel_id)
