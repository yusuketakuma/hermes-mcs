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


def _total_seconds(events):
    total = sum(event["model_s"] for event in events)
    if not math.isfinite(total):
        raise WorkflowError("cost_model_total_overflow")
    return round(total, 3)


def _cost_model(supplied) -> dict:
    if supplied is None:
        return dict(DEFAULT_COST_MODEL)
    if not isinstance(supplied, dict) or not supplied:
        raise WorkflowError("cost_model_required")
    missing = [k for k in DEFAULT_COST_MODEL if k not in supplied]
    if missing:
        raise WorkflowError("cost_model_incomplete:"
                            + ",".join(sorted(missing)))
    return {k: _num(v, k) for k, v in supplied.items()}


def _id(value, field) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WorkflowError(f"{field}_invalid")
    return value


def _validate_case(case) -> dict:
    if not isinstance(case, dict):
        raise WorkflowError("case_object_required")
    _id(case.get("case_id"), "case_id")
    targets = case.get("targets")
    if not isinstance(targets, list) or not targets:
        raise WorkflowError("case_targets_empty")
    targets = [_id(t, "case_target") for t in targets]
    if len(targets) != len(set(targets)):
        raise WorkflowError("case_target_duplicate")
    messages = case.get("messages")
    if not isinstance(messages, list) or not messages:
        raise WorkflowError("case_messages_empty")
    mids = []
    for m in messages:
        if not isinstance(m, dict):
            raise WorkflowError("case_message_object_required")
        mids.append(_id(m.get("message_id"), "case_message_id"))
        if not isinstance(m.get("has_quote_for", []), list):
            raise WorkflowError("case_message_quotes_list_required")
        for fid in m.get("has_quote_for", []):
            _id(fid, "case_message_quote_id")
    if len(mids) != len(set(mids)):
        raise WorkflowError("case_message_id_duplicate")
    card = case.get("card")
    if not isinstance(card, dict):
        raise WorkflowError("case_card_required")
    fact_lines = card.get("fact_lines", [])
    if not isinstance(fact_lines, list):
        raise WorkflowError("case_fact_lines_list_required")
    if any(not isinstance(line, dict) for line in fact_lines):
        raise WorkflowError("case_fact_line_object_required")
    line_ids = [_id(line.get("fact_id"), "case_fact_line_id")
                for line in fact_lines]
    if len(line_ids) != len(set(line_ids)):
        raise WorkflowError("case_fact_line_id_duplicate")
    pages = card.get("pages", 1)
    if isinstance(pages, bool) or not isinstance(pages, int) or pages < 1:
        raise WorkflowError("case_pages_invalid")
    attachments = case.get("attachments", [])
    if not isinstance(attachments, list):
        raise WorkflowError("case_attachments_list_required")
    for a in attachments:
        if not isinstance(a, dict):
            raise WorkflowError("case_attachment_object_required")
        _id(a.get("message_id"), "case_attachment_message_id")
        if not isinstance(a.get("needed_for", []), list):
            raise WorkflowError("case_attachment_needed_list_required")
        for fid in a.get("needed_for", []):
            _id(fid, "case_attachment_fact_id")
    return {"targets": targets, "messages": messages,
            "fact_lines": fact_lines, "pages": pages,
            "overview_lines": card.get("overview_lines", 0),
            "attachments": attachments}


def _measure_card(case: dict, cost: dict) -> dict:
    """One overview read, then per target: line scan to its position
    + evidence confirm + page drills beyond page 1."""
    events = [{"step": 1, "unit": "overview",
               "model_s": cost["overview_read_s"]}]
    step = 1
    line_pos = {line.get("fact_id"): i for i, line in
                enumerate(case["fact_lines"], 1)}
    located = {}
    for target in case["targets"]:
        pos = line_pos.get(target)
        scanned = pos if pos is not None else len(case["fact_lines"])
        for i in range(scanned):
            step += 1
            ref = case["fact_lines"][i]["fact_id"] \
                if i < len(case["fact_lines"]) else target
            events.append({"step": step, "unit": f"fact_line:{ref}",
                           "model_s": cost["fact_line_scan_s"]})
        if pos is None:
            located[target] = False
            step += 1
            events.append({"step": step, "unit": f"locate_miss:{target}",
                           "model_s": 0.0})
            continue
        located[target] = True
        step += 1
        events.append({"step": step, "unit": f"evidence:{target}",
                       "model_s": cost["evidence_read_s"]})
        page = (pos - 1) // _FACTS_PER_PAGE
        for _ in range(page):
            step += 1
            events.append({"step": step, "unit": f"page_drill:{target}",
                           "model_s": cost["page_drill_s"]})
    return {"steps": step, "located": located, "events": events,
            "model_s": _total_seconds(events)}


