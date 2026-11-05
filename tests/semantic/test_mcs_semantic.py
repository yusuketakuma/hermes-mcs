"""Synthetic mcs semantic contracts with isolated shared fixtures."""
import semantic_notice
import mcs_util
import semantic_drain
import semantic_extraction
import semantic_jev
import semantic_policy
import semantic_store
import datetime
import json
import time
import urllib.error
import os
import subprocess
import sys
import pytest
import semantic_jev as jev
from semantic_testkit import BODY, _FakeJev, _cfg as _cfg, _ledger, _llm, _message, _patient, _seeded
from mcs_semantic_testkit import ROOT as ROOT


# ---------- Jev client contract ----------

def test_registry_scope_is_explicit():
    assert len(jev.PROPOSITIONS) == 12
    for pid, p in jev.PROPOSITIONS.items():
        assert pid.startswith("P") and p["label_ja"]
        # instruction names its target — a renamed key can never
        # silently re-scope the evaluation (AT-011)
        assert "state.target" in p["instructions"]
        assert p["true"] and p["false"]


def test_noul_without_confidence_ok():
    q = {"q1": jev.noul_question("i", "t", "f")}
    out = jev.validate_answers(
        {"usage": {"input_tokens": 1, "output_tokens": 1}, "model": jev.JEV_MODEL, "answers": {"q1": {"type": "noul", "noul": 0.42}}}, q,
        jev.JEV_MODEL)
    assert out["answers"]["q1"]["noul"] == 0.42


def test_noul_type_confusion_rejected():
    q = {"q1": jev.noul_question("i", "t", "f")}
    for bad in ({"noul": True}, {"noul": "0.9"}, {"noul": None},
                {"noul": float("nan")}, {"noul": 1.5}, {}, "x"):
        if isinstance(bad, dict):
            bad = {"type": "noul", **bad}
        with pytest.raises(jev.JevError) as ei:
            jev.validate_answers({"usage": {"input_tokens": 1, "output_tokens": 1}, "model": jev.JEV_MODEL,
                                  "answers": {"q1": bad}}, q,
                                 jev.JEV_MODEL)
        assert ei.value.kind == "protocol_error"


def test_choice_validation():
    q = {"q1": jev.choice_question("i", {"a": "A", "b": "B"})}
    good = {"q1": {"type": "choice", "choice": "a", "confidence": 0.7,
                   "probabilities": {"a": 0.7, "b": 0.3}}}
    out = jev.validate_answers({"usage": {"input_tokens": 1, "output_tokens": 1}, "model": jev.JEV_MODEL,
                                "answers": good}, q, jev.JEV_MODEL)
    assert out["answers"]["q1"]["choice"] == "a"
    for bad in ({"choice": "z", "confidence": 0.7,
                 "probabilities": {"a": 0.7, "b": 0.3}},
                {"choice": "a", "confidence": 2.0,
                 "probabilities": {"a": 0.7, "b": 0.3}},
                {"choice": "a", "confidence": 0.7,
                 "probabilities": {"a": 0.7, "b": 0.7}},
                {"choice": "a", "confidence": 0.7},
                {"choice": "a"},
                ):
        bad = {"type": "choice", **bad}
        with pytest.raises(jev.JevError):
            jev.validate_answers({"usage": {"input_tokens": 1, "output_tokens": 1}, "model": jev.JEV_MODEL,
                                  "answers": {"q1": bad}}, q,
                                 jev.JEV_MODEL)


def test_answer_set_mismatch_rejected():
    q = {"a": jev.noul_question("i", "t", "f"),
         "b": jev.noul_question("i", "t", "f")}
    for bad in ({"a": {"noul": 0.5}},
                {"a": {"noul": 0.5}, "b": {"noul": 0.5},
                 "c": {"noul": 0.1}}):
        with pytest.raises(jev.JevError, match="protocol_error"):
            jev.validate_answers({"usage": {"input_tokens": 1, "output_tokens": 1}, "model": jev.JEV_MODEL,
                                  "answers": bad}, q, jev.JEV_MODEL)


def test_model_mismatch_rejected():
    q = {"a": jev.noul_question("i", "t", "f")}
    with pytest.raises(jev.JevError, match="model_mismatch"):
        jev.validate_answers(
            {"usage": {"input_tokens": 1, "output_tokens": 1}, "model": "jev-latest",
             "answers": {"a": {"noul": 0.5}}}, q, jev.JEV_MODEL)


