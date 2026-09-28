"""Low-signal prefilter tests (B1).

A body that misses BOTH the v1 rule pass and the broadened signal regex
is settled with a durable meta.prefilter='no_signal' marker instead of
an LLM call — recorded, out of pending, and re-evaluated on edit.
Synthetic fixtures + temp DB only.
"""
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "mcs"))

import extract_llm
import mcs_util
from extract_testkit import _hash, _ledger, _message


@pytest.fixture(autouse=True)
def _prefilter_on(monkeypatch):
    monkeypatch.setenv("MCS_EXTRACT_PREFILTER", "on")


def _artifacts(db, mid):
    return [dict(r) for r in db.db.execute(
        "SELECT * FROM artifacts WHERE kind='extract_llm'"
        " AND message_id=?", (mid,)).fetchall()]


def test_low_signal_unit():
    assert extract_llm._low_signal("了解です。", {"v": 1})
    assert extract_llm._low_signal("ありがとうございます", {"v": 1})
    # digits, clinical words, request vocabulary -> keep on the LLM path
    assert not extract_llm._low_signal("3時に伺います", {"v": 1})
    assert not extract_llm._low_signal("体調の報告です", {"v": 1})
    assert not extract_llm._low_signal("確認しました", {"v": 1})
    # any v1 finding or an unparseable hint pass never skips
    assert not extract_llm._low_signal("了解です", {"v": 1, "events": ["x"]})
    assert not extract_llm._low_signal("了解です", None)


def test_no_signal_message_marked_without_llm(tmp_path, monkeypatch):
    db = _ledger(tmp_path)
    db.save_messages([_message(mid=1, body="了解です。"),
                      _message(mid=2, body="体温38.2度、咳嗽あり")])
    calls = []
    monkeypatch.setattr(
        extract_llm, "llm_extract",
        lambda body, **_: calls.append(body) or {"summary": "ok"})

    res = extract_llm.run_pending(db, limit=10, budget_s=30)

    assert res["skipped"] == 1 and res["done"] == 1
    assert calls == ["体温38.2度、咳嗽あり"]     # only the signal body ran
    rows = _artifacts(db, 1)
    assert len(rows) == 1
    meta = json.loads(rows[0]["meta"])
    assert meta["prefilter"] == "no_signal"
    assert meta["hash"] == _hash(db, 1)
    assert meta["extract_version"] == extract_llm.EXTRACT_VERSION
    content = json.loads(rows[0]["content"])   # honest empty shape
    assert content["meds"] == [] and content["summary"] == ""
    db.close()


def test_prefilter_marker_leaves_pending(tmp_path, monkeypatch):
    db = _ledger(tmp_path)
    db.save_messages([_message(mid=1, body="承知しました")])
    monkeypatch.setattr(extract_llm, "llm_extract",
                        lambda body, **_: {"summary": "unreachable"})

    res = extract_llm.run_pending(db, limit=10, budget_s=30)
    assert res["skipped"] == 1
    res2 = extract_llm.run_pending(db, limit=10, budget_s=30)
    assert res2["skipped"] == 0 and res2["selected"] == 0
    db.close()


def test_body_edit_reenters_queue(tmp_path, monkeypatch):
    db = _ledger(tmp_path)
    db.save_messages([_message(mid=1, body="了解です")])
    monkeypatch.setattr(extract_llm, "llm_extract",
                        lambda body, **_: {"summary": "ok"})
    extract_llm.run_pending(db, limit=10, budget_s=30)
    assert db.db.execute(
        "SELECT COUNT(*) FROM artifacts WHERE kind='extract_llm'"
        " AND json_extract(meta,'$.prefilter') IS NOT NULL"
    ).fetchone()[0] == 1

    # an edit carrying signal changes the hash — the marker stops
    # covering the body and the LLM path runs normally
    msg = _message(mid=1, body="訂正: 脈拍120台です")
    db.save_messages([msg])
    res = extract_llm.run_pending(db, limit=10, budget_s=30)
    assert res["done"] == 1


def test_manifest_admitted_rows_bypass_prefilter(tmp_path, monkeypatch):
    db = _ledger(tmp_path)
    db.save_messages([_message(mid=1, body="了解です")])
    monkeypatch.setattr(extract_llm, "llm_extract",
                        lambda body, **_: {"summary": "ok"})

    res = extract_llm.run_pending(db, limit=10, budget_s=30,
                                  admitted_ids={1})
    assert res["done"] == 1 and res["skipped"] == 0
    db.close()


def test_env_off_disables_prefilter(tmp_path, monkeypatch):
    monkeypatch.setenv("MCS_EXTRACT_PREFILTER", "off")
    db = _ledger(tmp_path)
    db.save_messages([_message(mid=1, body="了解です")])
    monkeypatch.setattr(extract_llm, "llm_extract",
                        lambda body, **_: {"summary": "ok"})
    res = extract_llm.run_pending(db, limit=10, budget_s=30)
    assert res["done"] == 1 and res["skipped"] == 0
    db.close()


def test_v1_artifact_written_for_filtered_body(tmp_path, monkeypatch):
    """Speed-lane coverage: a filtered body still mints its extract_v1
    artifact so instant-analysis readers never wait on the LLM lane."""
    db = _ledger(tmp_path)
    db.save_messages([_message(mid=1, body="了解です")])
    monkeypatch.setattr(extract_llm, "llm_extract",
                        lambda body, **_: {"summary": "ok"})
    extract_llm.run_pending(db, limit=10, budget_s=30)
    rows = db.db.execute(
        "SELECT meta FROM artifacts WHERE kind='extract_v1'"
        " AND message_id=1").fetchall()
    assert len(rows) == 1
    db.close()


