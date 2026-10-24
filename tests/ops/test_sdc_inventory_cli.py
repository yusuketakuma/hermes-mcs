"""Exercise opt-in consent and real bootstrap routing with synthetic files."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

import mcs_cli
import sdc_inventory_cli
from adapters.sdc.inventory_sender import CONSENT, POLICY_SCHEMA, endpoint_hash, initial_status
from adapters.sdc.storage import Store

CONFIG = {'origin': 'https://ingest.invalid', 'audience': 'sdc:synthetic', 'environment': 'synthetic'}
DEVICE = '11111111-1111-4111-8111-111111111111'
REVISION = '33333333-3333-4333-8333-333333333333'
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def world(tmp_path, monkeypatch):
    root = tmp_path / 'private'
    cfg = {'runtime_mode': 'standalone', 'sdc': {'state_root': str(root)}, 'patient_body': 'SYNTHETIC_PRIVATE'}
    monkeypatch.setattr(sdc_inventory_cli.mcs_util, 'load_config', lambda: cfg)
    monkeypatch.setattr(sdc_inventory_cli.mcs_runtime, 'python_executable', lambda _: sys.executable)
    with Store(root, create=True) as store:
        store.write('endpoint.json', CONFIG)
        store.write('state.json', {'state': 'active', 'enabled': True, 'device_id': DEVICE})
    return root, cfg


def test_enable_requires_numbered_consent_and_persists_only_typed_public_policy(world, monkeypatch, capsys):
    root, _ = world
    monkeypatch.setattr('builtins.input', lambda _: '1')
    assert sdc_inventory_cli.main(['enable', '--public-package', 'Synthetic_Public', '--lock-package', 'synthetic-public==1.2.3']) == 0
    with Store(root) as store:
        policy = store.read('inventory-policy.json')
        assert policy['schema_version'] == POLICY_SCHEMA
        assert policy['consent_revision'] == CONSENT
        assert policy['public_packages'] == ['synthetic-public']
        assert policy['lock_entries'] == {'synthetic-public': '1.2.3'}
        assert policy['device_id'] == DEVICE
        assert policy['endpoint_hash'] == endpoint_hash(CONFIG)
        assert store.read('inventory-status.json')['next_due'] == 0
    text = capsys.readouterr().out
    assert '1: 許可する / 2: 中止する（既定）' in text
    assert 'SYNTHETIC_PRIVATE' not in text + json.dumps(policy)


@pytest.mark.parametrize('answer', ['', '2', 'yes'])
def test_nonapproval_never_enables_or_changes_policy(world, monkeypatch, answer):
    root, _ = world
    monkeypatch.setattr('builtins.input', lambda _: answer)
    assert sdc_inventory_cli.main(['enable', '--public-package', 'synthetic-public']) == 0
    with Store(root) as store:
        assert store.read('inventory-policy.json') is None


def test_enable_without_lock_records_unknown_not_empty_complete_lock(world, monkeypatch):
    root, _ = world
    monkeypatch.setattr('builtins.input', lambda _: '1')
    assert sdc_inventory_cli.main(['enable', '--public-package', 'synthetic-public']) == 0
    with Store(root) as store:
        assert store.read('inventory-policy.json')['lock_entries'] is None


@pytest.mark.parametrize('options', [[], ['--public-package', 'file:///SYNTHETIC_PRIVATE'],
                                   ['--public-package', 'synthetic-public', '--lock-package', 'private-package==1.0'],
                                   ['--public-package', 'synthetic-public', '--lock-package', 'synthetic-public==https://SYNTHETIC_PRIVATE']])
def test_missing_or_unsafe_package_inputs_do_not_echo_values(world, options, capsys):
    root, _ = world
    assert sdc_inventory_cli.main(['enable', *options]) == 1
    with Store(root) as store:
        assert store.read('inventory-policy.json') is None
    text = capsys.readouterr()
    assert 'SYNTHETIC_PRIVATE' not in text.out + text.err


def test_invalid_runtime_mode_cannot_enable_inventory(world, monkeypatch, capsys):
    root, cfg = world
    cfg['runtime_mode'] = 'SYNTHETIC_PRIVATE_INVALID_MODE'
    monkeypatch.setattr('builtins.input', lambda _: pytest.fail('consent must not open'))
    assert sdc_inventory_cli.main(['enable', '--public-package', 'synthetic-public']) == 1
    with Store(root) as store:
        assert store.read('inventory-policy.json') is None
    assert 'SYNTHETIC_PRIVATE' not in capsys.readouterr().err


def test_wrong_selected_interpreter_cannot_enable_inventory(world, monkeypatch):
    root, _ = world
    monkeypatch.setattr(sdc_inventory_cli.mcs_runtime, 'python_executable', lambda _: '/synthetic/other/python')
    monkeypatch.setattr('builtins.input', lambda _: pytest.fail('consent must not open'))
    assert sdc_inventory_cli.main(['enable', '--public-package', 'synthetic-public']) == 1
    with Store(root) as store:
        assert store.read('inventory-policy.json') is None


def test_same_venv_alias_is_allowed_but_other_venv_with_same_base_binary_is_rejected(world, tmp_path, monkeypatch):
    root, _ = world
    base = tmp_path / 'synthetic-base-python'
    base.write_text('synthetic binary identity')
    active, other = tmp_path / 'active-venv/bin', tmp_path / 'other-venv/bin'
    active.mkdir(parents=True)
    other.mkdir(parents=True)
    for target in (active / 'python', active / 'python3', other / 'python'):
        target.symlink_to(base)
    monkeypatch.setattr(sdc_inventory_cli.sys, 'executable', str(active / 'python3'))
    monkeypatch.setattr(sdc_inventory_cli.mcs_runtime, 'python_executable', lambda _: str(active / 'python'))
    monkeypatch.setattr('builtins.input', lambda _: '1')
    assert sdc_inventory_cli.main(['enable', '--public-package', 'synthetic-public']) == 0
    with Store(root) as store:
        revision = store.read('inventory-policy.json')['revision']
    monkeypatch.setattr(sdc_inventory_cli.mcs_runtime, 'python_executable', lambda _: str(other / 'python'))
    monkeypatch.setattr('builtins.input', lambda _: pytest.fail('other venv must not reach consent'))
    assert sdc_inventory_cli.main(['enable', '--public-package', 'synthetic-public']) == 1
    with Store(root) as store:
        assert store.read('inventory-policy.json')['revision'] == revision


def test_disable_is_available_even_with_invalid_mode_and_preserves_heartbeat_and_spool(world, capsys):
    root, cfg = world
    cfg['runtime_mode'] = 'invalid'
    with Store(root) as store:
        state = store.read('state.json')
        policy = {'schema_version': POLICY_SCHEMA, 'enabled': True, 'consent_revision': CONSENT,
                  'public_packages': ['synthetic-public'], 'lock_entries': None, 'revision': REVISION,
                  'approved_at': 1792713600000, 'device_id': DEVICE, 'endpoint_hash': endpoint_hash(CONFIG)}
        store.write('inventory-policy.json', policy)
        store.write('inventory-spool.json', {'synthetic_reserved_data': 'preserve'})
        store.write('inventory-status.json', initial_status(REVISION))
    assert sdc_inventory_cli.main(['disable']) == 0
    with Store(root) as store:
        assert store.read('inventory-policy.json')['enabled'] is False
        assert store.read('state.json') == state
        assert store.read('inventory-spool.json') == {'synthetic_reserved_data': 'preserve'}
    assert '通常の状態監視は継続' in capsys.readouterr().out


def test_status_only_reads_fixed_nonsecret_fields(world, capsys):
    root, _ = world
    with Store(root) as store:
        store.write('inventory-status.json', {'last_result': 'SYNTHETIC_PRIVATE_ERROR'})
    assert sdc_inventory_cli.main(['status']) == 0
    result = json.loads(capsys.readouterr().out)
    assert result == {'enabled': False, 'public_package_count': 0, 'lock_status': 'unknown', 'last_result': 'SDC_INVENTORY_DISABLED'}


def test_existing_cli_pins_selected_runtime_before_inventory_dispatch(monkeypatch):
    monkeypatch.delenv('MCS_LIFECYCLE_PINNED', raising=False)
    monkeypatch.setattr(mcs_cli, 'load_config', lambda: {'runtime_mode': 'standalone'})
    monkeypatch.setattr(mcs_cli.mcs_runtime, 'python_executable', lambda _: '/synthetic/selected/python')
    monkeypatch.setattr(mcs_cli.os, 'access', lambda *_: True)
    calls = []
    def execv(executable, argv):
        calls.append((executable, argv))
        raise RuntimeError('synthetic exec boundary')
    monkeypatch.setattr(mcs_cli.os, 'execv', execv)
    with pytest.raises(RuntimeError, match='synthetic exec boundary'):
        mcs_cli.main(['inventory', 'status'])
    assert calls[0][0] == '/synthetic/selected/python'
    assert calls[0][1][2:] == ['inventory', 'status']


def test_pinned_cli_dispatches_inventory_without_adding_parameters(monkeypatch):
    monkeypatch.setenv('MCS_LIFECYCLE_PINNED', '1')
    calls = []
    monkeypatch.setattr(sdc_inventory_cli, 'main', lambda args: calls.append(args) or 0)
    assert mcs_cli.main(['inventory', 'enable', '--public-package', 'synthetic-public']) == 0
    assert calls == [['enable', '--public-package', 'synthetic-public']]


def test_actual_bootstrap_accepts_inventory_and_forwards_literal_arguments(tmp_path):
    home = tmp_path / 'synthetic-home'
    executable = home / '.mcs/venv/bin/python3'
    executable.parent.mkdir(parents=True)
    executable.write_text(f'#!{sys.executable}\nimport json,sys\nprint(json.dumps(sys.argv[1:]))\n')
    executable.chmod(0o700)
    env = dict(os.environ, HOME=str(home), MCS_ROOT=str(home / '.mcs'))
    result = subprocess.run(['/bin/sh', str(ROOT / 'scripts/mcs'), 'inventory', 'status'],
                            env=env, capture_output=True, text=True, timeout=5)
    assert result.returncode == 0
    assert json.loads(result.stdout) == [str(ROOT / 'mcs/ops/mcs_cli.py'), 'inventory', 'status']


@pytest.mark.parametrize('argument', ['--state-root', '--url', '--path'])
def test_inventory_cli_has_no_arbitrary_path_or_url_option(argument):
    with pytest.raises(SystemExit) as result:
        sdc_inventory_cli.main(['enable', argument, '/synthetic/unapproved'])
    assert result.value.code == 2
