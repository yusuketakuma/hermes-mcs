"""Synthetic extract llm transport contracts with isolated shared fixtures."""
import extract_llm_engine
import extract_llm_errors
import extract_llm_provider
import extract_llm_runner
import extract_schema
import json
import time
import pytest
import extract_llm
from extract_testkit import _ledger, _message
from extract_llm_testkit import _Opener as _Opener, _http_error as _http_error, _request_from_opener as _request_from_opener


def test_probe_prefers_schema_then_caches(monkeypatch):
    monkeypatch.setattr(extract_llm, "_FMT_MODE", None)
    monkeypatch.setattr(extract_llm_provider, '_FMT_MODE', extract_llm._FMT_MODE)
    op = _Opener({"schema": "ok"})
    monkeypatch.setattr(extract_llm, "_opener_request", _request_from_opener(op))
    monkeypatch.setattr(extract_llm_errors, '_opener_request', extract_llm._opener_request)
    monkeypatch.setattr(extract_llm_provider, '_opener_request', extract_llm._opener_request)
    assert extract_llm._probe_format() == "schema"
    assert extract_llm._probe_format() == "schema"
    assert len(op.calls) == 1          # probed once per process


def test_probe_falls_back_schema_to_object(monkeypatch):
    monkeypatch.setattr(extract_llm, "_FMT_MODE", None)
    monkeypatch.setattr(extract_llm_provider, '_FMT_MODE', extract_llm._FMT_MODE)
    op = _Opener({"schema": 400, "object": "ok"})
    monkeypatch.setattr(extract_llm, "_opener_request", _request_from_opener(op))
    monkeypatch.setattr(extract_llm_errors, '_opener_request', extract_llm._opener_request)
    monkeypatch.setattr(extract_llm_provider, '_opener_request', extract_llm._opener_request)
    assert extract_llm._probe_format() == "object"


def test_probe_all_rejected_means_plain(monkeypatch):
    monkeypatch.setattr(extract_llm, "_FMT_MODE", None)
    monkeypatch.setattr(extract_llm_provider, '_FMT_MODE', extract_llm._FMT_MODE)
    op = _Opener({"schema": 422, "object": 400})
    monkeypatch.setattr(extract_llm, "_opener_request", _request_from_opener(op))
    monkeypatch.setattr(extract_llm_errors, '_opener_request', extract_llm._opener_request)
    monkeypatch.setattr(extract_llm_provider, '_opener_request', extract_llm._opener_request)
    assert extract_llm._probe_format() == "plain"


def test_probe_endpoint_down_stays_plain_no_raise(monkeypatch):
    monkeypatch.setattr(extract_llm, "_FMT_MODE", None)
    monkeypatch.setattr(extract_llm_provider, '_FMT_MODE', extract_llm._FMT_MODE)

    class Down:
        def open(self, req, timeout=None):
            raise OSError("conn refused")

    monkeypatch.setattr(extract_llm, "_opener_request", _request_from_opener(Down()))
    monkeypatch.setattr(extract_llm_errors, '_opener_request', extract_llm._opener_request)
    monkeypatch.setattr(extract_llm_provider, '_opener_request', extract_llm._opener_request)
    assert extract_llm._probe_format() == "plain"


def test_extract_sends_response_format_when_supported(monkeypatch):
    monkeypatch.setattr(extract_llm, "_FMT_MODE", None)
    monkeypatch.setattr(extract_llm_provider, '_FMT_MODE', extract_llm._FMT_MODE)
    op = _Opener({"schema": "ok"})
    monkeypatch.setattr(extract_llm, "_opener_request", _request_from_opener(op))
    monkeypatch.setattr(extract_llm_errors, '_opener_request', extract_llm._opener_request)
    monkeypatch.setattr(extract_llm_provider, '_opener_request', extract_llm._opener_request)
    extract_llm.llm_extract("本文")
    extract_req = op.calls[-1]
    assert extract_req["response_format"]["type"] == "json_schema"
    assert extract_req["response_format"]["json_schema"] \
        == extract_schema.SCHEMA


