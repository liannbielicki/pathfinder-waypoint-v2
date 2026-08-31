"""Canonical item corpus: expandable, organic, versioned — nothing hard-coded.

Resolution is structured (mechanism, channel) + fuzzy (concept text) over the
full corpus behind one replaceable function; no vector infrastructure.
"""

import time

from sqlalchemy import select

from waypoint.items import RESOLVER_VERSION, resolve_item
from waypoint.tables import ItemRow

REC = {
    "mechanism": "invoice_delivery",
    "channel": "sms",
    "pro_facing_concept": "Send your open invoices from the app so you get paid faster",
}


async def test_first_resolution_creates_a_new_item_organically(db_session) -> None:
    resolved = await resolve_item(db_session, REC)
    assert resolved.created is True
    assert resolved.item_version == "v1"
    assert resolved.resolver_version == RESOLVER_VERSION
    row = await db_session.get(ItemRow, resolved.item_id)
    assert row is not None
    assert row.mechanism == "invoice_delivery"
    assert row.status == "active"


async def test_identical_concept_resolves_to_the_same_item_and_version(db_session) -> None:
    first = await resolve_item(db_session, REC)
    second = await resolve_item(db_session, REC)
    assert second.item_id == first.item_id
    assert second.item_version == first.item_version
    assert second.created is False


async def test_near_identical_concept_fuzzy_matches_the_existing_item(db_session) -> None:
    first = await resolve_item(db_session, REC)
    tweaked = {**REC, "pro_facing_concept": REC["pro_facing_concept"] + "!"}
    second = await resolve_item(db_session, tweaked)
    assert second.item_id == first.item_id
    assert second.item_version == first.item_version


async def test_drifted_concept_bumps_the_item_version(db_session) -> None:
    first = await resolve_item(db_session, REC)
    drifted = {
        **REC,
        "pro_facing_concept": (
            "Send your open invoices from the app to collect overdue payments "
            "and remind late customers automatically"
        ),
    }
    second = await resolve_item(db_session, drifted)
    assert second.item_id == first.item_id
    assert second.item_version == "v2"
    row = await db_session.get(ItemRow, first.item_id)
    assert row.version == 2
    # Organic versioned metadata: the prior concept is preserved, not lost.
    assert REC["pro_facing_concept"] in str(row.item_metadata)


async def test_different_mechanism_never_matches_structurally(db_session) -> None:
    first = await resolve_item(db_session, REC)
    other = await resolve_item(db_session, {**REC, "mechanism": "review_requests"})
    assert other.item_id != first.item_id


async def test_unrelated_concept_creates_a_second_item(db_session) -> None:
    first = await resolve_item(db_session, REC)
    other = await resolve_item(db_session, {
        **REC,
        "pro_facing_concept": "Turn on online booking so customers schedule themselves",
    })
    assert other.item_id != first.item_id
    assert other.created is True


async def test_corpus_is_not_bounded_to_a_fixed_theme_count(db_session) -> None:
    for i in range(10):
        await resolve_item(db_session, {
            "mechanism": f"mechanism_{i}", "channel": "sms",
            "pro_facing_concept": f"Completely distinct theme number {i} about topic {i}",
        })
    count = len((await db_session.execute(select(ItemRow))).scalars().all())
    assert count == 10


async def test_resolution_stays_fast_over_a_large_corpus(db_session) -> None:
    """Corpus-performance guard: full-corpus fuzzy resolution over 500 items
    in one (mechanism, channel) slice stays well under a second."""
    for i in range(500):
        db_session.add(ItemRow(
            mechanism="invoice_delivery", channel="sms",
            concept=f"Invoice reminder variant {i} with distinct wording {i * 7}",
            concept_hash=f"hash-{i}", version=1,
        ))
    await db_session.commit()
    started = time.perf_counter()
    resolved = await resolve_item(db_session, REC)
    elapsed = time.perf_counter() - started
    assert elapsed < 1.0
    assert resolved.created is True  # none of the variants is close enough
