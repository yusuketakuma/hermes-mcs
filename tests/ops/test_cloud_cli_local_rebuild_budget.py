"""The local-rebuild dry run at production scale through the CLI's bounded reader: the month tally
gets its own finite budget once, candidates and history keep the shared one, the bundle stops at
its limit, and the approval re-proves everything in its own transaction (fully synthetic)."""
import argparse
import json
import sqlite3
import time
import uuid

import pytest

import cloud_cli
import cloud_policy
import extract_llm_local_rebuild as local_rebuild
import mcs_requests
import mcs_util
from test_cloud_local_rebuild import legacy as legacy, paid as paid

__all__ = ["legacy", "paid"]
MESSAGES, PAIRS = 2800, 12          # ~67k accounting rows, as the M1 scale probe
MONTH_END_PAIRS = 15                # ~84k rows: the month-end estimate (~80k, Astra r5)
MONTH_END_META = 925                # chars: ~3.7KB observed per row (strings x4), production density


def _clone(legacy, tmp_path, monkeypatch, pairs, pad=0):
    """The legacy unit's real reservation/result pair cloned to 2800 messages x ``pairs``, each meta
    padded with a synthetic filler to ``pad`` chars, then published as the CLI's ledger
    (data/ledger.db under a synthetic HOME)."""
    from semantic_testkit import _message
    ledger, calls = legacy
    db = ledger.db
    seed = {kind: dict(db.execute("SELECT * FROM artifacts WHERE kind=? AND message_id=1", (kind,)).fetchone())
            for kind in ('cloud_usage_reserve', 'cloud_usage_result')}
    ledger.save_messages([_message(mid) for mid in range(3, MESSAGES + 3)])
    rows = []
    for mid in range(3, MESSAGES + 3):
        for _ in range(pairs):
            rid = uuid.uuid4().hex
            for kind, row in seed.items():
                meta = json.loads(row['meta'])
                meta.update(reservation_id=rid, message_id=mid, unit_key=str(mid))
                text = json.dumps(meta)
                if pad > len(text):
                    meta['synthetic_pad'] = 'x' * (pad - len(text) - 20)
                    text = json.dumps(meta)
                rows.append((kind, 1, mid, row['model'], text, row['content'], row['created_at']))
    if pad:
        # the month edges the tally must still cover: a prior-month reservation whose result came
        # this month (late), and reservations with no result yet (unsettled), on the oldest
        # messages so the newest-first bundle never reaches them
        month = cloud_policy._periods(time.time())[1]
        for mid in range(3, 13):
            rid = uuid.uuid4().hex
            for kind, row in seed.items():
                meta = json.loads(row['meta'])
                meta.update(reservation_id=rid, message_id=mid, unit_key=str(mid))
                rows.append((kind, 1, mid, row['model'], json.dumps(meta), row['content'],
                             month - 86400 if kind == 'cloud_usage_reserve' else row['created_at']))
        for mid in range(13, 23):
            meta = json.loads(seed['cloud_usage_reserve']['meta'])
            meta.update(reservation_id=uuid.uuid4().hex, message_id=mid, unit_key=str(mid))
            row = seed['cloud_usage_reserve']
            rows.append(('cloud_usage_reserve', 1, mid, row['model'], json.dumps(meta), row['content'],
                         row['created_at']))
    with db:
        db.executemany("INSERT INTO artifacts(kind,project_id,message_id,model,meta,content,created_at) "
                       "VALUES(?,?,?,?,?,?,?)", rows)
    root = tmp_path / 'synthetic-root'
    (root / 'data').mkdir(parents=True)
    path = root / 'data' / 'ledger.db'
    with sqlite3.connect(str(path)) as target:
        db.backup(target)
    monkeypatch.setattr(mcs_util, 'HOME', str(root))
    return path


@pytest.fixture
def scaled(legacy, tmp_path, monkeypatch):
    return _clone(legacy, tmp_path, monkeypatch, PAIRS)


def _plan(capsys, tmp_path, *extra):
    parser = argparse.ArgumentParser()
    cloud_cli.add_parsers(parser.add_subparsers(dest='command'))
    out = tmp_path / f'synthetic-plan-{uuid.uuid4().hex}.json'
    args = parser.parse_args(['cloud-llm', 'local-rebuild-plan', '--out', str(out), *extra])
    rc = args.fn(args)
    report = json.loads(capsys.readouterr().out.split('\n件数')[0])
    return rc, report, (json.loads(out.read_text()) if out.exists() else None)


