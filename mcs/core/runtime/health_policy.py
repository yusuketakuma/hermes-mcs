"""Share pure health freshness and collection-stall observation policy."""
import os

from stats_math import finite_number as _finite_number

HEALTH_REL = os.path.join("data", "health.json")
WATCH_STATUS_REL = os.path.join("data", "health_watch_status.json")
STATUS_MAX_BYTES = 1024 * 1024

DEFAULT_TICK_S = 600        # deployed cron cadence: */10 * * * *
DEFAULT_MAX_MISSED = 2      # miss two whole ticks before 'stale'
RUN_GRACE_S = 480           # run_check's whole-run deadline
# A pinned timeline certifies only on a full rescan; lag alone is normal.
COVERAGE_STALL_S = 24 * 3600
# A queued render this old on the live transport is stalled. A complete
# notification measurement older than this cannot vouch for renders queued
# since, so a carried measurement expires at the same age.
CARD_STALL_S = 1800
# Full extraction-coverage and patient-context COUNTs run at most this often.
COVERAGE_COUNT_S = 3600
# A recorded patient-context count is carried, marked stale, while refreshes
# keep failing for at most three refresh periods; older is unknown.
PATIENT_CONTEXT_CARRY_S = 3 * COVERAGE_COUNT_S
# An original still awaiting its OCR result is never pruned; past this many
# days since download it is reported by count and oldest age only.
DEFAULT_OCR_RETAINED_WARN_DAYS = 30
# Why an unfinished run stopped, as runs.error of a 'crashed' row. Only a
# reason its evidence proves is written; anything else reads as 'unknown'.
INTERRUPT_REASONS = ("watchdog_exit", "update_preempted", "host_restart", "unknown")


def _tick_settings(cfg: dict) -> tuple[float, float]:
    """(tick_interval_s, max_missed_runs) with defaults applied to
    missing or invalid values."""
    h = cfg.get("health") if isinstance(cfg, dict) else None
    h = h if isinstance(h, dict) else {}
    tick = h.get("tick_interval_s", DEFAULT_TICK_S)
    missed = h.get("max_missed_runs", DEFAULT_MAX_MISSED)
    if not (_finite_number(tick) and tick > 0):
        tick = DEFAULT_TICK_S
    if not (_finite_number(missed) and missed >= 0):
        missed = DEFAULT_MAX_MISSED
    return tick, missed


def ocr_retained_warn_days(cfg: dict) -> int:
    """Return health.attachment_ocr_retained_warn_days, or the default for a missing or invalid value."""
    h = cfg.get("health") if isinstance(cfg, dict) else None
    value = h.get("attachment_ocr_retained_warn_days") if isinstance(h, dict) else None
    return value if type(value) is int and 1 <= value <= 3650 else DEFAULT_OCR_RETAINED_WARN_DAYS


def freshness_deadline(cfg: dict) -> int:
    """tick_interval_s * (max_missed_runs + 1) from config — a reader
    must not assume 'healthy' while the producer could legitimately be
    inside its allowed missed-run window."""
    tick, missed = _tick_settings(cfg)
    deadline = tick * (int(missed) + 1)
    return max(1, int(deadline)) if _finite_number(deadline) \
        else DEFAULT_TICK_S * (DEFAULT_MAX_MISSED + 1)