def test_extract_degrades_mid_run_on_format_reject(monkeypatch):
    """Probe says schema OK but the real call gets 400 (server swap) —
    degrade to plain once and retry, don't fail the message."""
    monkeypatch.setattr(extract_llm, "_FMT_MODE", None)
    monkeypatch.setattr(extract_llm_provider, '_FMT_MODE', extract_llm._FMT_MODE)
    state = {"degraded": False}

    class Swap:
        def __init__(self):
            self.calls = []

        def open(self, req, timeout=None):
            import io
            data = json.loads(req.data.decode())
            self.calls.append(data)
            if data.get("response_format", {}).get("type") \
                    == "json_schema" and len(self.calls) > 1 \
                    and not state["degraded"]:
                state["degraded"] = True
                raise _http_error(400)
            return io.BytesIO(json.dumps(
                {"choices": [{"message":
                              {"content": '{"symptoms":[],"summary":"ok"}'}, "finish_reason": "stop"}]}
            ).encode())

    op = Swap()
    monkeypatch.setattr(extract_llm, "_opener_request", _request_from_opener(op))
    monkeypatch.setattr(extract_llm_errors, '_opener_request', extract_llm._opener_request)
    monkeypatch.setattr(extract_llm_provider, '_opener_request', extract_llm._opener_request)
    out = extract_llm.llm_extract("本文")
    assert out == {"symptoms": [], "summary": "ok"}
    # degrade is ONE rung on the ladder — schema -> object, not
    # straight to plain
    assert extract_llm_provider._FMT_MODE == "object"
    # probe(schema) + rejected schema call + retried object call
    assert len(op.calls) == 3


def test_chunked_full_coverage_and_merge(monkeypatch):
    """A >3000-char body is covered in full; outputs merge
    deterministically (progression-preserving dedup, last-vitals,
    any-high urgency, no partial summary)."""
    # vitals need their labelled numbers in the body — the vitals guard
    # anchors each key to the label nearest its value (脈は80 -> hr)
    part_a = "あ" * 2500 + "脈は80"
    part_b = "い" * 600 + "脈は90 SpO2は95" + "い" * 580
    body = part_a + "\n" + part_b
    seen_bodies = []

    def fake_call(prompt, **_ignored):
        # rsplit — the few-shot examples also contain a 対象本文: block;
        # the REAL target carries the posted timestamp label (F09)
        tgt = prompt.rsplit(
            "対象本文(投稿日時: 不明):\n<<<\n", 1)[1].rsplit(
            "\n>>>\nJSON:", 1)[0]
        seen_bodies.append(tgt)
        if tgt.startswith("あ"):
            return {"meds": [{"name": "薬A", "action": "stop"}],
                    "symptoms": [{"text": "発熱", "status": "new"}],
                    "vitals": {"hr": 80}, "urgency": "routine",
                    "summary": "前半の要約"}
        return {"meds": [{"name": "薬A", "action": "start"},
                         {"name": "薬B"}],
                "symptoms": [{"text": "発熱", "status": "resolved"}],
                "vitals": {"hr": 90, "spo2": 95}, "urgency": "high",
                "urgency_evidence": ["脈は90"],
                "summary": "後半の要約"}

    monkeypatch.setattr(extract_llm, "_llm_call", fake_call)
    monkeypatch.setattr(extract_llm_engine, '_llm_call', extract_llm._llm_call)
    monkeypatch.setattr(extract_llm_runner, '_llm_call', extract_llm._llm_call)
    out = extract_llm.llm_extract(body)
    assert "".join(seen_bodies) == body      # full coverage, no drop
    # (name, subject, action) key — a stop AND a later start coexist
    assert [(m["name"], m["action"]) for m in out["meds"]] == [
        ("薬A", "stop"), ("薬A", "start"), ("薬B", None)]
    # Both reported states survive; source order alone must not erase history.
    assert out["symptoms"] == [
        {"text": "発熱", "negated": False, "status": "new", "unverified": True},
        {"text": "発熱", "negated": False, "status": "resolved", "unverified": True}]
    assert out["vitals"] == {"hr": 90.0, "spo2": 95.0}
    assert out["urgency"] == "high"
    # a part-summary must not masquerade as the whole message's gist
    assert "summary" not in out
    assert out["_chunks_total"] == 2


