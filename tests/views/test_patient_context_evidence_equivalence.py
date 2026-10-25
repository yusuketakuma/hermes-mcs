"""evidence() keeps read_model's detail records, snapshot and coverage while reading
only those sections, and the shared context inputs give the same patient context
(synthetic ledger only)."""
import json
import time

import pytest

import read_model
from patient_context import extract_context
from patient_context_view import evidence
from semantic_testkit import _message
from test_read_model import _artifact, _db, _hash_of

BODY = "病名: 架空病A\n既往歴: 以前の架空病B\nADL: 歩行は介助"


def _seed(db, mid, *, project_id=1):
    body = db.db.execute("SELECT body_text FROM messages WHERE message_id=?", (mid,)).fetchone()[0]
    if project_id == 1:
        _artifact(db, "extract_v1", mid, {"patient_context": extract_context(body)},
                  {"hash": _hash_of(db, mid)})
    else:                                       # a legacy row written before artifacts carried a project
        db.db.execute("INSERT INTO artifacts(kind,project_id,message_id,content,meta,created_at) "
                      "VALUES('extract_v1',NULL,?,?,?,?)",
                      (mid, json.dumps({"patient_context": extract_context(body)}),
                       json.dumps({"hash": _hash_of(db, mid)}), time.time()))
        db.db.commit()


@pytest.mark.parametrize("case", ["current", "deleted", "empty_body", "legacy"])
def test_evidence_matches_the_full_detail_read(tmp_path, case):
    db = _db(tmp_path, ())
    try:
        db.save_messages([_message(1, body=BODY), _message(2, body="病名: 架空別投稿病")], project_id=1)
        _seed(db, 1, project_id=None if case == "legacy" else 1)
        _seed(db, 2)
        if case == "deleted":
            db.db.execute("UPDATE messages SET body_state='deleted' WHERE message_id=2")
        elif case == "empty_body":
            db.db.execute("UPDATE messages SET body_text='' WHERE message_id=2")
        db.db.commit()
        full = read_model.read_model(db.db, scope="detail", project_id=1)
        ev = evidence(db.db, 1)
        assert ev["snapshot"] == full["snapshot"]
        assert ev["coverage"]["read_model"] == full["coverage"]
        chat = [(i["message_id"], i["category"], i["quote"]) for i in ev["items"]]
        expected = [(c["source"]["message_id"], c["category"], c["evidence"])
                    for r in full["records"] for c in r["patient_context"]]
        assert expected and all(chat.count(x) >= expected.count(x) for x in expected)
        for record in full["records"]:                # self-read inputs = the batch inputs
            bare = {k: v for k, v in record.items() if k != "body_text"}
            assert read_model.message_patient_context(db.db, bare, "detail") == record["patient_context"]
        if case in ("deleted", "empty_body"):
            assert not [x for x in chat if x[0] == 2]
    finally:
        db.close()
