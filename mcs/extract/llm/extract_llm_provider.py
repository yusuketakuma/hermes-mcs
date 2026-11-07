"""Coordinate provider calls and format probes within shared admission budgets."""
import contextlib
import json
import sys
import time
import inference_version as inference
import bounded_http
import anthropic_client
import openai_client
import cloud_policy
import cloud_inference
import extract_schema
import extract_quality
from extraction_provenance import (
    provider_result_hash as _provider_result_hash,
    evidence_matches_route as _evidence_matches_route,
)
import local_llm
import mcs_util
from mcs_util import HOME, json_object, load_config, loads_dict

from extract_llm_call_context import (
    _CALL_NOTES,
    _choose_slot,
    _opener_request,
)
from extract_llm_prompts import (
    MAX_TOKENS,
)
from extract_llm_qc import (
    _hold_sql,
    _prior_extract,
)
from extract_llm_source_contract import (
    ENDPOINT,
    EXTRACT_VERSION,
    MODEL,
    TIMEOUT,
    _DEFERRED,
    _ENDPOINT_PIN,
    _INFERENCE_VERSION,
    _MODEL_PIN,
)

_FMT_MODE = None  # None=unprobed | "schema" | "object" | "plain"


_FMT_TS = 0.0


_PROBE_RETRY_S = 600


_NEXT_FMT = {"schema": "object", "object": "plain"}


def resolved_model(cfg: dict | None) -> str:
    """Return the extraction model name for `cfg`; a patched MODEL constant wins."""
    return MODEL if MODEL != _MODEL_PIN else local_llm.resolve(cfg)[1]


def _resolved_llm() -> tuple[str, str]:
    """Resolve the configured server for generation and its health/format probes."""
    pinned = getattr(_CALL_NOTES, "target", None)
    if pinned is not None:
        return pinned
    cfg = load_config()
    endpoint = local_llm.resolve(cfg)[0]
    return (ENDPOINT if ENDPOINT != _ENDPOINT_PIN else endpoint,
            resolved_model(cfg))


def _probe_format(deadline: float | None = None) -> str | None:
    """Detect the best response_format the server accepts, via the
    shared loopback adapter.  Fallback order: json_schema -> json_object
    -> plain; acceptance requires a parseable JSON object reply — a 200
    with prose means the constraint silently failed open.  Probe failure
    only downgrades the mode — it never raises into the extraction
    path."""
    global _FMT_MODE, _FMT_TS
    if _FMT_MODE == "schema":
        return _FMT_MODE
    if _FMT_MODE is not None \
            and time.monotonic() - _FMT_TS < _PROBE_RETRY_S:
        return _FMT_MODE
    endpoint, model = _resolved_llm()
    if local_llm.admission_enabled():
        # T20: the probe sends real inference POSTs — it passes the
        # same admission gate and consumes its class budget
        mode = local_llm.admitted_probe_format(
            "mcs.extract", endpoint, model, extract_schema.SCHEMA, timeout=10,
            deadline=deadline, request_fn=_opener_request,
            slot=_choose_slot(),
            verify=lambda text: json_object(text) is not None)
    else:
        mode = local_llm.probe_format(
            endpoint, model, extract_schema.SCHEMA, timeout=10,
            deadline=deadline, request_fn=_opener_request,
            slot=_choose_slot(),
            verify=lambda text: json_object(text) is not None)
    if mode is None:
        return _FMT_MODE  # GPU turn busy: nothing sent, probe again next call
    _FMT_MODE = mode
    _FMT_TS = time.monotonic()
    return _FMT_MODE


def _leaf_record(meta, r, evidence, content):
    digest = _provider_result_hash(content)
    if (evidence is None or digest is None
            or not _evidence_matches_route(evidence, meta["actual_model"], r.get("_provider_contract"))):
        return None
    return {"version": 1, "stage": "extract", **evidence, "result_hash": digest,
            "source_binding": r["_source_binding"], "context": meta["context"],
            "piece_sha256": meta["piece_sha256"], "chunk": meta["chunk"],
            "chunk_size": meta["chunk_size"], "plan_version": meta["plan_version"],
            "extract_version": EXTRACT_VERSION, "inference_version": _INFERENCE_VERSION,
            "model": meta["actual_model"], "provider_contract": r.get("_provider_contract"),
            **({"quality_lease": meta["quality_lease"]} if "quality_lease" in meta else {})}


