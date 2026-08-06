"""Versioned field contract for the secure org context pack.

This module is the security boundary, expressed as code. ALLOWED_FIELDS is
the complete set of names that may reach Pathfinder from the Snowflake
source; FORBIDDEN_FIELDS names things we expect to see nearby and refuse on
sight, so a query shape change fails loudly instead of leaking.

STATUS: PROPOSED at ``org-context-v2``. Pending analytics + security sign-off
(see docs/superpowers/specs/2026-07-29-secure-org-by-org-snowflake-design.md,
"The Assignment", and docs/superpowers/specs/2026-08-01-org-context-v2-design.md
for the v2 field table, thresholds, and PII analysis). Amending either set
requires bumping CONTRACT_VERSION.

v2 adds 18 fields to v1's 11 and RE-OPENS sign-off: v1's approval does not
carry over. The 11 v1 fields and their thresholds are unchanged, so nothing
previously approved changed meaning — but the version literal moved, and the
SQL in integrations/n8n/pathfinder-org-context.json must emit
``'org-context-v2'`` in lockstep. A mismatch makes minimize_row refuse every
row, which is the intended failure mode: an unreviewed field set must not
reach a prompt wearing an approved version.

Every value that survives minimize_row() is serialized verbatim into an
Anthropic prompt by grounding_critic.py, and is persisted alongside the run
in run records and Supabase. Only banded, bucketed, boolean, or
consent-state values belong here — never raw amounts, dates, free text, or
identifiers.

Earlier revisions of this docstring justified the allowlist as preventing
egress to a "third party". That framing is retired and should not be used:
Pathfinder's ANTHROPIC_API_KEY is on Housecall Pro's own secured Anthropic
account, so sending banded org data to the model is sanctioned, not an
external disclosure. The allowlist still earns its place for two narrower
reasons: pack values persist into our own artifacts, so a leaked PII column
propagates internally; and a value the model reads is a value the model will
assert to a customer, so anything unverifiable in here becomes a false claim
in an outgoing message.

Enforcement is on both axes: key names are checked against ALLOWED_FIELDS /
FORBIDDEN_FIELDS (both matched case-insensitively, so a miscased column —
``OPEN_AR_BAND`` from a native Snowflake cursor, ``Customer_Email`` from a
renamed column — is recognized rather than silently dropped), and
allowlisted values are checked for shape — only None, str, int, float, and
bool are accepted. A dict, list, tuple, set, bytes, or any other non-scalar
under an allowlisted key raises ContractViolation rather than being
stringified, because ``str(container)`` would silently smuggle nested
identifiers into the prompt.

A row that matches NO allowlisted field at all is treated as a query-shape
failure, not as an empty result: an empty context wearing an approved
contract version is worse than a loud refusal, because the grounding critic
would then block every idea as ungrounded with nothing logging the cause.
A row that DOES carry allowlisted keys whose values are all blank is a
legitimate empty result and returns ``{}``. Two keys normalizing onto one
allowlisted field is likewise a refusal, since column order would otherwise
silently decide which value the prompt asserts.

Forbidden fields raise ForbiddenFieldViolation, a ContractViolation
subclass, so alerting can distinguish a near-miss PII leak from a renamed
column. Every other breach raises ContractViolation itself.

A source row may also declare its own contract version (``contract_version``,
the name §1 of docs/org_context_contract.md asks analytics to emit). If it
declares one that disagrees with CONTRACT_VERSION, the row is refused rather
than silently relabelled with this module's own version.
"""
from __future__ import annotations

from typing import Any

CONTRACT_VERSION = "org-context-v2"

