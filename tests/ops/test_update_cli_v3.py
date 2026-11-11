"""update_cliのv3 status DTO表示とlegacy auto抑止配線を合成Storeで検証する。

実config・実SDC state・実control root・実更新経路には触れない。
runtime_entry.control_rootをtmp_path配下へ差し替え、Storeはその配下の合成root。
v3正本はcontrol_root(runtime_home)配下 — sdc.state_rootではない(spec 02/03)。
"""
import json
import time

import pytest

import runtime_entry
import update_cli
from adapters.sdc.storage import Store
from adapters.sdc import release_auto_state as state

H = 'a' * 64
U = '12345678-1234-4234-8234-123456789abc'
COMMIT = 'b' * 40


@pytest.fixture
def control_root(tmp_path, monkeypatch):
    """runtime_entry.control_rootをtmp_path/'ctl'へ固定する。"""
    root = tmp_path / 'ctl'
    monkeypatch.setattr(runtime_entry, 'control_root',
                        lambda *a, **k: root)
    return root


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


def test_v3_control_root_resolution(control_root):
    # control_root()の解決結果をそのまま返す — dir存在判定は呼出側。
    assert update_cli._v3_state_root() == str(control_root)


def test_v3_missing_dir_is_unmigrated_not_blocked(control_root):
    assert not control_root.exists()
    assert update_cli._v3_auto_blocked() is False
    dto = update_cli._v3_status_dto()
    assert dto['state'] == 'uninitialized'


def test_v3_empty_store_does_not_block_legacy_auto(control_root):
    with Store(control_root, create=True):
        pass
    # release-owner.json不在=未移行 → legacy autoは従来どおり
    assert update_cli._v3_auto_blocked() is False
    assert update_cli._v3_status_dto()['state'] == 'uninitialized'


def test_v3_owner_state_blocks_legacy_auto(control_root):
    with Store(control_root, create=True) as store:
        store.write(state.OWNER_NAME, _owner_value(store))
    assert update_cli._v3_auto_blocked() is True
    dto = update_cli._v3_status_dto()
    assert dto['state'] == 'idle'
    assert dto['phase'] is None


def test_v3_unreadable_owner_blocks_legacy_auto(control_root):
    with Store(control_root, create=True) as store:
        store.write(state.OWNER_NAME, {'bogus': True})
    # parse不能=読取不能 → fail-closedで抑止、DTOはunreadable
    assert update_cli._v3_auto_blocked() is True
    assert update_cli._v3_status_dto()['state'] == 'unreadable'


def test_v3_store_open_failure_blocks_legacy_auto(control_root):
    control_root.mkdir(mode=0o700)
    # dirはあるがStoreのprivate-root検査(0700以外等)で開けない → 抑止
    import os
    os.chmod(control_root, 0o755)
    assert update_cli._v3_auto_blocked() is True
    assert update_cli._v3_status_dto()['state'] == 'unreadable'


def test_v3_ignores_sdc_state_root(control_root, tmp_path, monkeypatch):
    """sdc.state_rootにowner stateがあってもv3正本ではない — control rootのみ見る。"""
    decoy = tmp_path / 'sdc-state'
    with Store(decoy, create=True) as store:
        store.write(state.OWNER_NAME, _owner_value(store))
    monkeypatch.setattr(update_cli.mcs_util, 'load_config',
                        lambda: {'sdc': {'state_root': str(decoy)}})
    # control root未作成 → decoyがあっても未移行扱い(fail-closed方向でない)
    assert update_cli._v3_auto_blocked() is False
    assert update_cli._v3_status_dto()['state'] == 'uninitialized'


def test_cmd_status_includes_v3_dto(control_root, monkeypatch, capsys):
    with Store(control_root, create=True) as store:
        store.write(state.OWNER_NAME, _owner_value(store))
    monkeypatch.setattr(update_cli.journal_store, 'load_state',
                        lambda path: {})
    monkeypatch.setattr(update_cli, '_current_version',
                        lambda: ('v9.9.9', 'd' * 40))
    assert update_cli.cmd_status() == 0
    out = json.loads(capsys.readouterr().out)
    assert out['v3']['state'] == 'idle'
    assert out['v3']['schema_version'] == 'mcs.release-status-local.v1'


