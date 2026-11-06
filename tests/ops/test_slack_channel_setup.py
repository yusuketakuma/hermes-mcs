"""Explicit channel setup uses synthetic native clients and private temporary files."""
import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

import slack_channel_setup as setup
from adapters.slack import provision


def config():
    return {'mcs_login_id': 'synthetic', 'runtime_mode': 'hermes',
            'notify': {'interactive': 'slack', 'slack': {
                'team_id': 'T000000001', 'application_id': 'A000000001',
                'profile': 'synthetic', 'allowed_user_ids': ['U000000001']}}}


class Client:
    def __init__(self):
        self.created = []
        self.invites = []
        self.channels = {}
        self.members = {}
        self.failure = None
        self.retry_handlers = ['default-retry']
        self.observed_handlers = []

    async def auth_test(self):
        return {'ok': True, 'team_id': 'T000000001', 'bot_id': 'B000000001', 'user_id': 'U000000009'}

    async def bots_info(self, **kwargs):
        return {'ok': True, 'bot': {'id': 'B000000001', 'app_id': 'A000000001', 'deleted': False}}

    async def conversations_create(self, name, is_private):
        assert is_private is True
        self.created.append(name)
        self.observed_handlers.append(list(self.retry_handlers))
        if self.failure == 'lost':
            raise TimeoutError
        if self.failure == 'cancel':
            raise asyncio.CancelledError
        if self.failure:
            return {'ok': False, 'error': self.failure}
        channel = 'C00000000' + str(len(self.created))
        self.channels[channel] = {'id': channel, 'is_private': True, 'is_member': True, 'is_archived': False}
        self.members[channel] = ['U000000009']
        return {'ok': True, 'channel': self.channels[channel]}

    async def conversations_info(self, channel):
        return {'ok': channel in self.channels, 'channel': self.channels.get(channel)}

    async def conversations_members(self, channel, **kwargs):
        return {'ok': True, 'members': self.members[channel], 'response_metadata': {'next_cursor': ''}}

    async def conversations_invite(self, channel, users):
        self.invites.append((channel, users))
        self.observed_handlers.append(list(self.retry_handlers))
        self.members[channel] += users.split(',')
        return {'ok': True}


def consume(root, client, cfg):
    return asyncio.run(provision.consume(root, client, setup.plan(cfg)['binding']))


def test_new_two_private_channels_complete_and_replay_without_creation(tmp_path):
    cfg, client = config(), Client()
    setup.prepare(tmp_path, cfg)
    assert consume(tmp_path, client, cfg)['state'] == 'verified'
    saved = setup.candidate(tmp_path, cfg)
    assert client.created == ['mcs', 'mcslog']
    assert client.invites == [('C000000001', 'U000000001'), ('C000000002', 'U000000001')]
    assert saved['notify_target'] == 'slack:C000000001'
    assert saved['notify_system_target'] == 'slack:C000000002'
    assert saved['notify']['slack']['channel_id'] == 'C000000001'
    assert 'allowed_chat_ids' not in saved['notify']['slack']
    setup.applied(tmp_path, cfg, saved)
    assert setup.prepare(tmp_path, saved) is None
    assert setup.candidate(tmp_path, saved) == saved
    assert len(client.created) == 2


def test_existing_patient_members_never_changed(tmp_path):
    cfg, client = config(), Client()
    cfg['notify_target'] = 'slack:C000000099'
    cfg['notify']['slack']['channel_id'] = 'C000000099'
    client.channels['C000000099'] = {'id': 'C000000099', 'is_private': True, 'is_member': True, 'is_archived': False}
    setup.prepare(tmp_path, cfg)
    assert consume(tmp_path, client, cfg)['state'] == 'verified'
    assert client.created == ['mcslog']
    assert client.invites == [('C000000001', 'U000000001')]
    assert setup.candidate(tmp_path, cfg)['notify_target'] == cfg['notify_target']


