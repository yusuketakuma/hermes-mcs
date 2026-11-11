"""quarantine子bootstrap (M6/W10 -I -S entry) を合成env/fakeで検証する。

実process・実Q lock・実sysctlは使わない。acquire/birth_fn/mainは注入。
"""
import json
import os
import sys

import pytest

import quarantine_bootstrap as qb

RECEIPT = '11111111-2222-4333-8444-555555555555'
OP = 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'
NONCE = 'a' * 64
BOOT = 'b' * 64
BIRTH = 'c' * 64
INSTANCE = '99999999-8888-4777-8666-555555555555'


def _env(**over):
    env = {'MCS_Q_RECEIPT_ID': RECEIPT, 'MCS_Q_OPERATION_ID': OP,
           'MCS_Q_STARTUP_NONCE': NONCE, 'MCS_Q_GENERATION': '7',
           'MCS_Q_BOOT_ID': BOOT, 'MCS_Q_PYCACHE_PREFIX': '/tmp/pc',
           'MCS_Q_SERVICE_ID': 'svc-a', 'MCS_Q_SERVICE_KIND': 'resident',
           'MCS_Q_RUNTIME_HOME': '/rt', 'MCS_Q_STATE_DIR': '/st',
           'MCS_Q_SOURCE_ROOT': '/src'}
    env.update(over)
    return env


def _active(**over):
    a = {'operation_id': OP, 'startup_nonce': NONCE,
         'activation_receipt_id': RECEIPT, 'candidate_generation': 7,
         'phase': 'APPLIED'}
    a.update(over)
    return a


_DEFAULT = object()


class FakeEntry:
    def __init__(self, owner=_DEFAULT, managed=True):
        self.owner = {'active': _active()} if owner is _DEFAULT else owner
        self.managed = managed
        self.closed = False

    def close(self):
        self.closed = True


# --- validate_env -------------------------------------------------------------

def test_validate_env_happy():
    b = qb.validate_env(_env())
    assert b['GENERATION'] == 7 and b['SERVICE_KIND'] == 'resident'


@pytest.mark.parametrize('key', qb._REQUIRED)
def test_validate_env_missing_fail_closed(key):
    env = _env()
    del env['MCS_Q_' + key]
    with pytest.raises(qb.BootstrapError):
        qb.validate_env(env)


def test_validate_env_rejects_non_v4_uuid():
    with pytest.raises(qb.BootstrapError):
        qb.validate_env(_env(MCS_Q_OPERATION_ID='not-a-uuid'))


def test_validate_env_rejects_v5_uuid():
    # Uuid schemaはv4のみ: uuid5形を拒否
    with pytest.raises(qb.BootstrapError):
        qb.validate_env(_env(MCS_Q_RECEIPT_ID='11111111-2222-5333-8444-555555555555'))


def test_validate_env_rejects_bad_nonce_kind_generation():
    with pytest.raises(qb.BootstrapError):
        qb.validate_env(_env(MCS_Q_STARTUP_NONCE='xyz'))
    with pytest.raises(qb.BootstrapError):
        qb.validate_env(_env(MCS_Q_SERVICE_KIND='bogus'))
    with pytest.raises(qb.BootstrapError):
        qb.validate_env(_env(MCS_Q_GENERATION='0'))
    with pytest.raises(qb.BootstrapError):
        qb.validate_env(_env(MCS_Q_GENERATION='x'))


# --- pycache / entry ----------------------------------------------------------

def test_check_pycache_match_and_mismatch(monkeypatch):
    monkeypatch.setattr(sys, 'pycache_prefix', '/tmp/pc')
    qb.check_pycache('/tmp/pc')
    with pytest.raises(qb.BootstrapError) as e:
        qb.check_pycache('/tmp/other')
    assert e.value.code == 'PYCACHE_MISMATCH'


def test_verify_entry_binding_matrix():
    b = qb.validate_env(_env())
    qb.verify_entry(FakeEntry(), b)
    for owner in ({'active': None}, {}, None):
        with pytest.raises(qb.BootstrapError):
            qb.verify_entry(FakeEntry(owner=owner), b)
    with pytest.raises(qb.BootstrapError):
        qb.verify_entry(FakeEntry(managed=False), b)
    for field, val in (('operation_id', 'x'), ('startup_nonce', 'y'),
                       ('activation_receipt_id', RECEIPT.replace('1', '2', 1)),
                       ('candidate_generation', 8), ('phase', 'STARTED')):
        e = FakeEntry(owner={'active': _active(**{field: val})})
        with pytest.raises(qb.BootstrapError):
            qb.verify_entry(e, b)


# --- witness / heartbeat --------------------------------------------------------

def test_write_witness_atomic_and_parent_compatible(tmp_path):
    b = qb.validate_env(_env(MCS_Q_STATE_DIR=str(tmp_path)))
    path = qb.write_witness(b['STATE_DIR'], b, pid=1234, birth=BIRTH,
                            process_instance_id=INSTANCE,
                            loaded_source_sha256='d' * 64, state='running',
                            observed_at=1000)
    doc = json.loads(open(path, 'rb').read())
    assert doc['schema_version'] == 'mcs.quarantine-witness.v3'
    assert doc['receipt_id'] == RECEIPT and doc['pid'] == 1234
    assert not os.path.exists(path + '.tmp')


