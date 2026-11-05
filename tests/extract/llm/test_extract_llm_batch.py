"""Synthetic extract llm batch contracts with isolated shared fixtures."""
import extract_llm_engine
import extract_llm_errors
import extract_llm_resident
import extract_llm_runner
import extract_schema
import json
import extract_llm
from extract_testkit import _ledger, _message
from extract_llm_testkit import _batch_ledger as _batch_ledger


def test_batch_extracts_all_rows_one_call(tmp_path, monkeypatch):
    """Context-free single-chunk rows share one batched call — the
    items envelope routes each extraction back to its own body."""
    db = _batch_ledger(tmp_path)
    calls = []

    def fake_call(prompt, **kw):
        calls.append(prompt)
        return {"items": [{"i": i, "symptoms": [], "summary": f"s{i}",
                           "urgency": "routine"} for i in range(4)]}
    monkeypatch.setattr(extract_llm, "_llm_call", fake_call)
    monkeypatch.setattr(extract_llm_engine, '_llm_call', extract_llm._llm_call)
    monkeypatch.setattr(extract_llm_runner, '_llm_call', extract_llm._llm_call)
    res = extract_llm.run_pending(db, limit=10, budget_s=30, batch_k=4)
    assert res["done"] == 4 and len(calls) == 1
    assert "対象0(" in calls[0] and "対象3(" in calls[0]
    for mid in range(1, 5):
        arts = db.artifacts("extract_llm", message_id=mid)
        assert arts and json.loads(
            arts[0]["content"])["summary"].startswith("s")
        meta = json.loads(arts[0]["meta"])
        assert meta["integrity"]["batch"] == 4
    db.close()


def test_batch_k2_commits_the_whole_document_completeness_claim(tmp_path, monkeypatch):
    """A batched direct commit carries no chunk checkpoints: its only leaf is
    the artifact itself — the persisted completeness claim must reproduce
    from the committed document (recomputed, not trusted from runtime meta)."""
    db = _ledger(tmp_path)
    db.ensure_patient(1)
    db.save_messages([_message(mid=1, body="本文1"), _message(mid=2, body="本文2")])
    monkeypatch.setattr(
        extract_llm, "_llm_call",
        lambda p, **kw: {"items": [{"i": 0, "symptoms": [], "summary": "s0"},
                                   {"i": 1, "symptoms": [], "summary": "s1"}]})
    monkeypatch.setattr(extract_llm_engine, '_llm_call', extract_llm._llm_call)
    monkeypatch.setattr(extract_llm_runner, '_llm_call', extract_llm._llm_call)
    res = extract_llm.run_pending(db, limit=10, budget_s=30, batch_k=2)
    assert res["done"] == 2
    for mid in (1, 2):
        arts = db.artifacts("extract_llm", message_id=mid)
        meta = json.loads(arts[0]["meta"])
        claim = meta["integrity"]["completeness"]
        assert set(claim) == {"0"} and meta["integrity"]["completeness_chunks"] == 1
        assert claim["0"] == {"state": "hint_gaps_resolved",
                              "remaining_hint_candidates": 0}
    db.close()


def test_batch_skips_context_and_multichunk_rows(tmp_path, monkeypatch):
    """Rows with thread context keep the single-call path — batching
    is only for context-free single-chunk bodies."""
    db = _ledger(tmp_path)
    db.ensure_patient(1)
    db.save_messages([
        _message(mid=1, body="親投稿",
                 posted_at="2026-09-18T00:00:00+09:00"),
        _message(mid=2, body="返信本文", parent_id=1,
                 posted_at="2026-09-19T00:00:00+09:00"),
        _message(mid=3, body="単発A"), _message(mid=4, body="単発B")])
    batch_calls = []
    monkeypatch.setattr(
        extract_llm, "_llm_call",
        lambda p, **kw: batch_calls.append(p) or {"items": [
            {"i": 0, "symptoms": [], "summary": "s0"}, {"i": 1, "symptoms": [], "summary": "s1"},
            {"i": 2, "symptoms": [], "summary": "s2"}]})
    monkeypatch.setattr(extract_llm_engine, '_llm_call', extract_llm._llm_call)
    monkeypatch.setattr(extract_llm_runner, '_llm_call', extract_llm._llm_call)
    singles = []
    monkeypatch.setattr(
        extract_llm, "llm_extract",
        lambda body, **kw: singles.append(kw) or {"summary": "one"})
    monkeypatch.setattr(extract_llm_runner, 'llm_extract', extract_llm.llm_extract)
    res = extract_llm.run_pending(db, limit=10, budget_s=30, batch_k=4)
    assert res["done"] == 4
    assert len(singles) == 1                 # the reply carried context
    assert singles[0].get("context")
    # every context-free body went through batch prompts, never singles;
    # grouping itself is selection-order dependent
    assert len(batch_calls) >= 1
    joined = "\n".join(batch_calls)
    assert "親投稿" in joined and "単発A" in joined and "単発B" in joined
    assert "返信本文" not in joined
    db.close()