def test_production_scale_builds_a_full_bundle_and_the_approval_re_proves_it(scaled, tmp_path, capsys, monkeypatch):
    rc, report, plan = _plan(capsys, tmp_path)
    assert report['state'] == 'bundle_full' and report['stop_reason'] is None and rc == 0
    assert report['examined'] == report['eligible'] == 20 and report['next_after'] is not None
    tally = report['tally_read']               # the tally phase's own use, counts only
    assert set(tally) == {'rows', 'bytes', 'rows_limit', 'bytes_limit'}
    assert 0 < tally['rows'] <= tally['rows_limit'] and 0 < tally['bytes'] <= tally['bytes_limit']
    assert report['candidates'] == MESSAGES + 2 and report['excluded']['unverified'] == 0
    assert local_rebuild.valid_plan(plan) and len(plan['units']) == 20
    from ledger import Ledger
    from contextlib import closing, contextmanager

    observed = {'stage': 'before_bounded', 'tally': 'not_reached', 'units': 0,
                'sqlite': 'none', 'bounded_s': None, 'deadline_reached': None}
    observed['tally_phases'] = {name: {'started': 0, 'completed': 0, 'seconds': 0.0}
                                for name in ('timestamps', 'update', 'with_prior')}
    original_bounded, original_tally = local_rebuild._bounded, cloud_policy._tally

    @contextmanager
    def bounded(db, seconds):
        started = time.monotonic()
        try:
            with original_bounded(db, seconds) as deadline:
                try:
                    yield deadline
                finally:
                    ended = time.monotonic()
                    observed['bounded_s'] = round(ended - started, 6)
                    observed['deadline_reached'] = ended >= deadline
        except sqlite3.OperationalError as error:
            code = getattr(error, 'sqlite_errorcode', None)
            kinds = {sqlite3.SQLITE_INTERRUPT: 'interrupt', sqlite3.SQLITE_BUSY: 'busy',
                     sqlite3.SQLITE_LOCKED: 'locked'}
            observed['sqlite'] = kinds.get(code & 0xff, 'other') if isinstance(code, int) else 'unknown'
            raise

    def tally_call(*args, **kwargs):
        try:
            return original_tally(*args, **kwargs)
        except (cloud_policy.AnthropicError, ArithmeticError, ValueError, TypeError, KeyError) as error:
            kinds = ((cloud_policy.AnthropicError, 'anthropic'), (ArithmeticError, 'arithmetic'),
                     (ValueError, 'value'), (TypeError, 'type'), (KeyError, 'key'))
            observed['tally'] = next(name for kind, name in kinds if isinstance(error, kind))
            raise

    def stage(name, func):
        def call(*args, **kwargs):
            observed['stage'] = name
            if name == '_tally_valid':
                observed['tally'] = 'running'
            result = func(*args, **kwargs)
            if name == '_tally_valid' and observed['tally'] == 'running':
                observed['tally'] = 'valid' if result else 'invalid'
            if name == 'unit_proof':
                observed['units'] += 1
            return result
        return call

    def tally_phase(name, func):
        def call(*args, **kwargs):
            phase = observed['tally_phases'][name]
            phase['started'] += 1
            started = time.monotonic()
            try:
                result = func(*args, **kwargs)
                phase['completed'] += 1
                return result
            finally:
                phase['seconds'] += time.monotonic() - started
        return call

    def compare_read_paths(db, source, month):
        import hashlib
        result = {'status': 'unavailable', 'runs': []}
        signatures = []
        owns_transaction = False
        try:
            if db.in_transaction:
                return result
            with closing(db.execute('BEGIN')):
                owns_transaction = True
            for label, table in (('current', source), ('not_indexed', 'artifacts NOT INDEXED'),
                                 ('not_indexed', 'artifacts NOT INDEXED'), ('current', source)):
                digest, count = hashlib.sha256(), 0
                started = time.monotonic()
                with closing(db.execute(
                    "SELECT artifact_id,kind,project_id,message_id,model,meta,created_at "
                    f"FROM {table} "
                    "WHERE kind IN ('cloud_usage_reserve','cloud_usage_result') AND created_at>=? "
                    "AND artifact_id>? ORDER BY artifact_id", (month, 0))) as rows:
                    for row in rows:
                        digest.update(f'{row[0]}\n'.encode('ascii'))
                        count += 1
                signature = digest.digest()
                seconds = time.monotonic() - started
                result['runs'].append({'plan': label, 'rows': count, 'seconds': round(seconds, 6)})
                signatures.append((count, signature))
            result['status'] = 'match' if all(item == signatures[0] for item in signatures[1:]) else 'mismatch'
        except Exception:
            return result
        finally:
            if owns_transaction:
                try:
                    db.rollback()
                    if db.in_transaction:
                        result['status'] = 'unavailable'
                except Exception:
                    result['status'] = 'unavailable'
        return result

    with closing(Ledger(str(scaled))) as ledger:
        with monkeypatch.context() as patch:
            patch.setattr(local_rebuild, '_bounded', bounded)
            patch.setattr(cloud_policy, '_tally', tally_call)
            for name in ('_database_identity', 'approved_units', '_tally_valid', 'unit_proof'):
                patch.setattr(local_rebuild, name, stage(name, getattr(local_rebuild, name)))
            for owner, name, phase in ((cloud_policy, '_validate_accounting_timestamps', 'timestamps'),
                                       (cloud_policy._Tally, 'update', 'update'),
                                       (cloud_policy._Tally, '_with_prior', 'with_prior')):
                patch.setattr(owner, name, tally_phase(phase, getattr(owner, name)))
            tally_before = dict(cloud_policy.TALLY_COST)
            started = time.monotonic()
            receipt = mcs_requests.apply_command(ledger, {
                'version': 1, 'cmd': local_rebuild.COMMAND, 'command_id': str(uuid.uuid4()),
                'actor': 'synthetic-human', 'human_confirmed': True, 'project_id': None,
                'reason': '完全合成の確認', 'plan': plan})
            elapsed = time.monotonic() - started
            observed['tally_rebuilds'] = cloud_policy.TALLY_COST['rebuilds'] - tally_before['rebuilds']
            observed['tally_s'] = round(cloud_policy.TALLY_COST['seconds'] - tally_before['seconds'], 6)
        observed.update(order_index_state='unavailable', plan_index='unavailable', temp_btree='unavailable',
                        comparison={'status': 'unavailable', 'runs': []})
        scan_config = None
        try:
            import re
            import ledger_schema
            state = (ledger_schema.cloud_usage_order_index_state(ledger.db)
                     if not ledger.db.in_transaction else 'unavailable')
            if state in ('present', 'absent', 'schema_mismatch'):
                observed['order_index_state'] = state
                source = cloud_policy._accounting_order_source(ledger.db)
                expected = (f"artifacts INDEXED BY {ledger_schema.CLOUD_USAGE_ORDER_INDEX}"
                            if state == 'present' else 'artifacts')
                now = time.time()
                month = cloud_policy._periods(now)[1]
                if (source == expected and not ledger.db.in_transaction
                        and month == cloud_policy._periods(now - elapsed)[1]):
                    scan_config = (source, month)
                    with closing(ledger.db.execute(
                        "EXPLAIN QUERY PLAN SELECT artifact_id,kind,project_id,message_id,model,meta,created_at "
                        f"FROM {source} "
                        "WHERE kind IN ('cloud_usage_reserve','cloud_usage_result') AND created_at>=? "
                        "AND artifact_id>? ORDER BY artifact_id", (month, 0))) as rows:
                        details = [row[3].lower() for row in rows]
                    if details:
                        indexes = {label for pattern, label in (
                            (r'\bidx_cloud_usage_artifact_order\b', 'order_index'),
                            (r'\bidx_artifacts_kind_created\b', 'kind_created_index'))
                            if any(re.search(pattern, detail) for detail in details)}
                        if len(indexes) == 1 and (state == 'present' or 'order_index' not in indexes):
                            observed['plan_index'] = next(iter(indexes))
                        else:
                            observed['plan_index'] = 'other'
                        observed['temp_btree'] = any('temp b-tree' in detail for detail in details)
        except Exception:
            pass   # diagnostics never replace the approval result
        if scan_config is not None:
            observed['comparison'] = compare_read_paths(ledger.db, *scan_config)
        source = expected = month = details = scan_config = None
    error = receipt.get('error')
    known_errors = (local_rebuild.UNVERIFIED, 'local_rebuild_database_changed',
                    'local_rebuild_already_approved', 'local_rebuild_bundle_changed')
    category = error if error in known_errors else 'none' if error is None else 'other'
    diagnostic = f"error={category}, elapsed_s={elapsed:.3f}, {observed}"
    print(diagnostic)
    assert receipt['outcome'] == 'applied', diagnostic
    assert receipt['approved'] == 20, diagnostic
    assert observed['comparison']['status'] != 'mismatch', diagnostic