@pytest.mark.parametrize('failure', ['name_taken', 'lost'])
def test_unknown_or_rejected_creation_is_never_retried(tmp_path, failure):
    cfg, client = config(), Client()
    client.failure = failure
    setup.prepare(tmp_path, cfg)
    consume(tmp_path, client, cfg)
    client.failure = None
    consume(tmp_path, client, cfg)
    assert client.created == ['mcs']
    assert setup.read(tmp_path)['state'] != 'verified'


def test_missing_scope_rejection_retries_only_on_explicit_rerun(tmp_path, monkeypatch):
    cfg, client = config(), Client()
    client.failure = 'missing_scope'
    setup.prepare(tmp_path, cfg)
    consume(tmp_path, client, cfg)
    state = setup.read(tmp_path)
    assert state['roles']['patient']['state'] == 'blocked'
    assert state['roles']['patient']['create_error'] == 'missing_scope'
    client.failure = None
    assert consume(tmp_path, client, cfg) == state
    assert client.created == ['mcs']
    import service_management
    import setup_integration
    def integrate(*args):
        pending = setup.read(tmp_path)
        assert pending['state'] == pending['roles']['patient']['state'] == 'pending'
        return True
    monkeypatch.setattr(setup_integration, '_apply_plugin_integration', integrate)
    def cli(_exe, _profile, *command):
        if command == ('gateway', 'status'):
            return SimpleNamespace(returncode=0)
        assert command == ('gateway', 'restart')
        consume(tmp_path, client, cfg)
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(service_management, 'hermes_cli', cli)
    setup.run_setup(tmp_path, cfg, SimpleNamespace(yes=True))
    assert setup.read(tmp_path)['state'] == 'verified'
    assert client.created == ['mcs', 'mcs', 'mcslog']


def test_consume_uses_single_attempt_copy_without_mutating_client(tmp_path):
    cfg, client = config(), Client()
    setup.prepare(tmp_path, cfg)
    assert consume(tmp_path, client, cfg)['state'] == 'verified'
    assert client.observed_handlers and all(h == [] for h in client.observed_handlers)
    assert client.retry_handlers == ['default-retry']


def test_consume_rejects_client_without_single_attempt_surface(tmp_path):
    cfg, client = config(), Client()
    client.retry_handlers = None
    setup.prepare(tmp_path, cfg)
    state = consume(tmp_path, client, cfg)
    assert state['state'] == 'blocked'
    assert state['reason'] == 'slack_setup_client_unverified'
    assert client.created == []


def test_existing_public_patient_channel_is_verified_without_move(tmp_path):
    cfg, client = config(), Client()
    cfg['notify_target'] = 'slack:C000000099'
    cfg['notify']['slack']['channel_id'] = 'C000000099'
    client.channels['C000000099'] = {'id': 'C000000099', 'is_private': False,
                                     'is_member': True, 'is_archived': False}
    setup.prepare(tmp_path, cfg)
    assert consume(tmp_path, client, cfg)['state'] == 'verified'
    assert client.created == ['mcslog']
    assert 'C000000099' not in client.created


def test_plan_system_equal_to_card_channel_supplements_mcslog():
    cfg = config()
    cfg['notify_target'] = 'slack:C000000001'
    cfg['notify']['slack']['channel_id'] = 'C000000002'
    cfg['notify_system_target'] = 'slack:C000000002'
    desired = setup.plan(cfg)
    assert desired['roles']['patient'] == {'name': 'mcs', 'id': 'C000000001', 'state': 'existing'}
    assert desired['roles']['system'] == {'name': 'mcslog', 'id': None, 'state': 'pending'}


def test_plan_local_target_with_slack_cards_is_not_safe():
    cfg = config()
    cfg['notify_target'] = 'local'
    desired = setup.plan(cfg)
    assert desired['roles']['patient']['state'] == 'pending'
    assert desired['roles']['system']['state'] == 'pending'


def test_plan_rejects_unverified_card_channel_before_creation():
    cfg = config()
    cfg['notify']['slack']['channel_id'] = 'not-a-channel'
    with pytest.raises(ValueError, match='card_channel_invalid'):
        setup.plan(cfg)


