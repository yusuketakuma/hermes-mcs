"""Synthetic derivation reserves snapshot verification time without hiding unfinished work."""
import json
import sqlite3

import pytest

import drug_map
import extract
import health_report
import health_watch
import mcs_signals
import rollup
import run_check
import snapshots
from extract_testkit import _ledger


def test_derive_reserves_verification_and_existing_tail_for_every_stage(tmp_path, monkeypatch):
    ledger = _ledger(tmp_path)
    seen = {}
    try:
        def verification_budget(path):
            assert path == ledger.db.execute("PRAGMA database_list").fetchone()[2]
            assert path != run_check.DB
            return 154
        monkeypatch.setattr(snapshots, "snapshot_verify_timeout_s", verification_budget)
        monkeypatch.setattr(run_check.time, "monotonic", lambda: 0)
        def rules(*args, deadline):
            seen["rules"] = deadline
            return {"done": 0, "pids": []}
        def drugs(*args, deadline):
            seen["drugs"] = deadline
            return {"pids": [], "status": "complete"}
        def scan(*args, deadline, commit_deadline, **kwargs):
            seen["scan"] = deadline
            seen["scan_commit"] = commit_deadline
            return rollup.RollupScan((), (1,), True, (("deadline", 1),))
        def rebuild(*args, deadline, **kwargs):
            seen["rebuild"] = deadline
            return 0
        def signals(*args, deadline):
            seen["signals"] = deadline
            return {"errors": []}
        monkeypatch.setattr(extract, "run_pending", rules)
        monkeypatch.setattr(drug_map, "configured", lambda cfg: (None, None))
        monkeypatch.setattr(drug_map, "derive", drugs)
        monkeypatch.setattr(rollup, "scan_fair_dirty_projects", scan)
        monkeypatch.setattr(rollup, "rebuild_many", rebuild)
        monkeypatch.setattr(mcs_signals, "evaluate", signals)
        result = {"errors": []}
        run_check.stage_derive(ledger, result, 480, {}, llm_budget_cap=0)
        assert seen == pytest.approx({"rules": 148, "drugs": 296 / 3, "scan": 296 / 3,
                                     "scan_commit": 592 / 3, "rebuild": 592 / 3, "signals": 296})
        assert result["rollup_scan"]["state"] == "unknown"
        assert result["rollup_scan"]["unknown_reasons"] == {"deadline": 1}
        assert result["errors"] == ["deadline_deferred:rollup_scan"]
        assert result["deferred_stages"] == ["rollup_scan"]
        assert run_check.RUN_DEADLINE_S == 480
    finally:
        ledger.close()


def test_derive_reserves_rebuild_time_and_excludes_unknown_touched(tmp_path, monkeypatch):
    ledger = _ledger(tmp_path)
    clock = [20]
    seen = {}
    try:
        monkeypatch.setattr(snapshots, "snapshot_verify_timeout_s", lambda path: 20)
        monkeypatch.setattr(run_check.time, "monotonic", lambda: clock[0])
        monkeypatch.setattr(extract, "run_pending", lambda *a, **k: {"done": 0, "pids": [1, 2, 3]})
        monkeypatch.setattr(drug_map, "configured", lambda cfg: (None, None))
        monkeypatch.setattr(drug_map, "derive", lambda *a, **k: {"pids": [], "status": "complete"})
        monkeypatch.setattr(mcs_signals, "evaluate", lambda *a, **k: {"errors": []})

        def scan(_ledger, *, deadline, commit_deadline, **kwargs):
            seen["scan_deadline"] = deadline
            seen["commit_deadline"] = commit_deadline
            clock[0] = deadline
            return rollup.RollupScan((1,), (2,), True, (("deadline", 1),))

        def rebuild(_ledger, project_ids, *, deadline, **kwargs):
            seen["rebuilt"] = set(project_ids)
            seen["rebuild_start"] = clock[0]
            seen["rebuild_deadline"] = deadline
            return len(project_ids)

        monkeypatch.setattr(rollup, "scan_fair_dirty_projects", scan)
        monkeypatch.setattr(rollup, "rebuild_many", rebuild)
        result = {"errors": [], "trickle_imports": [{"pid": 2}]}
        run_check.stage_derive(ledger, result, 200, {}, llm_budget_cap=0)

        # 20 seconds for snapshot verification and 30 for the existing tail.
        assert seen["commit_deadline"] == seen["rebuild_deadline"] == pytest.approx(320 / 3)
        assert seen["scan_deadline"] == seen["rebuild_start"] == pytest.approx(190 / 3)
        assert seen["rebuild_start"] < seen["rebuild_deadline"]
        assert seen["rebuilt"] == {1, 3}
        assert result["rollups"] == 2
        assert result["rollup_scan"]["state"] == "unknown"
        assert result["rollup_scan"]["unknown_reasons"] == {"deadline": 1}
        assert result["errors"] == ["deadline_deferred:rollup_scan"]
        assert result["deferred_stages"] == ["rollup_scan"]
    finally:
        ledger.close()