@contextlib.contextmanager
def pin_target(target):
    """One job's actual endpoint/model tuple, never a global overwrite or log payload."""
    previous = getattr(_CALL_NOTES, "target", None)
    _CALL_NOTES.target = target
    try:
        yield
    finally:
        _CALL_NOTES.target = previous


def _note_list() -> list:
    notes = getattr(_CALL_NOTES, "items", None)
    if notes is None:
        notes = _CALL_NOTES.items = []
    return notes


def _integrity_note(response, kind: str = "first", probe: int = 0,
                     downgrades: int = 0, err_out: dict | None = None) -> None:
    """Bounded per-call integrity record for shared extraction paths: counts and
    token usage only — never payload text."""
    # Probe/downgrade counts ride on the answered call's note.
    note = {
        "status": None if response is None else response.get("status"),
        "finish_reason": None if response is None
                         else response.get("finish_reason"),
        "usage": None if response is None else response.get("usage"),
        "timings": None if response is None else response.get("timings"),
        "kind": kind, "probe": probe, "downgrades": downgrades,
    }
    if type((err_out or {}).get("slot_wait_ms")) is int:
        note["slot_wait_ms"] = err_out["slot_wait_ms"]
        note["slot_wait_confirmed"] = err_out.get("slot_wait_confirmed")
    if response is not None and response.get("reused_response") is True:
        note["reused_response"] = True
    _note_list().append(note)


def _integrity_summary(notes: list) -> dict:
    calls = 0
    reused = 0
    usage, timings, repair_timings = {}, {}, {}
    finishes = {}
    repair_calls = probe_runs = downgrades = 0
    slot_wait_ms = slot_wait_unconfirmed = 0
    slot_waits = False
    for note in notes:
        if note.get("reused_response") is True:
            reused += 1
            continue
        calls += 1
        finish = note.get("finish_reason")
        if finish is not None:
            finishes[finish] = finishes.get(finish, 0) + 1
        repair = note.get("kind") == "repair"
        repair_calls += int(repair)
        for key in ("probe", "downgrades"):
            if type(note.get(key)) is int:
                if key == "probe":
                    probe_runs += note[key]
                else:
                    downgrades += note[key]
        if type(note.get("slot_wait_ms")) is int:
            slot_waits = True
            slot_wait_ms += note["slot_wait_ms"]
            slot_wait_unconfirmed += int(note.get("slot_wait_confirmed") == 0)
        for key, value in (note.get("usage") or {}).items():
            if key in ("prompt_tokens", "completion_tokens", "total_tokens") \
                    and type(value) is int and value >= 0:
                usage[key] = usage.get(key, 0) + value
        measured = note.get("timings") or {}
        paired = all(type(measured.get(key)) in (int, float)
                     and 0 <= measured[key] <= sys.float_info.max
                     for key in ("prompt_n", "cache_n"))
        for key, value in measured.items():
            if key in ("prompt_n", "cache_n") and not paired:
                continue
            if key in ("prompt_n", "prompt_ms", "predicted_n", "predicted_ms", "cache_n") \
                    and type(value) in (int, float) and 0 <= value <= sys.float_info.max:
                timings[key] = timings.get(key, 0) + value
                if repair and key in ("prompt_n", "cache_n"):
                    repair_timings[key] = repair_timings.get(key, 0) + value
    timings = {key: value for key, value in timings.items()
               if 0 <= value <= sys.float_info.max}
    repair_timings = {key: value for key, value in repair_timings.items()
                      if 0 <= value <= sys.float_info.max}
    summary = {"calls": calls,
               "length_stops": finishes.get("length", 0),
               "finish_reasons": finishes,
               "usage": usage or None,
               "timings": timings or None,
               # legacy notes without a kind count as first calls, so
               # calls == first_calls + repair_calls always holds
               "first_calls": calls - repair_calls,
               "repair_calls": repair_calls,
               "probe_runs": probe_runs,
               "format_downgrades": downgrades,
               "repair_timings": repair_timings or None}
    if slot_waits:
        summary["slot_wait_ms"] = slot_wait_ms
        summary["slot_wait_unconfirmed"] = slot_wait_unconfirmed
    if reused:
        summary["reused_responses"] = reused
    return summary


