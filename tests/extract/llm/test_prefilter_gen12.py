"""Widened low-signal prefilter (X7a, generation 12).

Content-less posts settle with a durable meta.prefilter='no_signal'
marker instead of an LLM call, but a cue-bearing short reply or a reply
to a thread's request stays on the LLM path. Replacing a fact-bearing
prior with the empty marker must pass the protected-loss gate, and the
marker row is never picked up by extract_qc. Fully synthetic fixtures
and temp DBs only.
"""
import json

import pytest

import extract_llm
import extract_llm_qc
import extract_llm_runner
from extract_testkit import _hash, _ledger, _message
from inference_version import CURRENT_VERSION, VERSION_KEY


@pytest.fixture(autouse=True)
def _prefilter_on(monkeypatch):
    monkeypatch.setenv("MCS_EXTRACT_PREFILTER", "on")


def _stub_llm(monkeypatch, calls):
    monkeypatch.setattr(
        extract_llm, "llm_extract",
        lambda body, **_: calls.append(body) or {"summary": "完全合成の抽出結果"})
    monkeypatch.setattr(extract_llm_runner, "llm_extract", extract_llm.llm_extract)


def _prefilter_rows(db, mid):
    return [dict(r) for r in db.db.execute(
        "SELECT meta, content FROM artifacts WHERE kind='extract_llm'"
        " AND message_id=? AND json_extract(meta,'$.prefilter') IS NOT NULL",
        (mid,)).fetchall()]


@pytest.mark.parametrize("body", [
    "ありがとうございます。", "ありがとうございました", "ありがとう。",
    "お疲れ様です", "お疲れ様でした。",
    "失礼します", "承知しました。", "承知いたしました",
    "了解です", "了解しました。", "承知しました。了解です。",
])
def test_empty_post_settled_without_llm(tmp_path, monkeypatch, body):
    db = _ledger(tmp_path)
    db.save_messages([_message(mid=1, body=body)])
    calls = []
    _stub_llm(monkeypatch, calls)

    res = extract_llm.run_pending(db, limit=10, budget_s=30)

    assert res["skipped"] == 1 and calls == []
    rows = _prefilter_rows(db, 1)
    assert len(rows) == 1
    meta = json.loads(rows[0]["meta"])
    assert meta["prefilter"] == "no_signal" and meta["hash"] == _hash(db, 1)
    content = json.loads(rows[0]["content"])
    assert content["meds"] == [] and "summary" not in content
    # settled: a second pass never re-reads the row
    res2 = extract_llm.run_pending(db, limit=10, budget_s=30)
    assert res2["skipped"] == 0 and res2["selected"] == 0
    db.close()


@pytest.mark.parametrize("body", [
    "承知しました。卵アレルギーはありません。",
    "了解です。合成薬ABで発疹が出ました。",
    "よろしくお願いします。",   # the rule hints read it as a request
    "ありがとうございます。追加で血液検査を希望します。",
])
def test_cue_or_fact_reply_reaches_the_llm(tmp_path, monkeypatch, body):
    db = _ledger(tmp_path)
    db.save_messages([_message(mid=1, body=body)])
    calls = []
    _stub_llm(monkeypatch, calls)

    res = extract_llm.run_pending(db, limit=10, budget_s=30)

    assert res["done"] == 1 and res["skipped"] == 0 and calls == [body]
    assert _prefilter_rows(db, 1) == []
    db.close()


def test_reply_to_a_request_thread_is_not_filtered(tmp_path, monkeypatch):
    db = _ledger(tmp_path)
    db.save_messages([
        _message(mid=1, body="本日の血圧と脈拍を確認してください。"),
        _message(mid=2, body="ありがとうございます。", parent_id=1),
    ])
    calls = []
    _stub_llm(monkeypatch, calls)

    res = extract_llm.run_pending(db, limit=10, budget_s=30)

    # the empty courtesy reply answers a standing request — kept for the LLM
    assert res["done"] == 2 and sorted(calls) == sorted(
        ["本日の血圧と脈拍を確認してください。", "ありがとうございます。"])
    assert _prefilter_rows(db, 2) == []
    db.close()