def test_chunk_failure_fails_message_not_partial(monkeypatch):
    """Any failed chunk fails the WHOLE message (None -> error path) —
    a partial artifact would gate re-extraction while looking complete."""
    body = "あ" * 2500 + "\n" + "い" * 1200
    calls = {"n": 0}

    def fake_call(prompt, **_ignored):
        calls["n"] += 1
        return {"symptoms": [], "summary": "ok"} if calls["n"] == 1 else None

    monkeypatch.setattr(extract_llm, "_llm_call", fake_call)
    monkeypatch.setattr(extract_llm_engine, '_llm_call', extract_llm._llm_call)
    monkeypatch.setattr(extract_llm_runner, '_llm_call', extract_llm._llm_call)
    assert extract_llm.llm_extract(body) is None
    assert calls["n"] == 2

    def all_fail(prompt, **_ignored):
        return None

    monkeypatch.setattr(extract_llm, "_llm_call", all_fail)
    monkeypatch.setattr(extract_llm_engine, '_llm_call', extract_llm._llm_call)
    monkeypatch.setattr(extract_llm_runner, '_llm_call', extract_llm._llm_call)
    assert extract_llm.llm_extract(body) is None


def test_chunk_deadline_defers_without_error(tmp_path, monkeypatch):
    """A deadline mid-chunk returns _DEFERRED — run_pending leaves the
    message pending WITHOUT writing an error artifact (budget
    exhaustion is not a retryable failure)."""
    monkeypatch.setattr(extract_llm, "_llm_call",
                        lambda prompt, **_ignored: {"symptoms": [], "summary": "ok"})
    monkeypatch.setattr(extract_llm_engine, '_llm_call', extract_llm._llm_call)
    monkeypatch.setattr(extract_llm_runner, '_llm_call', extract_llm._llm_call)
    body = "あ" * 2500 + "\n" + "い" * 1200
    # deadline between chunk 1 and chunk 2
    assert extract_llm.llm_extract(
        body, deadline=time.monotonic()) is extract_llm._DEFERRED

    db = _ledger(tmp_path)
    db.save_messages([_message(body=body)])
    monkeypatch.setattr(
        extract_llm, "llm_extract",
        lambda b, **_: extract_llm._DEFERRED)
    monkeypatch.setattr(extract_llm_runner, 'llm_extract', extract_llm.llm_extract)
    res = extract_llm.run_pending(db, limit=10, budget_s=30)
    assert res["done"] == 0 and res["failed"] == 0
    assert db.artifacts("extract_llm") == []   # no error row written
    db.close()


def test_parallel_workers_process_all_selected(tmp_path, monkeypatch):
    """workers>1 fans out llm_extract across threads; every selected
    message still lands exactly one current v2 artifact."""
    db = _ledger(tmp_path)
    db.save_messages([_message(mid=i, body=f"本文{i}") for i in range(1, 6)])
    monkeypatch.setattr(extract_llm, "_probe_format", lambda **kw: "plain")
    monkeypatch.setattr(extract_llm_provider, '_probe_format', extract_llm._probe_format)
    monkeypatch.setattr(extract_llm_runner, '_probe_format', extract_llm._probe_format)
    monkeypatch.setattr(extract_llm, "llm_extract",
                        lambda body, **_: {"summary": f"s:{body}"})
    monkeypatch.setattr(extract_llm_runner, 'llm_extract', extract_llm.llm_extract)
    res = extract_llm.run_pending(db, limit=10, budget_s=30, workers=3)
    assert res["done"] == 5 and res["failed"] == 0 and res["left"] == 0
    arts = db.artifacts("extract_llm")
    assert len(arts) == 5
    assert all(json.loads(a["meta"])["extract_version"] == extract_llm.EXTRACT_VERSION
               for a in arts)
    db.close()