def test_batch_omitted_item_falls_back_to_single(tmp_path, monkeypatch):
    """An item the batch omits gets an in-run single retry instead of
    staying pending or silently failing."""
    db = _batch_ledger(tmp_path, n=4)
    monkeypatch.setattr(
        extract_llm, "_llm_call",
        lambda p, **kw: {"items": [{"i": 0, "symptoms": [], "summary": "s0"},
                                   {"i": 2, "symptoms": [], "summary": "s2"}]})
    monkeypatch.setattr(extract_llm_engine, '_llm_call', extract_llm._llm_call)
    monkeypatch.setattr(extract_llm_runner, '_llm_call', extract_llm._llm_call)
    seen = []
    monkeypatch.setattr(
        extract_llm, "llm_extract",
        lambda body, **kw: seen.append(body) or {"summary": "fix"})
    monkeypatch.setattr(extract_llm_runner, 'llm_extract', extract_llm.llm_extract)
    res = extract_llm.run_pending(db, limit=10, budget_s=30, batch_k=4)
    assert res["done"] == 4 and len(seen) == 2   # i1, i3 retried singly
    db.close()


def test_batch_envelope_failure_residues_to_singles(
        tmp_path, monkeypatch):
    """A dead batch call burns nothing: every row gets the single path
    (and its repair pass) rather than a shared error."""
    db = _batch_ledger(tmp_path, n=4)
    monkeypatch.setattr(extract_llm, "_llm_call", lambda p, **kw: None)
    monkeypatch.setattr(extract_llm_engine, '_llm_call', extract_llm._llm_call)
    monkeypatch.setattr(extract_llm_runner, '_llm_call', extract_llm._llm_call)
    monkeypatch.setattr(extract_llm, "llm_extract",
                        lambda body, **kw: {"summary": "fix"})
    monkeypatch.setattr(extract_llm_runner, 'llm_extract', extract_llm.llm_extract)
    res = extract_llm.run_pending(db, limit=10, budget_s=30, batch_k=4)
    assert res["done"] == 4 and res["failed"] == 0
    db.close()


def test_batch_call_notes_reset_between_runs(tmp_path, monkeypatch):
    """The batch lane clears the shared thread-local call notes each
    pass — a batch-only resident worker must not grow it one entry per
    call (singles already reset inside llm_extract)."""
    db = _batch_ledger(tmp_path, n=4)

    def fake_call(prompt, **kw):
        # mirror the real _llm_call's note append
        extract_llm._integrity_note(
            {"status": 200, "finish_reason": "stop"})
        return {"items": [{"i": i, "symptoms": [], "summary": f"s{i}"}
                          for i in range(4)]}

    monkeypatch.setattr(extract_llm, "_llm_call", fake_call)
    monkeypatch.setattr(extract_llm_engine, '_llm_call', extract_llm._llm_call)
    monkeypatch.setattr(extract_llm_runner, '_llm_call', extract_llm._llm_call)
    for round_ in range(3):
        if round_:
            db.save_messages(
                [_message(mid=100 + round_ * 10 + i, body=f"追加{i}")
                 for i in range(4)])
        extract_llm.run_pending(db, limit=10, budget_s=30, batch_k=4)
    assert len(extract_llm._note_list()) == 1   # last batch's note only
    db.close()


