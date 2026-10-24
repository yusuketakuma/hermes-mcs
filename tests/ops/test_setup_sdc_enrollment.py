"""Optional monitoring never substitutes general setup consent for enrollment consent."""
import ast
from pathlib import Path
import shlex

import pytest

import mcs_setup
import sdc_cli
import setup_wizard
from adapters.sdc.storage import Store

CONFIG = {'origin': 'https://ingest.invalid', 'audience': 'synthetic', 'environment': 'synthetic'}


def test_missing_trusted_endpoint_does_not_prompt_write_or_connect(tmp_path, monkeypatch, capsys):
    root = tmp_path / 'missing'
    cfg = {'sdc': {'state_root': str(root)}, 'saved_core': 'synthetic'}
    monkeypatch.setattr('builtins.input', lambda _: pytest.fail('unconfigured prompt'))
    monkeypatch.setattr(sdc_cli, 'main', lambda _: pytest.fail('unconfigured connect'))
    setup_wizard.offer_sdc_enrollment(cfg)
    assert not root.exists()
    assert cfg['saved_core'] == 'synthetic'
    assert '未設定' in capsys.readouterr().out


@pytest.mark.parametrize('answer,flags', [('', None), ('3', None), ('1', None), ('2', [])])
def test_optional_entry_is_separate_from_actual_enrollment_consent(tmp_path, monkeypatch, capsys, answer, flags):
    root = tmp_path / 'private'
    with Store(root, create=True) as store:
        store.write('endpoint.json', CONFIG)
    calls = []
    monkeypatch.setattr('builtins.input', lambda _: answer)
    monkeypatch.setattr(sdc_cli, 'main', lambda argv: calls.append(argv) or 0)
    setup_wizard.offer_sdc_enrollment({'sdc': {'state_root': str(root)}})
    assert calls == ([['connect', '--state-root', str(root), *flags]] if flags is not None else [])
    # A customer menu choice is never turned into the credential-change confirmation.
    assert_customer_guidance(capsys.readouterr().out, root, shown=answer == '1')
    with Store(root) as store:
        assert store.read('enrollment-consent.json', {}) == {}
        assert store.customer is False


def assert_customer_guidance(out, root, *, shown):
    command = 'mcs connect --customer --state-root ' + shlex.quote(str(root)) + ' --confirm-human --reason '
    assert (command in out and '本体設定は保存済み' in out and '保留' in out) is shown
    assert 'SYNTHETIC' not in out


def test_optional_failure_preserves_core_config_and_receipt(tmp_path, monkeypatch, capsys):
    root = tmp_path / 'private'
    with Store(root, create=True) as store:
        store.write('endpoint.json', CONFIG)
        store.write('enrollment-consent.json', {'synthetic_receipt': True})
    cfg = {'sdc': {'state_root': str(root)}, 'saved_core': 'synthetic'}
    monkeypatch.setattr('builtins.input', lambda _: '2')
    monkeypatch.setattr(sdc_cli, 'main', lambda _: 1)
    setup_wizard.offer_sdc_enrollment(cfg)
    assert cfg['saved_core'] == 'synthetic'
    with Store(root) as store:
        assert store.read('enrollment-consent.json') == {'synthetic_receipt': True}
    assert '本体設定は保持' in capsys.readouterr().out


def test_init_optional_entry_is_guarded_by_interactive_success():
    # The updater's --yes path never interprets its general flag as SDC consent.
    tree = ast.parse(Path(mcs_setup.__file__).read_text())
    fn = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'cmd_init')
    calls = [node for node in ast.walk(fn) if isinstance(node, ast.If) and ast.unparse(node.test) == 'not args.yes' and any(isinstance(child, ast.Call) and ast.unparse(child.func) == 'setup_wizard.offer_sdc_enrollment' for child in ast.walk(node))]
    assert len(calls) == 1


@pytest.mark.parametrize('customer', [False, True])
def test_existing_credential_mode_is_kept_without_migration(tmp_path, monkeypatch, capsys, customer):
    root = tmp_path / 'private'
    with Store(root, create=True) as store:
        store.write('endpoint.json', CONFIG)
        if customer:
            store.enable_customer()
        else:
            store.write('credential', {'key': 'a1' * 32})
    calls = []
    monkeypatch.setattr('builtins.input', lambda _: '1')
    monkeypatch.setattr(sdc_cli, 'main', lambda argv: calls.append(argv) or 0)
    setup_wizard.offer_sdc_enrollment({'sdc': {'state_root': str(root)}})
    assert calls == ([] if customer else [['connect', '--state-root', str(root)]])
    assert_customer_guidance(capsys.readouterr().out, root, shown=customer)
    with Store(root) as store:
        assert store.customer is customer


