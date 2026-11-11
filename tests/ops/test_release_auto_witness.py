"""v3 private witness deps面: provider合成とfail-closed検証。"""
import hashlib
import json
import os
from pathlib import Path

import pytest

from adapters.sdc.storage import Store
from release_auto_witness import PrivateWitness, WitnessError

EXAMPLES = (Path(__file__).resolve().parents[2]
            / 'adapters/sdc/contracts/mcs-auto/v3/examples')


def _example(name):
    return json.loads((EXAMPLES / name).read_text())['data']


SCOPE = _example('offer.json')['scope']
PROFILE = _example('standing-consent.json')['profile']
CURRENT = _example('prepared-public.json')['current_release']


def _sha(tag):
    return hashlib.sha256(tag.encode()).hexdigest()


def _ident(**over):
    base = {'device': 1, 'inode': 2, 'uid': 501, 'mode': 0o100644, 'nlink': 1,
            'size': 10, 'mtime_ns': 3, 'ctime_ns': 4}
    base.update(over)
    return base


def _witness(logical_id, tag):
    return {'logical_id': logical_id, 'identity': _ident(), 'sha256': _sha(tag)}


LOCKS = [_witness(f'lock-{k}', f'lock-{k}') for k in 'guqrcamo']
CONFIGS = [_witness('config-main', 'config')]
ACTIVE = {'operation_id': 'op-1', 'prepare_deadline': 1_700_000_000_000,
          'phase': 'PREPARING'}


def make_witness(store, ledger_file=None, **over):
    if ledger_file is None:
        ledger_file = store.root / 'ledger.db'
        ledger_file.write_bytes(b'x')
    providers = {
        'file_witnesses': lambda: {'locks': [dict(w) for w in LOCKS],
                                   'configs': [dict(w) for w in CONFIGS]},
        'source_facts': lambda: {'source_tree_sha256': _sha('tree'),
                                 'runtime_fingerprint': _sha('rt')},
        'ledger_stat': lambda: os.stat(ledger_file),
        'stage_stat': lambda: {'path': '/synthetic/stage',
                               'identity': _ident(size=4096)},
        'backup_detail': lambda: {'path': '/synthetic/backup.tar',
                                  'sha256': _sha('backup')},
        'retired_processes': lambda: [
            {'service_id': 'ai.mcs.extract-drainer',
             'process_instance_id': '12345678-1234-1234-1234-123456789abc',
             'generation': 3, 'boot_id': _sha('boot'),
             'birth_witness': _sha('birth'), 'retired': True}],
        'required_free_bytes': lambda: 1024,
        'reserved_owner_bytes': lambda: 4096,
    }
    providers.update(over)
    return PrivateWitness(store, **providers)


def seed_owner(store):
    consent = _example('standing-consent.json')
    value = {
        'schema_version': 'mcs.release-owner.v3',
        'scope': dict(SCOPE),
        'root_fingerprint': store.root_fingerprint(),
        'repository_fingerprint': _sha('repo'),
        'activation_sha256': _sha('act'),
        'control_closure_sha256': PROFILE['control_closure_sha256'],
        'revision': 1, 'execution_revision': 1,
        'trust_json': '{}', 'legacy_evidence': [], 'operation_evidence': [],
        'lock_fingerprints': {k: _sha(k) for k in 'GUQRCAMO'},
        'installed_source_manifest_sha256': _sha('src'),
        'verified_source_manifest_sha256': _sha('ver'),
        'profile': dict(PROFILE),
        'consent': dict(consent),
        'highest_seen': dict(CURRENT), 'installed': dict(CURRENT),
        'highest_installed': dict(CURRENT), 'verified_installed': dict(CURRENT),
        'runtime_generation': 1,
        'bindings': [{'code_seq': 1, 'release_id': CURRENT['release_id']}],
        'active': None, 'spent': [], 'outcomes': [], 'outbox': [],
        'prepare_budget': [],
    }
    store.write('release-owner.json', value)


def test_witness_assembles_all_fields(tmp_path):
    with Store(tmp_path / 'private-auto', create=True) as store:
        seed_owner(store)
        w = make_witness(store)
        result = w.witness_private(ACTIVE)
        assert result['root_fingerprint'] == store.root_fingerprint()
        assert result['repository_fingerprint'] == _sha('repo')
        assert len(result['lock_identities']) == 8
        assert len(result['configuration_files']) == 1
        led = os.stat(store.root / 'ledger.db')
        assert result['ledger_device'] == str(led.st_dev)
        assert result['ledger_inode'] == str(led.st_ino)
        assert result['stage_identity']['device'] == '1'
        assert result['stage_identity']['nlink'] == 1
        assert result['stage_path'] == '/synthetic/stage'
        assert result['backup_path'] == '/synthetic/backup.tar'
        assert result['retired_processes'][0]['retired'] is True
        assert result['required_free_bytes'] == 1024
        assert result['reserved_owner_bytes'] == 4096
        assert result['prepared_until'] == ACTIVE['prepare_deadline']
        # schema_version/publicはownerが付与 — ここでは含まない
        assert 'schema_version' not in result and 'public' not in result