def test_batch_item_evidence_validates_against_own_body(
        tmp_path, monkeypatch):
    """An item quoting body B under index A fails to locate its
    evidence — and an item that only validates WITH drops joins the
    residue so the single lane's repair pass gets a shot at it."""
    db = _ledger(tmp_path)
    db.ensure_patient(1)
    db.save_messages([_message(mid=1, body="カロナール服用中"),
                      _message(mid=2, body="ロキソプロフェン中止")])
    monkeypatch.setattr(
        extract_llm, "_llm_call",
        lambda p, **kw: {"items": [
            {"i": 0, "symptoms": [], "meds": [{"name": "ロキソプロフェン", "action": "stop",
                               "status": "past", "subject": "patient", "negated": False,
                               "evidence": "ロキソプロフェン中止"}]},
            {"i": 1, "symptoms": [], "meds": [{"name": "ロキソプロフェン",
                               "evidence": "ロキソプロフェン中止"}]}]})
    monkeypatch.setattr(extract_llm_engine, '_llm_call', extract_llm._llm_call)
    monkeypatch.setattr(extract_llm_runner, '_llm_call', extract_llm._llm_call)
    singles = []
    monkeypatch.setattr(
        extract_llm, "llm_extract",
        lambda body, **kw: singles.append(body) or {
            "meds": [{"name": "カロナール", "evidence": "カロナール服用中"}]})
    monkeypatch.setattr(extract_llm_runner, 'llm_extract', extract_llm.llm_extract)
    res = extract_llm.run_pending(db, limit=10, budget_s=30, batch_k=4)
    assert res["done"] == 2
    # selection is DESC: tups[0]=mid2 keeps its locating batch item;
    # tups[1]=mid1 dropped (quote absent in its own body) -> single
    assert singles == ["カロナール服用中"]
    a1 = json.loads(
        db.artifacts("extract_llm", message_id=1)[0]["content"])
    a2 = json.loads(
        db.artifacts("extract_llm", message_id=2)[0]["content"])
    assert a1["meds"][0]["evidence"] == "カロナール服用中"
    assert a2["meds"][0]["evidence"] == "ロキソプロフェン中止"
    db.close()


def test_batch_default_off_and_zero_disables(tmp_path, monkeypatch):
    """batch_k stays opt-in: the default call and batch_k=0 both keep
    the single-call reference path."""
    db = _batch_ledger(tmp_path, n=3)
    calls = []
    monkeypatch.setattr(
        extract_llm, "llm_extract",
        lambda body, **kw: calls.append(body) or {"summary": "s"})
    monkeypatch.setattr(extract_llm_runner, 'llm_extract', extract_llm.llm_extract)
    monkeypatch.setattr(
        extract_llm, "_llm_call",
        lambda p, **kw: calls.append("BATCH") or None)
    monkeypatch.setattr(extract_llm_engine, '_llm_call', extract_llm._llm_call)
    monkeypatch.setattr(extract_llm_runner, '_llm_call', extract_llm._llm_call)
    res = extract_llm.run_pending(db, limit=10, budget_s=30)
    assert res["done"] == 3 and "BATCH" not in calls
    calls.clear()
    res = extract_llm.run_pending(db, limit=10, budget_s=30, batch_k=0)
    assert res["done"] == 0      # already covered — nothing reselected
    db.close()


def test_prompt_head_is_byte_stable_and_batch_shares_spec():
    """_PROMPT_HEAD must stay byte-identical (chunk checkpoints hash
    it) while _BATCH_HEAD reuses the spec prefix for cache sharing."""
    assert extract_llm._PROMPT_HEAD == (
        extract_llm._PROMPT_SPEC + extract_llm._PROMPT_EXAMPLES)
    assert extract_llm._PROMPT_HEAD.startswith("あなたは在宅医療")
    assert "例3:" in extract_llm._PROMPT_EXAMPLES
    assert "例4:" in extract_llm._PROMPT_EXAMPLES
    assert extract_llm._PROMPT_HEAD.endswith("\n\n")
    assert '"kind": "request|question|self_plan"' in extract_llm._PROMPT_SPEC
    assert "〜をお願いできますか 等、相手に行動を求める丁寧表現を含む）は request" \
        in extract_llm._PROMPT_SPEC
    assert extract_llm._BATCH_HEAD.startswith(extract_llm._PROMPT_SPEC)


def test_batch_prompt_indexes_targets_and_hints():
    p = extract_llm._batch_prompt(
        [("本文A", "2026-09-19T00:00:00+09:00",
          {"meds": [{"name": "x"}]}),
         ("本文B", None, None)])
    assert "対象0(" in p and "対象1(" in p
    assert "対象0の決定的候補" in p and "対象1の決定的候補" not in p
    assert p.endswith("JSON:") and "本文B" in p