@pytest.mark.parametrize('existing', [False, True])
@pytest.mark.parametrize('interrupt', [EOFError, KeyboardInterrupt])
def test_optional_selection_interrupt_keeps_successful_core_setup(tmp_path, monkeypatch, existing, interrupt):
    root = tmp_path / 'private'
    with Store(root, create=True) as store:
        store.write('endpoint.json', CONFIG)
        if existing:
            store.write('credential', {'key': 'a1' * 32})
    cfg = {'sdc': {'state_root': str(root)}, 'saved_core': 'synthetic'}
    def stop(_):
        raise interrupt()
    monkeypatch.setattr('builtins.input', stop)
    monkeypatch.setattr(sdc_cli, 'main', lambda _: pytest.fail('interrupted selection must not connect'))
    assert setup_wizard.offer_sdc_enrollment(cfg) is None
    assert cfg['saved_core'] == 'synthetic'
    with Store(root) as store:
        assert store.read('enrollment-consent.json', {}) == {}


def test_unissued_managed_provision_defers_without_reading_credentials_or_connecting(tmp_path, monkeypatch, capsys):
    from adapters.sdc import provision

    root = tmp_path / 'private'
    with Store(root, create=True) as store:
        store.write('endpoint.json', CONFIG)
    cfg = {'sdc': {'state_root': str(root)}, 'saved_core': 'synthetic'}
    def hold(store):
        raise ValueError('SDC_PROVISION_HOLD')
    monkeypatch.setattr(provision, 'monitor_endpoint', hold)
    monkeypatch.setattr(Store, 'read', lambda *a, **kw: pytest.fail('unissued provision must not read endpoint or credentials'))
    monkeypatch.setattr('builtins.input', lambda _: pytest.fail('unissued provision must not prompt registration'))
    monkeypatch.setattr(sdc_cli, 'main', lambda _: pytest.fail('unissued provision must not connect'))
    setup_wizard.offer_sdc_enrollment(cfg)
    output = capsys.readouterr().out
    assert cfg['saved_core'] == 'synthetic'
    assert '本体設定は保存済み' in output and '--prepare-provision-root' in output
    assert '--import-provision' in output and '既存領域や復元元は作り直しません' in output


@pytest.mark.parametrize('started', [False, True])
def test_managed_monitor_reopen_requires_reapproval_only_before_registration(tmp_path, monkeypatch, capsys, started):
    import copy
    import hashlib
    import json

    from adapters.sdc import provision
    from adapters.sdc.support_contract import canonical

    root = tmp_path / 'private'
    vectors = json.loads((Path(__file__).resolve().parents[2] /
                          'adapters/sdc/contracts/provision/provision-vectors.json').read_text())
    now = vectors['now']
    bundle = copy.deepcopy(next(item['value'] for item in vectors['positive']
                                if item['definition'] == 'Bundle' and item['value']['profile_id'] == 'monitor'))
    def forbidden(*args, **kwargs):
        pytest.fail('setup offer must not use customer, native, HTTP or service effects')
    monkeypatch.setattr(Store, '_keychain', forbidden)
    monkeypatch.setattr(Store, 'enable_customer', forbidden)
    monkeypatch.setattr(sdc_cli, 'Transport', forbidden)
    monkeypatch.setattr(sdc_cli, 'service', forbidden)
    with Store(root, create_new=True) as store:
        prepared = provision.prepare_root(store, confirm_human=True, reason='synthetic-setup-provision', now_ms=now)
        bundle['root_id'] = prepared['root_id']
        raw = canonical(bundle, max_bytes=16384).encode('ascii')
        expected = {key: bundle[key] for key in ('console_origin', 'tenant', 'installation', 'environment',
                                                 'enrollment_id', 'profile_id', 'origin', 'audience')}
        expected['bundle_hash'] = hashlib.sha256(raw).hexdigest()
        plan = provision.prepare_import(store, raw, expected=expected, now_ms=now)
        provision.commit_import(store, plan, confirm_human=True, reason='synthetic-setup-provision', now_ms=now)
        if started:
            provision.begin_registration(store, now_ms=now)
            with store.lock():
                provision.guard_registration(store, {}, {})
                store.write('credential', {'key': 'a1' * 32})
                store.write('state.json', {'state': 'pending', 'enabled': False, 'sequence': 0,
                                           'claim_id': '11111111-1111-4111-8111-111111111111'})
        before = provision.status(store, now_ms=now)
        assert before['registration_started'] is started and before['acknowledged'] is False
    files_before = {path.name: path.read_bytes() for path in root.iterdir()}
    cfg = {'sdc': {'state_root': str(root)}, 'saved_core': 'synthetic'}
    cfg_before = copy.deepcopy(cfg)
    calls = []
    def choose(_):
        assert started, 'unstarted managed root must only explain reapproval'
        return '1'
    monkeypatch.setattr('builtins.input', choose)
    monkeypatch.setattr(sdc_cli, 'main', lambda argv: calls.append(argv) or 0)
    setup_wizard.offer_sdc_enrollment(cfg)
    assert calls == ([['connect', '--state-root', str(root)]] if started else [])
    assert cfg == cfg_before
    assert {path.name: path.read_bytes() for path in root.iterdir()} == files_before
    assert not (root / 'endpoint.json').exists()
    with Store(root) as store:
        assert provision.status(store, now_ms=now) == before
    if not started:
        assert '--import-provision FILE' in capsys.readouterr().out