def test_candidate_keeps_local_text_target_and_binds_created_card_channel(tmp_path):
    cfg, client = config(), Client()
    cfg['notify_target'] = 'local'
    setup.prepare(tmp_path, cfg)
    assert consume(tmp_path, client, cfg)['state'] == 'verified'
    saved = setup.candidate(tmp_path, cfg)
    assert saved['notify_target'] == 'local'
    assert saved['notify']['slack']['channel_id'] == 'C000000001'
    assert saved['notify_system_target'] == 'slack:C000000002'


def test_cancel_records_unknown_and_never_recreates(tmp_path):
    cfg, client = config(), Client()
    client.failure = 'cancel'
    setup.prepare(tmp_path, cfg)
    with pytest.raises(asyncio.CancelledError):
        consume(tmp_path, client, cfg)
    assert setup.read(tmp_path)['roles']['patient']['state'] == 'unknown'
    client.failure = None
    consume(tmp_path, client, cfg)
    assert client.created == ['mcs']


@pytest.mark.parametrize('field', ['plan_hash', 'root', 'created'])
def test_forged_request_or_created_flag_is_refused(tmp_path, field):
    cfg = config()
    state = setup.prepare(tmp_path, cfg)
    if field == 'created':
        state['roles']['patient']['created'] = True
    else:
        state[field] = 'synthetic-invalid'
    setup.path(tmp_path).write_text(json.dumps(state))
    with pytest.raises(ValueError, match='state_unverified'):
        setup.read(tmp_path)


def test_symlink_data_parent_is_refused(tmp_path):
    real = tmp_path / 'private'
    real.mkdir()
    (tmp_path / 'data').symlink_to(real, target_is_directory=True)
    with pytest.raises(ValueError, match='root_unverified'):
        setup.prepare(tmp_path, config())


def test_profile_mismatch_refused_before_integration(tmp_path, monkeypatch):
    cfg = config()
    import setup_integration
    monkeypatch.setattr(setup_integration, '_apply_plugin_integration', lambda *args: pytest.fail('wrong profile write'))
    with pytest.raises(ValueError, match='binding_mismatch'):
        setup.run_setup(tmp_path, cfg, SimpleNamespace(yes=True, plugin_profile='foreign'))


def test_normal_hermes_bootstrap_runs_before_config_cas(tmp_path, monkeypatch):
    cfg, client = config(), Client()
    import mcs_util
    import setup_config_snapshot
    import setup_integration
    import service_management
    conf = tmp_path / 'config.json'
    monkeypatch.setattr(mcs_util, 'CONF_PATH', str(conf))
    monkeypatch.setattr(setup_config_snapshot, 'CONF_PATH', str(conf))
    seen = []
    def integration(candidate, args):
        assert not conf.exists()
        assert args.plugin_profile == 'synthetic'
        seen.append('integration')
        return True
    monkeypatch.setattr(setup_integration, '_apply_plugin_integration', integration)
    monkeypatch.setattr(mcs_util, 'hermes_exe', lambda cfg: 'synthetic-hermes')
    def cli(exe, profile, *command, **kwargs):
        assert profile == 'synthetic'
        if command == ('gateway', 'status'):
            return SimpleNamespace(returncode=1)
        seen.append(command[-1])
        if command[-1] == 'start':
            consume(tmp_path, client, cfg)
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(service_management, 'hermes_cli', cli)
    args = SimpleNamespace(yes=True, plugin_profile=None)
    _, initial = setup_config_snapshot._init_config_snapshot()
    saved = setup.run_setup(tmp_path, cfg, args)
    setup_config_snapshot._init_save_config(saved, initial, args)
    setup.applied(tmp_path, cfg, saved)
    assert seen == ['integration', 'install', 'start']
    assert json.loads(conf.read_text()) == saved
    assert setup.read(tmp_path)['state'] == 'applied'
    import notify_spool
    monkeypatch.setattr(setup_integration, '_apply_plugin_integration', lambda candidate, args:
                        candidate['notify']['slack']['channel_id'] == 'C000000001')
    monkeypatch.setattr(notify_spool, 'ensure_dirs', lambda root: None)
    monkeypatch.setattr(notify_spool, 'publish_flags', lambda candidate, root: seen.append('flags'))
    monkeypatch.setattr(setup, 'reconnect_ready', lambda root, candidate: seen.append('worker_ready'))
    setup.finish_runtime(tmp_path, saved, args)
    setup.finish_runtime(tmp_path, saved, args)
    assert seen == ['integration', 'install', 'start', 'flags', 'worker_ready']
    assert setup.read(tmp_path)['worker_ready'] is True


