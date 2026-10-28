"""Synthetic recovery barriers and operator-visible receipt observations."""
import json
import uuid

import pytest

import drug_alias
import job_ops
import mcs_drug
import mcs_requests
import ledger
from test_drug_alias import _commit_fault, request, rollback
from test_drug_alias import corpus as corpus


def _uncertain(store, cfg, committed):
    proposed = drug_alias.proposal(store.db, cfg, 'キラナン', 'fiction-i1')
    req = request(proposed)
    connection = store.db
    store.db = _commit_fault(connection, committed)
    try:
        with pytest.raises(mcs_requests.AliasCommitUnknown):
            mcs_requests.apply_command(store, req)
    finally:
        store.db = connection
    return req


@pytest.mark.parametrize('committed', [False, True])
def test_queue_retry_and_receipt_report_recovery_hold(corpus, tmp_path, monkeypatch, capsys, committed):
    store, cfg, _ = corpus
    req = request(drug_alias.proposal(store.db, cfg, 'キラナン', 'fiction-i1'))
    inbox = tmp_path / 'data' / 'cmd'
    inbox.mkdir()
    mcs_requests.enqueue(req, inbox)
    result = {'errors': []}
    connection = store.db
    store.db = _commit_fault(connection, committed)
    try:
        job_ops.drain_commands(store, result, str(inbox))
    finally:
        store.db = connection
    assert result['errors'] == ['drug_alias_commit_unknown']
    assert list(inbox.glob('*.json'))
    job_ops.drain_commands(store, result, str(inbox))
    assert not list(inbox.glob('*.json'))
    snapshot = ledger.publish_snapshot(str(tmp_path / 'data/ledger.db'), str(tmp_path / 'snapshots'))
    monkeypatch.setattr(mcs_drug, 'HOME', str(tmp_path))
    assert mcs_drug.main(['alias-receipt', req['command_id'], '--snapshot', snapshot]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status['outcome'] == 'unknown' and status['recovery_journal'] == 'present'
    assert status['recorded_receipt']['outcome'] == ('applied' if committed else 'rejected')
    assert (tmp_path / 'data/drug-map/alias-switch.pending.json').exists()


def test_queue_stops_if_unknown_commit_left_transaction_open(corpus, tmp_path):
    store, cfg, _ = corpus
    req = request(drug_alias.proposal(store.db, cfg, 'キラナン', 'fiction-i1'))
    inbox = tmp_path / 'data' / 'cmd'
    inbox.mkdir()
    mcs_requests.enqueue(req, inbox)
    result = {'errors': []}
    connection = store.db
    store.db = _commit_fault(connection, None)
    try:
        with pytest.raises(mcs_requests.AliasCommitUnknown):
            job_ops.drain_commands(store, result, str(inbox))
    finally:
        store.db = connection
    assert result['errors'] == ['drug_alias_commit_unknown']
    assert connection.in_transaction and list(inbox.glob('*.json'))
    connection.rollback()


@pytest.mark.parametrize('problem', ['invalid', 'capacity'])
def test_unreadable_retirements_hold_only_alias_command(corpus, tmp_path, monkeypatch, problem):
    store, cfg, config = corpus
    req = request(drug_alias.proposal(store.db, cfg, 'キラナン', 'fiction-i1'))
    other = {'version': 1, 'cmd': 'ops.pause', 'command_id': str(uuid.uuid4()),
             'actor': 'synthetic', 'human_confirmed': True,
             'project_id': 1, 'feature': 'semantic'}
    monkeypatch.setattr(mcs_requests, 'ALIAS_RETIRED_LIMIT', 2)
    entries = ['invalid'] if problem == 'invalid' else [str(uuid.uuid4()) for _ in range(3)]
    with store.db:
        store.db.executemany('INSERT INTO artifacts(kind,content,created_at) VALUES(?,?,0)',
                             [(mcs_requests.ALIAS_RETIRED_KIND, value) for value in entries])
    inbox = tmp_path / 'data' / 'cmd'
    inbox.mkdir()
    mcs_requests.enqueue(req, inbox)
    mcs_requests.enqueue(other, inbox)
    before = config.read_bytes()
    result = {'errors': []}
    job_ops.drain_commands(store, result, str(inbox))
    remaining = list(inbox.glob('*.json'))
    assert len(remaining) == 1 and json.loads(remaining[0].read_text()) == req
    assert store.db.execute('SELECT 1 FROM command_receipts WHERE command_id=?',
                            (req['command_id'],)).fetchone() is None
    other_receipt = store.db.execute('SELECT outcome FROM command_receipts WHERE command_id=?',
                                     (other['command_id'],)).fetchone()
    assert other_receipt['outcome'] == 'applied'
    assert config.read_bytes() == before
    assert result['errors'] == [f'alias_retirement_{problem}']
    job_ops.drain_commands(store, result, str(inbox))
    assert result['errors'] == [f'alias_retirement_{problem}']
    assert len(list(inbox.glob('*.json'))) == 1


def test_retirement_error_with_open_transaction_stops_queue(corpus, tmp_path):
    store, cfg, _ = corpus
    req = request(drug_alias.proposal(store.db, cfg, 'キラナン', 'fiction-i1'))
    other = {'version': 1, 'cmd': 'ops.pause', 'command_id': str(uuid.uuid4()),
             'actor': 'synthetic', 'human_confirmed': True,
             'project_id': 1, 'feature': 'semantic'}
    with store.db:
        store.db.execute('INSERT INTO artifacts(kind,content,created_at) VALUES(?,?,0)',
                         (mcs_requests.ALIAS_RETIRED_KIND, 'invalid'))
    inbox = tmp_path / 'data' / 'cmd'
    inbox.mkdir()
    mcs_requests.enqueue(req, inbox)
    mcs_requests.enqueue(other, inbox)
    connection = store.db
    class LeaveOpen:
        def __getattr__(self, name):
            return getattr(connection, name)
        def __enter__(self):
            connection.__enter__()
            return self
        def __exit__(self, kind, value, tb):
            return False
    store.db = LeaveOpen()
    result = {'errors': []}
    try:
        with pytest.raises(mcs_requests.AliasRetirementUnavailable):
            job_ops.drain_commands(store, result, str(inbox))
    finally:
        store.db = connection
    assert connection.in_transaction and result['errors'] == ['alias_retirement_invalid']
    assert len(list(inbox.glob('*.json'))) == 2
    assert connection.execute('SELECT 1 FROM command_receipts WHERE command_id=?',
                              (other['command_id'],)).fetchone() is None
    connection.rollback()


def test_queued_rollback_retires_earlier_unknown_apply_before_retry(corpus, tmp_path):
    store, cfg, config = corpus
    req = request(drug_alias.proposal(store.db, cfg, 'キラナン', 'fiction-i1'))
    recovery = rollback(req['command_id'])
    inbox = tmp_path / 'data' / 'cmd'
    inbox.mkdir()
    mcs_requests.enqueue(req, inbox)
    mcs_requests.enqueue(recovery, inbox)
    connection = store.db
    class OneFault:
        failed = False
        def __getattr__(self, name):
            return getattr(connection, name)
        def __enter__(self):
            connection.__enter__()
            return self
        def __exit__(self, kind, value, tb):
            if kind is None and not self.failed:
                self.failed = True
                connection.rollback()
                raise OSError('synthetic indeterminate commit')
            return connection.__exit__(kind, value, tb)
    store.db = OneFault()
    result = {'errors': []}
    try:
        job_ops.drain_commands(store, result, str(inbox))
    finally:
        store.db = connection
    assert result['errors'] == ['drug_alias_commit_unknown']
    assert len(list(inbox.glob('*.json'))) == 1
    assert req['command_id'] in mcs_requests.alias_retired_commands(connection)
    assert json.loads(config.read_text()) == cfg
    job_ops.drain_commands(store, result, str(inbox))
    assert not list(inbox.glob('*.json')) and json.loads(config.read_text()) == cfg
    assert 'cmd_invalid: alias_recovery_retired' in result['errors']


def test_legacy_journal_rollback_retires_unreceipted_apply(corpus):
    store, cfg, config = corpus
    req = _uncertain(store, cfg, False)
    journal = config.parent / 'data/drug-map/alias-switch.pending.json'
    assert 'payload_hash' not in json.loads(journal.read_text())
    assert mcs_requests.apply_command(store, rollback(req['command_id']))['outcome'] == 'applied'
    assert not journal.exists() and json.loads(config.read_text()) == cfg
    for _ in range(2):
        result = mcs_requests.apply_command(store, req)
        assert result['error'] == 'alias_recovery_retired'
        assert json.loads(config.read_text()) == cfg
    other = request(req['proposal'])
    assert mcs_requests.apply_command(store, other)['outcome'] == 'applied'


def test_rollback_keeps_applied_receipt(corpus):
    store, cfg, config = corpus
    req = _uncertain(store, cfg, True)
    row = store.db.execute('SELECT receipt_json FROM command_receipts WHERE command_id=?',
                           (req['command_id'],)).fetchone()[0]
    assert mcs_requests.apply_command(store, rollback(req['command_id']))['outcome'] == 'applied'
    assert store.db.execute('SELECT receipt_json FROM command_receipts WHERE command_id=?',
                            (req['command_id'],)).fetchone()[0] == row
    assert not mcs_requests.alias_retired_commands(store.db)
    assert json.loads(config.read_text()) == cfg


@pytest.mark.parametrize('committed', [False, True])
def test_recovery_receipt_and_retirement_share_commit(corpus, committed):
    store, cfg, config = corpus
    req = _uncertain(store, cfg, False)
    recovery = rollback(req['command_id'])
    connection = store.db
    store.db = _commit_fault(connection, committed)
    try:
        with pytest.raises(mcs_requests.AliasCommitUnknown):
            mcs_requests.apply_command(store, recovery)
    finally:
        store.db = connection
    assert (req['command_id'] in mcs_requests.alias_retired_commands(connection)) is committed
    assert (connection.execute('SELECT 1 FROM command_receipts WHERE command_id=?',
                               (recovery['command_id'],)).fetchone() is not None) is committed
    assert (config.parent / 'data/drug-map/alias-switch.pending.json').exists()


def test_retirement_survives_restart_and_normal_pruning(corpus):
    import maintenance
    store, cfg, config = corpus
    req = _uncertain(store, cfg, False)
    assert mcs_requests.apply_command(store, rollback(req['command_id']))['outcome'] == 'applied'
    maintenance.prune_history(store.db)
    store.db.commit()
    db_path = config.parent / 'data' / 'ledger.db'
    reopened = ledger.Ledger(str(db_path))
    try:
        assert mcs_requests.apply_command(reopened, req)['error'] == 'alias_recovery_retired'
    finally:
        reopened.close()


def test_retirement_capacity_blocks_recovery_without_changing_config(corpus, monkeypatch):
    store, cfg, config = corpus
    req = _uncertain(store, cfg, False)
    before = config.read_bytes()
    monkeypatch.setattr(mcs_requests, 'ALIAS_RETIRED_LIMIT', 2)
    monkeypatch.setattr(drug_alias, 'ALIAS_RETIRED_LIMIT', 2)
    with store.db:
        store.db.executemany('INSERT INTO artifacts(kind,content,created_at) VALUES(?,?,0)',
                             [(mcs_requests.ALIAS_RETIRED_KIND, str(uuid.uuid4())) for _ in range(2)])
    result = mcs_requests.apply_command(store, rollback(req['command_id']))
    assert result['error'] == 'alias_retirement_capacity'
    assert config.read_bytes() == before
    assert (config.parent / 'data/drug-map/alias-switch.pending.json').exists()


def test_retirement_bound_and_kind_index(corpus):
    store, _, _ = corpus
    plan = store.db.execute(
        'EXPLAIN QUERY PLAN SELECT typeof(content),length(CAST(content AS BLOB)),'
        'substr(content,1,37) FROM artifacts WHERE kind=? LIMIT ?',
        (mcs_requests.ALIAS_RETIRED_KIND, mcs_requests.ALIAS_RETIRED_LIMIT + 1)).fetchall()
    assert any('SEARCH artifacts USING INDEX idx_artifacts_' in row[3] for row in plan)
    with store.db:
        store.db.executemany('INSERT INTO artifacts(kind,content,created_at) VALUES(?,?,0)',
                             [(mcs_requests.ALIAS_RETIRED_KIND, str(uuid.uuid4()))
                              for _ in range(mcs_requests.ALIAS_RETIRED_LIMIT + 1)])
    with pytest.raises(ValueError, match='alias_retirement_capacity'):
        mcs_requests.alias_retired_commands(store.db)


@pytest.mark.parametrize('damage', ['invalid', 'duplicate', 'nul_suffix'])
def test_duplicate_or_invalid_retirement_evidence_blocks_apply(corpus, damage):
    store, cfg, config = corpus
    proposed = drug_alias.proposal(store.db, cfg, 'キラナン', 'fiction-i1')
    value = str(uuid.uuid4())
    values = {'invalid': ['invalid'], 'duplicate': [value, value], 'nul_suffix': [value + '\x00extra']}[damage]
    with store.db:
        store.db.executemany('INSERT INTO artifacts(kind,content,created_at) VALUES(?,?,0)',
                             [(mcs_requests.ALIAS_RETIRED_KIND, value) for value in values])
    before = config.read_bytes()
    with pytest.raises(RuntimeError, match='alias_retirement_unavailable'):
        mcs_requests.apply_command(store, request(proposed))
    assert config.read_bytes() == before