# The ten features whose per-feature attach/usage state v2 emits, in the order
# the SQL emits them. Named here rather than spelled out ten times below so the
# SQL, the allowlist, and the prompt semantics layer cannot drift apart.
#
# These are FEATURE KEYS, not Snowflake column names: ``online_booking`` is
# ``feature_by_orgday.BOOKING_WIDGET`` and ``time_tracking`` covers the
# ``TIME_TRACKING`` attach flag. See the v2 design doc's field table.
PER_FEATURE_STATE_FEATURES: tuple[str, ...] = (
    "online_booking",
    "premium_reviews",
    "sales_proposal",
    "service_agreements",
    "hcp_assist",
    "quickbooks",
    "voip",
    "card_on_file",
    "time_tracking",
    "flat_rate_pricing",
)

# The closed domain of every ``feature_<name>_state`` field.
#
# ``attached_usage_unknown`` is NOT a synonym for ``attached_unused`` and must
# never be collapsed into it. "They pay for this and do not use it" argues for
# activation help; "we cannot see whether they use it" argues for asking. Four
# of the ten features have an attach flag but no verified usage source, and
# preserving that distinction is the reason v2 exists.
FEATURE_STATES: frozenset[str] = frozenset(
    {
        "not_attached",
        "attached_unused",
        "attached_active",
        "attached_usage_unknown",
    }
)

PER_FEATURE_STATE_FIELDS: tuple[str, ...] = tuple(
    f"feature_{name}_state" for name in PER_FEATURE_STATE_FEATURES
)

# Banded / boolean / consent-state fields only. No raw values.
ALLOWED_FIELDS: frozenset[str] = frozenset(
    {
        # feature adoption + plan value
        # NOTE: feature_adoption_band is a COUNT of attached paid add-ons. It
        # says nothing about usage, and reading it as "well adopted" is the
        # v1 misreading v2 exists to fix. The per-feature states below are the
        # authority on usage.
        "feature_adoption_band",
        "plan_gap_band",
        "ltv_score_band",
        # money flow (bands only — never invoice rows or payment identifiers)
        "open_ar_band",
        "ar_aging_band",
        # jobs / workflow (bands only — never customer-level job detail)
        "jobs_created_28d_band",
        "estimates_created_28d_band",
        "invoices_sent_28d_band",
        # communications + consent (counts and state only)
        "outreach_count_28d_band",
        "sms_consent_state",
        "email_consent_state",
        # v2: prioritization. Which single feature gap to lead with, how big it
        # is, and whether the Pro is already paying for it. All banded or drawn
        # from a closed, live-verified vocabulary — never a dollar figure.
        "recommended_focus",
        "recommended_focus_value_band",
        "recommended_focus_retention_lift_band",
        "top_unused_paid_feature",
        # v2: firmographic context. Every one is a closed vocabulary; `vertical`
        # in particular is MAPPED in SQL from 174 dirty source values onto 16
        # buckets, because an unbounded free-text string reaching a third-party
        # prompt is exactly what this contract exists to prevent.
        "vertical",
        "plan_tier",
        "org_size_band",
        "tenure_band",
        # v2: per-feature attach + usage state, the whole point of the version.
        *PER_FEATURE_STATE_FIELDS,
    }
)

# Named explicitly so a query shape change trips a loud failure. This is not
# exhaustive of all PII — it is the set we expect adjacent to the source
# tables listed in the spec's Source Data Boundary.
FORBIDDEN_FIELDS: frozenset[str] = frozenset(
    {
        "customer_name",
        "customer_email",
        "customer_phone",
        "address",
        "message_subject",
        "message_body",
        "sms_body",
        "email_body",
        "invoice_id",
        "invoice_amount",
        "payment_id",
        "payment_method",
        "card_last4",
        "bank_account",
    }
)

_BLANK = frozenset({"", "unknown"})

# Precomputed lookups. Hoisted to module level so they are built once at
# import rather than rebuilt on every minimize_row() call.
_FORBIDDEN_LOWER: frozenset[str] = frozenset(name.lower() for name in FORBIDDEN_FIELDS)
# Maps a lowercased incoming key to the CANONICAL allowlist spelling, which is
# what gets emitted. Snowflake returns column names uppercase natively, so
# case-insensitive matching is required for the allowlist to work at all.
_ALLOWED_BY_LOWER: dict[str, str] = {name.lower(): name for name in ALLOWED_FIELDS}

