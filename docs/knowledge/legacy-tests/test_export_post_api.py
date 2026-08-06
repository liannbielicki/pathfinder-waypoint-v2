from __future__ import annotations

import urllib.error

import pytest

from pathfinder.export.post_api import (
    LCM_ROW_FIELDS,
    LcmClient,
    LcmPostError,
    _headers,
    rows_to_payload,
    summarize_receipts,
    to_lcm_category,
)


def http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://lcm.example", code, "boom", None, None)


class FakeTransport:
    def __init__(self, response=None, fail: Exception | None = None):
        self.response = response or {"ok": True}
        self.fail = fail
        self.calls: list[tuple[str, str, dict | None]] = []

    def __call__(self, method: str, url: str, payload: dict | None) -> dict:
        self.calls.append((method, url, payload))
        if self.fail:
            raise self.fail
        return self.response


HEADER = ["org_id", "org_uuid", "email", "pro_name", "theme", "theme_category",
          "status", "approved", "message", "third_theme", "third_theme_category",
          "run_id"]


def sheet_row(email="A@X.com ", theme="Send follow-ups", category="Reputation & Reviews"):
    return ["100", "uu-0", email, "Alice", theme, category,
            "ready_for_review", "", "", "", "", "r1"]


def test_to_lcm_category_maps_observed_values():
    assert to_lcm_category("Reputation & Reviews") == "reviews"
    assert to_lcm_category("billing_repair") == "invoicing"
    assert to_lcm_category("revenue_collection") == "invoicing"
    assert to_lcm_category("booking_growth") == "scheduling"
    assert to_lcm_category("Lead Conversion & Estimating") == "estimates"
    assert to_lcm_category("price_change") == "price_change"
    assert to_lcm_category("follow_ups") == "follow_ups"
    assert to_lcm_category("Website Presence") == "website"
    assert to_lcm_category("Workflow Activation") == "other"
    assert to_lcm_category("") == "other"


def test_rows_to_payload_emits_exactly_the_agreed_fields():
    payload = rows_to_payload("b1", HEADER, [sheet_row()])
    assert payload["batch"] == "b1"
    (row,) = payload["rows"]
    assert set(row) == set(LCM_ROW_FIELDS)
    assert row["email"] == "a@x.com"  # lowercased + trimmed (her join key)
    assert row["theme"] == "Send follow-ups"
    assert row["theme_category"] == "reviews"
    assert row["pro_name"] == "Alice" and row["org_id"] == "100"
    assert row["row_id"] == "r1"  # stable = run_id, so retries dedup at her end


def test_row_id_is_stable_across_repeat_exports():
    first = rows_to_payload("b1", HEADER, [sheet_row()])
    again = rows_to_payload("b1", HEADER, [sheet_row()])
    assert first["rows"][0]["row_id"] == again["rows"][0]["row_id"]


def test_summarize_receipts_tallies_per_row_statuses():
    response = {"rows": [
        {"row_id": "r1", "status": "accepted"},
        {"row_id": "r2", "status": "duplicate"},
        {"row_id": "r3", "status": "rejected", "reason": "bad email"},
    ]}
    summary = summarize_receipts(response)
    assert "accepted=1" in summary and "duplicate=1" in summary
    assert "rejected=1" in summary and "r3" in summary and "bad email" in summary


def test_summarize_receipts_tolerates_unknown_shapes():
    assert summarize_receipts({}) == ""
    assert summarize_receipts({"ok": True}) == ""
    assert summarize_receipts(None) == ""


def test_headers_include_bearer_and_vercel_bypass_when_set():
    headers = _headers(token="tok123", vercel_bypass="vb456")
    assert headers["Authorization"] == "Bearer tok123"
    assert headers["x-vercel-protection-bypass"] == "vb456"
    assert headers["Content-Type"] == "application/json"


def test_headers_omit_auth_and_bypass_when_unset():
    headers = _headers(token=None, vercel_bypass=None)
    assert "Authorization" not in headers
    assert "x-vercel-protection-bypass" not in headers