def test_deferred_batch_rows_fall_back_to_singles(tmp_path, monkeypatch):
    """A batch deferred by the completion floor still tries its rows
    as singles — a lone call can fit a remainder too short for the
    batch."""
    db = _batch_ledger(tmp_path)
    calls = {"batch": 0, "single": 0}

    def _call(prompt, **kw):
        if kw.get("schema") is extract_schema.SCHEMA_BATCH:
            calls["batch"] += 1
            return extract_llm._DEFERRED
        calls["single"] += 1
        return {"symptoms": [], "summary": "s"}
    monkeypatch.setattr(extract_llm, "_llm_call", _call)
    monkeypatch.setattr(extract_llm_engine, '_llm_call', extract_llm._llm_call)
    monkeypatch.setattr(extract_llm_runner, '_llm_call', extract_llm._llm_call)
    res = extract_llm.run_pending(db, limit=10, budget_s=30, batch_k=4)
    assert calls == {"batch": 1, "single": 4}
    assert res["done"] == 4 and res["deferred"] == 0
    db.close()


def test_deferred_batch_fall_back_parallel(tmp_path, monkeypatch):
    """Same residue routing under the thread-pool path: deferred batch
    rows are re-submitted as singles, not dropped on the floor."""
    db = _batch_ledger(tmp_path)
    calls = {"batch": 0, "single": 0}

    def _call(prompt, **kw):
        if kw.get("schema") is extract_schema.SCHEMA_BATCH:
            calls["batch"] += 1
            return extract_llm._DEFERRED
        calls["single"] += 1
        return {"symptoms": [], "summary": "s"}
    monkeypatch.setattr(extract_llm, "_llm_call", _call)
    monkeypatch.setattr(extract_llm_engine, '_llm_call', extract_llm._llm_call)
    monkeypatch.setattr(extract_llm_runner, '_llm_call', extract_llm._llm_call)
    res = extract_llm.run_pending(db, limit=10, budget_s=30, batch_k=4,
                                  workers=3)
    assert calls["batch"] == 1 and calls["single"] == 4
    assert res["done"] == 4
    db.close()


def test_cli_batch_flag_range(monkeypatch, capsys):
    """--batch accepts 0..8; anything else exits 2 with bad_batch —
    a mistyped value must not silently morph into a giant or disabled
    batch."""
    for bad in ("-1", "9", "99"):
        monkeypatch.setattr(extract_llm.sys, "argv",
                            ["extract_llm", "--batch", bad])
        assert extract_llm.main() == 2
        assert json.loads(capsys.readouterr().out)["error"] == "bad_batch"


def test_cli_batch_default_and_pass_through(monkeypatch, tmp_path):
    """The flag reaches run_pending verbatim — default _BATCH_K for
    drainers, 0 when explicitly disabled."""
    seen = {}
    monkeypatch.setattr(extract_llm, "_open_current_ledger",
                        lambda: type("DB", (), {"close": lambda s: None})())
    monkeypatch.setattr(extract_llm_resident, '_open_current_ledger', extract_llm._open_current_ledger)
    monkeypatch.setattr(extract_llm, "acquire_run_lock", lambda:
                        extract_llm.writer_lock.acquire_run_lock(str(tmp_path / "run.lock")))
    monkeypatch.setattr(extract_llm_errors, 'acquire_run_lock', extract_llm.acquire_run_lock)
    monkeypatch.setattr(extract_llm_resident, 'acquire_run_lock', extract_llm.acquire_run_lock)
    monkeypatch.setattr(
        extract_llm, "run_pending",
        lambda *a, **kw: seen.update(kw) or {"done": 0, "failed": 0,
                                             "left": 0, "selected": 0})
    monkeypatch.setattr(extract_llm_resident, 'run_pending', extract_llm.run_pending)
    for argv, want in (
            (["extract_llm"], extract_llm._BATCH_K),
            (["extract_llm", "--batch", "0"], 0),
            (["extract_llm", "--batch", "8"], 8)):
        monkeypatch.setattr(extract_llm.sys, "argv", argv)
        assert extract_llm.main() == 0
        assert seen["batch_k"] == want
