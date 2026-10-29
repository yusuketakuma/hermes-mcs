"""Synthetic deadline deferral preserves source fences and transaction ownership."""
from contextlib import closing
import sqlite3

import pytest

import rollup
from test_inference_v1_rollup import _seed


def _stored(ledger):
    return [tuple(row) for row in ledger.db.execute(
        "SELECT artifact_id,content,meta FROM artifacts WHERE kind=? ORDER BY artifact_id", (rollup.KIND,))]


def test_expired_scan_keeps_every_unvisited_patient_unknown(tmp_path, monkeypatch):
    with closing(_seed(tmp_path)) as ledger:
        ledger.ensure_patient(2)
        monkeypatch.setattr(rollup.time, "monotonic", lambda: 10)
        monkeypatch.setattr(rollup, "_dirty_project_snapshot", lambda *a, **k: pytest.fail("expired scan"))
        scan = rollup.scan_dirty_projects(ledger, deadline=10)
        assert scan.dirty == () and scan.unknown == (1, 2) and scan.incomplete
        assert dict(scan.unknown_reasons) == {"deadline": 2}
        assert not ledger.db.in_transaction


def test_scan_that_crosses_deadline_does_not_mark_partial_check_clean(tmp_path, monkeypatch):
    with closing(_seed(tmp_path)) as ledger:
        ledger.ensure_patient(2)
        clock = [0]
        monkeypatch.setattr(rollup.time, "monotonic", lambda: clock[0])
        def slow(*args, **kwargs):
            clock[0] = 10
            return False
        monkeypatch.setattr(rollup, "_dirty_project_snapshot", slow)
        scan = rollup.scan_dirty_projects(ledger, deadline=5)
        assert scan.unknown == (1, 2) and scan.incomplete and scan.dirty == ()
        assert dict(scan.unknown_reasons) == {"deadline": 2}


def test_recheck_deadline_keeps_unconfirmed_clean_patients_unknown(tmp_path, monkeypatch):
    with closing(_seed(tmp_path)) as ledger:
        ledger.ensure_patient(2)
        ledger.db.execute("CREATE TABLE synthetic_marker(value INTEGER)")
        ledger.db.commit()
        clock, calls = [0], []
        monkeypatch.setattr(rollup.time, "monotonic", lambda: clock[0])
        def probe(*args, **kwargs):
            calls.append(True)
            if len(calls) == 1:
                with sqlite3.connect(ledger.db.execute("PRAGMA database_list").fetchone()[2]) as other:
                    other.execute("INSERT INTO synthetic_marker VALUES(1)")
            if len(calls) == 3:
                clock[0] = 10
            return False
        monkeypatch.setattr(rollup, "_dirty_project_snapshot", probe)
        scan = rollup.scan_dirty_projects(ledger, deadline=5)
        assert scan.unknown == (1, 2) and scan.incomplete and scan.rechecked == 1
        assert dict(scan.unknown_reasons) == {"deadline": 2}
        assert not ledger.db.in_transaction


def test_rebuild_after_build_deadline_writes_nothing_and_keeps_remaining_work(tmp_path, monkeypatch):
    with closing(_seed(tmp_path)) as ledger:
        ledger.ensure_patient(2)
        rollup.rebuild(ledger, 1)
        before = _stored(ledger)
        real = rollup.build_rollup
        clock = [0]
        monkeypatch.setattr(rollup.time, "monotonic", lambda: clock[0])
        def slow(*args, **kwargs):
            result = real(*args, **kwargs)
            clock[0] = 10
            return result
        monkeypatch.setattr(rollup, "build_rollup", slow)
        unknown = []
        assert rollup.rebuild_many(ledger, [1, 2], deadline=5, source_unknown=unknown) == 0
        assert unknown == [1, 2] and _stored(ledger) == before
        assert not ledger.db.in_transaction


def test_deadline_after_epoch_write_rolls_back_entire_rollup(tmp_path, monkeypatch):
    with closing(_seed(tmp_path)) as ledger:
        clock = [0]
        real = ledger.bump_inference_analysis_epoch_tx
        monkeypatch.setattr(rollup.time, "monotonic", lambda: clock[0])
        def expire(*args, **kwargs):
            real(*args, **kwargs)
            clock[0] = 10
        monkeypatch.setattr(ledger, "bump_inference_analysis_epoch_tx", expire)
        with pytest.raises(rollup.RollupDeadlineExceeded):
            rollup.rebuild(ledger, 1, deadline=5)
        assert _stored(ledger) == [] and not ledger.db.in_transaction


