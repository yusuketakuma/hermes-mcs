"""Synthetic pharmacist workflow comparison harness (T6).

Measures the mechanical review burden of locating target facts and
their evidence on two surfaces over fabricated cases:

- ``card``   — the notification/structured surface: one overview read,
  then fact lines scanned in display order to the target's position,
  plus one evidence confirmation per located fact (and one page drill
  per page boundary when the render exceeds a page).
- ``source`` — the raw thread: messages opened and scanned in posted
  order until each target's evidence quote is reached, plus one
  attachment open when the case marks it needed.

Steps are counted units; seconds come from a DECLARED cost model
(DEFAULT_COST_MODEL or an explicit, fully-bounded override).  This is
a mechanics instrument, never a clinical or human-time measurement —
every report marks clinical validity and real workload benefit as
NOT_TESTED.  Cases carry ids, counters and coverage maps only; message
bodies and prompt text never enter the harness.
"""
from __future__ import annotations

import math

WORKFLOW_SCHEMA = "semantic-workflow/v1"

DEFAULT_COST_MODEL = {
    "overview_read_s": 8.0,      # read the card overview once
    "fact_line_scan_s": 1.5,     # scan one fact line while searching
    "evidence_read_s": 2.0,      # confirm evidence on a located fact
    "page_drill_s": 3.0,         # open an extra fact page
    "message_open_s": 4.0,       # open one source message
    "message_scan_s": 3.0,       # scan one message body for the quote
    "attachment_open_s": 6.0,    # open one attachment for its content
}

_FACTS_PER_PAGE = 20            # declared model; real pages use character budgets


class WorkflowError(ValueError):
    """Case or cost model is not safe to measure."""


def _num(value, field):
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise WorkflowError(f"cost_model_invalid:{field}")
    try:
        value = float(value)
    except OverflowError:
        raise WorkflowError(f"cost_model_invalid:{field}") from None
    if not math.isfinite(value) or value <= 0:
        raise WorkflowError(f"cost_model_invalid:{field}")
    return value


def _id(value, field) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WorkflowError(f"{field}_invalid")
    return value


def _p(values: list, q: float):
    if not values:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1, math.ceil(q * len(ordered)) - 1)
    return ordered[max(0, idx)]


