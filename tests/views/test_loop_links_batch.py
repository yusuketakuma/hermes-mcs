"""The batch loop evaluator reads request links once per project and gives each
candidate exactly the links the per-candidate read would (synthetic ledger)."""
import json

import request_loops
from test_loop_relation_rejections_view import _setup


def _request(db, title):
    cur = db.db.execute(
        "INSERT INTO requests(project_id,source_message_id,source_hash,title,status,revision,"
        "created_at,updated_at) VALUES(1,1,'h',?,'open',1,1,1)", (title,))
    return cur.lastrowid


def _link(db, content):
    db.db.execute("INSERT INTO artifacts(kind,project_id,message_id,content,model,meta,created_at) "
                  "VALUES('request_loop_link',1,1,?,'human','{}',1)", (content,))


def test_two_candidates_in_one_project_never_share_linked_requests(tmp_path):
    db, _, template, _, meta = _setup(tmp_path)
    try:
        base = json.loads(template["content"])
        db.artifact_add("loop_candidate", json.dumps({**base, "description": "合成依頼2"}),
                        project_id=1, message_id=1, meta=meta)
        rows = list(db.db.execute(
            "SELECT * FROM artifacts WHERE kind='loop_candidate' ORDER BY artifact_id"))
        first, second = (row["artifact_id"] for row in rows)
        with db.db:
            r1, r2, r3 = _request(db, "合成1"), _request(db, "合成2"), _request(db, "合成3")
            _link(db, json.dumps({"request_id": r1, "loop_artifact_id": first}))
            _link(db, json.dumps({"request_id": r2, "loop_artifact_id": second}))
            _link(db, json.dumps({"request_id": r3, "loop_artifact_id": first}))
            _link(db, json.dumps({"request_id": r1, "loop_artifact_id": str(first)}))  # text ref
            _link(db, '{"request_id": %d, "loop_artifact_id": %d' % (r2, second))      # broken
        trace = []
        db.db.set_trace_callback(trace.append)
        states = request_loops.effective_loop_states(db.db, rows,
                                                     policy=request_loops.latest_policy(db.db))
        db.db.set_trace_callback(None)
        linked = {s["artifact_id"]: s["linked_requests"] for s in states}
        for aid in (first, second):
            assert linked[aid] == request_loops.loop_links(db.db, 1, loop_id=aid)
        assert [x["request_id"] for x in linked[first]] == [r3, r1]          # newest link first
        assert [x["request_id"] for x in linked[second]] == [r2]
        assert sum("request_loop_link" in sql for sql in trace) == 1         # one read per project
    finally:
        db.close()