def test_the_month_end_tally_at_production_density_fits_its_own_budget(legacy, tmp_path, capsys, monkeypatch):
    """~84k accounting rows at ~3.7KB observed per row (fully synthetic filler, no real meta): the
    tally passes once inside its own budget and the dry run stops at a full bundle; the measured
    use is past the former 256 MiB, which the month end would have exceeded."""
    _clone(legacy, tmp_path, monkeypatch, MONTH_END_PAIRS, pad=MONTH_END_META)
    rc, report, plan = _plan(capsys, tmp_path)
    assert rc == 0 and report['state'] == 'bundle_full' and report['stop_reason'] is None
    assert report['eligible'] == 20 and report['excluded']['unverified'] == 0
    assert local_rebuild.valid_plan(plan) and len(plan['units']) == 20
    tally = report['tally_read']
    assert tally['rows'] >= 80000 and 256 * 1024 * 1024 < tally['bytes'] <= tally['bytes_limit']
    assert report['candidates'] == MESSAGES + 2      # the late and unsettled rows void nothing


def test_a_history_changed_between_plan_and_approval_rejects_the_whole_bundle(scaled, tmp_path, capsys):
    _rc, _report, plan = _plan(capsys, tmp_path)
    from ledger import Ledger
    from contextlib import closing
    with closing(Ledger(str(scaled))) as ledger:
        with ledger.db:
            ledger.db.execute("UPDATE artifacts SET meta=json_set(meta,'$.synthetic_note','x') WHERE "
                              "kind='cloud_usage_result' AND message_id=?", (plan['units'][-1]['message_id'],))
        receipt = mcs_requests.apply_command(ledger, {
            'version': 1, 'cmd': local_rebuild.COMMAND, 'command_id': str(uuid.uuid4()),
            'actor': 'synthetic-human', 'human_confirmed': True, 'project_id': None,
            'reason': '完全合成の確認', 'plan': plan})
        assert receipt['outcome'] == 'rejected' and local_rebuild.approved_units(ledger.db) == {}