def test_distinct_existing_slack_targets_verified_without_creates(tmp_path):
    cfg, client = config(), Client()
    cfg.update(notify_target='slack:C000000098', notify_system_target='slack:C000000099')
    for channel in ('C000000098', 'C000000099'):
        client.channels[channel] = {'id': channel, 'is_private': True, 'is_member': True, 'is_archived': False}
    setup.prepare(tmp_path, cfg)
    assert consume(tmp_path, client, cfg)['state'] == 'verified'
    assert setup.candidate(tmp_path, cfg) == cfg
    assert client.created == client.invites == []


def test_alias_system_target_cannot_bypass_separation():
    cfg = config()
    cfg.update(notify_target='slack:C000000001', notify_system_target='slack:#mcs')
    with pytest.raises(ValueError, match='system_channel_invalid'):
        setup.plan(cfg)


def test_changed_config_cannot_apply_verified_receipt(tmp_path):
    cfg, client = config(), Client()
    setup.prepare(tmp_path, cfg)
    consume(tmp_path, client, cfg)
    other = deepcopy(cfg)
    other['notify_target'] = 'slack:C000000099'
    with pytest.raises(ValueError, match='config_changed'):
        setup.candidate(tmp_path, other)


def test_final_native_reconnect_requires_fresh_worker_witness(tmp_path, monkeypatch):
    import card_worker_status
    import mcs_util
    import service_management
    cfg = config()
    data = tmp_path / 'data'
    status = card_worker_status._path(data, 'slack', 'synthetic')
    status.parent.mkdir(parents=True)
    status.write_bytes(b'old')
    status.chmod(0o600)
    monkeypatch.setattr(mcs_util, 'hermes_exe', lambda cfg: 'synthetic-hermes')
    monkeypatch.setattr(card_worker_status, 'status', lambda *args: {'state': 'started'})
    def restart(exe, profile, *command):
        assert profile == 'synthetic' and command == ('gateway', 'restart')
        status.write_bytes(b'fresh')
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(service_management, 'hermes_cli', restart)
    setup.reconnect_ready(tmp_path, cfg)


def test_updater_calls_verified_same_setup_owner(tmp_path, monkeypatch):
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location('synthetic_slack_upgrade',
        Path(__file__).resolve().parents[2] / 'scripts' / 'mcs_upgrade.py')
    upgrade = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(upgrade)
    seen = []
    monkeypatch.setattr(upgrade, '_installed_setup_command', lambda repo, action: ['synthetic', action])
    monkeypatch.setattr(upgrade, '_lifetime_fds', lambda: {})
    def run(command, **kwargs):
        assert command == ['synthetic', 'slack-channels']
        assert 'PYTHONPATH' not in kwargs['env'] and kwargs['env']['MCS_LIFECYCLE_PINNED'] == '1'
        seen.append(command)
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(upgrade.subprocess, 'run', run)
    assert upgrade._complete_slack_channels({'PYTHONPATH': 'synthetic'}, tmp_path) == 0
    assert len(seen) == 1


