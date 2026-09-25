"""HCP feature catalog: resolve the features a Pro's brief references to their
human-readable meaning, so the idea generator knows what each feature IS and
whether this Pro uses it. Loaded once at import from the packaged CSV.

No RAG, no dump: only features the brief already references are resolved,
mirroring warmstart.retrieve's select-the-relevant-thing discipline.
"""

import csv
import json
import re
from dataclasses import dataclass
from pathlib import Path

from waypoint.n8n import OrgBrief

# Packaged runtime data, same convention as worker.CALIBRATION_PATH.
CATALOG_PATH = Path(__file__).parents[2] / "data" / "hcp_feature_catalog.csv"

_STATE_PREFIX = "feature_"
_STATE_SUFFIX = "_state"
_ATTACHED_STATES = frozenset({"attached_active", "attached_unused", "attached_usage_unknown"})
_CTA_COLUMNS = ("label", "url", "works_on", "notes")

# works_on's real delivery channels. The CSV also uses this column for
# catalog sentinels (broken/no_cta/not_applicable) that are not channels.
_REAL_CHANNELS = frozenset({"web", "ios", "mobile"})

# Sentence boundary = ". " followed by a capital. Skips "e.g. " / "i.e. " which
# several descriptions contain, so the first sentence is not truncated mid-clause.
_SENTENCE_END = re.compile(r"\. (?=[A-Z])")

# Only when the feasibility toggle is on: one directive appended after the block.
# Matches what the payload actually carries: a "[reachable on: ...]" tag per
# feature, or none at all (broken/no_cta/not_applicable are filtered out by
# _feasibility_suffix and never appear here, so the directive must not promise
# a "broken" signal the model will never see).
_FEASIBILITY_DIRECTIVE = (
    "Feasibility: prefer features reachable on this touch's delivery channel; "
    "do not anchor an idea on a feature with no channel reachable for the "
    "channel being sent."
)


@dataclass(frozen=True)
class CatalogEntry:
    feature: str
    description: str  # already trimmed to first sentence
    ctas: tuple[dict[str, str], ...]  # label/url/works_on/notes rows, file order


def _first_sentence(text: str) -> str:
    match = _SENTENCE_END.search(text)
    return text[: match.start() + 1] if match else text


def _load(path: Path = CATALOG_PATH) -> dict[str, CatalogEntry]:
    rows_by_feature: dict[str, list[dict[str, str]]] = {}
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            feature = (row.get("feature") or "").strip()
            if feature:
                rows_by_feature.setdefault(feature, []).append(row)
    catalog: dict[str, CatalogEntry] = {}
    for feature, rows in rows_by_feature.items():
        description = next(
            (r["description"].strip() for r in rows if (r.get("description") or "").strip()),
            "",
        )
        ctas = tuple(
            {col: (r.get(col) or "").strip() for col in _CTA_COLUMNS} for r in rows
        )
        catalog[feature] = CatalogEntry(feature, _first_sentence(description), ctas)
    return catalog


CATALOG: dict[str, CatalogEntry] = _load()


def resolve_cta(feature_key: str) -> dict[str, str] | None:
    """Return the first usable catalog CTA; sentinel rows are not destinations."""
    entry = CATALOG.get(feature_key)
    if entry is None:
        return None
    return next(
        (
            cta
            for cta in entry.ctas
            if cta["works_on"] in _REAL_CHANNELS
            and cta["label"]
            and cta["url"]
            and not any(
                marker in cta["notes"].casefold()
                for marker in ("untested", "unverified", "failed to load")
            )
        ),
        None,
    )


def _state_features(brief: OrgBrief) -> dict[str, str]:
    """{feature_key: state} for every feature_<key>_state set on the brief, in
    field-declaration order. Derived from the model's fields, so a new
    feature_<key>_state column resolves with no change here.

    Intentionally a superset: the brief exposes only ~10 feature_<key>_state
    columns plus top_unused_paid_feature, so a feature like wisetack (whose
    brief field is wisetack_state, not feature_wisetack_state) only surfaces
    here when it's the top_unused_paid_feature. That's deliberate, not a bug.
    """
    out: dict[str, str] = {}
    for name in type(brief).model_fields:
        if name.startswith(_STATE_PREFIX) and name.endswith(_STATE_SUFFIX):
            value = getattr(brief, name)
            if value is not None:
                out[name[len(_STATE_PREFIX) : -len(_STATE_SUFFIX)]] = value
    return out


def available_feature_keys(brief: OrgBrief) -> set[str]:
    states = _state_features(brief)
    entitled = {key for key, state in states.items() if state in _ATTACHED_STATES}
    if (
        brief.top_unused_paid_feature
        and (
            brief.top_unused_paid_feature not in states
            or states[brief.top_unused_paid_feature] in _ATTACHED_STATES
        )
    ):
        entitled.add(brief.top_unused_paid_feature)
    if brief.curated_context is None:
        return entitled
    # Promoted cards only establish plan compatibility, not attachment. A card
    # filtered out for this plan must never be recovered from the state alone.
    product_context = brief.curated_context.get("pc", {})
    if not isinstance(product_context, dict):
        return set()
    return {
        key for key in entitled
        if isinstance(product_context.get(key), dict)
        and product_context[key].get("e") == "available"
    }


def _feasibility_suffix(entry: CatalogEntry) -> str:
    works = sorted({c["works_on"] for c in entry.ctas} & _REAL_CHANNELS)
    return f" [reachable on: {', '.join(works)}]" if works else ""


def feature_context(brief: OrgBrief, *, feasibility: bool) -> str:
    """The resolved-features block for one Pro, or "" if nothing resolves.
    Ordered union: top_unused_paid_feature first (the priority activation
    target), then every feature_<key>_state present; deduped by key."""
    states = _state_features(brief)
    top = brief.top_unused_paid_feature or None
    keys: list[str] = ([top] if top else []) + [k for k in states if k != top]

    lines: list[str] = []
    for key in keys:
        entry = CATALOG.get(key)
        if entry is None or not entry.description:
            continue  # unresolvable or description-less pointer: skip, never crash
        tag = ", TOP UNUSED PAID FEATURE" if key == top else ""
        state = states.get(key, "unknown")
        line = f"- {key} (state: {state}{tag}): {entry.description}"
        if feasibility:
            line += _feasibility_suffix(entry)
        lines.append(line)
    if not lines:
        return ""

    header = "HCP features referenced in this Pro's context (reference data):"
    block = "\n".join([header, *lines])
    return f"{block}\n{_FEASIBILITY_DIRECTIVE}" if feasibility else block


def waypoint_context(brief: OrgBrief, *, feasibility: bool) -> str:
    """Return promoted context when present, otherwise the legacy Waypoint brief."""
    verified_destination_keys = sorted(
        key for key in available_feature_keys(brief) if resolve_cta(key) is not None
    )
    if brief.curated_context is not None:
        return json.dumps(
            {**brief.curated_context, "verified_destination_keys": verified_destination_keys},
            sort_keys=True,
            separators=(",", ":"),
        )
    known = brief.model_dump(mode="json", exclude_none=True, exclude={"curated_context"})
    unknown = [
        name for name in type(brief).model_fields
        if name != "curated_context" and getattr(brief, name) is None
    ]
    context = json.dumps(
        {"known": known, "unknown": unknown, "plan_eligibility": "unverified",
         "verified_destination_keys": verified_destination_keys},
        separators=(",", ":"),
    )
    block = feature_context(brief, feasibility=feasibility)
    return f"{context}\n{block}" if block else context
