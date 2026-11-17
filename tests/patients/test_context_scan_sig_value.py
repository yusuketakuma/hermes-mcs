"""The scan signature keeps its stored value while decoding each row once (synthetic table)."""

import json
import sqlite3

import patient_context_refresh_state as state
from inference_version import CURRENT_VERSION, VERSION_KEY, current_meta_pred
from mcs_util import loads_dict, valid_hash


def reference(db, pid, source_keys=None):
    """Frozen copy of the pre-change algorithm: the value stored in scan_sig must not move."""
    rows = [tuple(r) for r in db.execute(
        "SELECT message_id,content,meta FROM artifacts WHERE kind IN (?,?) AND project_id=? AND "
        + current_meta_pred("meta", target=CURRENT_VERSION) + " ORDER BY artifact_id", (state.SCAN, state.CHUNK, pid))]
    if source_keys is not None:
        selected = set(source_keys)
        rows = [row for row in rows if loads_dict(row[2]) is None
                or (loads_dict(row[2]) or {}).get('source_key', row[0]) in selected]
    keys = {(loads_dict(content) or {}).get("accepted_candidate_ref", {}).get("repair_input_key")
            for mid, content, meta in rows if isinstance((loads_dict(content) or {}).get("accepted_candidate_ref"), dict)
            and valid_hash((loads_dict(content) or {})["accepted_candidate_ref"].get("repair_input_key"))}
    for key in sorted(keys):
        rows.extend(tuple(row) for row in db.execute("SELECT message_id,content,meta FROM artifacts WHERE kind=? "
            "AND project_id=? AND CASE WHEN json_valid(meta) THEN json_extract(meta,'$.repair_input_key') END=? "
            "ORDER BY artifact_id", (state.SOURCE_REPAIR_CANDIDATE, pid, key)))
    return state._digest(rows)


def build():
    db = sqlite3.connect(':memory:')
    db.execute('CREATE TABLE artifacts(artifact_id INTEGER PRIMARY KEY, kind TEXT, project_id INT, message_id INT, content TEXT, meta TEXT)')
    key = 'a' * 64

    def add(kind, mid, content, meta, pid=1):
        db.execute('INSERT INTO artifacts(kind,project_id,message_id,content,meta) VALUES(?,?,?,?,?)',
                   (kind, pid, mid, content, meta))
    add(state.SCAN, 1, json.dumps({'accepted_candidate_ref': {'repair_input_key': key}}), json.dumps({VERSION_KEY: CURRENT_VERSION, 'source_key': 1}))
    add(state.SCAN, 2, '{}', json.dumps({VERSION_KEY: CURRENT_VERSION, 'source_key': 'x'}))
    add(state.CHUNK, 3, 'not json', json.dumps({VERSION_KEY: CURRENT_VERSION}))
    add(state.CHUNK, 4, '{"accepted_candidate_ref": 5}', json.dumps({VERSION_KEY: CURRENT_VERSION, 'source_key': 9}))
    add(state.SCAN, 5, '{}', json.dumps({VERSION_KEY: CURRENT_VERSION, 'source_key': 1}), pid=2)
    add(state.SOURCE_REPAIR_CANDIDATE, 1, '{"c":1}', json.dumps({'repair_input_key': key}))
    return db


def test_scan_sig_value_is_unchanged_and_each_row_decodes_once(monkeypatch):
    db = build()
    for keys in (None, [1], ['x'], [9], [], [1, 'x', 9]):
        assert state._scan_sig(db, 1, source_keys=keys) == reference(db, 1, keys)
    calls = []
    real = state.loads_dict
    monkeypatch.setattr(state, 'loads_dict', lambda v: calls.append(v) or real(v))
    state._scan_sig(db, 1, source_keys=[1])
    assert len(calls) <= 2 * 4  # 4 current rows x (meta + content), was up to 6 per row
