"""metadata_watch_targets のシグナル枝は、旧い相関EXISTSと同じ対象を返す。"""
import json

import pytest

import ledger as ledger_mod
from mcs_signals import _thresholds

NOW = 1_800_000_000.0
DAY = 86400

# The pre-optimization third OR branch, kept verbatim as the reference.
OLD_SIGNAL_BRANCH = """
  SELECT m.message_id FROM messages m
  JOIN patients p ON p.project_id=m.project_id
  WHERE p.is_archived=0 AND m.body_state IS NOT 'deleted'
    AND EXISTS(SELECT 1 FROM artifacts a,json_each(
      CASE WHEN json_valid(a.content) THEN a.content ELSE '{}' END,'$.evidence.message_ids') e
      WHERE a.kind='signal_v1' AND json_valid(a.content) AND json_valid(a.meta)
        AND a.project_id=m.project_id AND e.value=m.message_id
        AND json_extract(a.content,'$.type')='pharmacist_request_unanswered'
        AND (json_extract(a.content,'$.state')='open'
          OR (json_extract(a.content,'$.state')='resolved'
            AND m.posted_at_ts>=?
            AND EXISTS(SELECT 1 FROM message_metadata cap,json_each(
              CASE WHEN json_valid(cap.content) THEN cap.content ELSE '{}' END,
              '$.reactions.value') r
              WHERE cap.message_id=m.message_id AND cap.source='capture'
                AND json_extract(r.value,'$.self_reacted')=1
                AND json_extract(r.value,'$.type') IN ('accepted','completed'))))
        AND NOT EXISTS(SELECT 1 FROM artifacts newer
          WHERE newer.kind=a.kind AND json_valid(newer.meta)
            AND json_extract(newer.meta,'$.key')=json_extract(a.meta,'$.key')
            AND newer.artifact_id>a.artifact_id))
  ORDER BY m.message_id
"""


@pytest.fixture
def led(tmp_path):
    lg = ledger_mod.Ledger(str(tmp_path / "ledger.db"))
    yield lg
    lg.db.close()


def _msg(db, mid, pid=1, ts=NOW):
    db.execute("INSERT INTO messages(message_id,project_id,posted_at_ts,body_text,"
               "content_hash,body_state) VALUES(?,?,?,?,?,'full')",
               (mid, pid, ts, "合成", f"{mid:064x}"))


def _sig(db, key, state, mids, pid=1, typ="pharmacist_request_unanswered"):
    db.execute("INSERT INTO artifacts(kind,project_id,content,meta,created_at)"
               " VALUES('signal_v1',?,?,?,?)",
               (pid, json.dumps({"type": typ, "state": state,
                                 "evidence": {"message_ids": mids}}),
                json.dumps({} if key is None else {"key": key}), NOW))


def _self_react(db, mid, kind="accepted"):
    db.execute("INSERT INTO message_metadata(message_id,source,content,checked_at)"
               " VALUES(?,'capture',?,?)",
               (mid, json.dumps({"reactions": {"value": [
                   {"type": kind, "count": 1, "self_reacted": True}]}}), NOW))


def test_signal_branch_matches_correlated_reference(led):
    db = led.db
    db.execute("INSERT INTO patients(project_id,patient_name,is_archived) VALUES(1,'合成A',0)")
    db.execute("INSERT INTO patients(project_id,patient_name,is_archived) VALUES(2,'合成B',0)")
    old_ts = NOW - 400 * DAY  # outside any fyi_max_age_d window
    for mid in range(1, 15):
        _msg(db, mid, ts=old_ts if mid in (4, 9) else NOW)
    _msg(db, 20, pid=2)
    _sig(db, "open", "open", [1, 2])
    _sig(db, "resolved-in", "resolved", [3])          # self-reacted, recent
    _sig(db, "resolved-old", "resolved", [4])         # self-reacted, too old
    _sig(db, "resolved-noreact", "resolved", [5])
    _sig(db, "superseded", "open", [6])
    _sig(db, "superseded", "resolved", [7])           # latest wins; 6 dropped
    _sig(db, "reopened", "resolved", [8])
    _sig(db, "reopened", "open", [8, 9])
    _sig(db, "other-type", "open", [10], typ="family_concern")
    _sig(db, None, "open", [11])                      # no key: always latest
    _sig(db, None, "open", [12])
    _sig(db, "wrong-room", "open", [20], pid=1)       # project mismatch
    _sig(db, "string-id", "open", ["13"])
    for mid in (3, 4, 7):
        _self_react(db, mid)
    _self_react(db, 14, "viewed")
    _sig(db, "viewed", "resolved", [14])
    db.commit()

    cutoff = NOW - _thresholds(db)["fyi_max_age_d"] * DAY
    expected = [r[0] for r in db.execute(OLD_SIGNAL_BRANCH, (cutoff,))]
    got = [r["message_id"] for r in led.metadata_watch_targets(limit=-1, now=NOW)]
    assert got == expected
    assert got == [1, 2, 3, 7, 8, 9, 11, 12, 13]