# ---------- endpoint circuit breaker + disk guard ----------

def _endpoint_dead(monkeypatch):
    """LLM calls return nothing and the endpoint probe reports dead."""
    monkeypatch.setattr(extract_llm, "llm_extract", lambda body, **_: None)
    monkeypatch.setattr(extract_llm, "_llm_up", lambda **_: False)


def test_circuit_trips_after_repeated_endpoint_down(tmp_path, monkeypatch):
    db = _ledger(tmp_path)
    db.save_messages([_message(mid=1, body="体温38度です")])
    _endpoint_dead(monkeypatch)

    for _ in range(mcs_util._CIRCUIT_TRIP):
        extract_llm.run_pending(db, limit=10, budget_s=30)
    assert mcs_util.circuit_open_s(db) > 0

    calls = []
    monkeypatch.setattr(extract_llm, "llm_extract",
                        lambda body, **_: calls.append(body))
    res = extract_llm.run_pending(db, limit=10, budget_s=30)
    assert calls == []                       # lane closed — no claims
    assert res["circuit_open_s"] is not None and res["done"] == 0
    # no error rows burned either — the row simply stays pending
    assert db.db.execute(
        "SELECT COUNT(*) FROM artifacts WHERE kind='extract_llm'"
    ).fetchone()[0] == 0
    db.close()


def test_circuit_resets_on_answered_call(tmp_path, monkeypatch):
    db = _ledger(tmp_path)
    db.save_messages([_message(mid=1, body="体温38度です")])
    _endpoint_dead(monkeypatch)
    for _ in range(mcs_util._CIRCUIT_TRIP):
        extract_llm.run_pending(db, limit=10, budget_s=30)
    assert mcs_util.circuit_open_s(db) > 0

    # server recovers before the cooldown ends: force-close the file
    # to simulate time passing, then a successful call clears state
    mcs_util.circuit_state_path(db).unlink()
    monkeypatch.setattr(extract_llm, "llm_extract",
                        lambda body, **_: {"summary": "ok"})
    res = extract_llm.run_pending(db, limit=10, budget_s=30)
    assert res["done"] == 1
    assert not mcs_util.circuit_state_path(db).exists()
    db.close()


def test_disk_guard_defers_llm_lane(tmp_path, monkeypatch):
    db = _ledger(tmp_path)
    db.save_messages([_message(mid=1, body="体温38度です")])
    monkeypatch.setenv("MCS_DISK_GUARD_MB", "999999999")
    calls = []
    monkeypatch.setattr(extract_llm, "llm_extract",
                        lambda body, **_: calls.append(body))

    res = extract_llm.run_pending(db, limit=10, budget_s=30)
    assert calls == [] and res["done"] == 0
    assert res["disk_free_mb"] is not None
    db.close()


def test_prefilter_still_settles_when_circuit_open(tmp_path, monkeypatch):
    """No-signal rows never needed the endpoint — markers still write
    while the breaker holds the LLM lane closed."""
    db = _ledger(tmp_path)
    db.save_messages([_message(mid=1, body="了解です"),
                      _message(mid=2, body="体温38度です")])
    mcs_util.circuit_state_path(db).write_text(
        json.dumps({"open_until": time.time() + 3600}))
    calls = []
    monkeypatch.setattr(extract_llm, "llm_extract",
                        lambda body, **_: calls.append(body))
    res = extract_llm.run_pending(db, limit=10, budget_s=30)
    assert res["skipped"] == 1 and calls == []
    meta = json.loads(_artifacts(db, 1)[0]["meta"])
    assert meta["prefilter"] == "no_signal"
    db.close()


# U05-F01: bodies the validator's own event cues recognise (death,
# fall, visit/exam wording) were settled as routine without an LLM
# read. Fully synthetic sentences.
@pytest.mark.parametrize("body", [
    "昨日亡くなりました。",
    "夜中に亡くなられました",
    "ベッドから落ちました",
    "廊下で倒れていました",
    "外来に行ってきました",
    "家に来てくださいました",
])
def test_event_cue_bodies_never_low_signal(body):
    import extract
    hints = extract.extract_message(body, "2026-09-20T10:00:00+09:00")
    assert not extract_llm._low_signal(body, hints)


def test_every_event_cue_match_is_not_low_signal():
    # invariant: the prefilter is a superset of the validator's cues
    for kind, cue in extract_llm._EVENT_CUES.items():
        for token in cue.pattern.replace("(?:", "").replace(")", "") \
                .split("|"):
            body = f"本日{token}の件"
            if cue.search(body):
                assert not extract_llm._low_signal(body, {"v": 1}), \
                    (kind, token)


def test_eol_body_reaches_llm_not_prefilter(tmp_path, monkeypatch):
    db = _ledger(tmp_path)
    db.save_messages([_message(mid=1, body="昨日亡くなりました。")])
    calls = []
    monkeypatch.setattr(
        extract_llm, "llm_extract",
        lambda body, **_: calls.append(body) or {"summary": "ok"})
    res = extract_llm.run_pending(db, limit=10, budget_s=30)
    assert res["skipped"] == 0 and calls == ["昨日亡くなりました。"]
    db.close()