@pytest.mark.parametrize("state", ["snippet", "partial"])
def test_reply_to_a_partial_root_fails_safe(tmp_path, monkeypatch, state):
    """A snippet root may be missing the request's tail: no denial, no filter."""
    db = _ledger(tmp_path)
    db.save_messages([
        _message(mid=1, body="本日の血圧を", state=state),
        _message(mid=2, body="ありがとうございます。", parent_id=1),
    ])
    calls = []
    _stub_llm(monkeypatch, calls)

    extract_llm.run_pending(db, limit=10, budget_s=30)

    assert "ありがとうございます。" in calls
    assert _prefilter_rows(db, 2) == []
    db.close()


def test_reply_with_unreadable_root_fails_safe(tmp_path, monkeypatch):
    db = _ledger(tmp_path)
    db.save_messages([
        _message(mid=1, body="ありがとうございます。", parent_id=999),
    ])
    calls = []
    _stub_llm(monkeypatch, calls)

    res = extract_llm.run_pending(db, limit=10, budget_s=30)

    assert res["done"] == 1 and calls == ["ありがとうございます。"]
    assert _prefilter_rows(db, 1) == []
    db.close()


def test_empty_marker_replacing_fact_bearing_prior_records_protected_loss(tmp_path, monkeypatch):
    db = _ledger(tmp_path)
    db.save_messages([_message(mid=1, body="ありがとうございます。")])
    # a protected assertion: negated or non-patient, its quote grounded in
    # this exact body — protected_lost must see it lost to the empty marker
    prior_content = {"meds": [{"name": "合成薬A", "action": "start",
                               "subject": "family",
                               "evidence": "ありがとうございます。"}],
                     "urgency": "routine"}
    # a generation-11-equivalent prior: real facts under the same body hash
    db.artifact_add(
        "extract_llm", json.dumps(prior_content), project_id=1, message_id=1,
        meta={VERSION_KEY: CURRENT_VERSION, "hash": _hash(db, 1),
              "extract_version": extract_llm.EXTRACT_VERSION - 1})
    calls = []
    _stub_llm(monkeypatch, calls)

    extract_llm.run_pending(db, limit=10, budget_s=30)

    receipts = [json.loads(r["content"]) for r in db.db.execute(
        "SELECT content FROM artifacts WHERE kind=? AND message_id=1",
        ("extract_upgrade_v1",)).fetchall()]
    assert any(r.get("state") == "held_prior" and r.get("reason") == "protected_loss"
               for r in receipts), receipts
    # the empty marker must not have replaced the protected prior
    rows = [json.loads(r["content"]) for r in db.db.execute(
        "SELECT content FROM artifacts WHERE kind='extract_llm' AND message_id=1").fetchall()]
    assert not _prefilter_rows(db, 1)
    assert any(r.get("meds") == prior_content["meds"] for r in rows), rows
    db.close()


def test_no_signal_marker_is_outside_extract_qc_pending(tmp_path, monkeypatch):
    db = _ledger(tmp_path)
    db.save_messages([_message(mid=1, body="ありがとうございます。" * 40)])
    _stub_llm(monkeypatch, [])

    extract_llm.run_pending(db, limit=10, budget_s=30)
    assert _prefilter_rows(db, 1)

    pending = {r[0] for r in db.db.execute(
        "SELECT m.message_id FROM messages m WHERE "
        + extract_llm.pending_pred(db.db)).fetchall()}
    assert 1 not in pending
    thin = db.db.execute(
        "SELECT 1 FROM messages m WHERE EXISTS ("
        + extract_llm_qc._thin_pending_sql(db.db) + ")").fetchall()
    assert thin == []
    db.close()
