"""Idempotent LCM Personalization boundary. Pathfinder performs zero sends.

LCM Personalization receives themes, turns them into Housecall Pro SMS,
performs QA/edit/approval, and queues delivery. Its contract requires one POST per batch of theme rows
(never one row per request) and sits behind Vercel deployment protection, so
every request needs both the bearer token and the separate
`x-vercel-protection-bypass` secret. Rows are keyed by `pro_uuid`, never
email/name — Pathfinder never sends PII across this boundary.

Durable rows are committed before the POST so a crash between send and
receipt leaves rows whose retry reuses the same idempotency keys; the LCM
side is also idempotent per (batch, row_id), so retrying the whole batch is
always safe.
"""

import typing
from typing import Any, Literal, cast

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from waypoint.models import PENDING_AUDIENCE_QUERY, HandoffReceipt
from waypoint.tables import CandidateRow, HandoffRow, MeasurementRow, RunRow, WinnerRow

if typing.TYPE_CHECKING:
    from waypoint.settings import Settings


class HandoffUnavailable(Exception):
    pass


def handoff_key(run_id: str, row_id: str) -> str:
    return f"{run_id}:{row_id}"


_STATUSES: tuple[str, ...] = typing.get_args(HandoffReceipt.model_fields["status"].annotation)


def _receipt_status(status: str) -> Literal["accepted", "rejected", "duplicate"]:
    # LCM statuses we don't recognize are treated as rejected rather than
    # asserted into a Literal that doesn't match reality.
    return cast(Literal["accepted", "rejected", "duplicate"],
                status if status in _STATUSES else "rejected")


class LCMClient:
    def __init__(self, url: str, token: str, bypass_token: str, session: AsyncSession,
                 timeout: float = 30.0, client: httpx.AsyncClient | None = None) -> None:
        self.url = url
        self.session = session
        self._client = client or httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=False,
            headers={
                "authorization": f"Bearer {token}",
                "x-vercel-protection-bypass": bypass_token,
            },
        )

    async def _load_existing(self, keys: list[str]) -> dict[str, HandoffRow]:
        rows = (await self.session.execute(
            select(HandoffRow).where(HandoffRow.idempotency_key.in_(keys))
        )).scalars()
        return {row.idempotency_key: row for row in rows}

    async def handoff(self, run_id: str, rows: list[dict[str, Any]]) -> list[HandoffReceipt]:
        """Send one batch POST for `rows` (each carrying `pro_uuid`, `theme`,
        `theme_category`, `org_id`, `row_id`). Rows already answered by a
        prior call are skipped; only unanswered rows go out on the wire."""
        def key(row: dict[str, Any]) -> str:
            return handoff_key(run_id, row["row_id"])

        def unanswered(existing: dict[str, HandoffRow]) -> list[dict[str, Any]]:
            return [row for row in rows if existing[key(row)].response is None]

        keys = [key(row) for row in rows]
        existing = await self._load_existing(keys)

        # Dedupe by idempotency_key first: ON CONFLICT can't affect the same
        # conflict target twice within one statement, so two rows with the
        # same row_id in this call must collapse to a single insert row.
        to_insert = {key(row): row for row in rows if key(row) not in existing}
        if to_insert:
            # Durable pending row BEFORE the POST: a crash between send and
            # receipt leaves a row whose retry reuses the same idempotency key.
            # A concurrent handoff() call inserting the same key is a no-op
            # here, not an exception — no rollback that could drop siblings.
            await self.session.execute(
                pg_insert(HandoffRow).on_conflict_do_nothing(
                    index_elements=["idempotency_key"]
                ),
                [
                    {
                        "run_id": run_id, "winner_id": row["row_id"],
                        "idempotency_key": ikey, "payload": row, "status": "pending",
                    }
                    for ikey, row in to_insert.items()
                ],
            )
            await self.session.commit()
            existing = await self._load_existing(keys)

        pending = unanswered(existing)
        if pending:
            try:
                response = await self._client.post(
                    self.url, json={"batch": run_id, "rows": pending}
                )
            except httpx.HTTPError as error:
                raise HandoffUnavailable(f"LCM intake unreachable: {error}") from error
            if response.status_code >= 300:
                # Batch-level failure carries no per-row information (see
                # Pathfinder Intake API §4/§5) — leave every row pending so
                # the whole batch is safely retried next call.
                raise HandoffUnavailable(
                    f"LCM intake returned {response.status_code}: {response.text}"
                )
            try:
                body = response.json()
            except ValueError:
                body = {"raw": response.text}
            per_row = {
                item.get("row_id"): item for item in body.get("rows", [])
            } if isinstance(body, dict) else {}
            missing = [row["row_id"] for row in pending if row["row_id"] not in per_row]
            if missing:
                # Only a 202 carries a real per-row breakdown; don't guess a
                # status for an unconfirmed row, and don't partially commit —
                # leave the whole batch pending so it's safely retryable.
                raise HandoffUnavailable(
                    f"LCM intake response missing rows for row_ids: {missing}"
                )
            for row in pending:
                result = per_row[row["row_id"]]
                handoff_row = existing[key(row)]
                # The audit record must match the wire: a retry rebuilds rows
                # fresh, so a payload stored on an earlier attempt can be stale.
                handoff_row.payload = row
                handoff_row.response = result
                handoff_row.status = _receipt_status(result.get("status", "rejected"))
            await self.session.commit()

        return [
            HandoffReceipt(
                handoff_id=existing[key(row)].id,
                idempotency_key=key(row),
                status=_receipt_status(existing[key(row)].status),
            )
            for row in rows
        ]

    async def aclose(self) -> None:
        await self._client.aclose()


