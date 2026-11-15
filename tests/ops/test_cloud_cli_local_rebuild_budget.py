"""The local-rebuild dry run at production scale through the CLI's bounded reader: the month tally
gets its own finite budget once, candidates and history keep the shared one, the bundle stops at
its limit, and the approval re-proves everything in its own transaction (fully synthetic)."""
import argparse
import json
import sqlite3
import uuid

import pytest

import cloud_cli
import extract_llm_local_rebuild as local_rebuild
import mcs_requests
import mcs_util
from test_cloud_local_rebuild import legacy as legacy, paid as paid

__all__ = ["legacy", "paid"]
MESSAGES, PAIRS = 2800, 12          # ~67k accounting rows, as the M1 scale probe


@pytest.fixture
def scaled(legacy, tmp_path, monkeypatch):
    """The legacy unit's real reservation/result pair cloned to 2800 messages x 12 pairs, then
    published as the CLI's ledger (data/ledger.db under a synthetic HOME)."""
    from semantic_testkit import _message
    ledger, calls = legacy
    db = ledger.db
    seed = {kind: dict(db.execute("SELECT * FROM artifacts WHERE kind=? AND message_id=1", (kind,)).fetchone())
            for kind in ('cloud_usage_reserve', 'cloud_usage_result')}
    ledger.save_messages([_message(mid) for mid in range(3, MESSAGES + 3)])
    rows = []
    for mid in range(3, MESSAGES + 3):
        for _ in range(PAIRS):
            rid = uuid.uuid4().hex
            for kind, row in seed.items():
                meta = json.loads(row['meta'])
                meta.update(reservation_id=rid, message_id=mid, unit_key=str(mid))
                rows.append((kind, 1, mid, row['model'], json.dumps(meta), row['content'], row['created_at']))
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


def _plan(capsys, tmp_path, *extra):
    parser = argparse.ArgumentParser()
    cloud_cli.add_parsers(parser.add_subparsers(dest='command'))
    out = tmp_path / f'synthetic-plan-{uuid.uuid4().hex}.json'
    args = parser.parse_args(['cloud-llm', 'local-rebuild-plan', '--out', str(out), *extra])
    rc = args.fn(args)
    report = json.loads(capsys.readouterr().out.split('\n件数')[0])
    return rc, report, (json.loads(out.read_text()) if out.exists() else None)


def test_production_scale_builds_a_full_bundle_and_the_approval_re_proves_it(scaled, tmp_path, capsys):
    rc, report, plan = _plan(capsys, tmp_path)
    assert report['state'] == 'bundle_full' and report['stop_reason'] is None and rc == 0
    assert report['examined'] == report['eligible'] == 20 and report['next_after'] is not None
    assert report['candidates'] == MESSAGES + 2 and report['excluded']['unverified'] == 0
    assert local_rebuild.valid_plan(plan) and len(plan['units']) == 20
    from ledger import Ledger
    from contextlib import closing
    with closing(Ledger(str(scaled))) as ledger:
        receipt = mcs_requests.apply_command(ledger, {
            'version': 1, 'cmd': local_rebuild.COMMAND, 'command_id': str(uuid.uuid4()),
            'actor': 'synthetic-human', 'human_confirmed': True, 'project_id': None,
            'reason': '完全合成の確認', 'plan': plan})
    assert receipt['outcome'] == 'applied' and receipt['approved'] == 20


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
    assert report['eligible'] == 0 and report['excluded']['unverified'] == report['candidates']


def test_the_history_phase_keeps_the_shared_budget(scaled, tmp_path, capsys, monkeypatch):
    """The tally's own budget is never borrowed: history over the shared byte budget stops there."""
    monkeypatch.setattr(cloud_cli, 'READ_BYTES', 200 * 1024)            # candidates fit, history does not
    rc, report, plan = _plan(capsys, tmp_path)
    assert report['state'] == 'partial' and report['stopped_at'] == 'history'
    assert report['stop_reason'] == 'observation_budget_exceeded' and report['eligible'] < 20


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
