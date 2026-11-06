"""Completed creation is a no-op; optional patient surfaces retain exact recovery."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

import mcs_setup
import service_management
import setup_integration
import slack_channel_setup as setup
from mcs_setup_testkit import _init_env
from test_slack_channel_setup import Client, config, consume
from test_system_profile_routing import created


def completed(root, monkeypatch):
    cfg = created(root)
    monkeypatch.setattr(setup_integration, '_apply_plugin_integration', lambda *a: True)
    monkeypatch.setattr(setup, 'reconnect_ready', lambda *a: None)
    setup.finish_runtime(root, cfg, SimpleNamespace(yes=True))
    assert setup.read(root)['worker_ready'] is True
    return cfg


def forbidden(*args, **kwargs):
    pytest.fail('creation no-op must not call external bootstrap or rewrite its receipt')


@pytest.mark.parametrize('setting', ['job_budget_seconds=900', 'notify.slack.project_ids=[2]'])
def test_completed_creation_allows_normal_init_config_cas_without_changing_receipts(tmp_path, monkeypatch, setting):
    cfg = completed(tmp_path, monkeypatch)
    _init_env(monkeypatch, tmp_path, cfg)
    before = setup.path(tmp_path).read_bytes()
    monkeypatch.setattr(mcs_setup, '_init_store_secrets', lambda *_: None)
    monkeypatch.setattr(setup_integration, '_apply_plugin_integration', forbidden)
    monkeypatch.setattr(service_management, 'hermes_cli', forbidden)
    normal = []
    def integration(candidate, args):
        assert setup_integration._plugin_profile(args, 'slack') == candidate['notify']['slack']['profile']
        normal.append(deepcopy(candidate))
        return True
    monkeypatch.setattr(mcs_setup, '_apply_plugin_integration', integration)
    assert mcs_setup.main(['init', '--yes', '--set', setting]) == 0
    saved = json.loads((tmp_path / 'c.json').read_text())
    if setting.startswith('job_budget'):
        assert saved['job_budget_seconds'] == 900
    else:
        assert saved['notify']['slack']['project_ids'] == [2]
    assert normal == [saved]
    assert setup.path(tmp_path).read_bytes() == before


@pytest.mark.parametrize('change', ['budget', 'projects'])
def test_completed_creation_is_a_shared_noop_without_resealing_new_config(tmp_path, monkeypatch, change, capsys):
    cfg = completed(tmp_path, monkeypatch)
    if change == 'budget':
        cfg['job_budget_seconds'] = 900
    else:
        cfg['notify']['slack']['project_ids'] = [2]
    before = setup.path(tmp_path).read_bytes()
    monkeypatch.setattr(setup_integration, '_apply_plugin_integration', forbidden)
    monkeypatch.setattr(service_management, 'hermes_cli', forbidden)
    assert setup.prepare(tmp_path, cfg) is None
    assert setup.run_setup(tmp_path, cfg, SimpleNamespace(yes=True)) == cfg
    assert setup.candidate(tmp_path, cfg) == cfg
    setup.stage_config(tmp_path, cfg, cfg)
    setup.applied(tmp_path, cfg, cfg)
    monkeypatch.setattr(mcs_setup, 'HOME', str(tmp_path))
    monkeypatch.setattr(mcs_setup, '_init_config_snapshot', lambda: (cfg, {}))
    monkeypatch.setattr(mcs_setup, '_init_save_config', forbidden)
    assert mcs_setup.cmd_slack_channels(SimpleNamespace()) == 0
    assert '新規作成' in capsys.readouterr().out
    assert setup.path(tmp_path).read_bytes() == before


def test_initial_complete_distinct_surfaces_need_no_creation_record_or_external_calls(tmp_path, monkeypatch):
    cfg = config()
    cfg.update(notify_target='slack:C000000098', notify_system_target='slack:C000000099')
    cfg['notify']['slack']['channel_id'] = 'C000000097'
    monkeypatch.setattr(setup_integration, '_apply_plugin_integration', forbidden)
    monkeypatch.setattr(service_management, 'hermes_cli', forbidden)
    assert setup.prepare(tmp_path, cfg) is None
    assert setup.run_setup(tmp_path, cfg, SimpleNamespace(yes=True)) == cfg
    assert setup.candidate(tmp_path, cfg) == cfg
    setup.stage_config(tmp_path, cfg, cfg)
    setup.applied(tmp_path, cfg, cfg)
    monkeypatch.setattr(mcs_setup, 'HOME', str(tmp_path))
    monkeypatch.setattr(mcs_setup, '_init_config_snapshot', lambda: (cfg, {}))
    monkeypatch.setattr(mcs_setup, '_init_save_config', forbidden)
    assert mcs_setup.cmd_slack_channels(SimpleNamespace()) == 0
    assert setup.read(tmp_path) is None


@pytest.mark.parametrize('has_record', [False, True])
def test_creation_noop_keeps_current_profile_and_rejects_explicit_mismatch(tmp_path, monkeypatch, has_record):
    if has_record:
        cfg = completed(tmp_path, monkeypatch)
        cfg['job_budget_seconds'] = 900
    else:
        cfg = config()
        cfg.update(notify_target='slack:C000000098', notify_system_target='slack:C000000099')
        cfg['notify']['slack']['channel_id'] = 'C000000098'
    before = setup.path(tmp_path).read_bytes() if has_record else None
    monkeypatch.setattr(setup_integration, '_apply_plugin_integration', forbidden)
    monkeypatch.setattr(service_management, 'hermes_cli', forbidden)
    for profile in ('synthetic', 'default'):
        cfg['notify']['slack']['profile'] = profile
        args = SimpleNamespace(yes=True, plugin_profile=None, plugin_user_ids='U000000002')
        assert setup.run_setup(tmp_path, cfg, args) == cfg
        assert args.plugin_profile == profile and args.plugin_user_ids == 'U000000002'
        with pytest.raises(ValueError, match='binding_mismatch'):
            setup.run_setup(tmp_path, cfg, SimpleNamespace(yes=True, plugin_profile='other'))
    assert (setup.path(tmp_path).read_bytes() if has_record else None) == before


@pytest.mark.parametrize('phase', ['pending', 'unknown', 'verified', 'applied_post_pending'])
def test_unfinished_intent_cannot_use_the_completed_creation_noop(tmp_path, phase):
    cfg = created(tmp_path)
    record = setup.read(tmp_path)
    if phase == 'applied_post_pending':
        record['worker_ready'] = False
    else:
        record['state'] = phase
        record.pop('worker_ready', None)
    setup.save(tmp_path, record)
    cfg['job_budget_seconds'] = 900
    before = setup.path(tmp_path).read_bytes()
    for operation in (lambda: setup.prepare(tmp_path, cfg),
                      lambda: setup.candidate(tmp_path, cfg),
                      lambda: setup.stage_config(tmp_path, cfg, cfg),
                      lambda: setup.applied(tmp_path, cfg, cfg)):
        with pytest.raises(ValueError):
            operation()
    assert setup.path(tmp_path).read_bytes() == before


@pytest.mark.parametrize('text,card', [(None, None), ('C000000098', None), (None, 'C000000098'),
                                      ('C000000098', 'C000000098'), ('C000000098', 'C000000097'),
                                      ('', None), (None, '')])
def test_optional_patient_surface_matrix_reuses_only_verified_ids(tmp_path, monkeypatch, text, card):
    cfg, client = config(), Client()
    if text is not None:
        cfg['notify_target'] = 'slack:' + text if text else ''
    if card is not None:
        cfg['notify']['slack']['channel_id'] = card
    for channel in {text, card} - {None, ''}:
        client.channels[channel] = {'id': channel, 'is_private': True, 'is_member': True, 'is_archived': False}
    assert setup.preflight_errors(cfg)[0] == []
    _init_env(monkeypatch, tmp_path, cfg)
    monkeypatch.setattr(mcs_setup, '_init_store_secrets', lambda *_: None)
    monkeypatch.setattr(setup_integration, '_apply_plugin_integration', lambda *a: True)
    def cli(exe, profile, *command):
        if command == ('gateway', 'restart'):
            consume(tmp_path, client, cfg)
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(service_management, 'hermes_cli', cli)
    ready = []
    def post(root, candidate):
        assert json.loads((tmp_path / 'c.json').read_text()) == candidate
        ready.append(candidate['notify']['slack']['channel_id'])
    monkeypatch.setattr(setup, 'reconnect_ready', post)
    assert mcs_setup.main(['init', '--yes']) == 0
    saved = json.loads((tmp_path / 'c.json').read_text())
    patient = text or card or setup.read(tmp_path)['roles']['patient']['id']
    assert saved['notify_target'] == 'slack:' + (text or patient)
    assert saved['notify']['slack']['channel_id'] == (card or patient)
    assert mcs_setup.validate_config(saved)[0] == []
    assert client.created == (['mcs', 'mcslog'] if not text and not card else ['mcslog'])
    assert ready == ([] if card else [patient])


@pytest.mark.parametrize('interrupted', [False, True])
def test_card_completion_requires_postcas_worker_even_when_no_channel_was_created(tmp_path, monkeypatch, interrupted):
    cfg, client = config(), Client()
    cfg.update(notify_target='slack:C000000098', notify_system_target='slack:C000000099')
    for channel in ('C000000098', 'C000000099'):
        client.channels[channel] = {'id': channel, 'is_private': True, 'is_member': True, 'is_archived': False}
    _init_env(monkeypatch, tmp_path, cfg)
    monkeypatch.setattr(mcs_setup, '_init_store_secrets', lambda *_: None)
    monkeypatch.setattr(setup_integration, '_apply_plugin_integration', lambda *a: True)
    def cli(exe, profile, *command):
        if command == ('gateway', 'restart'):
            consume(tmp_path, client, cfg)
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(service_management, 'hermes_cli', cli)
    ready = []
    monkeypatch.setattr(setup, 'reconnect_ready', lambda *_: ready.append(True))
    if interrupted:
        applied = setup.applied
        monkeypatch.setattr(setup, 'applied', lambda *_: (_ for _ in ()).throw(OSError('synthetic after CAS')))
        assert mcs_setup.main(['init', '--yes']) == 1
        assert setup.read(tmp_path)['state'] == 'verified' and ready == []
        monkeypatch.setattr(setup, 'applied', applied)
    assert mcs_setup.main(['init', '--yes']) == 0
    assert ready == [True] and setup.read(tmp_path)['worker_ready'] is True
    assert client.created == []


@pytest.mark.parametrize('field,value', [('notify_target', ' '), ('notify.slack.channel_id', ' '),
                                       ('notify.slack.channel_id', 42)])
def test_optional_surface_does_not_defer_invalid_nonempty_values(field, value):
    cfg = config()
    if field == 'notify_target':
        cfg[field] = value
    else:
        cfg['notify']['slack']['channel_id'] = value
    try:
        errors, _ = setup.preflight_errors(cfg)
    except ValueError:
        return
    assert errors