def test_cmd_status_shows_uninitialized_before_migration(control_root,
                                                        monkeypatch, capsys):
    monkeypatch.setattr(update_cli.journal_store, 'load_state',
                        lambda path: {})
    monkeypatch.setattr(update_cli, '_current_version',
                        lambda: ('v9.9.9', 'd' * 40))
    assert update_cli.cmd_status() == 0
    out = json.loads(capsys.readouterr().out)
    assert out['v3']['state'] == 'uninitialized'


def _consent_value():
    """_owner_value互換の最小StandingConsent(enabled=true, epoch=1)。"""
    return {
        'schema_version': 'mcs.standing-consent.v3',
        'consent_id': '9e8d7c6b-1234-4abc-8def-0123456789ab',
        'scope': {'product': 'hermes-mcs', 'tenant': 'tenant-a',
                  'installation': 'inst-a', 'device_id': U,
                  'realm_epoch': 'realm-1', 'environment': 'synthetic',
                  'stream_id': 'st'},
        'profile': {'profile_id': 'auto-mini', 'profile_sha256': '3' * 64,
                    'runtime_mode': 'standalone', 'os': 'macos',
                    'cpu': 'arm64', 'python': [3, 14],
                    'control_closure_sha256': '4' * 64},
        'allowed_class': 'code-only-compatible',
        'timezone': 'Asia/Tokyo',
        'windows': [{'weekday': 0, 'start_minute': 60, 'end_minute': 180}],
        'max_prepare_seconds': 120, 'max_prepare_count_24h': 3,
        'max_prepare_seconds_24h': 360,
        'local_consent_epoch': 1, 'policy_epoch': 1,
        'central_policy_id': '11111111-2222-4333-8444-555555555555',
        'central_policy_sha256': '5' * 64,
        'confirmation_receipt_id': '66666666-7777-4888-8999-000000000000',
        'confirmed_at': 1794268799000, 'expires_at': 1794355200000,
        'enabled': True, 'early_adopter': False,
        'rollout_policy_sha256': '6' * 64}


def test_cmd_auto_disable_revokes_consent(control_root, capsys):
    with Store(control_root, create=True) as store:
        value = _owner_value(store)
        value['consent'] = _consent_value()
        store.write(state.OWNER_NAME, value)
    assert update_cli.cmd_auto_disable(None) == 0
    out = json.loads(capsys.readouterr().out)
    assert out['status'] == 'ok' and out['action'] == 'disabled'
    assert out['local_consent_epoch'] == 2
    assert out['central_notified'] is False
    with Store(control_root) as store:
        owner = state.load(store)
    assert owner['consent']['enabled'] is False
    assert owner['consent']['local_consent_epoch'] == 2


def test_cmd_auto_disable_noop_without_consent(control_root, capsys):
    with Store(control_root, create=True) as store:
        store.write(state.OWNER_NAME, _owner_value(store))
    assert update_cli.cmd_auto_disable(None) == 0
    out = json.loads(capsys.readouterr().out)
    assert out['action'] == 'noop'


def test_cmd_auto_disable_unavailable_without_state(control_root, capsys):
    assert not control_root.exists()
    assert update_cli.cmd_auto_disable(None) == 1
    out = json.loads(capsys.readouterr().out)
    assert out['status'] == 'unavailable'


def _status_with_consent(control_root, consent):
    import release_auto_status as v3_status
    with Store(control_root, create=True) as store:
        value = _owner_value(store)
        value['consent'] = consent
        store.write(state.OWNER_NAME, value)
    with Store(control_root) as store:
        return v3_status.status_dto(store)['consent']


def test_status_consent_active(control_root):
    consent = _consent_value()
    consent['expires_at'] = int(time.time() * 1000) + 86_400_000
    assert _status_with_consent(control_root, consent) == 'active'


def test_status_consent_expired(control_root):
    consent = _consent_value()
    consent['expires_at'] = int(time.time() * 1000) - 1000
    assert _status_with_consent(control_root, consent) == 'expired'


def test_status_consent_revoked(control_root):
    consent = _consent_value()
    consent['enabled'] = False
    assert _status_with_consent(control_root, consent) == 'revoked'


def test_status_consent_none(control_root):
    assert _status_with_consent(control_root, None) is None