@pytest.mark.parametrize("target_changes", [False, True])
def test_external_commit_still_rejects_rebuild_even_when_unrelated(tmp_path, monkeypatch, target_changes):
    with closing(_seed(tmp_path)) as ledger:
        ledger.db.execute("CREATE TABLE synthetic_marker(value INTEGER)")
        ledger.db.commit()
        real = rollup.build_rollup
        def change(*args, **kwargs):
            result = real(*args, **kwargs)
            with sqlite3.connect(ledger.db.execute("PRAGMA database_list").fetchone()[2]) as other:
                if target_changes:
                    other.execute("UPDATE messages SET body_text='完全合成の訂正' WHERE message_id=1")
                else:
                    other.execute("INSERT INTO synthetic_marker VALUES(1)")
            return result
        monkeypatch.setattr(rollup, "build_rollup", change)
        monkeypatch.setattr(rollup.time, "monotonic", lambda: 0)
        with pytest.raises(rollup.RollupSourceChanged):
            rollup.rebuild(ledger, 1, deadline=5)
        assert _stored(ledger) == [] and not ledger.db.in_transaction


def test_normal_rebuild_completes_within_budget(tmp_path, monkeypatch):
    with closing(_seed(tmp_path)) as ledger:
        monkeypatch.setattr(rollup.time, "monotonic", lambda: 0)
        unknown = []
        assert rollup.rebuild_many(ledger, [1], deadline=5, source_unknown=unknown) == 1
        assert len(_stored(ledger)) == 1 and unknown == [] and not ledger.db.in_transaction


def test_deadline_does_not_end_borrowed_transaction_or_replace_progress_handler(tmp_path, monkeypatch):
    with closing(_seed(tmp_path)) as ledger:
        ledger.db.execute("UPDATE patients SET fetch_reason='synthetic-pending' WHERE project_id=1")
        calls = []
        ledger.db.set_progress_handler(lambda: calls.append(True) or 0, 1)
        monkeypatch.setattr(rollup.time, "monotonic", lambda: 10)
        with pytest.raises(rollup.RollupDeadlineExceeded):
            rollup.build_rollup(ledger.db, 1, deadline=5)
        assert ledger.db.in_transaction
        calls.clear()
        ledger.db.execute("SELECT count(*) FROM messages").fetchone()
        assert calls
        ledger.db.set_progress_handler(None, 0)
        ledger.db.rollback()
        assert ledger.db.execute("SELECT fetch_reason FROM patients WHERE project_id=1").fetchone()[0] is None


@pytest.mark.parametrize("kind", ["body", "extraction"])
def test_streamed_source_signature_checks_deadline(tmp_path, monkeypatch, kind):
    with closing(_seed(tmp_path)) as ledger:
        if kind == "extraction":
            ledger.artifact_add("extract_llm", "x" * 70000, project_id=1, message_id=1)
        monkeypatch.setattr(rollup.time, "monotonic", lambda: 10)
        with pytest.raises(rollup.RollupDeadlineExceeded):
            if kind == "body":
                rollup._body_source_fingerprint(ledger.db, 1, "text", deadline=5)
            else:
                rollup._extraction_source_signature(ledger.db, 1, deadline=5)
        assert not ledger.db.in_transaction


def test_default_build_preserves_legacy_page_reader_call_contract(tmp_path, monkeypatch):
    with closing(_seed(tmp_path)) as ledger:
        original = rollup._rollup_source_pages
        calls = []
        def legacy(db, pid, cursor, first_page, cfg, *, html_available):
            calls.append(True)
            yield from original(db, pid, cursor, first_page, cfg, html_available=html_available)
        monkeypatch.setattr(rollup, "_rollup_source_pages", legacy)
        assert rollup.build_rollup(ledger.db, 1)["msg_count"] == 1
        assert calls == [True] and not ledger.db.in_transaction