def _cloud_origin_for_checkpoint(provider, prompt, answer_text, *, finish_reason="stop"):
    """Capture one new response only after its settled reservation is proven."""
    if not provider.completed_reservations:
        raise ValueError("cloud_repair_evidence_invalid")
    reservation = provider.completed_reservations[-1]
    def read():
        rows = provider.ledger.db.execute(
            "SELECT artifact_id FROM artifacts WHERE kind=? AND project_id=? AND message_id IS ? "
            "AND CASE WHEN json_valid(meta) THEN json_extract(meta,'$.reservation_id')=? END LIMIT 2",
            (cloud_policy.KINDS[0], reservation["project_id"], reservation["message_id"],
             reservation["reservation_id"])).fetchall()
        if len(rows) != 1:
            raise ValueError("cloud_repair_evidence_invalid")
        arguments = {"reservation_artifact_id": rows[0][0], "reservation_id": reservation["reservation_id"],
            "route": reservation["route"], "project_id": reservation["project_id"],
            "message_id": reservation["message_id"], "unit_key": reservation["unit_key"],
            "owner": reservation["owner"], "prompt": prompt, "answer_text": answer_text,
            "contract": provider.contract, "purpose": provider.purpose, "finish_reason": finish_reason}
        evidence = cloud_inference.verify_cloud_repair_evidence(provider.ledger.db, **arguments)
        return {"version": 1, "answer_text": answer_text, "reservation_artifact_id": rows[0][0],
                "reservation_id": reservation["reservation_id"], "contract": provider.contract,
                "owner": reservation["owner"], "unit_key": reservation["unit_key"],
                "witness": evidence["witness"]}
    return provider.dispatch(read, result=True)


def _cloud_mode(ledger, cfg, route, project_id):
    if not cloud_policy.send_route(route):
        # A closed send route can never need provider settings — resolving
        # them here would read provider keys for work that cannot send.
        return "off"
    settings = cloud_inference.resolve_cloud_settings(cfg)
    if not cloud_policy.eligible(settings, route, project_id):
        return "off"
    mode = settings["mode"]
    purpose = "patient_context" if route == "patient_context" else "backlog"
    selection, errors = cloud_policy.resolve_profile(cfg, route=route, purpose=purpose, root=HOME)
    if errors or selection is None:
        return "off"
    # Quota/circuit denial is decided after recovering/checking this input's
    # prior attempt. Turning the route off here could send an unknown unit locally.
    if mode in ("on", "shadow") and not cloud_policy.selected_key(HOME, cfg):
        return "off"
    return mode


def _comparison_fact(source, kind, assertion, evidence=None, *, evidence_span=None):
    """Normalize one validated assertion with its source and grounded quote span."""
    body = source.get("body_text")
    if evidence_span is None:
        span = (mcs_util.locate_quote_span(body, evidence)
                if isinstance(body, str) and isinstance(evidence, str) and evidence else None)
    else:
        if (not isinstance(body, str) or not isinstance(evidence, str) or not evidence
                or not isinstance(evidence_span, (tuple, list)) or len(evidence_span) != 2
                or any(type(n) is not int for n in evidence_span)):
            raise ValueError("cloud_comparison_source_invalid")
        start, end = evidence_span
        if not 0 <= start < end <= len(body) or body[start:end] != evidence:
            raise ValueError("cloud_comparison_source_invalid")
        span = (start, end)
    return json.dumps({"source": [source.get(key) for key in
        ("source", "project_id", "message_id", "content_hash")],
        "kind": kind, "assertion": assertion, "span": span}, sort_keys=True, ensure_ascii=False)


def _comparison_extract(source, document):
    result = set()
    for key in extract_quality._FACT_LIST_FIELDS:
        for item in document.get(key) or []:
            quote = item.get("evidence", item.get("text")) if isinstance(item, dict) else None
            for fact in extract_quality.facts({key: [item]}):
                result.add(_comparison_fact(source, key, fact, quote))
    for fact in extract_quality.facts({key: document.get(key) for key in ("vitals", "reply")}):
        result.add(_comparison_fact(source, fact[0], fact))
    return result