def test_retryable_status_backoff():
    calls = []

    def post(body, timeout):
        calls.append(1)
        if len(calls) == 1:
            return 429, {"Retry-After": "0"}, b"{}"
        return 200, {}, json.dumps({
            "usage": {"input_tokens": 1, "output_tokens": 1},
            "model": jev.JEV_MODEL,
            "answers": {"a": {"type": "noul", "noul": 0.8}}}).encode()

    c = jev.JevClient(api_key="k", post_fn=post, max_attempts=3)
    out = c.evaluate({}, {"a": jev.noul_question("i", "t", "f")},
                     time.monotonic() + 30)
    assert out["answers"]["a"]["noul"] == 0.8 and len(calls) == 2


def test_payment_required_is_a_resource_wait_not_a_contract_error():
    import semantic_runtime
    calls = []

    def post(body, timeout):
        calls.append(1)
        return 402, {}, b"{}"

    c = jev.JevClient(api_key="k", post_fn=post)
    with pytest.raises(jev.JevError) as caught:
        c.evaluate({}, {"a": jev.noul_question("i", "t", "f")},
                   time.monotonic() + 30)
    err = caught.value
    assert (err.kind, err.status, err.retryable) == ("payment_required", 402, False)
    assert len(calls) == 1                       # never blind-retried
    assert semantic_jev.failure_class(err) == "resource"   # jobs wait
    assert semantic_runtime._circuit_failure_class(err) == "http_402"


def test_nonretryable_fail_fast():
    calls = []

    def post(body, timeout):
        calls.append(1)
        return 422, {}, b"{}"

    c = jev.JevClient(api_key="k", post_fn=post)
    with pytest.raises(jev.JevError, match="contract_error"):
        c.evaluate({}, {"a": jev.noul_question("i", "t", "f")},
                   time.monotonic() + 30)
    assert len(calls) == 1          # contract errors never blind-retry

    def post401(body, timeout):
        calls.append(1)
        return 401, {}, b"{}"

    c = jev.JevClient(api_key="k", post_fn=post401)
    with pytest.raises(jev.JevError, match="auth_error"):
        c.evaluate({}, {"a": jev.noul_question("i", "t", "f")},
                   time.monotonic() + 30)


def test_no_api_key():
    c = jev.JevClient(api_key=None, post_fn=lambda b, t: (200, {}, b""))
    with pytest.raises(jev.JevError, match="no_api_key"):
        c.evaluate({}, {"a": jev.noul_question("i", "t", "f")},
                   time.monotonic() + 30)


def test_verdict_thresholds():
    assert jev.verdict_for(0.9) == "MATCH"
    assert jev.verdict_for(0.5) == "UNDETERMINED"
    assert jev.verdict_for(0.1) == "NO_MATCH"


# ---------- config gate ----------

def test_config_fail_closed():
    scfg, errs = semantic_policy.semantic_config({})
    assert scfg["mode"] == "off" and errs == []
    scfg, errs = semantic_policy.semantic_config({"semantic": "yes"})
    assert scfg["mode"] == "off" and errs
    scfg, errs = semantic_policy.semantic_config(
        {"semantic": {"mode": "on", "daily_request_budget": -5,
                      "project_ids": "all"}})
    assert scfg["mode"] == "off"
    assert "config: semantic_mode_invalid" in errs
    assert "config: semantic_daily_request_budget_invalid" in errs
    assert "config: semantic_project_ids_invalid" in errs
    scfg, _ = semantic_policy.semantic_config(
        {"semantic": {"mode": "enforce", "model": "jev-latest"}})
    assert scfg["mode"] == "off"    # non-fixed model -> refuses stage
    for field, value in (("match_threshold", float("nan")),
                         ("job_budget_seconds", 10**1000),
                         ("job_budget_seconds", True),
                         ("daily_request_budget", -1),
                         ("match_threhsold", .8)):
        scfg, errors = semantic_policy.semantic_config(
            {"semantic": {"mode": "enforce", field: value}})
        assert errors and scfg["mode"] == "off"
    scfg, errors = semantic_policy.semantic_config({"semantic": {
        "mode": "enforce", "match_threshold": .2, "nomatch_threshold": .8}})
    assert errors and scfg["mode"] == "off"


