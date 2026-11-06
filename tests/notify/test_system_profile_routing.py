"""Only a verified created system channel selects the setup owner's profile."""
from copy import deepcopy
import json
from pathlib import Path
import sys

import pytest

import health_watch
from ledger import Ledger
import mcs_util
import notify_flush
import notify_sender
import slack_channel_setup as setup
from test_slack_channel_setup import Client, config, consume


def created(root, *, profile='synthetic', mode='hermes'):
    cfg, client = config(), Client()
    cfg['runtime_mode'] = mode
    cfg['notify']['slack']['profile'] = profile
    cfg['notify_bot_profile'] = 'legacy'
    setup.prepare(root, cfg)
    consume(root, client, cfg)
    saved = setup.candidate(root, cfg)
    setup.applied(root, cfg, saved)
    return saved


@pytest.mark.parametrize('profile,bot', [('synthetic', None), ('synthetic', 'legacy'), ('default', None)])
def test_created_channels_pin_their_binding_profile_for_both_surfaces(tmp_path, monkeypatch, profile, bot):
    cfg = created(tmp_path, profile=profile)
    if bot is None:
        del cfg['notify_bot_profile']
    before = deepcopy(cfg)
    monkeypatch.setattr(mcs_util, 'hermes_exe', lambda _: 'synthetic-hermes')
    argv = notify_sender.send_argv(cfg, cfg['notify_system_target'], str(tmp_path / 'config.json'), kind='run_failed')
    assert argv[:3] == ['synthetic-hermes', '-p', profile]
    # Text to the created patient channel is served by the same verified
    # binding profile — the creating bot is the only proven member.
    patient = notify_sender.send_argv(cfg, cfg['notify_target'], str(tmp_path / 'config.json'), kind='new_messages')
    assert patient[:3] == ['synthetic-hermes', '-p', profile]
    assert cfg == before


@pytest.mark.parametrize('existing,state', [(False, None), (True, 'applied'),
                                          (True, 'pending'), (True, 'blocked'),
                                          (True, 'unknown'), (True, 'verified')])
def test_legacy_or_existing_explicit_system_channel_keeps_text_profile(tmp_path, monkeypatch, existing, state):
    cfg = config()
    cfg.update(notify_target='slack:C000000098', notify_system_target='slack:C000000099', notify_bot_profile='legacy')
    cfg['notify']['slack']['channel_id'] = 'C000000098'
    if existing:
        del cfg['notify']['slack']['channel_id']
        client = Client()
        for channel in ('C000000098', 'C000000099'):
            client.channels[channel] = {'id': channel, 'is_private': True, 'is_member': True, 'is_archived': False}
        setup.prepare(tmp_path, cfg)
        consume(tmp_path, client, cfg)
        saved = setup.candidate(tmp_path, cfg)
        setup.applied(tmp_path, cfg, saved)
        cfg = saved
        record = setup.read(tmp_path)
        record['state'] = state
        if state != 'applied':
            record.pop('worker_ready', None)
        setup.save(tmp_path, record)
        assert client.created == []
    monkeypatch.setattr(mcs_util, 'hermes_exe', lambda _: 'synthetic-hermes')
    argv = notify_sender.send_argv(cfg, cfg['notify_system_target'], str(tmp_path / 'config.json'), kind='run_failed')
    assert argv[:3] == ['synthetic-hermes', '-p', 'legacy']


def test_created_receipt_survives_unrelated_config_changes_but_does_not_own_another_target(tmp_path, monkeypatch):
    cfg = created(tmp_path)
    cfg['health'] = {'tick_interval_s': 900}
    monkeypatch.setattr(mcs_util, 'hermes_exe', lambda _: 'synthetic-hermes')
    assert notify_sender.send_argv(cfg, cfg['notify_system_target'], str(tmp_path / 'config.json'), kind='run_failed')[1:3] == ['-p', 'synthetic']
    cfg['notify_system_target'] = 'slack:C000000099'
    assert notify_sender.send_argv(cfg, cfg['notify_system_target'], str(tmp_path / 'config.json'), kind='run_failed')[1:3] == ['-p', 'legacy']


@pytest.mark.parametrize('state', ['pending', 'blocked', 'unknown', 'verified'])
def test_unfinished_created_receipt_does_not_own_another_system_target(tmp_path, monkeypatch, state):
    cfg = created(tmp_path)
    record = setup.read(tmp_path)
    record['state'] = state
    record.pop('worker_ready', None)
    setup.save(tmp_path, record)
    cfg['notify_system_target'] = 'slack:C000000099'
    monkeypatch.setattr(mcs_util, 'hermes_exe', lambda _: 'synthetic-hermes')
    argv = notify_sender.send_argv(cfg, cfg['notify_system_target'], str(tmp_path / 'config.json'), kind='run_failed')
    assert argv[:3] == ['synthetic-hermes', '-p', 'legacy']


@pytest.mark.parametrize('field,value', [('profile', 'other'), ('profile', ''), ('profile', ' bad '),
                                       ('profile', None),
                                       ('team_id', 'T000000002'), ('application_id', 'A000000002')])
def test_created_system_channel_never_falls_back_when_binding_changes(tmp_path, field, value):
    cfg = created(tmp_path)
    cfg['notify']['slack'][field] = value
    with pytest.raises(notify_sender.SendFailed):
        notify_sender.send_argv(cfg, cfg['notify_system_target'], str(tmp_path / 'config.json'), kind='run_failed')