def test_parallel_defer_reasons_are_per_worker_and_count_crashes(tmp_path, monkeypatch, capsys):
    import threading
    db = _ledger(tmp_path)
    db.save_messages([_message(mid=i, body=f"本文{i}") for i in range(1, 4)])
    barrier = threading.Barrier(3, timeout=10)
    monkeypatch.setattr(extract_llm, "_probe_format", lambda **kw: "plain")
    monkeypatch.setattr(extract_llm_provider, '_probe_format', extract_llm._probe_format)
    monkeypatch.setattr(extract_llm_runner, '_probe_format', extract_llm._probe_format)

    def deferred(body, **kwargs):
        extract_llm._CALL_NOTES.defer_reason = {"本文1": "busy", "本文2": "admission"}.get(body)
        barrier.wait()
        if body == "本文3":
            raise RuntimeError("synthetic")
        return extract_llm._DEFERRED

    monkeypatch.setattr(extract_llm, "llm_extract", deferred)
    monkeypatch.setattr(extract_llm_runner, 'llm_extract', extract_llm.llm_extract)
    result = extract_llm.run_pending(db, limit=10, budget_s=30, workers=3)
    assert result["deferred"] == 3
    assert result["defer_reasons"] == {"busy": 1, "admission": 1, "worker_error": 1}
    events = [json.loads(line) for line in capsys.readouterr().err.splitlines()]
    failure = next(event for event in events if event.get("event") == "extract_worker_error")
    assert type(failure["ts"]) is float and type(failure["pid"]) is int
    assert failure["error"] == "RuntimeError" and "synthetic" not in json.dumps(failure)
    assert db.artifacts("extract_llm") == []
    db.close()


@pytest.mark.parametrize("kind,status,reason", [
    ("busy", None, "busy"), ("not_sent", None, "not_sent"),
    ("unreachable", None, "endpoint_down"), (None, 503, "endpoint_down"),
])
def test_local_defer_reason_matches_observed_transport(monkeypatch, kind, status, reason):
    monkeypatch.setattr(extract_llm, "_probe_format", lambda **kw: "plain")
    monkeypatch.setattr(extract_llm_provider, '_probe_format', extract_llm._probe_format)
    monkeypatch.setattr(extract_llm_runner, '_probe_format', extract_llm._probe_format)
    monkeypatch.setattr(extract_llm.local_llm, "admission_enabled", lambda: False)

    def call(*args, **kwargs):
        if kind:
            kwargs["error_out"]["kind"] = kind
        return {"status": status} if status else None

    monkeypatch.setattr(extract_llm.local_llm, "chat", call)
    extract_llm._CALL_NOTES.defer_reason = None
    assert extract_llm._llm_call("synthetic") is extract_llm._DEFERRED
    assert extract_llm._CALL_NOTES.defer_reason == reason