def test_rows_without_email_or_theme_are_skipped():
    rows = [sheet_row(), sheet_row(email="  "), sheet_row(theme="")]
    payload = rows_to_payload("b1", HEADER, rows)
    assert len(payload["rows"]) == 1


def test_post_rows_posts_mapped_payload():
    transport = FakeTransport(response={"accepted": 1})
    client = LcmClient("https://lcm.example/ingest", transport=transport)
    result = client.post_rows("b1", HEADER, [sheet_row()])
    assert result == {"accepted": 1}
    (method, url, payload), = transport.calls
    assert method == "POST" and url == "https://lcm.example/ingest"
    assert payload["rows"][0]["email"] == "a@x.com"


def test_post_payload_propagates_transport_errors():
    client = LcmClient("https://lcm.example/ingest",
                       transport=FakeTransport(fail=RuntimeError("boom")))
    with pytest.raises(RuntimeError):
        client.post_rows("b1", HEADER, [sheet_row()])


class FlakyTransport:
    """Fails N times with the given error, then succeeds."""

    def __init__(self, fail_times: int, error: Exception):
        self.fail_times = fail_times
        self.error = error
        self.calls = 0

    def __call__(self, method, url, payload):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise self.error
        return {"ok": True}


def test_5xx_is_retried_and_succeeds():
    transport = FlakyTransport(fail_times=1, error=http_error(500))
    client = LcmClient("https://lcm.example/ingest", transport=transport,
                       retry_wait=0)
    assert client.post_rows("b1", HEADER, [sheet_row()]) == {"ok": True}
    assert transport.calls == 2


def test_timeout_style_urlerror_is_retried():
    transport = FlakyTransport(fail_times=1,
                               error=urllib.error.URLError("timed out"))
    client = LcmClient("https://lcm.example/ingest", transport=transport,
                       retry_wait=0)
    assert client.post_rows("b1", HEADER, [sheet_row()]) == {"ok": True}


def test_5xx_exhausted_retries_raises_lcm_error():
    transport = FlakyTransport(fail_times=10, error=http_error(500))
    client = LcmClient("https://lcm.example/ingest", transport=transport,
                       retries=2, retry_wait=0)
    with pytest.raises(LcmPostError):
        client.post_rows("b1", HEADER, [sheet_row()])
    assert transport.calls == 3  # initial + 2 retries


@pytest.mark.parametrize("code,hint", [
    (400, "payload"),
    (401, "bearer"),
    (403, "bypass"),
    (413, "500"),
])
def test_4xx_is_not_retried_and_message_names_the_fix(code, hint):
    transport = FlakyTransport(fail_times=10, error=http_error(code))
    client = LcmClient("https://lcm.example/ingest", transport=transport,
                       retry_wait=0)
    with pytest.raises(LcmPostError) as excinfo:
        client.post_rows("b1", HEADER, [sheet_row()])
    assert transport.calls == 1
    assert hint in str(excinfo.value).lower()


def test_batches_over_500_rows_are_refused_before_posting():
    transport = FakeTransport()
    client = LcmClient("https://lcm.example/ingest", transport=transport)
    payload = {"batch": "b1", "rows": [{"email": f"p{i}@x.com"} for i in range(501)]}
    with pytest.raises(ValueError):
        client.post_payload(payload)
    assert transport.calls == []


def test_batch_status_gets_the_batch_resource():
    transport = FakeTransport(response={"batch": "b1", "total": 50,
                                        "pending": 0, "generated": 46,
                                        "skipped": 4, "failed": 0})
    client = LcmClient("https://lcm.example/api/pathfinder/intake",
                       transport=transport)
    status = client.batch_status("b1")
    assert status["generated"] == 46
    (method, url, payload), = transport.calls
    assert method == "GET" and url.endswith("/api/pathfinder/intake/b1")
    assert payload is None