def test_real_scan_timeout_preserves_time_to_rebuild_confirmed_dirty(tmp_path, monkeypatch):
    ledger = _ledger(tmp_path)
    clock = [20]
    scans, rebuilt = [], []
    try:
        ledger.ensure_patient(1)
        ledger.ensure_patient(2)
        monkeypatch.setattr(snapshots, "snapshot_verify_timeout_s", lambda path: 20)
        monkeypatch.setattr(run_check.time, "monotonic", lambda: clock[0])
        monkeypatch.setattr(extract, "run_pending", lambda *a, **k: {"done": 0, "pids": [2]})
        monkeypatch.setattr(drug_map, "configured", lambda cfg: (None, None))
        monkeypatch.setattr(drug_map, "derive", lambda *a, **k: {"pids": [], "status": "complete"})
        monkeypatch.setattr(mcs_signals, "evaluate", lambda *a, **k: {"errors": []})

        def dirty(_db, project_id, _now, *, deadline):
            scans.append(project_id)
            if project_id == 2:
                clock[0] = deadline
            return True

        def rebuild(current_ledger, project_id, *, cfg, deadline):
            rebuilt.append((project_id, clock[0], deadline))
            current_ledger.artifact_add(rollup.KIND, "synthetic", project_id=project_id)

        monkeypatch.setattr(rollup, "_dirty_project_snapshot", dirty)
        monkeypatch.setattr(rollup, "rebuild", rebuild)
        result = {"errors": []}
        run_check.stage_derive(ledger, result, 200, {}, llm_budget_cap=0)

        assert scans == [1, 2]
        assert len(rebuilt) == 1 and rebuilt[0] == pytest.approx((1, 190 / 3, 320 / 3))
        assert [row["project_id"] for row in ledger.artifacts(rollup.KIND)] == [1]
        assert result["rollups"] == 1
        assert result["rollup_scan"]["state"] == "unknown"
        assert result["rollup_scan"]["unknown_reasons"] == {"deadline": 1}
        assert result["errors"] == ["deadline_deferred:rollup_scan"]
        assert result["deferred_stages"] == ["rollup_scan"]
        assert not ledger.db.in_transaction
    finally:
        ledger.close()