def test_partial_creation_resumes_id_receipt_without_recreating_patient(tmp_path):
    cfg, client = config(), Client()
    original = client.conversations_create
    async def create(name, is_private):
        if name == 'mcslog':
            client.failure = 'lost'
        return await original(name, is_private)
    client.conversations_create = create
    setup.prepare(tmp_path, cfg)
    consume(tmp_path, client, cfg)
    assert setup.read(tmp_path)['roles']['patient']['create_receipt']['channel_id'] == 'C000000001'
    client.conversations_create = original
    client.failure = None
    consume(tmp_path, client, cfg)
    assert client.created == ['mcs', 'mcslog']
    assert setup.read(tmp_path)['state'] == 'unknown'


def test_existing_hermes_operators_read_without_copying_grants(tmp_path, monkeypatch):
    import mcs_util
    import service_management
    cfg = config()
    del cfg['notify']['slack']['allowed_user_ids']
    seen = []
    monkeypatch.setattr(mcs_util, 'hermes_exe', lambda cfg: 'synthetic-hermes')
    def cli(exe, profile, *command):
        assert profile == 'synthetic' and command[-1] == '--json'
        assert command[2].endswith('.slack_allowed_user_ids')
        seen.append(command)
        return SimpleNamespace(returncode=0, stdout='["U000000001"]')
    monkeypatch.setattr(service_management, 'hermes_cli', cli)
    request = setup.prepare(tmp_path, cfg)
    assert request['plan']['users'] == ['U000000001'] and len(seen) == 1
    assert 'allowed_user_ids' not in cfg['notify']['slack']


@pytest.mark.parametrize('cfg', [
    {'mcs_login_id': 'synthetic', 'runtime_mode': 'hermes',
     'notify': {'interactive': 'off'}},
    {'mcs_login_id': 'synthetic', 'runtime_mode': 'hermes',
     'notify_target': 'slack:C0000000001', 'notify_system_target': 'discord:1',
     'notify': {'interactive': 'slack', 'slack': {
         'team_id': 'T000000001', 'application_id': 'A000000001',
         'profile': 'synthetic', 'allowed_user_ids': ['U000000001']}}},
])
def test_cmd_slack_channels_noops_without_pending_slack(tmp_path, monkeypatch, cfg):
    import mcs_setup
    monkeypatch.setattr(mcs_setup, '_init_config_snapshot', lambda: (cfg, {}))
    monkeypatch.setattr(setup, 'run_setup',
                        lambda *a, **k: pytest.fail('run_setup called'))
    assert mcs_setup.cmd_slack_channels(SimpleNamespace()) == 0


def test_preflight_defers_only_missing_ids_for_pending_patient(tmp_path):
    cfg = config()
    errors, _ = setup.preflight_errors(cfg)
    assert errors == []
    existing = deepcopy(cfg)
    existing['notify_target'] = 'slack:C000000099'
    errors, _ = setup.preflight_errors(existing)
    assert any(error.startswith('notify.slack.channel_id:') for error in errors)
    other = {'mcs_login_id': 'synthetic', 'runtime_mode': 'hermes',
             'notify': {'interactive': 'off'}}
    errors, _ = setup.preflight_errors(other)
    assert any(error.endswith('notify_target') for error in errors)


def test_applied_receipt_replay_with_pre_provision_config_fails_fast(tmp_path, monkeypatch):
    cfg, client = config(), Client()
    setup.prepare(tmp_path, cfg)
    consume(tmp_path, client, cfg)
    saved = setup.candidate(tmp_path, cfg)
    setup.applied(tmp_path, cfg, saved)
    import setup_integration
    monkeypatch.setattr(setup_integration, '_apply_plugin_integration',
                        lambda *args: pytest.fail('applied intent must not re-provision'))
    with pytest.raises(ValueError, match='config_changed'):
        setup.run_setup(tmp_path, cfg, SimpleNamespace(yes=True))
    assert len(client.created) == 2


def test_operator_override_cannot_widen_sealed_grants(tmp_path, monkeypatch):
    import setup_integration
    monkeypatch.setattr(setup_integration, '_apply_plugin_integration', lambda *args: pytest.fail('changed grants'))
    with pytest.raises(ValueError, match='operator_binding_mismatch'):
        setup.run_setup(tmp_path, config(), SimpleNamespace(yes=True, plugin_profile=None,
                                                          plugin_user_ids='U000000002'))


