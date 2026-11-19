"""添付ページのローカル解析が保留された理由を固定コードで定義し、件数だけを正規化する。

The local page run reports the fixed code from the gate that actually stopped
it, with integer counts only: never patient data, paths or exception text, and
never a post-hoc lock or resource re-probe. ``unclassified`` means no fixed
gate cause was proven for that deferral, not that nothing was wrong.
"""
from __future__ import annotations

UNCLASSIFIED = "unclassified"

# Fixed defer reasons the shared local extraction call records on its
# per-thread notes while a page run is processed. Only these exact codes may
# reach the status record, and only as call observations — an observed reason
# is not proof that it caused the hold (a successful call can record one on
# an optional repair path), so it never joins the cause breakdown.
_LLM_DEFER_REASONS = frozenset({
    "admission", "busy", "checkpoint_unverified", "circuit_open",
    "currentness_unverified", "deadline", "endpoint_down", "not_sent",
    "probe_unobserved", "quality_checkpoint_unverified", "repair_unavailable",
    "local_rebuild_proof_changed", "local_rebuild_unverified",
})
_LLM_STATUS_CODES = frozenset(f"llm_{reason}" for reason in _LLM_DEFER_REASONS)


def llm_reason(raw):
    """Map one recorded extraction deferral to its fixed status code, or None."""
    return "llm_" + raw if type(raw) is str and raw in _LLM_DEFER_REASONS else None


def observed(reasons):
    """Keep fixed call-observation codes with positive integer counts."""
    if not isinstance(reasons, dict):
        return {}
    return {code: reasons[code] for code in sorted(reasons)
            if code in _LLM_STATUS_CODES and type(reasons[code]) is int and reasons[code] > 0}


def observed_valid(reasons, total):
    """Whether observed call reasons stay within the deferrals that recorded them."""
    return (type(reasons) is dict and type(total) is int and total > 0
            and 0 < len(reasons) <= len(_LLM_STATUS_CODES)
            and all(code in _LLM_STATUS_CODES and type(count) is int and count > 0
                    for code, count in reasons.items())
            and sum(reasons.values()) <= total)


# Priority order: the same order the page run checks its admission
# conditions, so a tie between counted codes resolves deterministically.
LOCAL_DEFER_REASONS = (
    "writer_refused",       # the stopping writer fence recorded a fixed refusal code
    "disk_unknown",         # free space could not be measured at the stopping gate
    "disk_low",             # free space under the configured floor at the stopping gate
    "memory_unknown",       # memory headroom could not be observed at admission
    "memory_low",           # memory headroom below the shared reserve at admission
    "inference_lock_busy",  # another process held the page-inference lock at admission
    "local_stage_error",    # the local stage raised; the job records it as a failure
)
KNOWN = frozenset((*LOCAL_DEFER_REASONS, UNCLASSIFIED))


def disk_reason(free, floor):
    """Return the disk code for a measured free/floor pair, or None when space suffices."""
    if free is None:
        return "disk_unknown"
    return "disk_low" if free < floor else None


def memory_reason(headroom):
    """Return the memory code for one headroom measurement, or None when it is sufficient."""
    if headroom is None:
        return "memory_unknown"
    return "memory_low" if headroom < 0 else None


def normalize(reasons, total):
    """Keep known positive integer counts and assign the remainder of ``total`` to unclassified."""
    if type(total) is not int or total <= 0:
        return {}
    kept = {}
    if isinstance(reasons, dict):
        for code in LOCAL_DEFER_REASONS:
            count = reasons.get(code)
            if type(count) is int and count > 0:
                kept[code] = count
    counted = sum(kept.values())
    if counted > total:
        return {UNCLASSIFIED: total}
    if counted < total:
        kept[UNCLASSIFIED] = total - counted
    return kept


def valid(reasons, total):
    """Whether a stored breakdown is exactly a normalized one for ``total`` deferrals."""
    return (type(reasons) is dict and type(total) is int and total > 0
            and 0 < len(reasons) <= len(KNOWN)
            and all(code in KNOWN and type(count) is int and count > 0
                    for code, count in reasons.items())
            and sum(reasons.values()) == total)


def primary(reasons):
    """Return the most counted known code only when it outnumbers unclassified, else unclassified."""
    if not isinstance(reasons, dict):
        return UNCLASSIFIED
    best, best_count = UNCLASSIFIED, 0
    for code in LOCAL_DEFER_REASONS:
        count = reasons.get(code)
        if type(count) is int and count > best_count:
            best, best_count = code, count
    unclassified = reasons.get(UNCLASSIFIED)
    if type(unclassified) is int and unclassified >= best_count:
        return UNCLASSIFIED
    return best