def test_cursor_busy_does_not_skip_confirmed_dirty_rebuild(tmp_path, monkeypatch):
    ledger = _ledger(tmp_path)
    rebuilt = []
    try:
        ledger.ensure_patient(1)
        monkeypatch.setattr(snapshots, "snapshot_verify_timeout_s", lambda path: 20)
        monkeypatch.setattr(run_check.time, "monotonic", lambda: 20)
        monkeypatch.setattr(extract, "run_pending", lambda *a, **k: {"done": 0, "pids": []})
        monkeypatch.setattr(drug_map, "configured", lambda cfg: (None, None))
        monkeypatch.setattr(drug_map, "derive", lambda *a, **k: {"pids": [], "status": "complete"})
        monkeypatch.setattr(mcs_signals, "evaluate", lambda *a, **k: {"errors": []})
        monkeypatch.setattr(rollup, "_dirty_project_snapshot", lambda *a, **k: True)
        add = ledger.artifact_add_tx

        def busy_cursor(kind, *args, **kwargs):
            if kind == "patient_walk_cursor_v1":
                raise sqlite3.OperationalError("database is locked")
            return add(kind, *args, **kwargs)

        def rebuild(current_ledger, project_id, *, cfg, deadline):
            rebuilt.append(project_id)
            current_ledger.artifact_add(rollup.KIND, "synthetic", project_id=project_id)

        monkeypatch.setattr(ledger, "artifact_add_tx", busy_cursor)
        monkeypatch.setattr(rollup, "rebuild", rebuild)
        result = {"errors": []}
        run_check.stage_derive(ledger, result, 200, {}, llm_budget_cap=0)

        assert rebuilt == [1]
        assert result["rollups"] == 1
        assert result["rollup_scan"]["state"] == "checked"
        assert "rollup: OperationalError" not in result["errors"]
        assert [row["project_id"] for row in ledger.artifacts(rollup.KIND)] == [1]
        assert not ledger.db.in_transaction
    finally:
        ledger.close()


@pytest.fixture
def derive_case(tmp_path, monkeypatch):
    ledger = _ledger(tmp_path)
    monkeypatch.setattr(run_check, 'HOME', str(tmp_path))
    monkeypatch.setattr(run_check, 'DB', ledger.db.execute('PRAGMA database_list').fetchone()[2])
    monkeypatch.setattr(snapshots, 'snapshot_verify_timeout_s', lambda path: 20)
    monkeypatch.setattr(run_check.time, 'monotonic', lambda: 20)
    monkeypatch.setattr(extract, 'run_pending', lambda *a, **k: {'done': 0, 'pids': [2, 3]})
    monkeypatch.setattr(drug_map, 'configured', lambda cfg: (None, None))
    monkeypatch.setattr(drug_map, 'derive', lambda *a, **k: {'pids': [], 'status': 'complete'})
    monkeypatch.setattr(mcs_signals, 'evaluate', lambda *a, **k: {'errors': []})

    def run(scan_result, *, changed=(), unknown=(), reasons=None):
        rebuilt = []

        def scan(*a, **k):
            if isinstance(scan_result, Exception):
                raise scan_result
            return scan_result

        def rebuild(_ledger, project_ids, *, source_changed, source_unknown,
                    source_unknown_reasons, **kwargs):
            rebuilt.extend(project_ids)
            source_changed.extend(changed)
            source_unknown.extend(unknown)
            source_unknown_reasons.update(reasons or {})
            return len(project_ids) - len(changed) - len(unknown)

        monkeypatch.setattr(rollup, 'scan_fair_dirty_projects', scan)
        monkeypatch.setattr(rollup, 'rebuild_many', rebuild)
        result = {'errors': [], 'incomplete': []}
        run_check.stage_derive(ledger, result, 200, {}, llm_budget_cap=0)
        return result, rebuilt

    try:
        yield ledger, run
    finally:
        ledger.close()


@pytest.mark.parametrize('reasons', [
    (), (('source_unverified', 2),),
    (('deadline', 1), ('source_unverified', 1)),
    (('deadline', 1), ('same_connection_changed', 1)),
    (('deadline', 1),), (('deadline', 0),),
])
def test_missing_or_mixed_scan_reasons_remain_scan_unknown(derive_case, reasons):
    _ledger, run = derive_case
    result, rebuilt = run(rollup.RollupScan((1,), (2, 3), True, reasons))
    assert rebuilt == [1]
    assert result['rollup_scan']['state'] == 'unknown'
    assert result['rollup_scan']['unknown_reasons'] == dict(reasons)
    assert result['errors'] == ['rollup: scan_unknown']
    assert 'deferred_stages' not in result


