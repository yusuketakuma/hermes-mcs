"""update_cliのv3 status DTO表示とlegacy auto抑止配線を合成Storeで検証する。

実config・実SDC state・実更新経路には触れない。mcs_util.load_configと
journal_store/_current_versionだけをstubし、Storeはtmp_path配下の合成root。
"""
import json

import update_cli
from adapters.sdc.storage import Store
from adapters.sdc import release_auto_state as state

H = 'a' * 64
U = '12345678-1234-4234-8234-123456789abc'
COMMIT = 'b' * 40


def _config(monkeypatch, root):
    cfg = {} if root is None else {'sdc': {'state_root': str(root)}}
    monkeypatch.setattr(update_cli.mcs_util, 'load_config', lambda: cfg)


def _owner_value(store):
    ref = {'release_id': H, 'code_seq': 1, 'source_commit': COMMIT,
           'artifact_sha256': 'c' * 64, 'artifact_size': 1024}
    return {
        'schema_version': 'mcs.release-owner.v3',
        'scope': {'product': 'hermes-mcs', 'tenant': 'tenant-a',
                  'installation': 'inst-a', 'device_id': U,
                  'realm_epoch': 'realm-1', 'environment': 'synthetic',
                  'stream_id': 'st'},
        'root_fingerprint': store.root_fingerprint(),
        'repository_fingerprint': 'd' * 64, 'activation_sha256': 'e' * 64,
        'control_closure_sha256': 'f' * 64,
        'revision': 1, 'execution_revision': 1,
        'trust_json': '{}', 'legacy_evidence': [], 'operation_evidence': [],
        'lock_fingerprints': {k: '0' * 64 for k in 'GUQRCAMO'},
        'installed_source_manifest_sha256': '1' * 64,
        'verified_source_manifest_sha256': '2' * 64,
        'profile': {'profile_id': 'auto-mini', 'profile_sha256': '3' * 64,
                    'runtime_mode': 'standalone', 'os': 'macos',
                    'cpu': 'arm64', 'python': [3, 14],
                    'control_closure_sha256': '4' * 64},
        'consent': None,
        'highest_seen': dict(ref), 'installed': dict(ref),
        'highest_installed': dict(ref), 'verified_installed': dict(ref),
        'runtime_generation': 1,
        'bindings': [{'code_seq': 1, 'release_id': H}],
        'active': None, 'spent': [], 'outcomes': [], 'outbox': [],
        'prepare_budget': []}


def test_v3_unconfigured_means_unmigrated(tmp_path, monkeypatch):
    _config(monkeypatch, None)
    assert update_cli._v3_state_root() is None
    assert update_cli._v3_auto_blocked() is False
    assert update_cli._v3_status_dto() is None


def test_v3_missing_dir_is_unmigrated_not_blocked(tmp_path, monkeypatch):
    root = tmp_path / 'sdc-never-created'
    _config(monkeypatch, root)
    assert update_cli._v3_auto_blocked() is False
    dto = update_cli._v3_status_dto()
    assert dto['state'] == 'uninitialized'


def test_v3_empty_store_does_not_block_legacy_auto(tmp_path, monkeypatch):
    with Store(tmp_path / 'sdc', create=True):
        pass
    _config(monkeypatch, tmp_path / 'sdc')
    # release-owner.json不在=未移行 → legacy autoは従来どおり
    assert update_cli._v3_auto_blocked() is False
    assert update_cli._v3_status_dto()['state'] == 'uninitialized'


def test_v3_owner_state_blocks_legacy_auto(tmp_path, monkeypatch):
    root = tmp_path / 'sdc'
    with Store(root, create=True) as store:
        store.write(state.OWNER_NAME, _owner_value(store))
    _config(monkeypatch, root)
    assert update_cli._v3_auto_blocked() is True
    dto = update_cli._v3_status_dto()
    assert dto['state'] == 'idle'
    assert dto['phase'] is None


def test_v3_unreadable_owner_blocks_legacy_auto(tmp_path, monkeypatch):
    root = tmp_path / 'sdc'
    with Store(root, create=True) as store:
        store.write(state.OWNER_NAME, {'bogus': True})
    _config(monkeypatch, root)
    # parse不能=読取不能 → fail-closedで抑止、DTOはunreadable
    assert update_cli._v3_auto_blocked() is True
    assert update_cli._v3_status_dto()['state'] == 'unreadable'


def test_v3_store_open_failure_blocks_legacy_auto(tmp_path, monkeypatch):
    root = tmp_path / 'sdc'
    root.mkdir(mode=0o700)
    _config(monkeypatch, root)
    # dirはあるがStoreのprivate-root検査(0700以外等)で開けない → 抑止
    import os
    os.chmod(root, 0o755)
    assert update_cli._v3_auto_blocked() is True
    assert update_cli._v3_status_dto()['state'] == 'unreadable'


def test_cmd_status_includes_v3_dto(tmp_path, monkeypatch, capsys):
    root = tmp_path / 'sdc'
    with Store(root, create=True) as store:
        store.write(state.OWNER_NAME, _owner_value(store))
    _config(monkeypatch, root)
    monkeypatch.setattr(update_cli.journal_store, 'load_state',
                        lambda path: {})
    monkeypatch.setattr(update_cli, '_current_version',
                        lambda: ('v9.9.9', 'd' * 40))
    assert update_cli.cmd_status() == 0
    out = json.loads(capsys.readouterr().out)
    assert out['v3']['state'] == 'idle'
    assert out['v3']['schema_version'] == 'mcs.release-status-local.v1'


def test_cmd_status_omits_v3_when_unconfigured(tmp_path, monkeypatch, capsys):
    _config(monkeypatch, None)
    monkeypatch.setattr(update_cli.journal_store, 'load_state',
                        lambda path: {})
    monkeypatch.setattr(update_cli, '_current_version',
                        lambda: ('v9.9.9', 'd' * 40))
    assert update_cli.cmd_status() == 0
    out = json.loads(capsys.readouterr().out)
    assert 'v3' not in out
