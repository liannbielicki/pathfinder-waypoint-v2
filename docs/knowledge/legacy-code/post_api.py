"""POST batch winners to the LCM copy tool's ingest endpoint.

Contract CONFIRMED by Allison (2026-07-28). Rows: ``email`` (required — her
Iterable join key, lowercased + trimmed), ``theme`` (required, free text),
``theme_category`` (recommended, from the agreed enum below), ``pro_name`` and
``org_id`` (optional, display/audit only), ``row_id`` (stable per row — we use
the run_id, so re-POSTing a batch after a failure carries identical ids and
her side dedups; retries are safe). Rows missing email or theme are skipped
here — they stay visible in the sheet/CSV for humans.

Envelope confirmed as-is: one POST per batch, ``{"batch": <label>,
"rows": [...]}`` to ``POST /api/pathfinder/intake`` with
``Authorization: Bearer <token>`` (``PATHFINDER_LCM_POST_TOKEN``; token
pending from Allison). Her endpoint returns 202 immediately with a per-row
receipt (accepted / duplicate / rejected+reason) — see summarize_receipts.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Optional

Transport = Callable[[str, str, Optional[dict]], dict]

MAX_BATCH_ROWS = 500  # her hard cap; a bigger batch returns 413


class LcmPostError(RuntimeError):
    """A non-retryable (or retry-exhausted) failure talking to the intake API."""


# Her status-code table (intake doc §4). 4xx are payload/config problems —
# retrying cannot help; the message says which knob to turn.
_HTTP_HINTS = {
    400: "malformed payload — check batch/rows shape",
    401: "bearer token missing/invalid — check LCM_PATHFINDER_API_KEY / PATHFINDER_LCM_POST_TOKEN",
    403: "Vercel bypass secret missing/wrong — check x-vercel-protection-bypass / PATHFINDER_LCM_VERCEL_BYPASS",
    404: "campaign problem on the LCM side — tell Allison",
    413: "batch over 500 rows — split it",
    422: "campaign problem on the LCM side — tell Allison",
}

LCM_ROW_FIELDS = ("row_id", "email", "theme", "theme_category", "pro_name", "org_id")

# Allison's agreed theme_category enum.
LCM_THEME_CATEGORIES = (
    "reviews", "invoicing", "scheduling", "price_change",
    "follow_ups", "estimates", "website", "other",
)

# Our idea_category values are a mix of internal snake_case buckets
# (billing_repair, booking_growth, ...) and free-text LLM labels
# ("Reputation & Reviews"). First keyword hit wins; anything unmatched is
# "other". Order matters: more specific stems before broader ones.
_CATEGORY_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("review", "reviews"),
    ("reputation", "reviews"),
    ("invoic", "invoicing"),
    ("billing", "invoicing"),
    ("payment", "invoicing"),
    ("collection", "invoicing"),
    ("schedul", "scheduling"),
    ("booking", "scheduling"),
    ("calendar", "scheduling"),
    ("dispatch", "scheduling"),
    ("price", "price_change"),
    ("pricing", "price_change"),
    ("follow", "follow_ups"),
    ("estimat", "estimates"),
    ("quote", "estimates"),
    ("website", "website"),
    ("web presence", "website"),
)


def to_lcm_category(idea_category: str) -> str:
    """Map an action-console idea_category onto Allison's agreed enum."""
    lowered = (idea_category or "").lower()
    for stem, category in _CATEGORY_KEYWORDS:
        if stem in lowered:
            return category
    return "other"


def rows_to_payload(
    batch_label: str, header: list[str], rows: list[list[str]]
) -> dict:
    """Build the POST body from sheet-shaped rows.

    Only rows with a non-empty email AND theme are included (her tool can't
    use the rest); email is normalized to her join-key format.
    """
    index = {col: i for i, col in enumerate(header)}

    def value(row: list[str], col: str) -> str:
        i = index.get(col)
        return str(row[i]).strip() if i is not None and i < len(row) else ""

    out_rows: list[dict] = []
    for row in rows:
        email = value(row, "email").lower()
        theme = value(row, "theme")
        if not email or not theme:
            continue
        out_rows.append({
            "row_id": value(row, "run_id"),
            "email": email,
            "theme": theme,
            "theme_category": to_lcm_category(value(row, "theme_category")),
            "pro_name": value(row, "pro_name"),
            "org_id": value(row, "org_id"),
        })
    return {"batch": batch_label, "rows": out_rows}