def test_the_tally_phase_over_its_own_budget_stops_at_tally_and_leaves_everything_unverified(
        scaled, tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(cloud_cli, 'TALLY_READ_BYTES', 1024 * 1024)     # below the tally's need
    rc, report, plan = _plan(capsys, tmp_path)
    assert rc == 1 and report['state'] == 'partial' and plan is None
    assert report['stopped_at'] == 'tally' and report['stop_reason'] == 'tally_budget_exceeded'
    assert report['tally_read']['bytes'] > report['tally_read']['bytes_limit']   # kept after the phase
    assert report['eligible'] == 0 and report['excluded']['unverified'] == report['candidates']


def test_the_history_phase_keeps_the_shared_budget(scaled, tmp_path, capsys, monkeypatch):
    """The tally's own budget is never borrowed: history over the shared byte budget stops there."""
    monkeypatch.setattr(cloud_cli, 'READ_BYTES', 200 * 1024)            # candidates fit, history does not
    rc, report, plan = _plan(capsys, tmp_path)
    assert report['state'] == 'partial' and report['stopped_at'] == 'history'
    assert report['stop_reason'] == 'observation_budget_exceeded' and report['eligible'] < 20
    assert rc == 1 and plan is None                                    # a partial read offers no bundle


def test_a_phase_restores_the_outer_budget_on_every_exit_and_shares_the_deadline(scaled):
    with cloud_cli._reader(seconds=60) as (db, _deadline):
        outer = db.row_factory.budget
        with pytest.raises(cloud_cli._ReaderLimit, match='tally_budget_exceeded'):
            with cloud_cli._reader_phase(db, rows=1, size=10 ** 9, reason='tally_budget_exceeded'):
                db.execute("SELECT artifact_id FROM artifacts LIMIT 5").fetchall()
        assert db.row_factory.budget is outer and outer['reason'] == 'observation_budget_exceeded'
    with cloud_cli._reader(seconds=0) as (db, _deadline):
        with pytest.raises((cloud_cli._ReaderLimit, sqlite3.OperationalError)):
            with cloud_cli._reader_phase(db, rows=10 ** 9, size=10 ** 12, reason='tally_budget_exceeded'):
                db.execute("SELECT artifact_id FROM artifacts").fetchall()
        assert db.row_factory.stop_reason == 'read_deadline'                 # the deadline is shared
