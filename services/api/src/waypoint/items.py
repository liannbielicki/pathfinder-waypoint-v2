"""Canonical item resolution over the expandable theme corpus.

`resolve_item` is the whole interface: structured filter (mechanism, channel)
plus stdlib fuzzy text match over the full corpus slice, no vector
infrastructure. A different retrieval system can replace this body without
touching the pipeline. The corpus is organic — items and versions are created
from real winners' recommendations; no theme set is hard-coded anywhere.

Thresholds: >= EXACT_THRESHOLD is the same concept (same item, same version);
>= DRIFT_THRESHOLD is the same idea reworded (same item, version bumped, prior
concept preserved in item_metadata); below that is a new item.
"""

import hashlib
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from waypoint.tables import ItemRow

RESOLVER_VERSION = "resolver_v1"
EXACT_THRESHOLD = 0.9
DRIFT_THRESHOLD = 0.6


@dataclass(frozen=True)
class ResolvedItem:
    item_id: str
    item_version: str
    resolver_version: str = RESOLVER_VERSION
    created: bool = False


def _concept_hash(concept: str) -> str:
    return hashlib.sha256(concept.strip().lower().encode()).hexdigest()


def _similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a.strip().lower(), b.strip().lower()).ratio()


async def resolve_item(session: AsyncSession, recommendation: dict[str, Any]) -> ResolvedItem:
    """Resolve a recommendation to its canonical (item_id, item_version).

    Never commits — rides the caller's transaction; the only flush is inside a
    SAVEPOINT so a concurrent-insert race degrades to a re-select, not a doomed
    transaction.
    """
    mechanism = str(recommendation.get("mechanism", ""))
    channel = str(recommendation.get("channel", ""))
    concept = str(recommendation.get("pro_facing_concept", ""))
    # Exact concept (any status): the unique constraint spans retired items
    # too, so an exact hash match must resolve to the owning row rather than
    # collide with it later.
    exact = (
        await session.execute(
            select(ItemRow).where(
                ItemRow.mechanism == mechanism,
                ItemRow.channel == channel,
                ItemRow.concept_hash == _concept_hash(concept),
            )
        )
    ).scalar_one_or_none()
    if exact is not None:
        return ResolvedItem(item_id=exact.id, item_version=f"v{exact.version}")
    rows = (
        await session.execute(
            select(ItemRow).where(
                ItemRow.mechanism == mechanism,
                ItemRow.channel == channel,
                ItemRow.status == "active",
            )
        )
    ).scalars().all()
    best: ItemRow | None = None
    best_score = 0.0
    for row in rows:
        score = _similarity(concept, row.concept)
        if score > best_score:
            best, best_score = row, score
    if best is not None and best_score >= EXACT_THRESHOLD:
        return ResolvedItem(item_id=best.id, item_version=f"v{best.version}")
    if best is not None and best_score >= DRIFT_THRESHOLD:
        # Same idea, reworded: bump the version, keep the prior concept as
        # organic versioned metadata instead of losing it. The bump is an
        # optimistic UPDATE guarded on the version we read, inside a SAVEPOINT:
        # a concurrent bump (rowcount 0) or a hash collision with another item
        # (IntegrityError) both degrade to re-reading the current row.
        history = list(best.item_metadata.get("versions", []))
        history.append({
            "version": best.version,
            "concept": best.concept,
            "resolver_version": RESOLVER_VERSION,
        })
        read_version = best.version
        try:
            async with session.begin_nested():
                result = await session.execute(
                    update(ItemRow)
                    .where(ItemRow.id == best.id, ItemRow.version == read_version)
                    .values(
                        version=read_version + 1,
                        concept=concept,
                        concept_hash=_concept_hash(concept),
                        item_metadata={**best.item_metadata, "versions": history},
                        resolver_version=RESOLVER_VERSION,
                    )
                    .execution_options(synchronize_session="fetch")
                )
            if result.rowcount:  # type: ignore[attr-defined]  # CursorResult at runtime
                return ResolvedItem(item_id=best.id, item_version=f"v{read_version + 1}")
        except IntegrityError:
            # Another item already owns this exact concept hash — resolve to
            # the owner, not to `best` (whose concept is something else).
            owner = (
                await session.execute(
                    select(ItemRow).where(
                        ItemRow.mechanism == mechanism,
                        ItemRow.channel == channel,
                        ItemRow.concept_hash == _concept_hash(concept),
                    )
                )
            ).scalar_one_or_none()
            if owner is not None:
                return ResolvedItem(item_id=owner.id, item_version=f"v{owner.version}")
        # Lost the optimistic race: a concurrent resolver bumped this item
        # first. session.get would hit the identity map and hand back the
        # STALE object — re-read the row the winner actually committed.
        await session.refresh(best)
        return ResolvedItem(item_id=best.id, item_version=f"v{best.version}")
    row = ItemRow(
        mechanism=mechanism,
        channel=channel,
        concept=concept,
        concept_hash=_concept_hash(concept),
        resolver_version=RESOLVER_VERSION,
    )
    try:
        async with session.begin_nested():
            session.add(row)
    except IntegrityError:
        # A concurrent worker created the identical item first — use theirs.
        existing = (
            await session.execute(
                select(ItemRow).where(
                    ItemRow.mechanism == mechanism,
                    ItemRow.channel == channel,
                    ItemRow.concept_hash == _concept_hash(concept),
                )
            )
        ).scalar_one()
        return ResolvedItem(item_id=existing.id, item_version=f"v{existing.version}")
    return ResolvedItem(item_id=row.id, item_version=f"v{row.version}", created=True)