@pytest.mark.parametrize('profile', ['--other', '..'])
def test_ambiguous_created_profile_is_never_passed_to_the_cli(tmp_path, profile):
    cfg = created(tmp_path, profile=profile)
    with pytest.raises(notify_sender.SendFailed):
        notify_sender.send_argv(cfg, cfg['notify_system_target'], str(tmp_path / 'config.json'), kind='run_failed')


@pytest.mark.parametrize('damage', ['corrupt', 'pending', 'blocked', 'unknown', 'verified', 'root'])
def test_unverified_setup_record_is_not_treated_as_legacy(tmp_path, damage):
    cfg = created(tmp_path)
    if damage == 'corrupt':
        setup.path(tmp_path).write_text('{')
    elif damage == 'root':
        state = setup.read(tmp_path)
        state['root'] = str(tmp_path / 'other')
        setup.path(tmp_path).write_text(json.dumps(state))
    else:
        state = setup.read(tmp_path)
        state['state'] = damage
        state.pop('worker_ready', None)
        setup.save(tmp_path, state)
    with pytest.raises(notify_sender.SendFailed):
        notify_sender.send_argv(cfg, cfg['notify_system_target'], str(tmp_path / 'config.json'), kind='run_failed')


def test_created_route_cannot_be_used_when_the_config_selects_another_system_target(tmp_path):
    cfg = created(tmp_path)
    created_target = cfg['notify_system_target']
    cfg['notify_system_target'] = 'slack:C000000099'
    with pytest.raises(notify_sender.SendFailed):
        notify_sender.send_argv(cfg, created_target, str(tmp_path / 'config.json'), kind='run_failed')


def test_independent_setup_receipt_cannot_authorize_a_hermes_profile(tmp_path):
    cfg = created(tmp_path, mode='standalone')
    cfg['runtime_mode'] = 'hermes'
    with pytest.raises(notify_sender.SendFailed):
        notify_sender.send_argv(cfg, cfg['notify_system_target'], str(tmp_path / 'config.json'), kind='run_failed')


@pytest.mark.parametrize('mode,target', [('hermes', 'discord:42'), ('standalone', 'slack:C000000099')])
def test_non_slack_or_independent_system_sender_does_not_read_setup_state(tmp_path, monkeypatch, mode, target):
    cfg = {'runtime_mode': mode, 'notify_bot_profile': 'legacy', 'notify_system_target': target}
    monkeypatch.setattr(setup, 'read', lambda _: pytest.fail('unrelated setup record'))
    monkeypatch.setattr(mcs_util, 'hermes_exe', lambda _: 'synthetic-hermes')
    argv = notify_sender.send_argv(cfg, target, str(tmp_path / 'config.json'), kind='run_failed')
    assert (argv[:3] == ['synthetic-hermes', '-p', 'legacy'] if mode == 'hermes' else '-p' not in argv)


def test_health_watcher_passes_system_kind_to_shared_sender(tmp_path, monkeypatch):
    cfg = created(tmp_path)
    sent = []
    monkeypatch.setattr(mcs_util, 'CONF_PATH', str(tmp_path / 'config.json'))
    monkeypatch.setattr(mcs_util, 'hermes_exe', lambda _: 'synthetic-hermes')
    monkeypatch.setattr(notify_sender, 'send', lambda argv, *a, **k: sent.append(argv))
    assert health_watch.deliver_alert(cfg, 'synthetic status') is True
    assert sent[0][1:3] == ['-p', 'synthetic']


def test_health_watcher_uses_the_selected_config_root_for_created_channel_receipts(tmp_path, monkeypatch):
    selected = tmp_path / 'selected'
    selected.mkdir(mode=0o700)
    cfg = created(selected)
    conf = selected / 'config.json'
    conf.write_text(json.dumps(cfg))
    (selected / 'data' / 'health.json').write_text(json.dumps({'overall': 'failed', 'at': 990}))
    original = setup.read
    def selected_read(root):
        assert Path(root) == selected
        return original(root)
    monkeypatch.setattr(setup, 'read', selected_read)
    monkeypatch.setattr(mcs_util, 'CONF_PATH', str(tmp_path / 'other' / 'config.json'))
    monkeypatch.setattr(mcs_util, 'hermes_exe', lambda _: 'synthetic-hermes')
    import health_report
    monkeypatch.setattr(health_report, 'attachment_free_mb', lambda path=None: None)
    sent = []
    monkeypatch.setattr(notify_sender, 'send', lambda argv, *a, **k: sent.append(argv))
    assert health_watch.main(['--home', str(selected), '--config', str(conf), '--now', '1000']) == 0
    assert len(sent) == 1 and sent[0][1:3] == ['-p', 'synthetic']


def test_flush_contains_argv_failure_before_any_send_or_inflight_progress(tmp_path, monkeypatch):
    led = Ledger(str(tmp_path / 'ledger.db'))
    monkeypatch.setattr(notify_flush, '_config', lambda: {'notify_system_target': 'slack:C000000099'})
    monkeypatch.setattr(mcs_util, 'hermes_exe', lambda _: sys.executable)
    def refuse(*args, **kwargs):
        raise notify_sender.SendFailed('system_profile_unverified')
    monkeypatch.setattr(notify_sender, 'send_argv', refuse)
    monkeypatch.setattr(notify_sender, 'send', lambda *a, **k: pytest.fail('no send'))
    try:
        event = led.outbox_add('run_failed', None, {})
        result = notify_flush.flush(led)
        row = led.db.execute('SELECT * FROM notify_outbox WHERE event_id=?', (event,)).fetchone()
        assert result['failed'] == 1 and row['state'] == 'failed' and row['attempts'] == 1
        assert not json.loads(row['progress'] or '{}').get('sending')
    finally:
        led.close()
