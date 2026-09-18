"""One database-backed gate for Waypoint and Context Workbench starts."""

from typing import Literal

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from waypoint.models import TERMINAL_RUN_STATUSES
from waypoint.settings import Settings
from waypoint.tables import FleetControlRow, RunRow, WorkbenchJobRow

Activity = Literal["idle", "waypoint", "workbench"]
_SETTLED_RUN_STATUSES = (*TERMINAL_RUN_STATUSES, "degraded")


async def lock_fleet(
    session: AsyncSession, settings: Settings
) -> FleetControlRow:
    fleet = (
        await session.execute(
            select(FleetControlRow).where(FleetControlRow.id == 1).with_for_update()
        )
    ).scalar_one_or_none()
    if fleet is None:
        fleet = FleetControlRow(
            id=1,
            killed=settings.KILL_SWITCH,
            learning_killed=settings.LEARNING_KILL_SWITCH,
            day_cost_limit=settings.DAY_COST_USD,
        )
        session.add(fleet)
        await session.flush()
    return fleet


async def current_activity(session: AsyncSession) -> Activity:
    workbench = await session.scalar(
        select(
            exists().where(WorkbenchJobRow.status.in_(("queued", "running")))
        )
    )
    if workbench:
        return "workbench"
    waypoint = await session.scalar(
        select(exists().where(RunRow.status.not_in(_SETTLED_RUN_STATUSES)))
    )
    return "waypoint" if waypoint else "idle"