def test_no_semantic_flag_no_job(tmp_path):
    db = _ledger(tmp_path)
    p = _patient(db)
    p.messages = [_message(1)]
    db.save_patient(p, notify={"source": "unread"})   # semantic=False
    assert db.db.execute("SELECT count(*) FROM fetch_jobs "
                         "WHERE kind='semantic'").fetchone()[0] == 0
    db.close()


def test_bundle_fixed_shape(tmp_path):
    db = _seeded(tmp_path)
    b = semantic_store.thread_bundle(db.db, 1, 1)
    assert b["bundle_id"].startswith("bundle_1_1_")
    assert {m["message_id"] for m in b["members"]} == {1, 2}
    assert b["content_quality"] == "full"
    assert b["registry_version"] == jev.REGISTRY_VERSION
    st = semantic_store.jev_state(b, 1)
    assert st["target"]["id"] == "m1" and BODY in st["target"]["text"]
    assert st["context"][0]["id"] == "m2"
    # sender minimization: profession/type only, no display names (INV-05)
    assert "sender_name" not in json.dumps(st)
    db.close()


def test_jev_unavailable_finishes_with_explicit_skip(tmp_path):
    db = _seeded(tmp_path)
    res = {"errors": []}
    out = semantic_drain.run_due(db, _cfg("shadow", budget=0), res,
                                 time.monotonic() + 60,
                                 jev_client=None, llm_fn=_llm)
    assert out["done"] == 1
    assert db.job_pending("semantic", 1, 1) is None
    art = db.artifacts("semantic_assess", message_id=1)[-1]
    assert json.loads(art["meta"])["technical_status"] == "skipped"
    assert json.loads(art["meta"])["jev_skip_reason"] == "disabled"
    db.close()


@pytest.mark.parametrize("legacy_content", [False, True])
def test_bundle_compact_writer_and_existing_json_retry(tmp_path, legacy_content):
    import pattern_cards

    db = _ledger(tmp_path)
    try:
        patient = _patient(db)
        body = BODY + ' 引用 "合成  空白"'
        parent = _message(1, body=body)
        parent.sender_name = "合成 職員"
        parent.organization = "合成 施設"
        parent.replies = [_message(2, parent=1)]
        patient.messages = [parent]
        db.save_patient(patient, notify={"source": "unread"}, semantic=True)
        payload = json.loads(db.job_pending("semantic", 1, 1)["payload"])
        expected = semantic_store.thread_bundle(db.db, 1, 1, payload["targets"])
        origin = payload.get("origin") if isinstance(payload.get("origin"), dict) else {}
        expected["origin_event_id"] = origin.get("event_id")
        expected["capture_origin"] = origin.get("source")

        result = semantic_drain.run_due(db, _cfg("shadow"), {"errors": []},
                                        time.monotonic() + 300, jev_client=_FakeJev(), llm_fn=_llm)
        assert result["done"] == 1
        [row] = db.artifacts("semantic_bundle", message_id=1)
        content = json.loads(row["content"])
        meta = json.loads(row["meta"])
        assert content == expected
        assert row["content"] == json.dumps(expected, ensure_ascii=False, separators=(",", ":"))
        assert content["members"][0]["body_original"] == body
        assert meta["fingerprint"] == expected["source_fingerprint"]
        assert semantic_store.bundle_fingerprint(expected["members"]) == expected["source_fingerprint"]

        compact_names = pattern_cards._generic_names(db.db, {"artifacts"})
        assert {"合成 職員", "合成 施設"} <= compact_names
        old_json = json.dumps(expected, ensure_ascii=False)
        assert old_json != row["content"] and json.loads(old_json) == content
        db.db.execute("SAVEPOINT legacy_bundle")
        db.db.execute("UPDATE artifacts SET content=? WHERE artifact_id=?", (old_json, row["artifact_id"]))
        assert pattern_cards._generic_names(db.db, {"artifacts"}) == compact_names
        db.db.execute("ROLLBACK TO legacy_bundle")
        db.db.execute("RELEASE legacy_bundle")

        if legacy_content:
            with db.db:
                db.db.execute("UPDATE artifacts SET content=? WHERE artifact_id=?", (old_json, row["artifact_id"]))
        before = [tuple(r) for r in db.db.execute(
            "SELECT artifact_id,content,meta FROM artifacts WHERE kind='semantic_bundle' ORDER BY artifact_id")]
        db.semantic_seed(1, [1, 2], {"source": "replay", "notification_free": True})
        assert db.job_pending("semantic", 1, 1)["state"] == "pending"
        replay = semantic_drain.run_due(db, _cfg("shadow"), {"errors": []},
                                        time.monotonic() + 300, jev_client=_FakeJev(), llm_fn=_llm)
        assert replay["done"] == 1
        after = [tuple(r) for r in db.db.execute(
            "SELECT artifact_id,content,meta FROM artifacts WHERE kind='semantic_bundle' ORDER BY artifact_id")]
        assert after == before and len(after) == 1
        assert json.loads(after[0][1]) == expected
    finally:
        db.close()