@pytest.mark.parametrize('error', [sqlite3.OperationalError, rollup.RollupSourceUnverified])
def test_scan_exceptions_remain_errors_without_disclosing_details(derive_case, error):
    _ledger, run = derive_case
    result, rebuilt = run(error('SYNTHETIC_PRIVATE_ERROR'))
    assert not rebuilt and 'rollup_scan' not in result
    assert result['errors'] == [f'rollup: {error.__name__}']
    assert 'deferred_stages' not in result
    assert 'SYNTHETIC_PRIVATE_ERROR' not in str(result)


@pytest.mark.parametrize('reason', ['source_changed', 'source_unknown'])
def test_scan_deferral_keeps_independent_rebuild_source_errors(derive_case, reason):
    _ledger, run = derive_case
    outcome = {'changed' if reason == 'source_changed' else 'unknown': (1,)}
    result, rebuilt = run(rollup.RollupScan((1,), (2, 3), True, (('deadline', 2),)), **outcome)
    assert rebuilt == [1] and result['rollups'] == 0
    assert result['errors'] == ['deadline_deferred:rollup_scan', f'rollup: {reason}']
    assert result[f'rollup_{reason}'] == 1
    assert result['rollup_scan']['state'] == 'unknown'
    assert result['deferred_stages'] == ['rollup_scan']


def test_scan_deferral_uses_the_common_compact_marker_with_later_deferral(derive_case, monkeypatch):
    _ledger, run = derive_case
    monkeypatch.setattr(mcs_signals, 'evaluate', lambda *a, **k: {'errors': [], 'detectors_deferred': True})
    result, rebuilt = run(rollup.RollupScan((1,), (2, 3), True, (('deadline', 2),)))
    assert rebuilt == [1]
    assert result['errors'] == ['deadline_deferred:rollup_scan,signals']
    assert result['deferred_stages'] == ['rollup_scan', 'signals']


@pytest.mark.parametrize(('reasons', 'expected', 'errors'), [
    ({'deadline': 2}, {'deadline': 2}, ['deadline_deferred:rollup_rebuild']),
    ({'source_unverified': 2}, {'source_unverified': 2}, ['rollup: source_unknown']),
    ({'deadline': 1, 'source_unverified': 1},
     {'deadline': 1, 'source_unverified': 1}, ['rollup: source_unknown']),
    ({}, {'source_unverified': 2}, ['rollup: source_unknown']),
    ({'deadline': True}, {'source_unverified': 2}, ['rollup: source_unknown']),
    ({'deadline': 2, 'unexpected': 1}, {'source_unverified': 2}, ['rollup: source_unknown']),
    ({'deadline': 1}, {'source_unverified': 2}, ['rollup: source_unknown']),
])
def test_rebuild_unknown_requires_valid_all_deadline_reasons(derive_case, reasons, expected, errors):
    _ledger, run = derive_case
    result, rebuilt = run(rollup.RollupScan((1, 2), (), False, ()),
                          unknown=(1, 2), reasons=reasons)
    assert rebuilt == [1, 2, 3]
    assert result['rollup_source_unknown'] == 2
    assert result['rollup_source_unknown_reasons'] == expected
    assert result['errors'] == errors
    if errors[0].startswith('deadline_deferred:'):
        assert result['deferred_stages'] == ['rollup_rebuild']
    else:
        assert 'deferred_stages' not in result


def test_rebuild_deadline_does_not_hide_source_changed(derive_case):
    _ledger, run = derive_case
    result, _rebuilt = run(rollup.RollupScan((1, 2), (), False, ()),
                           changed=(1,), unknown=(2,), reasons={'deadline': 1})
    assert result['rollup_source_changed'] == 1
    assert result['rollup_source_unknown_reasons'] == {'deadline': 1}
    assert result['errors'] == ['rollup: source_changed', 'deadline_deferred:rollup_rebuild']


