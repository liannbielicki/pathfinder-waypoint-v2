"""Pick the winning idea per org-mode action-console run.

Purely downstream of Supabase rows. Suppression and ranking are imported from
``pathfinder.action_console.scoring`` (the canonical console logic) so the
exported winner always matches the console's own top-of-list.
"""
from __future__ import annotations

import dataclasses
import json
from typing import Any

from pathfinder.action_console.models import GeneratedIdea
from pathfinder.action_console.scoring import (
    is_suppressed,
    ranked_ideas,
    support_tier_for_idea,
)

READY = "ready_for_review"
NOT_ORG_MODE = "not_org_mode"
NO_WINNER = "no_winner"
RUN_FAILED = "run_failed"


@dataclasses.dataclass
class WinnerRow:
    run_id: str
    status: str
    org_id: str = ""
    org_uuid: str = ""
    theme: str = ""
    theme_category: str = ""
    third_theme: str = ""
    third_theme_category: str = ""


def _evidence(run_row: dict[str, Any]) -> list[dict[str, Any]]:
    audience = run_row.get("audience") or {}
    if isinstance(audience, str):
        try:
            audience = json.loads(audience)
        except ValueError:
            return []
    rows = audience.get("org_uuid_evidence") or []
    return rows if isinstance(rows, list) else []


def _candidates(idea_rows: list[dict[str, Any]]) -> list[GeneratedIdea]:
    out: list[GeneratedIdea] = []
    for row in idea_rows:
        payload = row.get("payload") or {}
        try:
            idea = GeneratedIdea.from_dict(payload)
        except (KeyError, TypeError, ValueError):
            continue  # malformed legacy payload: skip, never abort the batch
        if not is_suppressed(idea)[0] and support_tier_for_idea(idea) == "supported":
            # Only "supported" may reach a real Pro. "provisional" explicitly
            # means the evidence does not support the claim (this is also how
            # a screen-then-confirm idea that failed its confirm-panel re-score
            # is flagged, even though it still carries its original, often
            # inflated, search-panel score) and "unsupported" is worse — the
            # send path must apply the same confidence verdict the display
            # path (support_tier_for_idea) already applies.
            out.append(idea)
    return out


def select_winner(
    run_id: str,
    run_row: dict[str, Any] | None,
    idea_rows: list[dict[str, Any]] | None,
) -> WinnerRow:
    if not run_row or run_row.get("status") != "completed" or idea_rows is None:
        return WinnerRow(run_id=run_id, status=RUN_FAILED)
    evidence = _evidence(run_row)
    if len(evidence) != 1:
        # Segment run (or empty evidence): refuse to attribute an org.
        return WinnerRow(run_id=run_id, status=NOT_ORG_MODE)
    org_uuid = str(evidence[0].get("org_uuid") or "")
    org_id = str(evidence[0].get("org_id") or "")
    ranked = ranked_ideas(_candidates(idea_rows))
    if not ranked:
        return WinnerRow(run_id=run_id, status=NO_WINNER, org_id=org_id, org_uuid=org_uuid)
    winner = ranked[0]
    third = ranked[2] if len(ranked) >= 3 else None
    return WinnerRow(
        run_id=run_id,
        status=READY,
        org_id=org_id,
        org_uuid=org_uuid,
        theme=winner.title,
        theme_category=winner.idea_category,
        third_theme=third.title if third else "",
        third_theme_category=third.idea_category if third else "",
    )


def select_winners(sink: Any, run_ids: list[str]) -> list[WinnerRow]:
    """One WinnerRow per run_id, input order preserved, failures isolated."""
    rows: list[WinnerRow] = []
    for run_id in run_ids:
        run_row = sink.get_action_run(run_id)
        idea_rows = sink.list_action_generated_ideas(run_id)
        rows.append(select_winner(run_id, run_row, idea_rows))
    return rows