def summarize_receipts(response) -> str:
    """One-line tally of her per-row receipts; '' when the shape is unknown."""
    if not isinstance(response, dict):
        return ""
    receipts = response.get("rows") or response.get("receipts")
    if not isinstance(receipts, list):
        return ""
    counts: dict[str, int] = {}
    rejected: list[str] = []
    for receipt in receipts:
        if not isinstance(receipt, dict):
            continue
        status = str(receipt.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
        if status == "rejected":
            rejected.append(
                f"{receipt.get('row_id', '?')}: {receipt.get('reason', 'no reason')}"
            )
    if not counts:
        return ""
    summary = "receipts: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
    if rejected:
        summary += " | rejected -> " + "; ".join(rejected)
    return summary


def _headers(*, token: str | None, vercel_bypass: str | None) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if vercel_bypass:
        # Vercel deployment protection: without this header the platform
        # returns its auth wall before the request ever reaches her endpoint.
        headers["x-vercel-protection-bypass"] = vercel_bypass
    return headers


def _default_transport(token: str | None, vercel_bypass: str | None = None) -> Transport:
    def transport(method: str, url: str, payload: dict | None) -> dict:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        for name, value in _headers(token=token, vercel_bypass=vercel_bypass).items():
            req.add_header(name, value)
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
        return json.loads(body) if body else {}

    return transport


class LcmClient:
    def __init__(
        self,
        url: str,
        *,
        token: str | None = None,
        vercel_bypass: str | None = None,
        transport: Transport | None = None,
        retries: int = 2,
        retry_wait: float = 2.0,
    ) -> None:
        self.url = url
        self.transport = transport or _default_transport(token, vercel_bypass)
        self.retries = retries
        self.retry_wait = retry_wait

    def _request(self, method: str, url: str, payload: dict | None) -> dict:
        # Intake is idempotent per (batch, row_id) — her doc's explicit advice
        # for 5xx/timeouts is "just retry the whole batch", so we do.
        attempts = self.retries + 1
        for attempt in range(attempts):
            try:
                return self.transport(method, url, payload)
            except urllib.error.HTTPError as exc:
                if exc.code >= 500 and attempt < attempts - 1:
                    time.sleep(self.retry_wait)
                    continue
                hint = _HTTP_HINTS.get(exc.code)
                if hint:
                    raise LcmPostError(f"HTTP {exc.code}: {hint}") from exc
                raise LcmPostError(
                    f"HTTP {exc.code} after {attempt + 1} attempt(s)"
                ) from exc
            except urllib.error.URLError as exc:
                if attempt < attempts - 1:  # network blip / timeout
                    time.sleep(self.retry_wait)
                    continue
                raise
        raise LcmPostError("unreachable")  # pragma: no cover

    def post_payload(self, payload: dict) -> dict:
        n_rows = len(payload.get("rows") or [])
        if n_rows > MAX_BATCH_ROWS:
            raise ValueError(
                f"{n_rows} rows exceeds the {MAX_BATCH_ROWS}-row intake cap — split the batch"
            )
        return self._request("POST", self.url, payload)

    def post_rows(
        self, batch_label: str, header: list[str], rows: list[list[str]]
    ) -> dict:
        return self.post_payload(rows_to_payload(batch_label, header, rows))

    def batch_status(self, batch_label: str) -> dict:
        """GET intake/<batch>: {pending, generated, skipped, failed, total}."""
        quoted = urllib.parse.quote(batch_label, safe="")
        return self._request("GET", f"{self.url.rstrip('/')}/{quoted}", None)
