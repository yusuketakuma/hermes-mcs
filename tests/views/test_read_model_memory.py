"""Whole-scope coverage must not retain all historical artifact bodies."""
import gc
import json
import sqlite3
import tracemalloc

import read_model


def _page_peak(messages):
    db = sqlite3.connect(':memory:')
    db.row_factory = sqlite3.Row
    db.executescript('''
        CREATE TABLE messages(message_id INTEGER PRIMARY KEY,project_id INTEGER,
            parent_id INTEGER,posted_at_ts INTEGER,body_state TEXT,
            content_hash TEXT,body_text TEXT);
        CREATE TABLE artifacts(artifact_id INTEGER PRIMARY KEY,message_id INTEGER,
            project_id INTEGER,kind TEXT,content TEXT,meta TEXT);
        CREATE INDEX idx_artifacts_kind_msg ON artifacts(kind,message_id);
    ''')
    content = json.dumps({'summary': 'SYNTHETIC ' * 450})
    meta = json.dumps({'hash': 'h'})
    db.executemany("INSERT INTO messages VALUES(?,1,NULL,1,'full','h','synthetic')",
                   ((mid,) for mid in range(messages)))
    db.executemany("INSERT INTO artifacts VALUES(?,?,1,'extract_llm',?,?)",
                   ((mid, mid, content, meta) for mid in range(messages)))
    gc.collect()
    tracemalloc.start()
    try:
        page = read_model.read_model(db, limit=1)
        peak = tracemalloc.get_traced_memory()[1]
        assert page['total'] == messages
        assert page['truncated'] is True
        assert len(page['records']) == 1
        assert page['records'][0]['message_id'] == 0
        assert page['coverage']['collection']['messages'] == messages
        assert page['coverage']['extraction']['extract_llm']['current'] == messages
        assert 'SYNTHETIC' not in json.dumps(page)
        return peak
    finally:
        tracemalloc.stop()
        db.close()


def test_limited_page_memory_does_not_scale_with_all_artifact_bodies():
    # Same row/body shape: doubling the archive previously doubled peak
    # allocation; the read now retains only one classification batch.
    assert _page_peak(2000) < _page_peak(1000) * 1.5
