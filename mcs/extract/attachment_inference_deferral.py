"""添付ページのローカル解析が保留された理由を固定コードで定義し、件数だけを正規化する。

The OCR job re-observes the admission conditions right after a deferral and
records only these fixed codes with integer counts: never patient data, paths
or exception text. ``unclassified`` means no condition was confirmed on the
re-check, not that nothing was wrong.
"""
from __future__ import annotations

UNCLASSIFIED = "unclassified"

# Priority order: the same order the page run checks its admission
# conditions, so a tie between counted codes resolves deterministically.
LOCAL_DEFER_REASONS = (
    "writer_refused",       # the shared writer fence recorded a fixed refusal code
    "disk_unknown",         # free space could not be measured on the re-check
    "disk_low",             # free space under the configured floor on the re-check
    "memory_unknown",       # memory headroom could not be observed on the re-check
    "memory_low",           # memory headroom below the shared reserve on the re-check
    "inference_lock_busy",  # another process held the page-inference lock
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