class AudienceLineageUnresolved(Exception):
    """The n8n flow never reported its query version; winners must not ship."""


def lcm_http_client(settings: Settings, timeout: float = 30.0) -> httpx.AsyncClient:
    """A long-lived transport for the LCM boundary — build once, share across
    calls (LCMClient accepts it via `client=`) instead of paying a TLS
    handshake per push."""
    return httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=False,
        headers={
            "authorization": f"Bearer {settings.HANDOFF_TOKEN.get_secret_value()}",
            "x-vercel-protection-bypass": settings.BYPASS_TOKEN.get_secret_value(),
        },
    )


def make_lcm_client(
    settings: Settings, session: AsyncSession, client: httpx.AsyncClient | None = None
) -> LCMClient:
    """The one place LCM wiring (url/token/bypass) is assembled."""
    return LCMClient(
        url=str(settings.HANDOFF_URL),
        token=settings.HANDOFF_TOKEN.get_secret_value(),
        bypass_token=settings.BYPASS_TOKEN.get_secret_value(),
        session=session,
        client=client,
    )


async def ready_rows(
    session: AsyncSession,
    run_id: str,
    *,
    pro_id: str | None = None,
    include_degraded: bool = True,
) -> list[dict[str, Any]]:
    """Every winner of `run_id` (optionally one Pro's) that is fully ready to
    hand off (has a measurement plan and its candidate), shaped as Pathfinder
    Intake rows. No PII — rows are keyed by pro_uuid.

    The audience-lineage guard lives HERE, on the one path every handoff
    caller routes through: a run whose n8n flow never reported its query
    version raises instead of returning shippable rows. With
    include_degraded=False, winners flagged with a panel_disclaimer are held
    back for operator-initiated handoff."""
    run = await session.get(RunRow, run_id)
    if run is None or run.audience_query == PENDING_AUDIENCE_QUERY:
        raise AudienceLineageUnresolved(
            "audience lineage unresolved: the n8n flow never reported a query version"
        )
    query = select(WinnerRow).where(WinnerRow.run_id == run_id, WinnerRow.kind == "winner")
    if pro_id is not None:
        query = query.where(WinnerRow.pro_id == pro_id)
    winners = (await session.execute(query)).scalars().all()
    if not include_degraded:
        winners = [w for w in winners if not w.evidence.get("panel_disclaimer")]
    # Set-based loading: two IN() prefetches instead of two SELECTs per winner.
    winner_ids = [w.id for w in winners]
    candidate_ids = [w.candidate_id for w in winners if w.candidate_id]
    measured_winner_ids: set[str] = set()
    if winner_ids:
        measured_winner_ids = {
            winner_id
            for winner_id in (
                await session.execute(
                    select(MeasurementRow.winner_id).where(
                        MeasurementRow.winner_id.in_(winner_ids)
                    )
                )
            ).scalars()
            if winner_id is not None
        }
    candidates_by_id: dict[str, CandidateRow] = {}
    if candidate_ids:
        candidates_by_id = {
            c.id: c
            for c in (
                await session.execute(
                    select(CandidateRow).where(CandidateRow.id.in_(candidate_ids))
                )
            ).scalars()
        }
    rows: list[dict[str, Any]] = []
    for winner in winners:
        candidate = candidates_by_id.get(winner.candidate_id) if winner.candidate_id else None
        if winner.id in measured_winner_ids and candidate is not None:
            rows.append(
                {
                    "pro_uuid": winner.pro_id,
                    # The full customer-moment text, not the title: Allison's SMS
                    # copywriter sees ONLY this field (her journeyStage), so a
                    # title here collapses compound themes to their headline.
                    "theme": candidate.recommendation["pro_facing_concept"],
                    "theme_category": candidate.recommendation["mechanism"],
                    "org_id": winner.evidence.get("org_id", ""),
                    "row_id": winner.id,
                }
            )
    return rows


async def push_ready_winners(
    session: AsyncSession,
    settings: Settings,
    run_id: str,
    *,
    pro_id: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> int:
    """Trickle push: one batch POST of the finished Pro's ready winner(s).
    LCMClient skips rows a prior call already answered, so calling this after
    EVERY completed Pro streams new winners to Allison's LCM as they land —
    QA can start while the run is still going. Scoping to `pro_id` keeps
    concurrent worker loops on disjoint rows (the job lease guarantees one
    worker per Pro), so pushes never race on the same handoff row.

    Returns how many ready rows were ensured delivered (0 when the run is
    stopped/failed, lineage is unresolved, or nothing is ready). Degraded-
    panel winners are held back for the operator's manual POST /handoff."""
    run = await session.get(RunRow, run_id)
    if run is None or run.status in ("stopped", "failed"):
        # An operator kill (or a failed run) must keep working: automatic
        # handoff would ship winners the operator just tried to withhold.
        return 0
    try:
        rows = await ready_rows(session, run_id, pro_id=pro_id, include_degraded=False)
    except AudienceLineageUnresolved:
        return 0  # same refusal as the manual endpoint, silently for the trickle
    if not rows:
        return 0
    lcm = make_lcm_client(settings, session, client=client)
    try:
        await lcm.handoff(run_id, rows)
    finally:
        if client is None:  # a shared transport outlives this call
            await lcm.aclose()
    return len(rows)