def _cloud_compare(ledger, *, route, project_id, local_facts, cloud_facts=None,
                   rejected=None, held=None, repairs=None, unit=None, pending=False):
    """Record measured comparison units; absent measurements remain unknown.
    A shadow unit with a ``unit`` key is first written ``pending`` with its
    local commit (so it is always in the denominator); its measured result
    later replaces that row in one transaction, so each unit is counted
    exactly once. Without a unit key the measurement is written as it is."""
    left = None if local_facts is None else set(local_facts)
    right = None if cloud_facts is None else set(cloud_facts)
    for value in (rejected, held, repairs):
        if value is not None and (type(value) is not int or value < 0):
            raise ValueError("cloud_comparison_invalid")
    with ledger.db:
        _cloud_compare_tx(ledger, route=route, project_id=project_id, left=left, right=right,
                          rejected=rejected, held=held, repairs=repairs, unit=unit, pending=pending)


def _cloud_compare_tx(ledger, *, route, project_id, left, right, rejected, held, repairs,
                      unit, pending):
    """The comparison write inside the caller's transaction (no commit here)."""
    if unit is not None and pending and ledger.db.execute(
            "SELECT 1 FROM artifacts WHERE kind='cloud_compare_v1' AND project_id=? AND "
            "json_valid(meta) AND json_extract(meta,'$.unit')=?", (project_id, unit)).fetchone():
        return
    if unit is not None and not pending:
        gone = ledger.db.execute(
            "DELETE FROM artifacts WHERE kind='cloud_compare_v1' AND project_id=? AND "
            "json_valid(meta) AND json_extract(meta,'$.unit')=? AND "
            "json_extract(meta,'$.uncompared')='pending'", (project_id, unit)).rowcount
        if not gone:       # already measured (or never committed): never count twice
            return
    ledger.artifact_add_tx("cloud_compare_v1", "{}", project_id=project_id,
        meta={"version": 2, "route": route, "units": 1,
              "local_facts": None if left is None else len(left),
              "cloud_facts": None if right is None else len(right),
              "matched": None if left is None or right is None else len(left & right),
              "validated": None if right is None else len(right), "validated_units": int(right is not None),
              "rejected": rejected, "rejected_units": None if rejected is None else int(rejected > 0),
              "rejection_observations": int(rejected is not None),
              "held": held, "held_observations": int(held is not None),
              "repairs": repairs, "repaired_units": None if repairs is None else int(repairs > 0),
              "repair_observations": int(repairs is not None),
              **({"unit": unit} if unit else {}),
              **({"uncompared": "pending"} if pending else {})})


def _cloud_reextract(ledger, row, *, background=False):
    if row["qc_src"] or row["thin_src"] or row["human_src"] or row["attempts"]:
        return False
    if ledger.db.execute(f"SELECT 1 FROM messages m WHERE m.message_id=? "
            f"AND EXISTS ({_hold_sql()})", (row["message_id"],)).fetchone():
        return False
    if background:
        return (cloud_policy.history_target_ready(ledger.db, row["project_id"], row["message_id"])
                and cloud_policy.history_thread_allowed(ledger.db, row["project_id"], row["message_id"]))
    prior = _prior_extract(ledger.db, row)
    meta = loads_dict(prior["meta"]) if prior else None
    if not meta or type(meta.get("extract_version")) is not int:
        return False
    if meta["extract_version"] < EXTRACT_VERSION or not inference.is_current(meta, target=_INFERENCE_VERSION):
        return True
    return ledger.db.execute(f"SELECT 1 FROM messages m WHERE m.message_id=? "
        f"AND EXISTS ({_hold_sql(released=True)})", (row["message_id"],)).fetchone() is not None


