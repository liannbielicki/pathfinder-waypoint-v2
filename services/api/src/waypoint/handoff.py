"""Idempotent LCM boundary. Pathfinder performs zero sends.

Allison's LCM tool (the Pathfinder Intake API) owns copy, human review, and
delivery via Iterable. Its contract requires one POST per batch of theme rows
(never one row per request) and sits behind Vercel deployment protection, so
every request needs both the bearer token and the separate
`x-vercel-protection-bypass` secret. Rows are keyed by `pro_uuid`, never
email/name — Pathfinder never sends PII across this boundary.

Durable rows are committed before the POST so a crash between send and
receipt leaves rows whose retry reuses the same idempotency keys; the LCM
side is also idempotent per (batch, row_id), so retrying the whole batch is
always safe.
"""

from typing import Any, Literal, cast

import httpx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from waypoint.models import HandoffReceipt
from waypoint.tables import HandoffRow


class HandoffUnavailable(Exception):
    pass


def handoff_key(run_id: str, row_id: str) -> str:
    return f"{run_id}:{row_id}"


_STATUSES: tuple[str, ...] = ("accepted", "rejected", "duplicate")


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

        to_insert = [row for row in rows if key(row) not in existing]
        if to_insert:
            # Durable pending row BEFORE the POST: a crash between send and
            # receipt leaves a row whose retry reuses the same idempotency key.
            for row in to_insert:
                self.session.add(HandoffRow(
                    run_id=run_id, winner_id=row["row_id"],
                    idempotency_key=key(row), payload=row, status="pending",
                ))
            try:
                await self.session.commit()
            except IntegrityError:
                # A concurrent request won some of these inserts; adopt the
                # true state instead of racing to send duplicates.
                await self.session.rollback()
            existing = await self._load_existing(keys)

        pending = unanswered(existing)
        if pending:
            try:
                response = await self._client.post(
                    self.url, json={"batch": run_id, "rows": pending}
                )
            except httpx.HTTPError as error:
                raise HandoffUnavailable(f"LCM intake unreachable: {error}") from error
            try:
                body = response.json()
            except ValueError:
                body = {"raw": response.text}
            per_row = {
                item.get("row_id"): item for item in body.get("rows", [])
            } if isinstance(body, dict) else {}
            for row in pending:
                result = per_row.get(row["row_id"])
                handoff_row = existing[key(row)]
                handoff_row.response = result if result is not None else body
                if result is not None:
                    handoff_row.status = result.get("status", "rejected")
                else:
                    handoff_row.status = "accepted" if response.status_code < 300 else "rejected"
            await self.session.commit()

        return [
            HandoffReceipt(
                handoff_id=existing[key(row)].id,
                idempotency_key=key(row),
                status=_receipt_status(existing[key(row)].status),
            )
            for row in rows
        ]