def test_evidence_span_locate():
    assert mcs_util.locate_quote_span(BODY, "カロナール300mg") == (4, 14)
    assert mcs_util.locate_quote_span(BODY, "存在しない文") is None
    assert mcs_util.locate_quote_span("aa aa", "aa") is None   # ambiguous
    # whitespace-normalized fallback resolves to the verbatim unique span
    src = "投与を\n  開始した。翌日に再評価。"
    s, e = mcs_util.locate_quote_span(src, "投与を 開始した。")
    assert src[s:e] == "投与を\n  開始した。"
    assert mcs_util.locate_quote_span("aa\naa aa aa", "aa aa") is None
    f, ok, dropped = semantic_extraction.extract_facts(
        lambda p: json.dumps({"facts": [{
            "statement": "x", "kind": "other", "status": "not_stated",
            "polarity": "affirmed", "time_text": None, "quantity": None,
            "evidence_quote": "存在しない引用"}]}),
        {"message_id": 1, "revision": "r", "body_original": BODY})
    assert ok and dropped == 0
    assert f[0]["validation_status"] == "unverified"
    assert f[0]["evidence_refs"] == []


# ---------- review-fix regressions ----------

def test_degraded_skips_delivered_history(tmp_path):
    """§19.3: degraded fallback covers only events whose base notice is
    STILL undelivered — an accepted/suppressed historical row must
    never spawn a '新着取得' notice."""
    db = _seeded(tmp_path)
    # the seeding left one pending new_messages event; add a second,
    # long-delivered one and age everything past the delay
    db.outbox_add("new_messages", 1, {"message_ids": [1]})
    old = db.db.execute(
        "SELECT event_id FROM notify_outbox WHERE kind='new_messages' "
        "ORDER BY event_id DESC").fetchone()["event_id"]
    db.outbox_mark(old, "accepted")
    db.db.execute("UPDATE notify_outbox SET created_at=?",
                  (time.time() - 7200,))
    db.db.commit()
    scfg = semantic_policy.semantic_config(
        _cfg("enforce", delayed_notice_seconds=900))[0]
    assert semantic_notice.emit_degraded(db, scfg) == 1   # only the pending one
    rows = db.db.execute(
        "SELECT payload FROM notify_outbox "
        "WHERE kind='semantic_notice'").fetchall()
    assert len(rows) == 1
    pl = json.loads(rows[0]["payload"])
    assert pl["degraded"] is True
    assert pl["src_event_id"] != old   # not the delivered event
    # idempotent — a second scan adds nothing
    assert semantic_notice.emit_degraded(db, scfg) == 0
    db.close()


def test_nonretryable_jev_error_fails_bounded(tmp_path):
    """Permanent contract errors stop after one job attempt, including later ticks."""
    db = _seeded(tmp_path)
    fake = _FakeJev(error=jev.JevError("protocol_error",
                                     retryable=False))
    res = {"errors": []}
    for _ in range(6):
        db.db.execute("UPDATE fetch_jobs SET next_try=0 "
                      "WHERE kind='semantic'")
        db.db.commit()
        semantic_drain.run_due(db, _cfg("shadow"), res,
                         time.monotonic() + 300,
                         jev_client=fake, llm_fn=_llm)
    job = db.db.execute(
        "SELECT state,attempts FROM fetch_jobs WHERE kind='semantic'"
    ).fetchone()
    assert job["state"] == "failed"
    assert job["attempts"] == 1
    db.close()


