"""structured_lines reads the source row once per message and renders exactly what
the self-reading urgency path renders (synthetic ledger only)."""
import sqlite3

import pytest

import clinical_urgency
import structured_view
from semantic_testkit import _message
from test_read_model import _artifact, _db, _hash_of

BODY = "本人が急変、至急確認してください。血圧 180/100。"
CASES = {
    "high": ({"urgency": "high", "urgency_evidence": ["本人が急変"]}, None, "full", "patient"),
    "routine": ({"urgency": "routine", "summary": "合成の定期報告"}, None, "full", "patient"),
    "unclear": ({"urgency": "unclear", "summary": "合成の確認"}, None, "full", "patient"),
    "rule_high": ({"summary": "合成"}, {"urgency": "high"}, "full", "patient"),
    "group": ({"urgency": "high", "urgency_evidence": ["本人が急変"]}, None, "full", "group"),
    "snippet": ({"urgency": "high", "urgency_evidence": ["本人が急変"]}, None, "snippet", "patient"),
}


def _seed(tmp_path, llm, rule, body_state, project_type):
    db = _db(tmp_path, ())
    db.save_messages([_message(1, body=BODY)], project_id=1)
    _artifact(db, "extract_llm", 1, llm, {"hash": _hash_of(db, 1)})
    if rule is not None:
        _artifact(db, "extract_v1", 1, rule, {"hash": _hash_of(db, 1)})
    db.db.execute("UPDATE messages SET body_state=? WHERE message_id=1", (body_state,))
    db.db.execute("UPDATE patients SET project_type=? WHERE project_id=1", (project_type,))
    db.db.commit()
    return db


def _self_reading(monkeypatch):
    """The pre-sharing oracle: urgency details read facts, rule and source themselves."""
    real = clinical_urgency.message_urgency_details
    monkeypatch.setattr(clinical_urgency, "message_urgency_details", lambda db, mid, **_: real(db, mid))


@pytest.mark.parametrize("case", sorted(CASES))
def test_one_source_read_and_identical_lines(tmp_path, monkeypatch, case):
    db = _seed(tmp_path, *CASES[case])
    try:
        trace = []
        db.db.set_trace_callback(trace.append)
        shared = structured_view.structured_lines(db.db, 1)
        db.db.set_trace_callback(None)
        assert sum("table_info(patients)" in sql for sql in trace) == 1
        with monkeypatch.context() as m:
            _self_reading(m)
            assert structured_view.structured_lines(db.db, 1) == shared
    finally:
        db.close()


def test_without_db_the_context_default_reads_nothing(monkeypatch):
    # structured_lines itself needs a connection; the db-less helper keeps its default
    calls = []
    monkeypatch.setattr(structured_view, "_source_row", lambda *a: calls.append(a))
    assert structured_view._source_context(None, 1) == (None, None, "patient")
    assert calls == []


def test_a_missing_patients_table_keeps_the_old_urgency_read(tmp_path):
    db = _seed(tmp_path, *CASES["high"])
    try:
        db.db.execute("PRAGMA foreign_keys=OFF")
        db.db.execute("DROP TABLE patients")
        with pytest.raises(sqlite3.OperationalError):            # as message_urgency_source always did
            structured_view.structured_lines(db.db, 1)
    finally:
        db.close()
