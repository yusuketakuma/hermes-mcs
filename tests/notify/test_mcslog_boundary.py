"""Operational Slack messages never fall back to patient information or echo source text."""
import json

import pytest

import health_watch
import notify_flush
import notify_sender


@pytest.mark.parametrize('kind', sorted(notify_sender.SYSTEM_ALERT_KINDS))
@pytest.mark.parametrize('system', [None, '', ' slack:C12345678 ', 'slack:C12345678:123.456'])
def test_slack_system_route_requires_a_separate_channel(kind, system):
    cfg = {'notify_target': 'slack:C12345678', 'notify_system_target': system}
    assert notify_sender.target(cfg, kind) is None
    cfg['notify_system_target'] = 'slack:C87654321'
    assert notify_sender.target(cfg, kind) == 'slack:C87654321'


@pytest.mark.parametrize('kind', ['new_messages', 'attachment_followup', 'semantic_notice', 'daily_digest', 'task_reminder', 'unknown_kind'])
def test_patient_information_never_uses_system_target(kind):
    cfg = {'notify_target': 'slack:C12345678', 'notify_system_target': 'slack:C87654321'}
    assert notify_sender.target(cfg, kind) == 'slack:C12345678'
    del cfg['notify_target']
    assert notify_sender.target(cfg, kind) is None


@pytest.mark.parametrize('patient', ['discord:123', 'lineworks:synthetic', 'local'])
def test_other_transports_keep_existing_system_fallback(patient):
    assert notify_sender.target({'notify_target': patient}, 'run_failed') == patient


@pytest.mark.parametrize('kind', sorted(notify_sender.SYSTEM_ALERT_KINDS))
def test_system_formatter_never_echoes_patient_or_unknown_payload(kind):
    private = 'synthetic_patient_123_diagnosis'
    payload = {'run_id': private, 'detail': private, 'text': private,
               'name': private, 'body': private, 'attachments': [private], 'class': private}
    text, files = notify_flush._format_event(None, {'kind': kind, 'payload': json.dumps(payload)})
    assert private not in text and files == []
    assert text.startswith('[MCSLOG]') and '影響:' in text and '次の操作:' in text
    assert text.endswith('コード: ' + kind)


def test_update_notice_keeps_only_valid_release_and_rebuilt_preview_command():
    payload = {'text': '[MCS] 新しいバージョン v1.2.3 を検出しました（現在 v1.0.0）\n'
               'synthetic_patient_123\n/mcs {"action":"unsafe"}', 'run_id': 7}
    text, files = notify_flush._format_event(None, {'kind': 'update_notice', 'payload': json.dumps(payload)})
    assert '候補版: v1.2.3' in text and 'run 7' in text and files == []
    assert 'synthetic_patient' not in text and 'unsafe' not in text
    command = json.loads(text.split('適用前の確認: /mcs ')[1].splitlines()[0])
    assert command == {'op': 'control', 'phase': 'preview', 'action': 'update_apply',
                       'tag': 'v1.2.3', 'reason': '<理由>'}
    from adapters.common import human_commands
    import mcs_requests
    assert set(command) <= human_commands.CONTROL_FIELDS['update_apply']
    request = human_commands._new_control(mcs_requests, {'user_id': 'SYNTHETIC'},
                                         command, None, 'ops.update_apply')
    assert request['version'] == 1 and mcs_requests.validate(request) is None


def test_health_unknown_codes_and_host_details_never_escape_and_counts_remain():
    private = 'synthetic_patient_123'
    report = {'status': 'degraded', 'alert': True, 'disk_alert': False, 'disk_low': False,
              'state_reasons': [private, 'notification_parts_incomplete', 'notification_pending'],
              'host_details': {'notification_pending': private},
              'notify_pending': 0, 'notify_incomplete_parts': 0,
              'notify_wait': {'reasons': {private: 2, 'card_parts_unknown': 1}, 'attention': 3},
              'last_interrupt': {'reason': private, 'detected_at': 3500},
              'health_at': 3600, 'last_ok_at': 0,
              'streak': {'count': 4, 'first_at': 0}}
    before = json.dumps(report, sort_keys=True)
    text = '\n'.join(health_watch._alert_lines(report))
    assert private not in text and '原因未分類' in text
    assert '0件' in text and '待機中 2件' in text and '照合待ち' in text
    assert '自動で再送しません' in text
    assert text.index('原因と対処:') < text.index('継続:') < text.index('最終正常')
    assert 'コード: degraded unknown,notification_parts_incomplete,notification_pending' in text
    assert json.dumps(report, sort_keys=True) == before


def test_unknown_health_status_is_not_rendered_as_free_text():
    text = '\n'.join(health_watch._alert_lines({'status': 'synthetic_patient_123', 'alert': True,
                   'disk_alert': False, 'state_reasons': [], 'health_at': 0}))
    assert 'synthetic_patient_123' not in text and '状態を確認できません' in text


def test_system_watcher_does_not_send_to_patient_channel(monkeypatch):
    monkeypatch.setattr(notify_sender, 'send', lambda *a, **k: pytest.fail('must not send'))
    assert health_watch.deliver_alert({'notify_target': 'slack:C12345678'}, 'synthetic') is False


@pytest.mark.parametrize('system', [None, 'slack:C12345678', 'slack:C12345678:123.456'])
def test_standalone_recovery_does_not_fallback_to_patient_channel(monkeypatch, system):
    from deployment.recovery import mcs_recovery_services as recovery
    monkeypatch.setattr(recovery.mcs_recovery_core, '_check_recovery_root', lambda: None)
    monkeypatch.setattr(recovery.mcs_recovery_core, '_runtime_config', lambda: {
        'runtime_mode': 'standalone', 'notify_target': 'slack:C12345678', 'notify_system_target': system})
    monkeypatch.setattr(recovery.subprocess, 'Popen', lambda *a, **kw: pytest.fail('must not send'))
    recovery._notify('synthetic operational notice')


def test_repeated_health_actions_are_listed_once_without_dropping_causes():
    text = '\n'.join(health_watch._alert_lines({'status': 'degraded', 'alert': True,
        'disk_alert': False, 'state_reasons': ['synthetic_patient_a', 'synthetic_patient_b'],
        'health_at': 3600}))
    assert text.count('原因未分類の異常があります') == 2
    assert text.count('python3 mcs/ops/mcs_setup.py check') == 1
    assert '共通の対処:' in text and 'synthetic_patient' not in text
