"""Share pure health freshness and collection-stall observation policy."""
import os

from stats_math import finite_number as _finite_number

HEALTH_REL = os.path.join("data", "health.json")
STATUS_MAX_BYTES = 1024 * 1024

DEFAULT_TICK_S = 600        # deployed cron cadence: */10 * * * *
DEFAULT_MAX_MISSED = 2      # miss two whole ticks before 'stale'
RUN_GRACE_S = 480           # run_check's whole-run deadline
# A pinned timeline certifies only on a full rescan; lag alone is normal.
COVERAGE_STALL_S = 24 * 3600


def _tick_settings(cfg: dict) -> tuple[dict, float, float]:
    """(health block, tick_interval_s, max_missed_runs) with defaults
    applied to missing or invalid values."""
    h = cfg.get("health") if isinstance(cfg, dict) else None
    h = h if isinstance(h, dict) else {}
    tick = h.get("tick_interval_s", DEFAULT_TICK_S)
    missed = h.get("max_missed_runs", DEFAULT_MAX_MISSED)
    if not (_finite_number(tick) and tick > 0):
        tick = DEFAULT_TICK_S
    if not (_finite_number(missed) and missed >= 0):
        missed = DEFAULT_MAX_MISSED
    return h, tick, missed


def freshness_deadline(cfg: dict) -> int:
    """tick_interval_s * (max_missed_runs + 1) from config — a reader
    must not assume 'healthy' while the producer could legitimately be
    inside its allowed missed-run window."""
    h, tick, missed = _tick_settings(cfg)
    deadline = tick * (int(missed) + 1)
    return max(1, int(deadline)) if _finite_number(deadline) \
        else DEFAULT_TICK_S * (DEFAULT_MAX_MISSED + 1)