# Field names a source row may use to declare the contract version it was
# built against. ``contract_version`` is the name docs/org_context_contract.md
# §1 asks analytics to emit; ``org_context_contract_version`` is the key this
# repo stamps onto the minimized pack, accepted here so a round-tripped row
# is checked rather than ignored.
_CONTRACT_VERSION_FIELDS: frozenset[str] = frozenset(
    {"contract_version", "org_context_contract_version"}
)


class ContractViolation(ValueError):
    """A source row breached the contract.

    Raised for a non-scalar allowlisted value, a row whose shape matches no
    allowlisted field at all, two row keys colliding onto one allowlisted
    field, or a row declaring a contract version other than CONTRACT_VERSION.

    A forbidden field raises the ``ForbiddenFieldViolation`` subclass instead.
    Catching ``ContractViolation`` still catches every case, including that one.
    """


class ForbiddenFieldViolation(ContractViolation):
    """A source row carried a name from FORBIDDEN_FIELDS.

    A SUBCLASS so every existing ``except ContractViolation`` handler keeps
    working unchanged. It exists because the two failures are not the same
    event operationally: "the query nearly handed us a card suffix" is a PII
    tripwire that should page someone, while "analytics renamed a column" is a
    shape typo that should open a ticket. Both previously arrived as one class
    behind one ``"org context rejected: "`` prefix, so Phase 2 alerting could
    not tell them apart. Alert on this type specifically.
    """


# Value types that may be coerced to text. Anything else under an
# allowlisted key is a contract breach, not something to stringify.
_SCALAR_TYPES = (str, int, float, bool)


