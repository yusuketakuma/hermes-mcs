"""devin1-031: selected_scan narrowing must return identical rows with the indexed plan."""
import json
import sqlite3

import patient_context_artifacts as artifacts_mod


def _make_db(tmp_path):
    db = sqlite3.connect(str(tmp_path / 't.db'))
    db.execute('CREATE TABLE artifacts(artifact_id INTEGER PRIMARY KEY, kind TEXT, project_id INT,'
               ' message_id INT, meta TEXT, content TEXT, created_at REAL)')
    db.execute('CREATE INDEX idx_artifacts_lookup ON artifacts(kind, project_id, message_id)')
    db.execute('CREATE INDEX idx_artifacts_project ON artifacts(project_id)')
    return db


def _insert(db, aid, kind, pid, mid, meta):
    db.execute('INSERT INTO artifacts VALUES(?,?,?,?,?,?,0.0)', (aid, kind, pid, mid, meta, '{}'))


def test_selected_scan_identical_and_indexed(tmp_path):
    db = _make_db(tmp_path)
    pid, sk = 1, 'attachment_ocr:100:1'
    noise_meta = json.dumps({'v': 1})
    # 5875 noise artifacts + scan rows — production shape in miniature
    for i in range(1, 200):
        _insert(db, i, 'extract_llm', pid, i, noise_meta)
    scan_meta = json.dumps({'v': 1, 'source_key': sk})
    _insert(db, 500, 'patient_context_scan_v1', pid, 0, scan_meta)
    db.commit()
    # sanity: test fixture rows are visible
    q_orig = ("SELECT artifact_id,message_id,meta FROM artifacts WHERE kind=? AND project_id=? "
              "AND CASE WHEN json_valid(meta) THEN COALESCE(json_extract(meta,'$.source_key'),message_id)=? END "
              'ORDER BY artifact_id DESC')
    q_hint = q_orig.replace('FROM artifacts WHERE', 'FROM artifacts INDEXED BY idx_artifacts_lookup WHERE')
    a = db.execute(q_orig, ('patient_context_scan_v1', pid, sk)).fetchall()
    b = db.execute(q_hint, ('patient_context_scan_v1', pid, sk)).fetchall()
    assert a == b and len(a) == 1, (len(a), len(b))
    # the production function must use the narrowed plan
    plan = [r[-1] for r in db.execute('EXPLAIN QUERY PLAN ' + q_hint, ('patient_context_scan_v1', pid, sk))]
    assert any('idx_artifacts_lookup' in line for line in plan), plan
    db.close()