def test_parallel_workers_keep_fail_count_and_backoff(tmp_path,
                                                      monkeypatch):
    """A None result in the parallel path still writes the error row —
    the serial commit loop is unchanged."""
    db = _ledger(tmp_path)
    db.save_messages([_message(mid=i, body=f"本文{i}") for i in range(1, 4)])
    monkeypatch.setattr(extract_llm, "_probe_format", lambda **kw: "plain")
    monkeypatch.setattr(extract_llm_provider, '_probe_format', extract_llm._probe_format)
    monkeypatch.setattr(extract_llm_runner, '_probe_format', extract_llm._probe_format)
    monkeypatch.setattr(extract_llm, "_llm_up", lambda **kw: True)
    monkeypatch.setattr(extract_llm_runner, '_llm_up', extract_llm._llm_up)
    monkeypatch.setattr(
        extract_llm, "llm_extract",
        lambda body, **_: None if body == "本文2" else {"summary": "ok"})
    monkeypatch.setattr(extract_llm_runner, 'llm_extract', extract_llm.llm_extract)
    res = extract_llm.run_pending(db, limit=10, budget_s=30, workers=3)
    assert res["done"] == 2 and res["failed"] == 1
    err = db.db.execute(
        "SELECT meta FROM artifacts WHERE kind='extract_llm' "
        "AND message_id=2").fetchone()
    assert json.loads(err["meta"])["error"] is True
    db.close()


def test_parallel_endpoint_down_keeps_later_successes(tmp_path,
                                                     monkeypatch):
    """Endpoint-down must not discard already-computed parallel results.
    pool.map materializes every result before the commit loop, so a
    None followed by successes must still persist the successes; only
    the error-row writes are skipped (the message stays pending instead
    of burning an attempt on an outage)."""
    db = _ledger(tmp_path)
    # the failing message sorts first (posted_at_ts DESC), so a `break`
    # would drop the two already-computed successes behind it
    db.save_messages([
        _message(mid=1, body="本文1", posted_at="2026-09-20T00:00:00+09:00"),
        _message(mid=2, body="本文2"),
        _message(mid=3, body="本文3")])
    monkeypatch.setattr(extract_llm, "_probe_format", lambda **kw: "plain")
    monkeypatch.setattr(extract_llm_provider, '_probe_format', extract_llm._probe_format)
    monkeypatch.setattr(extract_llm_runner, '_probe_format', extract_llm._probe_format)
    monkeypatch.setattr(extract_llm, "_llm_up", lambda **kw: False)
    monkeypatch.setattr(extract_llm_runner, '_llm_up', extract_llm._llm_up)
    monkeypatch.setattr(
        extract_llm, "llm_extract",
        lambda body, **_: None if body == "本文1" else {"summary": "ok"})
    monkeypatch.setattr(extract_llm_runner, 'llm_extract', extract_llm.llm_extract)
    res = extract_llm.run_pending(db, limit=10, budget_s=30, workers=3)
    assert res["done"] == 2 and res["failed"] == 1
    arts = {a["message_id"] for a in db.artifacts("extract_llm")}
    assert arts == {2, 3}
    db.close()


def test_unreachable_endpoint_defers_not_fails(tmp_path, monkeypatch):
    """Connection-refused (server down/starting) defers the message like
    a deadline expiry — no error row, no attempt burned, and the batch
    reports `deferred` so the drainer can back off. Distinct from a
    completed-but-bad response, which still fails normally."""
    db = _ledger(tmp_path)
    db.save_messages([_message(mid=1, body="本文1")])
    monkeypatch.setattr(extract_llm, "_probe_format", lambda **kw: "plain")
    monkeypatch.setattr(extract_llm_provider, '_probe_format', extract_llm._probe_format)
    monkeypatch.setattr(extract_llm_runner, '_probe_format', extract_llm._probe_format)

    def _down(prompt, **kw):
        kw["error_out"]["kind"] = "unreachable"
        return
    monkeypatch.setattr(extract_llm.local_llm, "chat", _down)
    assert extract_llm.llm_extract("本文1") is extract_llm._DEFERRED

    res = extract_llm.run_pending(db, limit=10, budget_s=30)
    assert res["done"] == 0 and res["failed"] == 0
    assert res["deferred"] == 1
    assert db.artifacts("extract_llm") == []
    db.close()


