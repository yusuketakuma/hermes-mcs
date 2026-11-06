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
    command = json.loads(text.split('患者情報チャンネルで実行（適用前の確認）: /mcs ')[1].splitlines()[0])
    assert command == {'op': 'control', 'phase': 'preview', 'action': 'update_apply',
                       'tag': 'v1.2.3', 'reason': '<理由>'}
    from adapters.common import human_commands
    import mcs_requests
    assert set(command) <= human_commands.CONTROL_FIELDS['update_apply']
    request = human_commands._new_control(mcs_requests, {'user_id': 'SYNTHETIC'},
                                         command, None, 'ops.update_apply')
    assert request['version'] == 1 and mcs_requests.validate(request) is None
    from adapters.slack.actions import Actions
    actions = object.__new__(Actions)
    actions._active = True
    actions._settings = {'team_id': 'SYNTHETIC_TEAM', 'application_id': 'SYNTHETIC_APP',
                         'allowed_user_ids': ['SYNTHETIC'], 'channel_id': 'C12345678'}
    body = {'team_id': 'SYNTHETIC_TEAM', 'api_app_id': 'SYNTHETIC_APP', 'user_id': 'SYNTHETIC'}
    assert actions._slash_scope_ok({**body, 'channel_id': 'C12345678'})
    assert not actions._slash_scope_ok({**body, 'channel_id': 'C87654321'})


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


@pytest.mark.parametrize('mode', ['standalone', 'hermes'])
@pytest.mark.parametrize('source', [
    'synthetic_patient_123 diagnosis /private/synthetic.pdf https://synthetic.invalid/patient',
    'RuntimeError: synthetic_patient_123',
    '[MCS] 中断された更新の後処理を完了しました synthetic_patient_123',
])
def test_recovery_sender_never_transmits_raw_input(monkeypatch, mode, source):
    from deployment.recovery import mcs_recovery_services as recovery
    captured = []
    class Process:
        def communicate(self, text, **kwargs):
            captured.append(json.loads(text) if isinstance(text, bytes) else text)
    def popen(argv, **kwargs):
        captured.append(argv)
        return Process()
    monkeypatch.setattr(recovery.mcs_recovery_core, '_check_recovery_root', lambda: None)
    monkeypatch.setattr(recovery.mcs_recovery_core, '_runtime_config', lambda: {
        'runtime_mode': mode, 'notify_target': 'slack:C12345678', 'notify_system_target': 'slack:C87654321'})
    monkeypatch.setattr(recovery.mcs_recovery_core, 'HOME', '/synthetic/home', raising=False)
    monkeypatch.setattr(recovery.mcs_recovery_core, 'REPO', '/synthetic/repo', raising=False)
    monkeypatch.setattr(recovery.os, 'access', lambda *a: True)
    monkeypatch.setattr(recovery.os.path, 'isfile', lambda *a: True)
    monkeypatch.setattr(recovery.shutil, 'which', lambda *a: '/synthetic/hermes')
    monkeypatch.setattr(recovery.subprocess, 'Popen', popen)
    recovery._notify(source)
    wire = json.dumps(captured, ensure_ascii=False)
    assert captured and '更新・復旧の状態確認' in wire
    assert 'synthetic_patient_123' not in wire and 'synthetic.pdf' not in wire
    assert 'https://' not in wire and 'RuntimeError' not in wire
    assert '🟢' not in wire


@pytest.mark.parametrize(('source', 'title'), [
    ('[MCS] 中断された更新の後処理を完了しました', '🟢 更新・復旧の処理完了'),
    ('[MCS] 更新の復旧後に再起動要求を保存・起動できません。'
     '更新記録と稼働プロセスを確認し、常駐プロセスを再起動してください。', '🔴 再起動要求の失敗・要確認'),
])
def test_recovery_fixed_results_keep_completion_distinct_from_failed_restart(source, title):
    from deployment.recovery import mcs_recovery_services as recovery
    text = recovery._notification_text(source)
    assert title in text
    assert 'mcs_update.py status' in text and 'mcs_setup.py doctor' in text
    assert '収集・通知の再開は別途確認' in text if '🟢' in title else '🟢' not in text


@pytest.mark.parametrize(('patient', 'system'), [
    ('slack:#patient', 'slack:C87654321'), ('slack:C12345678', 'slack:#mcslog'),
    ('slack:#same', 'slack:C12345678'), ('slack:D12345678', 'slack:C87654321'),
    ('slack:C12345678:123.456', 'slack:C87654321'), ('slack', 'slack:C87654321'),
])
def test_system_route_requires_resolved_channel_ids_in_sender_and_recovery(monkeypatch, patient, system):
    cfg = {'runtime_mode': 'standalone', 'notify_target': patient, 'notify_system_target': system}
    assert notify_sender.target(cfg, 'run_failed') is None
    assert notify_sender.target(cfg, 'new_messages') == patient
    from deployment.recovery import mcs_recovery_services as recovery
    monkeypatch.setattr(recovery.mcs_recovery_core, '_check_recovery_root', lambda: None)
    monkeypatch.setattr(recovery.mcs_recovery_core, '_runtime_config', lambda: cfg)
    monkeypatch.setattr(recovery.subprocess, 'Popen', lambda *a, **kw: pytest.fail('must not send'))
    recovery._notify('synthetic')
