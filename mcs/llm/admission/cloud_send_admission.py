"""Own the cloud-send refusal types, the stop-reason classifier and the shared spending-cap comparison."""
from __future__ import annotations

import mcs_util

CAP_KEYS = ("daily_request_budget", "daily_cost_limit_usd", "monthly_cost_limit_usd", "window_budget_usd")


class CloudFallback(Exception):
    """Discard this provider's unit and retry under the local contract."""


class CloudBusy(CloudFallback):
    """No shared cloud capacity was reserved or sent."""


class CloudObservationIncomplete(CloudFallback):
    """Defer adoption without discarding a received answer or starting a fallback."""


def caps(settings):
    """The spending limits one attempt is reserved under, comparable across settings reads."""
    out = {key: settings[key] for key in CAP_KEYS}
    # an earlier period start counts more spending, like a lower cap
    start = settings["window_start"]
    out["window_start"] = None if start is None else mcs_util.until_epoch(start)
    budgets = settings["provider_budgets"]
    provider_caps = budgets.get(settings["provider"], {})
    for key in ("daily_cost_limit_usd", "monthly_cost_limit_usd"):
        out["provider_" + key] = provider_caps.get(key, 0 if budgets else None)
    return out


def tightened(reserved, now):
    """True when a cap was set or lowered after the reservation: its unsent request must stop.

    The reservation itself stays counted at its maximum; only the send it has not made yet stops.
    """
    return any(reserved.get(key, now[key]) is None and now[key] is not None
               or None not in (reserved.get(key), now[key]) and now[key] < reserved[key]
               for key in now)


# Why a cloud unit stopped, judged by when it stopped (owner table, PATIENT_CONTEXT_P0_PLAN_20261115.md,
# with the owner decisions of 2026-11-16). ``progress`` means the unit already holds cloud work:
# a persisted cloud cursor or a paid reservation for its unit key. The verdicts:
#   defer  no local work; the unit waits for the cloud.
#   local  a definitive stop: the unit goes local and may record its earlier requests as replaced.
#   hold   today's path: local work only where the strict guard allows it, and nothing is replaced.
# Every code the send checks can return is declared below; an unlisted code holds.
FRESH_DEFER = frozenset({
    # transient: the send slot, a temporary key read failure, the day's caps, a cooling-down circuit
    "cloud_capacity", "env_file_unreadable", "daily_cap", "daily_cost_cap", "provider_daily_cap",
    "circuit_open", "rate_limited", "http_429", "transport", "timeout", "http_5xx", "http_529",
    "http_402", "contract_error",
})
PRE_SEND_LOCAL = frozenset({
    # definitive: the period, the month, the owner's stop, mode off, scope, facility review, prices
    "expired", "monthly_cap", "provider_monthly_cap", "paused", "mode_off", "scope", "route_closed",
    "provider_not_approved", "window_cap", "price_expired", "window_history_aged",
    # a source that already continues locally after a verified replacement
    "local_pinned",
})
# A latched circuit opens until a person resumes it, and the other codes clear by themselves:
# paid progress waits for them, while a fresh unit keeps today's path.
PRE_SEND_PROGRESS_DEFER = frozenset({
    "auth_error", "payment_required", "not_started", "deadline", "owner_transaction_active",
    "checkpoint_unverified",
})
# No context child is serving the cloud lane: paid progress waits for one; a fresh unit goes local.
PRE_SEND_TAKEOVER = frozenset({"cloud_worker_unavailable"})
PRE_SEND_HOLD = frozenset({
    "pricing_unverified", "profile_invalid", "provider_budget_missing", "window_history_pruned",
    "window_history_unverified",
    # the facility review record cannot be read: not a review decision, so nothing is replaced
    "transfer_review_unreadable",
    # mcs_util._private_env_lookup codes other than a temporary read failure
    "env_file_missing", "env_file_not_regular", "env_file_owner", "env_file_mode_not_0600",
    "env_file_too_large", "env_file_not_utf8", "key_missing", "key_export_prefix", "key_duplicate",
    "key_quote_invalid", "key_empty", "key_whitespace_or_control",
})
PRE_SEND_VERDICTS = {
    **{code: ("defer", "defer") for code in FRESH_DEFER},
    **{code: ("local", "local") for code in PRE_SEND_LOCAL},
    **{code: ("defer", "hold") for code in PRE_SEND_PROGRESS_DEFER},
    **{code: ("defer", "local") for code in PRE_SEND_TAKEOVER},
    **{code: ("hold", "hold") for code in PRE_SEND_HOLD},
}
# After a request may have been sent: an unknown or failed answer goes local, which the
# unit's reservation guard then lets replace it; a rate limit waits only for earlier progress.
AFTER_SEND_LOCAL = frozenset({
    "http_4xx", "http_5xx", "http_529", "transport", "timeout", "response_too_large", "usage_unknown",
    "protocol_error", "contract_error",
})
AFTER_SEND_PROGRESS_DEFER = frozenset({"http_429", "rate_limited"})
# The unit's own paid history refused the request: try local, where each row decides.
HISTORY_CODES = frozenset({
    "repeat_success", "repeat_failed", "repeat_unsettled", "result_missing", "disposition",
    "receipt_inconsistent", "cloud_result_missing", "cloud_result_discarded", "cloud_result_unsettled",
    "cloud_receipt_invalid", "cloud_answer_invalid", "cloud_answer_unresolved", "cloud_result_superseded",
})
STAGES = ("pre_send", "after_send", "history")
# The only values a caller may report as its cloud refusal.
REFUSAL_CODES = frozenset(PRE_SEND_VERDICTS) | AFTER_SEND_LOCAL | AFTER_SEND_PROGRESS_DEFER \
    | HISTORY_CODES | {"unknown"}


def classify_refusal(stage, code, *, progress):
    """``defer``, ``local`` or ``hold`` for one stop reason; an unlisted code holds."""
    if stage not in STAGES or type(progress) is not bool:
        raise ValueError("cloud_refusal_stage_invalid")
    if type(code) is not str:
        return "hold"
    if stage == "pre_send":
        verdicts = PRE_SEND_VERDICTS.get(code)
        return "hold" if verdicts is None else verdicts[0 if progress else 1]
    if stage == "after_send":
        if code in AFTER_SEND_PROGRESS_DEFER:
            return "defer" if progress else "local"
        return "local" if code in AFTER_SEND_LOCAL else "hold"
    return "local" if code in HISTORY_CODES else "hold"