def test_loading_server_defers_not_fails(tmp_path, monkeypatch):
    """A 503 (llama-server still loading after its daily restart) did no
    work either — defer like a refused connection."""
    db = _ledger(tmp_path)
    db.save_messages([_message(mid=1, body="本文1")])
    monkeypatch.setattr(extract_llm, "_probe_format", lambda **kw: "plain")
    monkeypatch.setattr(extract_llm_provider, '_probe_format', extract_llm._probe_format)
    monkeypatch.setattr(extract_llm_runner, '_probe_format', extract_llm._probe_format)
    monkeypatch.setattr(extract_llm, "_llm_up", lambda **kw: True)
    monkeypatch.setattr(extract_llm_runner, '_llm_up', extract_llm._llm_up)
    monkeypatch.setattr(extract_llm.local_llm, "chat", lambda prompt, **kw: {
        "text": None, "finish_reason": None, "usage": None, "status": 503})
    res = extract_llm.run_pending(db, limit=10, budget_s=120)
    assert res["done"] == 0 and res["failed"] == 0
    assert res["deferred"] == 1
    db.close()


def test_transport_error_still_fails(tmp_path, monkeypatch):
    """A non-refused transport failure (e.g. a timeout that consumed
    real server work) is NOT deferred — it stays on the normal failure
    path so attempts still bound a systematically stuck message."""
    db = _ledger(tmp_path)
    db.save_messages([_message(mid=1, body="本文1")])
    monkeypatch.setattr(extract_llm, "_probe_format", lambda **kw: "plain")
    monkeypatch.setattr(extract_llm_provider, '_probe_format', extract_llm._probe_format)
    monkeypatch.setattr(extract_llm_runner, '_probe_format', extract_llm._probe_format)
    monkeypatch.setattr(extract_llm, "_llm_up", lambda **kw: True)
    monkeypatch.setattr(extract_llm_runner, '_llm_up', extract_llm._llm_up)

    def _timeout(prompt, **kw):
        kw["error_out"]["kind"] = "transport"
        return
    monkeypatch.setattr(extract_llm.local_llm, "chat", _timeout)
    # budget above the doomed-call floor so the call actually fires
    res = extract_llm.run_pending(db, limit=10, budget_s=120)
    assert res["done"] == 0 and res["failed"] == 1
    assert res["deferred"] == 0
    db.close()


def test_chunk_checkpoint_resumes_without_reinference(tmp_path, monkeypatch):
    """F14: a message interrupted mid-chunk resumes from its durable
    checkpoint — completed chunks are never re-inferred."""
    db = _ledger(tmp_path)
    db.save_messages([_message(body="あ" * 2500 + "\n" + "い" * 1200)])
    calls = []

    def fake_call(prompt, **_):
        calls.append(1)
        return {"symptoms": [], "summary": "x"} if len(calls) == 1 \
            else extract_llm._DEFERRED

    monkeypatch.setattr(extract_llm, "_llm_call", fake_call)
    monkeypatch.setattr(extract_llm_engine, '_llm_call', extract_llm._llm_call)
    monkeypatch.setattr(extract_llm_runner, '_llm_call', extract_llm._llm_call)
    r = extract_llm.run_pending(db, limit=5, budget_s=60)
    assert r["deferred"] == 1 and r["done"] == 0
    assert db.db.execute(
        "SELECT count(*) FROM artifacts WHERE kind='extract_llm_chunk'"
    ).fetchone()[0] == 1

    calls.clear()
    monkeypatch.setattr(
        extract_llm, "_llm_call",
        lambda p, **_: calls.append(1) or {"symptoms": [], "summary": "y"})
    monkeypatch.setattr(extract_llm_engine, '_llm_call', extract_llm._llm_call)
    monkeypatch.setattr(extract_llm_runner, '_llm_call', extract_llm._llm_call)
    r = extract_llm.run_pending(db, limit=5, budget_s=60)
    assert r["done"] == 1
    assert len(calls) == 1
    assert db.db.execute(
        "SELECT count(*) FROM artifacts WHERE kind='extract_llm_chunk'"
    ).fetchone()[0] == 0
    assert db.db.execute(
        "SELECT count(*) FROM fetch_jobs WHERE kind='extract_claim'"
    ).fetchone()[0] == 0
    db.close()


