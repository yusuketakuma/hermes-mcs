"""Canonical-fact coverage in the patient rollup (T5).

The rollup prefers canonical_projection over extract_llm via
current_fact_pred; every verified fact must remain enumerable with
fact_id/evidence — newest generation wins per fact_id.
"""
import json

import pytest

from extract_testkit import _ledger, _message, _hash
import rollup
from semantic_projection import PROJECTION_VERSION


@pytest.fixture
def db(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.ensure_patient(1)
    yield ledger
    ledger.close()


def _fact(fid, kind, statement, quote="引用"):
    return {"fact_id": fid, "kind": kind, "statement": statement,
            "subject": "patient:1", "evidence_quote": quote,
            "evidence_ids": ["ev"], "importance": "T1",
            "validation_status": "verified"}


def test_rollup_and_cached_reader_do_not_resurrect_contradictory_vitals(db):
    body = "本人の血圧120/80を測定しました。"
    db.save_messages([_message(mid=1, body=body,
                               posted_at="2026-10-09T00:00:00+09:00")])
    db.artifact_add("extract_llm", json.dumps({"vitals": {"sbp": 180, "dbp": 80}}),
                    project_id=1, message_id=1, meta={"hash": _hash(db, 1)})
    current = rollup.build_rollup(db, 1)
    assert current["latest_vitals"] == {"at": "2026-10-09T00:00:00+09:00", "dbp": 80}
    old = {**current, "latest_vitals": {"at": "2026-10-09T00:00:00+09:00",
                                      "sbp": 180, "dbp": 80},
           "profile_extension": {"text": "架空の補足"},
           "medications": [{"name": "架空の旧薬", "dose": "旧形式を保持"}]}
    meta = {"period_check_version": rollup.PERIOD_CHECK_VERSION - 1}
    before = db.db.total_changes
    safe = rollup.current_cached_refs(db.db, 1, old, meta)
    assert safe["latest_vitals"] == current["latest_vitals"]
    assert safe["profile_extension"] == old["profile_extension"]
    assert safe["medications"] == old["medications"]
    assert db.db.total_changes == before


def _add(db, mid, kind, content, posted):
    db.save_messages([_message(mid=mid, body="合成本文",
                               posted_at=posted)])
    meta = {"hash": _hash(db, mid)}
    if kind in ("canonical_projection", "semantic_facts_v4"):
        meta["projection_version"] = PROJECTION_VERSION
    db.artifact_add(kind, json.dumps(content), project_id=1,
                    message_id=mid, meta=meta)


def test_rollup_collects_canonical_facts_newest_first(db):
    _add(db, 1, "canonical_projection",
         {"canonical_facts": [
             _fact("f_old", "allergy_intolerance", "旧アレルギー"),
             _fact("f_same", "vital_lab", "BP 110/70")]},
         "2026-09-18T00:00:00+09:00")
    _add(db, 2, "canonical_projection",
         {"canonical_facts": [
             _fact("f_same", "vital_lab", "BP 130/85", quote="130/85"),
             _fact("f_new", "adverse_drug_event", "嘔気", quote="嘔気")]},
         "2026-09-19T00:00:00+09:00")
    out = rollup.build_rollup(db, 1)
    facts = out["canonical_facts"]
    by_id = {f["fact_id"]: f for f in facts}
    assert set(by_id) == {"f_old", "f_same", "f_new"}
    # newest generation wins per fact_id
    assert by_id["f_same"]["statement"] == "BP 130/85"
    assert by_id["f_same"]["last"].startswith("2026-09-19")
    assert by_id["f_old"]["last"].startswith("2026-09-18")
    # evidence + kind ride along for the readers
    assert by_id["f_new"]["kind"] == "adverse_drug_event"
    assert by_id["f_new"]["evidence"] == "嘔気"


def test_symptom_dates_reuse_first_timestamp_row_without_rescanning(db, monkeypatch):
    for mid in (1, 2, 3, 4):
        _add(db, mid, "extract_v1", {"symptoms": [f"合成症状{mid}"]},
             f"2026-09-{mid:02d}T00:00:00+09:00")
    db.db.execute("UPDATE messages SET posted_at_ts=42 WHERE message_id IN (2,3)")
    db.db.execute("UPDATE messages SET posted_at_ts=NULL,posted_at='' "
                  "WHERE message_id IN (1,4)")
    msgs = db.db.execute("SELECT message_id,posted_at_ts,posted_at FROM messages "
                         "ORDER BY posted_at_ts DESC,message_id DESC").fetchall()
    expected = [{"symptom": f"合成症状{m['message_id']}",
                 "last": rollup.msgs_by_ts(msgs, m["posted_at_ts"] or 0)}
                for m in msgs]

    def no_rescan(*args):
        raise AssertionError("symptom date must reuse timestamp lookup")

    monkeypatch.setattr(rollup, "msgs_by_ts", no_rescan)
    assert rollup.build_rollup(db, 1)["recent_symptoms"] == expected
    assert [row["last"] for row in expected] == [
        "2026-09-03T00:00:00+09:00", "2026-09-03T00:00:00+09:00", "msg:4", "msg:4"]


def test_rollup_canonical_shadows_and_stale_falls_back(db):
    """current_fact_pred: a hash-current projection owns the fact
    source; once the revision changes it drops out and the legacy row
    is read instead."""
    _add(db, 1, "extract_llm", {"meds": [{"name": "旧薬"}]},
         "2026-09-18T00:00:00+09:00")
    _add(db, 2, "canonical_projection",
         {"meds": [{"name": "新薬", "action": "stop"}],
          "canonical_facts": [_fact("f1", "preference", "午前希望")]},
         "2026-09-19T00:00:00+09:00")
    out = rollup.build_rollup(db, 1)
    assert [f["fact_id"] for f in out["canonical_facts"]] == ["f1"]

    # body revision -> every stored artifact hash goes stale
    db.db.execute("UPDATE messages SET body_text='改訂本文',"
                  " content_hash='newhash' WHERE message_id=2")
    db.db.commit()
    out = rollup.build_rollup(db, 1)
    assert out.get("canonical_facts", []) == []


def test_rollup_skips_unlisted_canonical_entries(db):
    """Malformed canonical_facts entries never reach the read model."""
    _add(db, 1, "canonical_projection",
         {"canonical_facts": [
             {"statement": "fact_idなし"},          # no fact_id
             "not-a-dict",
             _fact("f1", "vital_lab", "BP 120/80")]},
         "2026-09-18T00:00:00+09:00")
    out = rollup.build_rollup(db, 1)
    assert [f["fact_id"] for f in out["canonical_facts"]] == ["f1"]


@pytest.mark.parametrize("invalid", ["foreign_patient", "deleted"])
def test_rule_rollup_requires_current_patient_source(db, invalid):
    _add(db, 1, "extract_v1", {"vitals": {"temp": 38.1}},
         "2026-09-18T00:00:00+09:00")
    if invalid == "foreign_patient":
        # Deliberate old-row corruption for the reader-defense assertion.
        db.db.execute("DROP TRIGGER g1_artifacts_msg_upd")
        db.db.execute("UPDATE artifacts SET project_id=2")
    else:
        db.db.execute("UPDATE messages SET body_state='deleted'")
    assert "latest_vitals" not in rollup.build_rollup(db, 1)


def test_malformed_lab_container_keeps_other_facts(db):
    _add(db, 1, "extract_llm", {"labs": 7, "summary": "合成要約"},
         "2026-09-18T00:00:00+09:00")
    assert rollup.build_rollup(db, 1)["summary"]["text"] == "合成要約"


@pytest.mark.parametrize("kind", ["extract_v1", "extract_llm", "canonical_projection"])
def test_rollup_sql_valid_source_at_python_depth_limit_is_safe(db, kind):
    _add(db, 1, kind, {"vitals": {"temp": 37.1}}, "2026-09-18T00:00:00+09:00")
    assert rollup.build_rollup(db, 1)["latest_vitals"]["temp"] == 37.1
    source_id, original = db.db.execute("SELECT artifact_id,content FROM artifacts").fetchone()
    deep = original[:-1] + ',"synthetic_unused":' + "[" * 999 + "0" + "]" * 999 + "}"
    assert db.db.execute("SELECT json_valid(?)", (deep,)).fetchone()[0] == 1
    try:
        json.loads(deep)
    except RecursionError:
        readable = False
    else:
        readable = True
    db.db.execute("UPDATE artifacts SET content=? WHERE artifact_id=?", (deep, source_id))
    db.db.commit()
    before = tuple(db.db.iterdump())
    changes = db.db.total_changes
    out = rollup.build_rollup(db, 1)
    assert out["msg_count"] == 1
    if readable:
        assert out["latest_vitals"]["temp"] == 37.1
    else:
        assert "latest_vitals" not in out
    assert tuple(db.db.iterdump()) == before
    assert db.db.total_changes == changes


@pytest.mark.parametrize(("field", "value"), [
    ("generated_at", "broken"), ("generated_at", float("inf")),
    ("next_med_period_check", "broken"),
])
def test_invalid_rollup_schedule_is_rebuilt(db, field, value):
    _add(db, 1, "extract_v1", {}, "2026-09-18T00:00:00+09:00")
    rollup.rebuild(db, 1)
    row = db.db.execute("SELECT meta FROM artifacts WHERE kind=?",
                        (rollup.KIND,)).fetchone()
    meta = json.loads(row[0])
    meta[field] = value
    db.db.execute("UPDATE artifacts SET meta=? WHERE kind=?",
                  (json.dumps(meta), rollup.KIND))
    assert rollup.dirty_projects(db) == [1]


@pytest.mark.parametrize("flag", [True, "false", 0, None, "yes"])
def test_rollup_keeps_item_unverified_flag(db, flag):
    """Unverified requests keep their flag through the rollup; anything
    but a literal False (or a missing key) fails closed (todo 15)."""
    _add(db, 1, "canonical_projection",
         {"requests": [
             {"to": "SYNTH-医師", "action": "確認済み依頼",
              "unverified": False},
             {"to": "SYNTH-薬局", "action": "未確認依頼",
              "unverified": flag},
             {"to": "SYNTH-訪看", "action": "フラグ無し依頼"}]},
         "2026-09-19T00:00:00+09:00")
    out = rollup.build_rollup(db, 1)
    assert [(r["ctx"], r["unverified"]) for r in out["recent_requests"]] \
        == [("確認済み依頼", False), ("未確認依頼", True),
            ("フラグ無し依頼", False)]


@pytest.mark.parametrize("kind", ["extract_llm", "canonical_projection"])
@pytest.mark.parametrize("flag", [True, "false", 0, None, [], {}])
def test_rollup_keeps_lab_verification_markers(db, kind, flag):
    labs = [{"name": "合成確認検査", "value": 1, "unverified": False},
            {"name": "合成候補検査", "value": 2, "unverified": flag},
            {"name": "合成旧形式検査", "value": 3}]
    _add(db, 1, kind, {"labs": labs}, "2026-10-01T00:00:00+09:00")
    out = rollup.build_rollup(db, 1)
    assert out["recent_labs"] == [
        {"at": "2026-10-01T00:00:00+09:00", **lab} for lab in labs]


def test_rollup_carries_request_kind_condition_due_text(db):
    """#20 order 3: LLM request rows expose kind/condition/due_text under
    NEW keys (req_kind/condition/due_text); kind (=to) and ctx are
    untouched, rows without them gain no keys, and no version bump means
    the rebuilt rollup is not re-dirtied."""
    _add(db, 1, "canonical_projection",
         {"requests": [
             {"to": "SYNTH-看護師", "action": "医師へ連絡", "kind": "request",
              "condition": "血圧が160を超えるようなら", "due": None,
              "due_text": "明日まで"},
             {"to": "SYNTH-医師", "action": "素の依頼"}]},
         "2026-09-19T00:00:00+09:00")
    out = rollup.build_rollup(db, 1)
    typed, plain = out["recent_requests"]
    assert (typed["kind"], typed["ctx"]) == ("SYNTH-看護師", "医師へ連絡")
    assert (typed["req_kind"], typed["condition"], typed["due_text"]) \
        == ("request", "血圧が160を超えるようなら", "明日まで")
    assert set(plain) == {"kind", "ctx", "at", "mid", "unverified"}
    rollup.rebuild(db, 1)
    assert rollup.dirty_projects(db) == []


def _thread(db, rows):
    """rows: (mid, parent, sender, posted, content) -> messages + LLM
    artifacts; the root request row is mid 1 by SYNTH-A."""
    for mid, parent, sender, posted, content in rows:
        msg = _message(mid=mid, body="合成本文", parent_id=parent,
                       posted_at=posted)
        msg.sender_name = sender
        msg.sender_id = {"SYNTH-A": 1, "SYNTH-B": 2, "SYNTH-C": 3}[sender]
        db.save_messages([msg])
        db.artifact_add("extract_llm", json.dumps(content), project_id=1,
                        message_id=mid, meta={"hash": _hash(db, mid)})


_REQ = {"requests": [{"to": "SYNTH-医師", "action": "処方変更を確認"}]}


def _reply(kind):
    return {"reply": {"kind": kind, "evidence": "合成本文"}}


@pytest.mark.parametrize(("replies", "state", "conflict"), [
    ([(2, 1, "SYNTH-B", "T01:00", _reply("done"))], "done", False),
    # the requester's own 「承知しました」 never answers the request
    ([(2, 1, "SYNTH-A", "T01:00", _reply("ack"))], None, False),
    ([(2, 1, "SYNTH-B", "T01:00", _reply("ack")),
      (3, 1, "SYNTH-B", "T02:00", _reply("done"))], "done", False),
    ([(2, 1, "SYNTH-B", "T01:00", _reply("done")),
      (3, 1, "SYNTH-C", "T02:00", _reply("cancel"))], "cancel", True),
    # done -> cancel -> done: the cancel is later than the FIRST done,
    # so it still conflicts (the walk is newest-first)
    ([(2, 1, "SYNTH-B", "T01:00", _reply("done")),
      (3, 1, "SYNTH-C", "T02:00", _reply("cancel")),
      (4, 1, "SYNTH-B", "T03:00", _reply("done"))], "cancel", True),
    # reply on an unrelated thread (root 9) is not this request's
    ([(9, None, "SYNTH-B", "T01:00", {}),
      (10, 9, "SYNTH-C", "T02:00", _reply("done"))], None, False),
    # a reply posted BEFORE the request never counts
    ([(2, 1, "SYNTH-B", "T00:00", _reply("done"))], None, False),
])
def test_rollup_thread_reply_state(db, replies, state, conflict):
    """#20-C order 4: thread-level, view-only reply_state on LLM request
    rows — strongest later reply kind from another sender in the same
    thread; cancel after done also flags reply_conflict."""
    day = "2026-09-19"
    _thread(db, [(1, None, "SYNTH-A", f"{day}T00:30:00+09:00", _REQ)] + [
        (mid, parent, sender, f"{day}{hm}:00+09:00", content)
        for mid, parent, sender, hm, content in replies])
    rows = rollup.build_rollup(db, 1)["recent_requests"]
    assert len(rows) == 1 and rows[0]["mid"] == 1
    assert rows[0].get("reply_state") == state
    assert rows[0].get("reply_conflict", False) is conflict
    assert rows[0]["unverified"] is False


def test_rollup_reply_survives_canonical_projection_shadowing(db):
    """A current canonical_projection shadows extract_llm as the fact
    source for that message, but reply is read from extract_llm itself
    (no other engine emits it), so an audited reply still counts."""
    _thread(db, [(1, None, "SYNTH-A", "2026-09-19T00:30:00+09:00", _REQ),
                 (2, 1, "SYNTH-B", "2026-09-19T01:00:00+09:00",
                  _reply("done"))])
    db.artifact_add("canonical_projection", json.dumps({"canonical_facts": []}),
                    project_id=1, message_id=2,
                    meta={"hash": _hash(db, 2), "projection_version": PROJECTION_VERSION})
    rows = rollup.build_rollup(db, 1)["recent_requests"]
    assert rows[0]["reply_state"] == "done"


def test_rollup_reply_work_is_bounded_by_returned_requests(db, monkeypatch):
    """Mixed rule/LLM requests keep their cap/order; old rows need no scan."""
    requests = [{"to": "SYNTH", "action": f"合成依頼{i}"} for i in range(120)]
    _thread(db, [(1, None, "SYNTH-A", "2026-09-19T00:30:00+09:00",
                 {"requests": requests}),
                (2, 1, "SYNTH-B", "2026-09-19T01:00:00+09:00", _reply("done"))])
    db.artifact_add("extract_v1", json.dumps(
        {"requests": [{"kind": "rule", "ctx": f"合成ルール{i}"} for i in range(4)]}),
        project_id=1, message_id=1, meta={"hash": _hash(db, 1)})
    calls = []

    class Stages(dict):
        def get(self, key, default=None):
            calls.append(key)
            return super().get(key, default)

    monkeypatch.setattr(rollup, "_REPLY_STAGE", Stages(rollup._REPLY_STAGE))
    rows = rollup.build_rollup(db, 1)["recent_requests"]
    assert [row["ctx"] for row in rows] == (
        [f"合成ルール{i}" for i in range(4)] + [f"合成依頼{i}" for i in range(11)])
    assert all(row["reply_state"] == "done" for row in rows[4:])
    assert calls == ["done"] * 11


def test_rollup_sql_valid_shadowed_reply_at_python_depth_limit_is_safe(db):
    _thread(db, [(1, None, "SYNTH-A", "2026-09-19T00:30:00+09:00", _REQ),
                 (2, 1, "SYNTH-B", "2026-09-19T01:00:00+09:00", _reply("done"))])
    db.artifact_add("canonical_projection", json.dumps({"canonical_facts": []}),
                    project_id=1, message_id=2,
                    meta={"hash": _hash(db, 2), "projection_version": PROJECTION_VERSION})
    source_id, original = db.db.execute(
        "SELECT artifact_id,content FROM artifacts WHERE kind='extract_llm' AND message_id=2"
    ).fetchone()
    deep = original[:-1] + ',"synthetic_unused":' + "[" * 999 + "0" + "]" * 999 + "}"
    assert db.db.execute("SELECT json_valid(?)", (deep,)).fetchone()[0] == 1
    try:
        json.loads(deep)
    except RecursionError:
        readable = False
    else:
        readable = True
    db.db.execute("UPDATE artifacts SET content=? WHERE artifact_id=?", (deep, source_id))
    db.db.commit()
    before = tuple(db.db.iterdump())
    changes = db.db.total_changes
    rows = rollup.build_rollup(db, 1)["recent_requests"]
    assert rows[0].get("reply_state") == ("done" if readable else None)
    assert tuple(db.db.iterdump()) == before
    assert db.db.total_changes == changes


def test_rollup_ignores_non_string_reply_kind(db):
    """A foreign/hand-written extract_llm row with reply.kind as a list
    must not abort the patient's rollup (unhashable in the stage set)."""
    _thread(db, [(1, None, "SYNTH-A", "2026-09-19T00:30:00+09:00", _REQ),
                 (2, 1, "SYNTH-B", "2026-09-19T01:00:00+09:00",
                  {"reply": {"kind": ["done"], "evidence": "x"}})])
    rows = rollup.build_rollup(db, 1)["recent_requests"]
    assert rows[0].get("reply_state") is None


def test_rollup_reply_with_unparseable_posted_at_is_never_later(db):
    """NULL posted_at_ts maps to 0 and can never be 'later' than the
    request, so a reply with an unparseable date sets nothing."""
    _thread(db, [(1, None, "SYNTH-A", "2026-09-19T00:30:00+09:00", _REQ),
                 (2, 1, "SYNTH-B", "not-a-date", _reply("done"))])
    rows = rollup.build_rollup(db, 1)["recent_requests"]
    assert "reply_state" not in rows[0]


@pytest.mark.parametrize(("request_id", "reply_id", "same_name", "state"), [
    (1, 2, True, "done"),       # identical display names, distinct people
    (1, 1, False, None),        # renamed sender, same person
    (None, 2, False, None),     # unknown identities cannot prove a reply
    (1, None, False, None),
    (None, None, False, None),
    (0, 2, False, None),
])
def test_rollup_reply_uses_sender_ids(db, request_id, reply_id, same_name, state):
    _thread(db, [(1, None, "SYNTH-A", "2026-09-19T00:30:00+09:00", _REQ),
                 (2, 1, "SYNTH-B", "2026-09-19T01:00:00+09:00",
                  _reply("done"))])
    db.db.execute("UPDATE messages SET sender_id=? WHERE message_id=1",
                  (request_id,))
    db.db.execute("UPDATE messages SET sender_id=? WHERE message_id=2",
                  (reply_id,))
    if same_name:
        db.db.execute("UPDATE messages SET sender_name='SYNTH-A'")
    assert rollup.build_rollup(db, 1)["recent_requests"][0].get("reply_state") \
        == state


def test_rollup_old_name_based_reply_is_rebuilt(db):
    _thread(db, [(1, None, "SYNTH-A", "2026-09-19T00:30:00+09:00", _REQ),
                 (2, 1, "SYNTH-B", "2026-09-19T01:00:00+09:00",
                  _reply("done"))])
    rollup.rebuild(db, 1)
    db.db.execute("UPDATE messages SET sender_id=1 WHERE message_id=2")
    db.db.execute("UPDATE artifacts SET meta=json_set(meta,"
                  " '$.period_check_version',2) WHERE kind=?", (rollup.KIND,))
    assert rollup.dirty_projects(db) == [1]
    rollup.rebuild(db, 1)
    content = db.db.execute("SELECT content FROM artifacts WHERE kind=?",
                            (rollup.KIND,)).fetchone()[0]
    assert "reply_state" not in json.loads(content)["recent_requests"][0]
    assert rollup.dirty_projects(db) == []


def test_pre_flag_rollup_is_rebuilt_with_unverified_flag(db):
    """A rollup persisted before todo 15 (version-1 meta, no
    'unverified' keys) is dirty once and rebuilt with the flag; the
    rebuilt row is not re-dirtied (no rebuild loop)."""
    _add(db, 1, "canonical_projection",
         {"requests": [{"to": "SYNTH-薬局", "action": "未確認依頼",
                        "unverified": True}]},
         "2026-09-19T00:00:00+09:00")
    rollup.rebuild(db, 1)
    content, meta = db.db.execute(
        "SELECT content, meta FROM artifacts WHERE kind=?",
        (rollup.KIND,)).fetchone()
    content, meta = json.loads(content), json.loads(meta)
    for r in content["recent_requests"]:
        del r["unverified"]
    meta["period_check_version"] = 1
    db.db.execute("UPDATE artifacts SET content=?, meta=? WHERE kind=?",
                  (json.dumps(content), json.dumps(meta), rollup.KIND))
    assert rollup.dirty_projects(db) == [1]

    for _ in range(2):  # an interrupted tick retrying stays idempotent
        rollup.rebuild(db, 1)
        rows = db.db.execute("SELECT content FROM artifacts WHERE kind=?",
                             (rollup.KIND,)).fetchall()
        assert len(rows) == 1
        assert [r["unverified"] for r in
                json.loads(rows[0][0])["recent_requests"]] == [True]
        assert rollup.dirty_projects(db) == []


@pytest.mark.parametrize("shadow, decodes", [(False, 2), (True, 4)])
def test_rollup_decodes_each_llm_blob_once(db, monkeypatch, shadow, decodes):
    """perf(#20): reply is read from the extract_llm row, but when that
    row is also the fact source its dict is reused — one decode per
    message (two only when a canonical_projection shadows it); the
    reply_state read is identical either way."""
    _thread(db, [(1, None, "SYNTH-A", "2026-09-19T00:30:00+09:00", _REQ),
                 (2, 1, "SYNTH-B", "2026-09-19T01:00:00+09:00",
                  _reply("done"))])
    if shadow:
        for mid in (1, 2):
            db.artifact_add("canonical_projection", json.dumps(
                {"requests": _REQ["requests"]} if mid == 1 else {}),
                project_id=1, message_id=mid,
                meta={"hash": _hash(db, mid), "projection_version": PROJECTION_VERSION})
    calls = []
    real = json.loads
    monkeypatch.setattr(rollup.json, "loads",
                        lambda s, *a, **k: calls.append(s) or real(s, *a, **k))
    rows = rollup.build_rollup(db, 1)["recent_requests"]
    assert rows[0]["reply_state"] == "done"
    assert len(calls) == decodes


# ---------- 連携サマリー (#21) ----------------------------------------------

def _summary(comment="合成サマリー"):
    return {"comment": comment, "updated_at": "2026-09-30T10:00:00+09:00",
            "is_editable": True,
            "user": {"profession": "看護師", "name": "合成 花子"}}


def test_rollup_carries_karte_summary_or_none(db):
    db.save_messages([_message(mid=1, body="合成本文",
                               posted_at="2026-09-01T09:00:00+09:00")])
    assert rollup.build_rollup(db, 1)["karte_summary"] is None
    db.karte_summary_store(1, 10, None)                    # unregistered
    ks = rollup.build_rollup(db, 1)["karte_summary"]
    assert ks["empty"] is True and ks["comment"] is None
    assert isinstance(ks["fetched_at"], float)
    db.karte_summary_store(1, 10, _summary())
    ks = rollup.build_rollup(db, 1)["karte_summary"]
    assert ks == {"comment": "合成サマリー",
                  "updated_at": "2026-09-30T10:00:00+09:00",
                  "updater_profession": "看護師", "empty": False,
                  "fetched_at": ks["fetched_at"]}
    assert "合成 花子" not in json.dumps(ks, ensure_ascii=False)


def test_new_karte_summary_dirties_rollup(db):
    db.save_messages([_message(mid=1, body="合成本文",
                               posted_at="2026-09-01T09:00:00+09:00")])
    rollup.rebuild(db, 1)
    assert rollup.dirty_projects(db) == []
    db.karte_summary_store(1, 10, _summary())
    assert rollup.dirty_projects(db) == [1]
    rollup.rebuild(db, 1)
    assert rollup.dirty_projects(db) == []
