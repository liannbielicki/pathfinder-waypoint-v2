"""Idempotent LCM boundary. Pathfinder performs zero sends.

Allison's LCM tool owns final copy, personalization, the Iterable DNC
failsafe, and delivery. This client creates one durable handoff artifact per
(run, winner) — the pending row is committed before the POST so a crash can
never double-hand-off, and the receipt is the record.
"""

from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from waypoint.models import HandoffReceipt, MeasurementPlan
from waypoint.tables import HandoffRow


class HandoffUnavailable(Exception):
    pass


def handoff_key(run_id: str, winner_id: str) -> str:
    return f"{run_id}:{winner_id}"


class LCMClient:
    def __init__(self, url: str, token: str, session: AsyncSession,
                 timeout: float = 30.0, client: httpx.AsyncClient | None = None) -> None:
        self.url = url
        self.session = session
        self._client = client or httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=False,
            headers={"authorization": f"Bearer {token}"},
        )

    async def handoff(self, winner: dict[str, Any], plan: MeasurementPlan,
                      lineage: dict[str, str]) -> HandoffReceipt:
        key = handoff_key(winner["run_id"], winner["winner_id"])
        row = (await self.session.execute(
            select(HandoffRow).where(HandoffRow.idempotency_key == key)
        )).scalar_one_or_none()
        if row is not None and row.response is not None:
            return HandoffReceipt(
                handoff_id=row.id, idempotency_key=key,
                status="accepted" if row.status == "accepted" else "rejected",
            )

        payload = {
            "idempotency_key": key,
            "pro_id": winner["pro_id"],
            "org_id": winner["org_id"],
            "winner": winner["recommendation"],
            "score": winner.get("score", {}),
            "measurement_plan": plan.model_dump(),
            "audience_lineage": lineage,
        }
        if row is None:
            # Durable pending row BEFORE the POST: a crash between POST and
            # receipt leaves a row whose retry reuses the same idempotency key.
            row = HandoffRow(
                run_id=winner["run_id"], winner_id=winner["winner_id"],
                idempotency_key=key, payload=payload, status="pending",
            )
            self.session.add(row)
            await self.session.commit()

        try:
            response = await self._client.post(self.url, json=payload)
        except httpx.HTTPError as error:
            raise HandoffUnavailable(f"LCM intake unreachable: {error}") from error

        try:
            body = response.json()
        except ValueError:
            body = {"raw": response.text}
        row.response = body
        row.status = "accepted" if response.status_code < 300 else "rejected"
        await self.session.commit()
        return HandoffReceipt(handoff_id=row.id, idempotency_key=key,
                              status="accepted" if row.status == "accepted" else "rejected")