def test_append_heartbeat_appends_jsonl(tmp_path):
    b = qb.validate_env(_env(MCS_Q_STATE_DIR=str(tmp_path)))
    qb.append_heartbeat(b['STATE_DIR'], b, 100)
    qb.append_heartbeat(b['STATE_DIR'], b, 200)
    path = os.path.join(str(tmp_path), 'quarantine', RECEIPT,
                        'heartbeat-svc-a.jsonl')
    lines = open(path).read().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[1])['heartbeat_at'] == 200


# --- run() ----------------------------------------------------------------------

def _source_root(tmp_path):
    root = tmp_path / 'src'
    (root / 'mcs').mkdir(parents=True)
    (root / 'mcs' / 'core').mkdir()
    (root / 'mcs' / 'core' / 'runtime_entry.py').write_text('# stub\n')
    (root / 'mcs' / 'ops').mkdir()
    (root / 'mcs' / 'ops' / 'release_quarantine.py').write_text('# stub\n')
    (root / 'mcs' / '_mcs_path.py').write_text('# stub\n')
    (root / 'app.py').write_text('x=1\n')
    return root


def test_run_writes_witness_and_calls_main(tmp_path, monkeypatch):
    src = _source_root(tmp_path)
    state = tmp_path / 'st'
    monkeypatch.setattr(sys, 'pycache_prefix', '/tmp/pc')
    env = _env(MCS_Q_STATE_DIR=str(state), MCS_Q_SOURCE_ROOT=str(src))
    entry = FakeEntry()
    calls = []
    # enter_source_pathは_mcs_path stubでflat登録をskipさせるためsource_root検証のみ通す
    monkeypatch.setattr(qb, 'enter_source_path', lambda root: None)
    monkeypatch.setattr(qb, 'default_birth', lambda pid: BIRTH)
    # measure_treeはstub moduleをimportできないためrun経路ではimport失敗する。
    # → measure_treeをモンキーパッチして親子整合を保つ
    import release_quarantine as rq
    monkeypatch.setattr(rq, 'measure_tree', lambda root: 'd' * 64)
    # run内のfrom importは既にpatch済みmoduleを使う
    result = qb.run(env, acquire=lambda home: entry,
                    birth_fn=lambda pid: BIRTH, main=lambda e, en: calls.append(1),
                    process_instance_id=INSTANCE, now_ms=lambda: 555)
    assert result is entry
    assert calls == [1]
    witness = json.loads(open(os.path.join(
        str(state), 'quarantine', RECEIPT, 'svc-a.json'), 'rb').read())
    assert witness['process_instance_id'] == INSTANCE
    assert witness['birth_witness'] == BIRTH
    assert witness['loaded_source_sha256'] == 'd' * 64
    assert witness['state'] == 'running'
    hb = os.path.join(str(state), 'quarantine', RECEIPT,
                      'heartbeat-svc-a.jsonl')
    assert os.path.exists(hb)


def test_run_binding_mismatch_holds_and_closes(tmp_path, monkeypatch):
    src = _source_root(tmp_path)
    state = tmp_path / 'st'
    monkeypatch.setattr(sys, 'pycache_prefix', '/tmp/pc')
    monkeypatch.setattr(qb, 'enter_source_path', lambda root: None)
    monkeypatch.setattr(qb, 'default_birth', lambda pid: BIRTH)
    entry = FakeEntry(owner={'active': _active(operation_id='bad')})
    env = _env(MCS_Q_STATE_DIR=str(state), MCS_Q_SOURCE_ROOT=str(src))
    with pytest.raises(qb.BootstrapError) as e:
        qb.run(env, acquire=lambda home: entry,
               birth_fn=lambda pid: BIRTH, now_ms=lambda: 1)
    assert e.value.code == 'BINDING_MISMATCH'
    assert entry.closed


def test_run_pycache_mismatch_rejects_before_acquire(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, 'pycache_prefix', '/tmp/WRONG')
    called = []
    env = _env(MCS_Q_SOURCE_ROOT=str(_source_root(tmp_path)))
    with pytest.raises(qb.BootstrapError) as e:
        qb.run(env, acquire=lambda home: called.append(1),
               birth_fn=lambda pid: BIRTH)
    assert e.value.code == 'PYCACHE_MISMATCH'
    assert called == []


def test_run_timer_and_disabled_states(tmp_path, monkeypatch):
    src = _source_root(tmp_path)
    state = tmp_path / 'st'
    monkeypatch.setattr(sys, 'pycache_prefix', '/tmp/pc')
    monkeypatch.setattr(qb, 'enter_source_path', lambda root: None)
    monkeypatch.setattr(qb, 'default_birth', lambda pid: BIRTH)
    import release_quarantine as rq
    monkeypatch.setattr(rq, 'measure_tree', lambda root: 'd' * 64)
    for kind, want in (('timer', 'armed'), ('disabled', 'stopped')):
        env = _env(MCS_Q_STATE_DIR=str(state), MCS_Q_SOURCE_ROOT=str(src),
                   MCS_Q_SERVICE_KIND=kind)
        entry = FakeEntry()
        qb.run(env, acquire=lambda home: entry,
               birth_fn=lambda pid: BIRTH,
               process_instance_id='88888888-7777-4666-8555-444444444444',
               now_ms=lambda: 1)
        doc = json.loads(open(os.path.join(
            str(state), 'quarantine', RECEIPT, 'svc-a.json'), 'rb').read())
        assert doc['state'] == want