def _llm_call(prompt: str, deadline: float | None = None,
              schema: dict | None = None,
              max_tokens: int = MAX_TOKENS,
              need_s: float | None = None, kind: str = "first", provider=None) -> dict | None:
    """One chat-completions round-trip -> the model's JSON object, or
    None on transport/parse failure. Applies the probed
    response_format; a format rejected mid-run (server restart/model
    swap) degrades ONE rung on the schema->object->plain ladder and
    retries — the global mode follows the degrade so later calls stop
    paying the rejected round-trip, and _probe_format's cooldown
    re-probes upward again.  `schema` overrides the single-message
    contract (the batch path pins _SCHEMA_BATCH).  `need_s` is the
    estimated seconds the call needs to finish — when less than that
    remains before `deadline`, the call is deferred BEFORE firing: a
    request killed mid-decode by the deadline wastes the whole
    generation, while a deferred row just stays pending.  Response
    integrity metadata is appended to thread-local integrity notes
    consumed by ``llm_extract``; the visible result is unchanged.
    `kind` labels the note ("first"/"repair"); the note also carries
    whether this call completed a format-check run and how many format
    downgrades preceded the answer. Known limit: a probe or downgrade
    whose call then returns None/_DEFERRED without a note is uncounted
    (the row re-runs)."""
    global _FMT_MODE
    if provider is not None:
        if deadline is not None and deadline - time.monotonic() < (need_s or 0):
            _CALL_NOTES.defer_reason = "deadline"
            return _DEFERRED
        response = provider(prompt, deadline=deadline, max_tokens=max_tokens,
                            need_s=need_s, kind=kind)
        _integrity_note(response, kind)
        if isinstance(provider, cloud_inference.CloudInferenceUnit) and isinstance(response, dict) and isinstance(response.get("text"), str):
            _CALL_NOTES.cloud_response = (provider, prompt, response["text"], response.get("finish_reason"))
        acceptance = local_llm.acceptance_error(response)
        if acceptance == "length_stop":
            return None
        if acceptance is not None:
            raise cloud_inference.CloudFallback("cloud_incomplete_response")
        answer = json_object(response.get("text"))
        if answer is None:
            raise cloud_inference.CloudFallback("cloud_invalid_response")
        if isinstance(provider, cloud_inference.CloudInferenceUnit):
            selection = getattr(provider, "selection", None)
            _CALL_NOTES.provider_response = (selection.get("provider") if selection is not None else
                "openai" if provider.model == openai_client.MODEL else
                "anthropic" if provider.model == anthropic_client.MODEL else None)
        return answer
    if deadline is not None \
            and deadline - time.monotonic() < (need_s or 0):
        _CALL_NOTES.defer_reason = "deadline"
        return _DEFERRED
    probed_at = _FMT_TS
    fmt = _probe_format(deadline=deadline)
    probe = int(_FMT_TS != probed_at)   # one format-check run may send multiple requests
    if fmt is None:
        _CALL_NOTES.defer_reason = ("currentness_unverified" if getattr(
            _CALL_NOTES, "medication_source_changed", False) else "probe_unobserved")
        return _DEFERRED   # no idle slot to probe on — nothing sent
    endpoint, model = _resolved_llm()
    downgrades = 0
    while True:
        # the same floor gates retries: a format degrade (4xx) loops
        # back here and would otherwise re-fire a full call that can
        # no longer finish before the deadline
        if deadline is not None \
                and deadline - time.monotonic() < (need_s or 0):
            _CALL_NOTES.defer_reason = "deadline"
            return _DEFERRED
        rf = None
        if fmt == "schema":
            rf = {"type": "json_schema",
                  "json_schema": schema or extract_schema.SCHEMA}
        elif fmt == "object":
            rf = {"type": "json_object"}
        err_out: dict = {}
        if local_llm.admission_enabled():
            # T20: every backend request holds a class permit through
            # confirmed terminal retirement — a held/deferred verdict
            # parks the message, it never becomes a silent failure
            response = local_llm.admitted_chat(
                "mcs.extract", prompt, endpoint=endpoint, model=model,
                max_tokens=max_tokens, timeout=TIMEOUT,
                deadline=deadline, response_format=rf,
                request_fn=_opener_request, error_out=err_out,
                turn_min_s=need_s or 0)
            if response is not None and response.get("admission"):
                _CALL_NOTES.defer_reason = "admission"
                return _DEFERRED
        else:
            response = local_llm.chat(
                prompt, endpoint=endpoint, model=model,
                max_tokens=max_tokens, timeout=TIMEOUT, deadline=deadline,
                response_format=rf,
                extra_payload={"id_slot": _choose_slot()},
                request_fn=_opener_request, error_out=err_out,
                turn_min_s=need_s or 0)
        if getattr(_CALL_NOTES, "medication_source_changed", False):
            _CALL_NOTES.transport = None
            _CALL_NOTES.defer_reason = "currentness_unverified"
            return _DEFERRED
        # Endpoint health of this call for run_pending's breaker: refused /
        # not sent and 503 are outages, a pre-deadline transport failure
        # (timeout, reset) is a slow or stuck server, a 2xx answer (or a 200
        # whose body is unusable) proves the endpoint alive;
        # budget expiry says nothing about the endpoint.
        expired = deadline is not None and time.monotonic() >= deadline
        status = response.get("status") if response else None
        _CALL_NOTES.transport = (
            "unreachable" if response is None
            and err_out.get("kind") in ("unreachable", "not_sent")
            else "busy" if response is None and err_out.get("kind") == "busy"
            else "unavailable" if status == 503
            else "transport" if response is None and not expired
            and err_out.get("kind") == "transport"
            else "expired" if response is None and expired
            and err_out.get("kind") == "transport"
            else "answered" if type(status) is int and 200 <= status < 300
            or response is None and err_out.get("kind") == "protocol"
            else None)
        _CALL_NOTES.transport_at = time.monotonic()
        if _CALL_NOTES.transport == "expired":
            _integrity_note(response, kind, probe, downgrades, err_out)
            return None  # a sent timeout consumes an attempt, not endpoint health
        if response is None and (err_out.get("kind") in ("unreachable", "busy", "not_sent")
                                 or expired) \
                or response is not None and response.get("status") == 503:
            # Connection refused or 503 (still loading the model) — the
            # server is down/starting, so the call did no work and
            # consumed nothing. Defer like a deadline expiry instead of
            # burning a retry attempt on every selected message (a llama
            # restart or reboot used to stampede attempts on whole batches).
            _CALL_NOTES.defer_reason = ("busy" if err_out.get("kind") == "busy" else
                "not_sent" if err_out.get("kind") == "not_sent" else
                "endpoint_down" if err_out.get("kind") == "unreachable" or status == 503 else
                "deadline")
            return _DEFERRED
        # 4xx rejects THE REQUEST — walk down one rung and retry; the
        # ladder ends at "plain" (no format to blame), and a plain-mode
        # rejection is the message's own failure.
        if status in (400, 404, 422) and fmt != "plain":
            fmt = _FMT_MODE = _NEXT_FMT[fmt]
            downgrades += 1
            continue
        _integrity_note(response, kind, probe, downgrades, err_out)
        # a length stop is truncated JSON: json_object would salvage an
        # inner complete object (one med dict) that validates to {} and
        # gets saved as the message's extraction — reject it as failed
        if local_llm.acceptance_error(response) is not None:
            return None
        text = response.get("text")
        if not isinstance(text, str):
            return None
        answer = json_object(text)
        if isinstance(answer, dict):
            _CALL_NOTES.provider_response = "local"
        return answer


def _slots_busy(deadline: float | None) -> dict | None:
    """/slots -> {id: is_processing}, or None when the probe fails."""
    try:
        status, _headers, raw = _opener_request(
            local_llm.probe_urls(_resolved_llm()[0])[1], "GET", None, 2, deadline)
        if status != 200 or len(raw) > bounded_http.MAX_RESPONSE_BYTES:
            return None
        return {s.get("id"): bool(s.get("is_processing"))
                for s in json.loads(raw.decode("utf-8"))}
    except Exception:
        return None


def pinned_slot_busy(deadline: float | None = None) -> bool:
    """Point sample for avoiding a call queued behind a busy fixed slot."""
    if local_llm.admission_enabled() or local_llm.scheduling_enabled():
        return False
    busy = _slots_busy(deadline)
    return bool(busy) and busy.get(_choose_slot()) is True


def _actual_model(r) -> str:
    """Return the model that served this row: its pinned target, else the default."""
    return r["_target"][1] if "_target" in r.keys() else MODEL