# ---------- review-fix regressions (second pass) ----------

def test_oversized_response_fails_fast():
    """A deterministic non-retryable failure (response_too_large) must
    stop after ONE post — looping the identical request would only
    burn the daily budget on an input that can never succeed."""
    calls = []

    def post(body, timeout):
        calls.append(1)
        return 200, {}, b"x" * (jev.MAX_RESPONSE_BYTES + 1)

    c = jev.JevClient(api_key="k", post_fn=post, max_attempts=3)
    with pytest.raises(jev.JevError) as ei:
        c.evaluate({}, {"a": jev.noul_question("i", "t", "f")},
                   time.monotonic() + 30)
    assert ei.value.detail == "response_too_large"
    assert len(calls) == 1                  # one request, not three


def test_config_threshold_order_fail_closed():
    """match <= nomatch would silently collapse the UNDETERMINED band
    (verdict_for checks MATCH first) — the inversion is reported and
    the whole feature fails closed to off."""
    scfg, errs = semantic_policy.semantic_config(
        {"semantic": {"mode": "shadow", "match_threshold": 0.3,
                      "nomatch_threshold": 0.7}})
    assert "config: semantic_threshold_order_invalid" in errs
    assert scfg["mode"] == "off"


def test_summary_prompt_carries_verdicts(tmp_path):
    """Jev verdicts are advisory context for the writer (§15.3) — the
    prompt must actually carry them, not silently drop the parameter."""
    db = _seeded(tmp_path)
    seen = []

    def capture(prompt):
        seen.append(prompt)
        return _llm(prompt)

    semantic_drain.run_due(db, _cfg("shadow"), {"errors": []},
                     time.monotonic() + 300,
                     jev_client=_FakeJev(), llm_fn=capture)
    summ = next(p for p in seen if "要約器" in p)
    assert "命題評価" in summ and "P01" in summ and "MATCH" in summ
    db.close()


def test_jst_day_boundary():
    """The daily Jev budget resets at JST midnight — the operation's
    actual day — not UTC midnight (09:00 JST)."""
    t = datetime.datetime(2026, 9, 20, 16, 0,
                          tzinfo=datetime.timezone.utc).timestamp()
    start = semantic_store._jst_day_start(t)
    jst = datetime.timezone(datetime.timedelta(hours=9))
    got = datetime.datetime.fromtimestamp(start, tz=jst)
    assert (got.year, got.month, got.day,
            got.hour, got.minute) == (2026, 9, 21, 0, 0)


def test_models_transport_wrapped():
    """models() must surface transport failure as JevError like
    evaluate() does — the smoke harness only catches JevError."""
    c = jev.JevClient(api_key="k")
    c.request = lambda *a, **k: (_ for _ in ()).throw(
        urllib.error.URLError("down"))
    with pytest.raises(jev.JevError, match="transport"):
        c.models()


def test_retry_after_header_any_casing():
    # HTTP field names are case-insensitive — a non-standard casing must
    # still be honored instead of falling back to exponential backoff (S-2)
    c = jev.JevClient(api_key="k", post_fn=lambda b, t: (200, {}, b""))
    assert c._retry_after({"RETRY-AFTER": "2.5"}) == 2.5
    assert c._retry_after({"Retry-After": "3"}) == 3.0
    assert c._retry_after({"x-other": "9"}) is None
    assert c._retry_after({"Retry-After": "soon"}) is None


def test_semantic_paths_follow_mcs_root(tmp_path):
    root = tmp_path / "root"
    code = (
        "import sys; sys.path.insert(0, %r); sys.path.insert(0, %r);"
        "import _mcs_path, semantic, semantic_transport;"
        "print(semantic.DB); print(semantic.CONF_PATH);"
        "print(semantic_transport._FMT_PATH); print(semantic_transport._LONG_PATH)"
        % (os.path.join(ROOT, "mcs"), os.path.join(ROOT, "mcs", "semantic")))
    env = {**os.environ, "MCS_ROOT": str(root),
           "HOME": str(tmp_path / "fakehome")}
    out = subprocess.run([sys.executable, "-c", code], env=env,
                         capture_output=True, text=True, check=True).stdout
    lines = out.split()
    assert len(lines) == 4
    for p in lines:
        assert p.startswith(str(root) + os.sep), p