def test_witness_rejects_missing_active_deadline(tmp_path):
    with Store(tmp_path / 'p', create=True) as store:
        seed_owner(store)
        w = make_witness(store)
        for bad in ({}, {'prepare_deadline': 'x'},
                    {'prepare_deadline': 0}, {'prepare_deadline': -1},
                    {'prepare_deadline': 1.5}):
            with pytest.raises(WitnessError):
                w.witness_private(bad)
        with pytest.raises(WitnessError):
            w.witness_private('not-a-dict')


def test_witness_requires_eight_locks(tmp_path):
    with Store(tmp_path / 'p', create=True) as store:
        seed_owner(store)
        for n in (7, 9):
            locks = [_witness(f'l{i}', f'l{i}') for i in range(n)]
            w = make_witness(
                store,
                file_witnesses=lambda locks=locks: {
                    'locks': locks, 'configs': CONFIGS})
            with pytest.raises(WitnessError) as e:
                w.witness_private(ACTIVE)
            assert e.value.code == 'WITNESS_LOCKS'


def test_witness_rejects_bad_file_witness(tmp_path):
    with Store(tmp_path / 'p', create=True) as store:
        seed_owner(store)
        bad = [dict(w) for w in LOCKS]
        bad[0] = {'logical_id': 'x', 'identity': {}, 'sha256': 'short'}
        w = make_witness(
            store,
            file_witnesses=lambda: {'locks': bad, 'configs': CONFIGS})
        with pytest.raises(WitnessError):
            w.witness_private(ACTIVE)
        # configs空も拒否
        w2 = make_witness(
            store,
            file_witnesses=lambda: {'locks': LOCKS, 'configs': []})
        with pytest.raises(WitnessError):
            w2.witness_private(ACTIVE)


def test_witness_rejects_bad_source_facts(tmp_path):
    with Store(tmp_path / 'p', create=True) as store:
        seed_owner(store)
        w = make_witness(
            store,
            source_facts=lambda: {'source_tree_sha256': 'zz',
                                  'runtime_fingerprint': _sha('rt')})
        with pytest.raises(WitnessError):
            w.witness_private(ACTIVE)
        w2 = make_witness(store, source_facts=lambda: None)
        with pytest.raises(WitnessError):
            w2.witness_private(ACTIVE)


def test_witness_rejects_bad_stage(tmp_path):
    with Store(tmp_path / 'p', create=True) as store:
        seed_owner(store)
        # nlink!=1 はspec FileIdentity const違反として事前拒否
        w = make_witness(
            store,
            stage_stat=lambda: {'path': '/s',
                                'identity': _ident(nlink=2)})
        with pytest.raises(WitnessError):
            w.witness_private(ACTIVE)
        w2 = make_witness(store, stage_stat=lambda: {'identity': {}})
        with pytest.raises(WitnessError):
            w2.witness_private(ACTIVE)


def test_witness_rejects_bad_backup_and_retired(tmp_path):
    with Store(tmp_path / 'p', create=True) as store:
        seed_owner(store)
        w = make_witness(
            store,
            backup_detail=lambda: {'path': '/b', 'sha256': 'x'})
        with pytest.raises(WitnessError):
            w.witness_private(ACTIVE)
        w2 = make_witness(store, retired_processes=lambda: [
            {'service_id': 's', 'process_instance_id': 'p',
             'generation': 0, 'boot_id': 'b', 'birth_witness': 'w',
             'retired': True}])
        with pytest.raises(WitnessError):
            w2.witness_private(ACTIVE)
        w3 = make_witness(store, retired_processes=lambda: 'not-list')
        with pytest.raises(WitnessError):
            w3.witness_private(ACTIVE)


def test_witness_rejects_bad_capacity(tmp_path):
    with Store(tmp_path / 'p', create=True) as store:
        seed_owner(store)
        for kwargs in ({'required_free_bytes': lambda: -1},
                       {'reserved_owner_bytes': lambda: 0},
                       {'reserved_owner_bytes': lambda: 9 * 1024 * 1024}):
            w = make_witness(store, **kwargs)
            with pytest.raises(WitnessError):
                w.witness_private(ACTIVE)


def test_witness_provider_exception_is_wrapped(tmp_path):
    with Store(tmp_path / 'p', create=True) as store:
        seed_owner(store)

        def boom():
            raise OSError('gone')

        w = make_witness(store, file_witnesses=boom)
        with pytest.raises(WitnessError) as e:
            w.witness_private(ACTIVE)
        assert e.value.code == 'WITNESS_FILE_WITNESSES'