def test_doomed_call_defers_before_firing(monkeypatch):
    """A call whose remaining budget is under need_s never reaches the
    transport — decode killed by the deadline would be pure waste; the
    row simply stays pending."""
    calls = []
    monkeypatch.setattr(extract_llm.local_llm, "chat",
                        lambda *a, **kw: calls.append(1) or {"text": "{}"})
    # "schema" short-circuits the capability probe entirely
    monkeypatch.setattr(extract_llm, "_FMT_MODE", "schema")
    monkeypatch.setattr(extract_llm_provider, '_FMT_MODE', extract_llm._FMT_MODE)
    d = extract_llm._llm_call("prompt",
                              deadline=time.monotonic() + 30,
                              need_s=90)
    assert d is extract_llm._DEFERRED and not calls
    # above the floor the call fires normally
    monkeypatch.setattr(
        extract_llm.local_llm, "chat",
        lambda *a, **kw: calls.append(1)
        or {"text": '{"summary":"x"}', "finish_reason": "stop",
            "usage": None, "timings": None, "status": 200})
    d = extract_llm._llm_call("prompt",
                              deadline=time.monotonic() + 95,
                              need_s=90)
    assert isinstance(d, dict) and len(calls) == 1


def test_llm_extract_short_budget_defers_without_call(tmp_path, monkeypatch):
    """run_pending leaves the row pending when the budget cannot fit
    even one extraction call — no doomed generation is started."""
    db = _ledger(tmp_path)
    db.ensure_patient(1)
    db.save_messages([_message(mid=1)])
    calls = []
    monkeypatch.setattr(extract_llm.local_llm, "chat",
                        lambda *a, **kw: calls.append(1) or {"text": "{}"})
    monkeypatch.setattr(extract_llm, "_FMT_MODE", "schema")
    monkeypatch.setattr(extract_llm_provider, '_FMT_MODE', extract_llm._FMT_MODE)
    res = extract_llm.run_pending(db, limit=5, budget_s=10)
    assert res["deferred"] == 1 and res["done"] == 0 and not calls
    # and the row is still extractable — nothing was marked failed
    monkeypatch.setattr(extract_llm, "llm_extract",
                        lambda body, **_: {"summary": "s"})
    monkeypatch.setattr(extract_llm_runner, 'llm_extract', extract_llm.llm_extract)
    res = extract_llm.run_pending(db, limit=5, budget_s=200)
    assert res["done"] == 1
    db.close()


def test_degrade_retry_respects_need_floor(monkeypatch):
    """A format-degrade retry is still a full generation — the loop
    gate applies need_s so a 4xx cannot re-fire a call that can no
    longer finish before the deadline."""
    now = [10_000.0]
    monkeypatch.setattr(extract_llm.time, "monotonic", lambda: now[0])
    calls = []

    def _chat(*a, **kw):
        calls.append(1)
        now[0] += 10                       # the round-trip ate 10s
        return {"status": 400, "text": "", "usage": None,
                "timings": None}
    monkeypatch.setattr(extract_llm.local_llm, "chat", _chat)
    monkeypatch.setattr(extract_llm, "_FMT_MODE", "schema")
    monkeypatch.setattr(extract_llm_provider, '_FMT_MODE', extract_llm._FMT_MODE)
    d = extract_llm._llm_call("p", deadline=now[0] + 95, need_s=90)
    # entry gate passes (95 >= 90) -> 400 -> degrade -> loop gate:
    # 95-10 = 85 < 90 -> deferred after ONE call, not a ladder burn
    assert d is extract_llm._DEFERRED and len(calls) == 1