def minimize_row(row: dict[str, Any]) -> dict[str, str]:
    """Reduce one source row to allowlisted, stringified context entries.

    Forbidden fields raise. BOTH the forbidden set and the allowlist are
    matched case-insensitively, so a miscased column fails loudly (forbidden)
    or is still recognized (allowlisted) instead of falling through as
    "unrecognized". Output keys are the canonical lowercase spelling from
    ALLOWED_FIELDS, so ``OPEN_AR_BAND`` from a native Snowflake cursor is
    emitted as ``open_ar_band``.

    Unrecognized (non-forbidden) fields are dropped silently ALONGSIDE at
    least one recognized field — the source may legitimately return
    correlation columns we do not consume. But a row that matches NO
    allowlisted field is a query-shape failure and raises: silently returning
    ``{}`` there would hand the caller an empty context stamped with an
    approved contract version, and the only visible symptom would be the
    grounding critic blocking every idea with no logged cause. The raised
    message names the unrecognized KEY NAMES only, never their values.

    A row that does carry allowlisted keys but whose every value is
    blank/``"unknown"``/None is a legitimate empty result and returns ``{}``
    without raising, so the pack's ``unknown`` list stays accurate.

    Two or more keys that normalize to the SAME allowlisted field (e.g. both
    ``open_ar_band`` and ``OPEN_AR_BAND``) raise: the emitted value would
    otherwise be decided by column order, silently. The message names the
    colliding KEY NAMES only, never their values.

    An allowlisted key whose value is not None and not a str/int/float/bool
    raises ContractViolation instead of being stringified.

    A forbidden field raises ``ForbiddenFieldViolation``, a subclass, so a PII
    tripwire is distinguishable from a shape typo without breaking any
    ``except ContractViolation`` handler.

    A row declaring a contract version (see ``_CONTRACT_VERSION_FIELDS``) that
    disagrees with CONTRACT_VERSION is refused: relabelling an honestly-stamped
    v2 row as v1 would hide a band-threshold change behind an approved version.
    """
    row = row or {}
    present_forbidden = sorted(
        key for key in row if str(key).lower() in _FORBIDDEN_LOWER
    )
    if present_forbidden:
        raise ForbiddenFieldViolation(
            "source row contains forbidden field(s): " + ", ".join(present_forbidden)
        )

    for key, value in row.items():
        if str(key).lower() not in _CONTRACT_VERSION_FIELDS:
            continue
        declared = "" if value is None else str(value).strip()
        if declared and declared != CONTRACT_VERSION:
            raise ContractViolation(
                f"source row declares contract version {declared!r} under "
                f"{str(key)!r}, but this build enforces {CONTRACT_VERSION!r}; "
                "refusing to relabel the row"
            )

    # Collision pass, BEFORE any value is read. Case-insensitive matching means
    # two distinct source columns can normalize onto one output key, and the
    # loop below would silently keep whichever the row happened to order last.
    # Refuse instead: during a view rename a quoted alias can coexist with the
    # unquoted column, and column order would decide what the prompt asserts.
    # Runs as its own pass so a collision is caught even when one of the two
    # values is blank and would have been dropped.
    collisions: dict[str, list[str]] = {}
    for key in row:
        canonical = _ALLOWED_BY_LOWER.get(str(key).lower())
        if canonical is not None:
            collisions.setdefault(canonical, []).append(str(key))
    colliding = sorted(
        (field, sorted(names))
        for field, names in collisions.items()
        if len(names) > 1
    )
    if colliding:
        detail = "; ".join(
            f"{field} <- " + ", ".join(names) for field, names in colliding
        )
        raise ContractViolation(
            "source row has multiple keys mapping to the same allowlisted "
            "field, so the emitted value would depend on column order. "
            "Refusing rather than picking one. Colliding key name(s): " + detail
        )

    out: dict[str, str] = {}
    unrecognized: list[str] = []
    matched_any = False
    for key, value in row.items():
        canonical = _ALLOWED_BY_LOWER.get(str(key).lower())
        if canonical is None:
            unrecognized.append(str(key))
            continue
        matched_any = True
        if value is None:
            continue
        if not isinstance(value, _SCALAR_TYPES):
            raise ContractViolation(
                f"allowlisted field {canonical!r} has non-scalar value of type "
                f"{type(value).__name__}; refusing to stringify a container "
                "into the prompt"
            )
        text = str(value).strip()
        if text.lower() in _BLANK:
            continue
        out[canonical] = text

    if not matched_any:
        named = ", ".join(sorted(unrecognized)) if unrecognized else "(row had no keys)"
        raise ContractViolation(
            "source row matched no allowlisted field; the query shape is not "
            "what this contract expects. Unrecognized key name(s): " + named
        )
    return out


class OrgIsolationError(ValueError):
    """Returned rows do not map 1:1 onto the requested orgs."""


def index_rows_by_org(
    rows: list[dict[str, Any]], requested: list[str]
) -> dict[str, dict[str, Any]]:
    """Map exactly one source row to each requested org UUID.

    Fail-closed on every contamination mode the spec names: a row for an
    org we did not ask for, two rows for the same org, a row with no org
    identity, or a requested org with no row. Any of these means the query
    or its parameters are not what we think they are.
    """
    wanted = list(requested)
    allowed = set(wanted)
    by_org: dict[str, dict[str, Any]] = {}
    for row in rows or []:
        raw = (row or {}).get("org_uuid")
        org_uuid = str(raw).strip() if raw is not None else ""
        if not org_uuid:
            raise OrgIsolationError("source row has no org_uuid")
        if org_uuid not in allowed:
            raise OrgIsolationError(
                f"source returned a row for an unrequested org: {org_uuid}"
            )
        if org_uuid in by_org:
            raise OrgIsolationError(f"source returned duplicate rows for org {org_uuid}")
        by_org[org_uuid] = row
    missing = [org for org in wanted if org not in by_org]
    if missing:
        raise OrgIsolationError(
            "source returned no row for requested org(s): " + ", ".join(missing)
        )
    return by_org