def test_scan_and_rebuild_deadline_use_one_deferred_marker(derive_case):
    _ledger, run = derive_case
    result, rebuilt = run(rollup.RollupScan((1,), (2,), True, (('deadline', 1),)),
                          unknown=(1,), reasons={'deadline': 1})
    assert rebuilt == [1, 3]
    assert result['errors'] == ['deadline_deferred:rollup_scan,rollup_rebuild']
    assert result['deferred_stages'] == ['rollup_scan', 'rollup_rebuild']


def test_scan_deadline_stays_partial_and_reaches_existing_health_deferred_guidance(
        derive_case, monkeypatch, tmp_path):
    import notify_cards
    import notify_cmds
    import notify_spool

    ledger, run = derive_case
    result, rebuilt = run(rollup.RollupScan((1,), (2, 3), True, (('deadline', 2),)))
    assert rebuilt == [1] and result['rollup_scan']['state'] == 'unknown'
    monkeypatch.setattr(notify_cmds, 'drain_int_commands', lambda *a, **k: None)
    monkeypatch.setattr(notify_spool, 'publish_flags', lambda *a, **k: None)
    monkeypatch.setattr(notify_cards, 'snapshot_dirty', lambda *a, **k: False)
    monkeypatch.setattr(run_check.maintenance, 'refresh_snapshot', lambda *a, **k: 'unchanged')
    run_id = ledger.begin_run(None)
    assert run_check._finish_run(ledger, {}, result, run_id, 200) == 'partial'
    stored = ledger.db.execute('SELECT status,error FROM runs WHERE run_id=?', (run_id,)).fetchone()
    assert tuple(stored) == ('partial', 'deadline_deferred:rollup_scan')
    block = health_report.collection_diagnostics_health(ledger.db, result['errors'], 3700)
    assert block['errors']['categories']['deferred'] == 1
    assert block['errors']['categories']['postprocess'] == 0
    path = tmp_path / 'health.json'
    path.write_text(json.dumps({'at': 3700, 'overall': 'degraded', 'run_status': 'partial',
                               'state_reasons': ['stage_errors'], 'collection_diagnostics': block}))
    report = health_watch.classify_health(str(path), 3710, 600)
    assert report['status'] == 'degraded'
    lines = '\n'.join(health_watch._reason_rows(report, report['state_reasons']))
    assert '時間内に終わらず先送り' in lines and '異常ではありません' in lines
    assert '対処不要。次回以降に実行されます' in lines
    assert '収集後の処理が失敗しました' not in lines


def test_rebuild_deadline_stays_partial_and_reaches_existing_health_guidance(
        derive_case, monkeypatch):
    import notify_cards
    import notify_cmds
    import notify_spool

    ledger, run = derive_case
    result, _rebuilt = run(rollup.RollupScan((1, 2), (), False, ()),
                           unknown=(2, 3), reasons={'deadline': 2})
    monkeypatch.setattr(notify_cmds, 'drain_int_commands', lambda *a, **k: None)
    monkeypatch.setattr(notify_spool, 'publish_flags', lambda *a, **k: None)
    monkeypatch.setattr(notify_cards, 'snapshot_dirty', lambda *a, **k: False)
    monkeypatch.setattr(run_check.maintenance, 'refresh_snapshot', lambda *a, **k: 'unchanged')
    run_id = ledger.begin_run(None)
    assert run_check._finish_run(ledger, {}, result, run_id, 200) == 'partial'
    stored = ledger.db.execute('SELECT status,error FROM runs WHERE run_id=?', (run_id,)).fetchone()
    assert tuple(stored) == ('partial', 'deadline_deferred:rollup_rebuild')
    block = health_report.collection_diagnostics_health(ledger.db, result['errors'], 3700)
    assert block['errors']['categories']['deferred'] == 1
    assert block['errors']['categories']['postprocess'] == 0