def _measure_source(case: dict, cost: dict) -> dict:
    """Open + scan messages in posted order until each target's quote
    is reached; an attachment marked needed costs one open."""
    quote_of = {}
    for m in case["messages"]:
        for fid in m.get("has_quote_for", []):
            quote_of.setdefault(fid, m["message_id"])
    attach_of = {}
    for a in case["attachments"]:
        for fid in a.get("needed_for", []):
            attach_of.setdefault(fid, a["message_id"])
    events = []
    step = 0
    opened = set()
    located = {}
    for target in case["targets"]:
        dest = quote_of.get(target) or attach_of.get(target)
        # Re-reading a message already opened for an earlier target is
        # free — sequential review retains what it has seen.
        found = dest is not None and dest in opened
        if not found:
            for m in case["messages"]:
                mid = m["message_id"]
                if mid in opened:
                    continue
                opened.add(mid)
                step += 1
                events.append({"step": step, "unit": f"message:{mid}",
                               "model_s": cost["message_open_s"]
                               + cost["message_scan_s"]})
                if mid == dest:
                    found = True
                    break
        if not found:
            located[target] = False
            step += 1
            events.append({"step": step, "unit": f"locate_miss:{target}",
                           "model_s": 0.0})
            continue
        located[target] = True
        if target in attach_of:
            step += 1
            events.append({"step": step,
                           "unit": f"attachment:{attach_of[target]}",
                           "model_s": cost["attachment_open_s"]})
    return {"steps": step, "located": located, "events": events,
            "model_s": _total_seconds(events)}


def measure_case(case: dict, *, cost_model=None) -> dict:
    """Steps + modelled seconds + located flags for both surfaces on
    one fabricated case — identical target set, identical coverage."""
    norm = _validate_case(case)
    cost = _cost_model(cost_model)
    return {"case_id": case["case_id"],
            "card": _measure_card(norm, cost),
            "source": _measure_source(norm, cost)}


def _p(values: list, q: float):
    if not values:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1, math.ceil(q * len(ordered)) - 1)
    return ordered[max(0, idx)]


def compare_workflow(cases: list, *, cost_model=None) -> dict:
    """Aggregate card-vs-source mechanics across fabricated cases."""
    if not isinstance(cases, list) or not cases:
        raise WorkflowError("cases_empty")
    seen = set()
    rows = []
    for case in cases:
        if not isinstance(case, dict):
            raise WorkflowError("case_object_required")
        cid = _id(case.get("case_id"), "case_id")
        if cid in seen:
            raise WorkflowError("case_id_duplicate")
        seen.add(cid)
        rows.append(measure_case(case, cost_model=cost_model))
    cost = _cost_model(cost_model)

    def _agg(key):
        steps = [r[key]["steps"] for r in rows]
        secs = [r[key]["model_s"] for r in rows]
        located = sum(sum(v is True for v in r[key]["located"].values())
                      for r in rows)
        targets = sum(len(r[key]["located"]) for r in rows)
        return {"steps_p50": _p(steps, .5), "steps_p95": _p(steps, .95),
                "model_s_p50": _p(secs, .5), "model_s_p95": _p(secs, .95),
                "located": located, "targets": targets}

    return {"schema_version": WORKFLOW_SCHEMA,
            "basis": "declared synthetic cost model — review-step and "
                     "modelled-second mechanics only, not human timing "
                     "and not clinical evidence",
            "cost_model": cost,
            "clinical_validity": "NOT_TESTED",
            "real_workload_benefit": "NOT_TESTED",
            "aggregate": {"cases": len(rows),
                          "card": _agg("card"), "source": _agg("source")},
            "cases": rows}