@pytest.mark.parametrize('entry', ['init', 'slack-channels'])
@pytest.mark.parametrize('interruption', ['before_hash', 'after_hash', 'after_cas'])
def test_setup_resumes_each_config_publication_window_without_external_writes(
        tmp_path, monkeypatch, entry, interruption):
    import mcs_setup
    import notify_spool
    import setup_integration
    from test_mcs_setup import _init_env
    cfg, client = config(), Client()
    _init_env(monkeypatch, tmp_path, cfg)
    monkeypatch.setattr(mcs_setup, '_init_store_secrets', lambda *_: None)
    monkeypatch.setattr(setup_integration, '_apply_plugin_integration', lambda *_: True)
    monkeypatch.setattr(notify_spool, 'publish_flags', lambda *_: None)
    finalized = []
    monkeypatch.setattr(setup, 'reconnect_ready', lambda *_: finalized.append(True))
    setup.prepare(tmp_path, cfg)
    consume(tmp_path, client, cfg)
    stage, applied = setup.stage_config, setup.applied
    def interrupted_stage(*args):
        if interruption == 'after_hash':
            stage(*args)
        raise OSError('synthetic interrupted publication')
    if interruption == 'after_cas':
        monkeypatch.setattr(setup, 'applied', lambda *_: (_ for _ in ()).throw(OSError('synthetic')))
    else:
        monkeypatch.setattr(setup, 'stage_config', interrupted_stage)
    command = ['init', '--yes'] if entry == 'init' else ['slack-channels']
    assert mcs_setup.main(command) == 1
    state = setup.read(tmp_path)
    assert state['state'] == 'verified'
    assert ('candidate_hash' in state) == (interruption != 'before_hash')
    saved = json.loads((tmp_path / 'c.json').read_text())
    assert ('notify_system_target' in saved) == (interruption == 'after_cas')
    monkeypatch.setattr(setup, 'stage_config', stage)
    monkeypatch.setattr(setup, 'applied', applied)
    assert mcs_setup.main(command) == 0
    assert setup.read(tmp_path)['worker_ready'] is True
    assert finalized == [True]
    assert client.created == ['mcs', 'mcslog']
    assert len(client.invites) == 2


@pytest.mark.parametrize('damage', ['changed_config', 'incomplete_receipt', 'missing_hash'])
def test_config_recovery_requires_exact_sealed_candidate_and_complete_receipt(tmp_path, damage):
    cfg, client = config(), Client()
    setup.prepare(tmp_path, cfg)
    consume(tmp_path, client, cfg)
    saved = setup.candidate(tmp_path, cfg)
    setup.stage_config(tmp_path, cfg, saved)
    if damage == 'changed_config':
        saved['self_posts'] = True
    else:
        state = setup.read(tmp_path)
        if damage == 'incomplete_receipt':
            del state['roles']['patient']['create_receipt']
        else:
            del state['candidate_hash']
        setup.path(tmp_path).write_text(json.dumps(state))
    with pytest.raises(ValueError, match='existing_intent_conflict|state_unverified'):
        setup.prepare(tmp_path, saved)
    assert client.created == ['mcs', 'mcslog']


@pytest.mark.parametrize('cfg,expected', [
    (config(), True),
    ({'notify_target': 'local', 'notify': {'interactive': 'off'}}, False),
    ({**config(), 'notify_system_target': 'discord:1'}, False),
    ({**config(), 'notify_target': 'local'}, True),
])
def test_required_is_pure_and_matches_plan_eligibility(monkeypatch, cfg, expected):
    import service_management
    monkeypatch.setattr(service_management, 'hermes_cli', lambda *_: pytest.fail('eligibility must be pure'))
    assert setup.required(cfg) is expected
    assert (setup.plan(cfg) is not None) is expected


def test_required_rejects_invalid_notify_instead_of_reporting_no_work():
    with pytest.raises(ValueError):
        setup.required({'notify': 'invalid'})
