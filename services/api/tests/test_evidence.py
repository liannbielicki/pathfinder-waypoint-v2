from waypoint.evidence import (
    PatternEvidence,
    evidence_block,
    failed_mechanisms,
    pattern_summaries,
)
from waypoint.tables import TouchOutcomeRow


def outcome(**kwargs) -> TouchOutcomeRow:
    defaults = {
        "recommendation_id": "w",
        "source": "test",
        "pro_id": "pro_1",
        "channel": "sms",
        "mechanism": "invoice_delivery",
        "journey_window": "churn_risk",
    }
    return TouchOutcomeRow(**{**defaults, **kwargs})


async def test_pattern_summaries_aggregate_by_channel_mechanism(db_session) -> None:
    db_session.add(outcome(recommendation_id="w1", returned_7d=True, returned_30d=True))
    db_session.add(outcome(recommendation_id="w2", returned_7d=False))
    db_session.add(outcome(recommendation_id="w3", mechanism="review_boost", unsubscribed=True))
    await db_session.commit()
    patterns = await pattern_summaries(db_session, "churn_risk", ["sms"])
    by_mech = {p.mechanism: p for p in patterns}
    assert by_mech["invoice_delivery"].sent == 2
    assert by_mech["invoice_delivery"].returned["7d"] == (1, 2)
    assert by_mech["invoice_delivery"].returned["30d"] == (1, 1)  # w2's 30d unmeasured
    assert by_mech["review_boost"].unsubscribed == 1


async def test_churn_risk_and_churn_risk_open_share_one_evidence_corpus(db_session) -> None:
    """Same objective, same history — and symmetrically, or the ungated window
    would run blind while its gated twin held every measured outcome."""
    db_session.add(outcome(recommendation_id="w1", returned_7d=True))
    db_session.add(
        outcome(recommendation_id="w2", journey_window="churn_risk_open", returned_7d=True)
    )
    await db_session.commit()
    for window in ("churn_risk", "churn_risk_open"):
        patterns = await pattern_summaries(db_session, window, ["sms"])
        assert [p.sent for p in patterns] == [2], window
        assert patterns[0].returned["7d"] == (2, 2), window


async def test_other_windows_keep_their_own_evidence(db_session) -> None:
    """Grouping churn_risk with churn_risk_open must not leak into onboarding."""
    db_session.add(outcome(recommendation_id="w1", returned_7d=True))
    await db_session.commit()
    assert await pattern_summaries(db_session, "onboarding", ["sms"]) == []


async def test_unattributed_outcomes_are_excluded_from_evidence(db_session) -> None:
    db_session.add(outcome(recommendation_id="w1", evidence_limitation="unattributed"))
    await db_session.commit()
    assert await pattern_summaries(db_session, "churn_risk", ["sms"]) == []


async def test_failed_mechanisms_use_the_day7_learning_checkpoint(db_session) -> None:
    """V3: Day 1 and Day 7 drive learning; the obsolete 30-day suppression is
    gone. A measured 7-day no-return fails a mechanism; a measured 30-day
    no-return alone (diagnostic only) does not."""
    db_session.add(outcome(recommendation_id="w1", unsubscribed=True))
    db_session.add(outcome(recommendation_id="w2", mechanism="review_boost", returned_7d=False))
    db_session.add(outcome(recommendation_id="w3", mechanism="ok_one", returned_7d=True))
    db_session.add(outcome(recommendation_id="w5", mechanism="slow_burn", returned_30d=False))
    db_session.add(outcome(recommendation_id="w4", pro_id="other", mechanism="not_mine",
                           unsubscribed=True))
    await db_session.commit()
    failed = await failed_mechanisms(db_session, "pro_1")
    assert set(failed) == {"invoice_delivery", "review_boost"}


async def test_pattern_summaries_track_the_day1_learning_checkpoint(db_session) -> None:
    db_session.add(outcome(recommendation_id="w1", returned_1d=True, returned_7d=True))
    db_session.add(outcome(recommendation_id="w2", returned_1d=False))
    await db_session.commit()
    patterns = await pattern_summaries(db_session, "churn_risk", ["sms"])
    assert patterns[0].returned["1d"] == (1, 2)


async def test_evidence_block_labels_day30_as_diagnostic() -> None:
    patterns = [
        PatternEvidence(
            channel="sms", mechanism="invoice_delivery", sent=3,
            returned={"1d": (1, 2), "7d": (2, 3), "30d": (2, 2)},
            unsubscribed=0,
        )
    ]
    text = evidence_block(patterns)
    assert "1d return 1/2" in text
    assert "7d return 2/3" in text
    assert "30d return 2/2 (diagnostic)" in text


async def test_evidence_block_is_honest_when_empty() -> None:
    text = evidence_block([])
    assert "No historical outcome evidence" in text
