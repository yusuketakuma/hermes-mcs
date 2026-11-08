"""Update notices use bounded facts and never replay private queued text."""
from copy import deepcopy
import json

import pytest

import notify_flush
import notify_sender


_CODES = (
    'available', 'applied', 'aborted', 'rolled_back', 'rollback_failed',
    'recovery_completed', 'recovery_required', 'runtime_rolled_back',
    'gateway_restart_failed', 'lineworks_restart_failed',
    'standalone_restart_failed', 'delivery_reconcile_held',
    'drug_master_changed', 'drug_insert_changed', 'unknown',
)
_HEADERS = {
    'available': 'ℹ️ 新しいバージョンがあります',
    'applied': '✅ 更新を適用しました',
    'aborted': '🟠 更新を中止しました',
    'rolled_back': '🟠 更新前の版へ戻しました',
    'rollback_failed': '🔴 更新前の版へ戻せませんでした',
    'recovery_completed': '🟢 中断した更新の復旧処理を完了しました',
    'recovery_required': '🔴 中断した更新を復旧できません',
    'runtime_rolled_back': '🟠 Pythonの移行を元に戻しました',
    'gateway_restart_failed': '🔴 Hermesの再起動要求に失敗しました',
    'lineworks_restart_failed': '🔴 LINE WORKSの再起動要求に失敗しました',
    'standalone_restart_failed': '🔴 MCS常駐処理の再起動要求を確認できません',
    'delivery_reconcile_held': '🟠 DB復元後の通知を照合待ちにしています',
    'drug_master_changed': 'ℹ️ 医薬品マスターの確認が必要です',
    'drug_insert_changed': 'ℹ️ 添付文書の改訂を検出しました',
    'unknown': '🟠 更新通知の結果を判定できません',
}
_PRIVATE = 'SYNTHETIC_PRIVATE_TOKEN_DO_NOT_TRANSMIT'
_URL = 'https://synthetic.invalid/private?token=' + _PRIVATE
_LEGACY_APPLIED = (
    '[MCS] 更新を適用しました: v1.1.2\n'
    'prev: 0123456789ab → abcdef012345'
)
_PREVIEW_PREFIX = (
    '/mcsの操作を許可したチャンネルで実行（適用前の確認）: /mcs '
)


def _render(payload):
    before = deepcopy(payload)
    text = notify_flush._system_event_text('update_notice', payload)
    assert payload == before
    assert text.startswith('[MCSLOG] ')
    assert sum(line.startswith('影響: ') for line in text.splitlines()) == 1
    assert sum(line.startswith('次の操作: ') for line in text.splitlines()) == 1
    return text


def _assert_code(text, code):
    assert text.splitlines()[-1] == (
        'コード: MCS-UPD-' + code.upper().replace('_', '-')
    )


def _assert_unknown(text):
    _assert_code(text, 'unknown')
    assert text.splitlines()[0] == '[MCSLOG] ' + _HEADERS['unknown']
    assert _PREVIEW_PREFIX not in text


def _preview(text):
    commands = [line.removeprefix(_PREVIEW_PREFIX)
                for line in text.splitlines() if line.startswith(_PREVIEW_PREFIX)]
    assert len(commands) == 1
    return json.loads(commands[0])


@pytest.mark.parametrize('code', _CODES)
def test_each_typed_update_notice_ignores_all_private_free_fields(code):
    baseline = _render({'notice_code': code, 'tag': 'v1.1.2'})
    payload = {
        'notice_code': code, 'tag': 'v1.1.2',
        'text': _PRIVATE + '\n' + _URL,
        'title': _PRIVATE, 'impact': _PRIVATE, 'action': _PRIVATE,
        'detail': 'RuntimeError: ' + _PRIVATE, 'exception': _PRIVATE,
        'error': _PRIVATE, 'reason': _PRIVATE, 'body': _PRIVATE,
        'url': _URL, 'status': _PRIVATE, 'run_id': _PRIVATE,
        'previous_sha': _PRIVATE,
        'attachments': [{'name': _PRIVATE, 'url': _URL}],
        'extra': {'patient_name': _PRIVATE, 'secret': [_PRIVATE]},
    }
    text = _render(payload)
    _assert_code(text, code)
    assert text.splitlines()[0] == '[MCSLOG] ' + _HEADERS[code]
    assert text == baseline
    assert _PRIVATE not in text and _URL not in text
    assert 'RuntimeError' not in text
    if code == 'available':
        assert _preview(text) == {
            'op': 'control', 'phase': 'preview', 'action': 'update_apply',
            'tag': 'v1.1.2', 'reason': '<理由>',
        }
    else:
        assert _PREVIEW_PREFIX not in text


@pytest.mark.parametrize('code', _CODES)
def test_queued_typed_payload_uses_same_safe_formatter_without_ledger(code):
    payload = {'notice_code': code, 'tag': 'v1.1.2', 'text': _PRIVATE}
    event = {'kind': 'update_notice', 'payload': json.dumps(payload)}
    before = deepcopy(event)
    text, files = notify_flush._format_event(None, event)
    assert (text, files) == (_render(payload), [])
    assert event == before


def test_applied_is_actionable_without_status_command_or_service_health_claim():
    text = _render({'notice_code': 'applied', 'tag': 'v1.1.2'})
    _assert_code(text, 'applied')
    assert '更新を適用しました' in text.splitlines()[0]
    assert '対応は不要' in text
    assert 'mcs_update.py status' not in text
    assert '全サービス正常' not in text and 'すべて正常' not in text
    assert '全サービスが正常' not in text


@pytest.mark.parametrize('code', _CODES)
def test_typed_code_wins_over_a_conflicting_legacy_success_header(code):
    payload = {'notice_code': code, 'tag': 'v2.3.4',
               'text': _LEGACY_APPLIED + '\n' + _PRIVATE}
    text = _render(payload)
    assert text == _render({'notice_code': code, 'tag': 'v2.3.4'})
    _assert_code(text, code)
    assert 'v1.1.2' not in text and _PRIVATE not in text


@pytest.mark.parametrize('code', [
    None, False, True, 0, 1, 1.5, [], {}, ['applied'], {'code': 'applied'},
    '', 'APPLIED', 'applied\n', 'applied ' + _PRIVATE,
])
def test_present_but_invalid_notice_code_cannot_fall_back_to_legacy_success(code):
    text = _render({'notice_code': code, 'tag': 'v1.1.2',
                    'text': _LEGACY_APPLIED + '\n' + _PRIVATE})
    _assert_unknown(text)
    assert _PRIVATE not in text


@pytest.mark.parametrize('tag', [
    'v0.0.0', 'v1.2.3', 'v9999.9999.9999', 'v0001.0002.0003',
])
def test_available_keeps_only_bounded_ascii_release_tags(tag):
    text = _render({'notice_code': 'available', 'tag': tag})
    _assert_code(text, 'available')
    assert '候補版: ' + tag in text.splitlines()
    assert _preview(text)['tag'] == tag


@pytest.mark.parametrize('tag', [
    None, False, 123, [], {}, '', '1.2.3', 'v1.2', 'v1.2.3.4',
    'v10000.2.3', 'v1.10000.3', 'v1.2.10000', 'v１.2.3',
    'v١.2.3', 'v1.2.3-rc1', 'v1.2.3\n', ' v1.2.3',
    'v1.2.3 ' + _PRIVATE, _URL,
])
def test_available_requires_a_valid_tag_even_if_legacy_text_has_one(tag):
    text = _render({
        'notice_code': 'available', 'tag': tag,
        'text': '[MCS] 新しいバージョン v1.2.3 を検出しました',
    })
    _assert_unknown(text)
    assert '候補版:' not in text
    assert _PRIVATE not in text and _URL not in text


@pytest.mark.parametrize('tag', ['candidate:' + 'a' * 40, _URL, [], {}])
def test_applied_can_be_reported_without_displaying_a_non_release_tag(tag):
    text = _render({'notice_code': 'applied', 'tag': tag})
    _assert_code(text, 'applied')
    assert '更新を適用しました' in text.splitlines()[0]
    assert '候補版:' not in text
    assert 'candidate:' not in text and _PRIVATE not in text
    assert _PREVIEW_PREFIX not in text


@pytest.mark.parametrize('token', ['v1.1.2', 'None', 'a' * 40, 'candidate:' + 'a' * 40])
def test_legacy_queued_applied_has_a_valid_header_and_exact_transition(token):
    text, files = notify_flush._format_event(None, {
        'kind': 'update_notice',
        'payload': json.dumps({'text': _LEGACY_APPLIED.replace('v1.1.2', token)}),
    })
    _assert_code(text, 'applied')
    assert '更新を適用しました' in text.splitlines()[0]
    assert '対応は不要' in text and files == []
    assert '0123456789ab' not in text and 'abcdef012345' not in text
    assert _PRIVATE not in text and _URL not in text
    if token != 'v1.1.2':
        assert token not in text
        assert not any(line.startswith(('候補版:', '対象版:')) for line in text.splitlines())


def test_legacy_local_candidate_success_does_not_display_or_preview_its_token():
    token = 'candidate:' + 'a' * 40
    source = _LEGACY_APPLIED.replace('v1.1.2', token)
    text = _render({'text': source})
    _assert_code(text, 'applied')
    assert '更新を適用しました' in text.splitlines()[0]
    assert token not in text and _PRIVATE not in text
    assert '候補版:' not in text and _PREVIEW_PREFIX not in text


@pytest.mark.parametrize('source', [
    '[MCS] 更新を適用しました: v1.1.2',
    _LEGACY_APPLIED.replace('prev: ', ''),
    _LEGACY_APPLIED.replace('0123456789ab', '0123456789a'),
    _LEGACY_APPLIED.replace('abcdef012345', 'abcdef0123456'),
    _LEGACY_APPLIED.replace('abcdef012345', 'gggggggggggg'),
    _LEGACY_APPLIED.replace(' → ', ' -> '),
    _LEGACY_APPLIED.replace('v1.1.2', 'v1.1.2 ' + _PRIVATE),
    _LEGACY_APPLIED.replace('v1.1.2', 'candidate:' + 'a' * 39),
    _LEGACY_APPLIED + '\n' + _PRIVATE + '\n' + _URL,
    _PRIVATE + '\n' + _LEGACY_APPLIED,
])
def test_unproven_legacy_success_is_unknown_without_echoing_input(source):
    text = _render({'text': source})
    _assert_unknown(text)
    assert _PRIVATE not in text and 'prev:' not in text


def test_legacy_available_reconstructs_preview_without_replaying_queued_body():
    payload = {
        'text': '[MCS] 新しいバージョン v1.2.3 を検出しました（現在 v1.0.0）\n'
                + _PRIVATE + '\n/mcs {"action":"unsafe"}\n' + _URL,
    }
    text = _render(payload)
    _assert_code(text, 'available')
    assert '候補版: v1.2.3' in text.splitlines()
    assert _preview(text) == {
        'op': 'control', 'phase': 'preview', 'action': 'update_apply',
        'tag': 'v1.2.3', 'reason': '<理由>',
    }
    assert _PRIVATE not in text and _URL not in text and 'unsafe' not in text


@pytest.mark.parametrize('status', ['applied', 'recovery_completed', _PRIVATE, {}, []])
def test_unknown_status_field_is_not_evidence_of_update_or_recovery_success(status):
    text = _render({'status': status, 'text': _PRIVATE})
    _assert_unknown(text)
    assert _PRIVATE not in text


@pytest.mark.parametrize(('system', 'expected'), [
    (None, None), ('slack:C12345678', None),
    ('slack:C87654321', 'slack:C87654321'),
])
def test_update_event_stays_on_operational_route_without_patient_fallback(system, expected):
    cfg = {'notify_target': 'slack:C12345678', 'notify_system_target': system}
    before = deepcopy(cfg)
    assert 'update_notice' in notify_sender.SYSTEM_ALERT_KINDS
    assert notify_sender.target(cfg, 'update_notice') == expected
    assert notify_sender.target(cfg, 'new_messages') == 'slack:C12345678'
    assert notify_sender.target(cfg, 'semantic_notice') == 'slack:C12345678'
    assert cfg == before


@pytest.mark.parametrize('result', ['merge_aborted', 'interrupted_pre_merge', 'mixed_tree_reset',
                                  'needs_manual', '', 'mixed_tree_reset\n' + _PRIVATE])
def test_legacy_recovery_requires_a_known_complete_result(result):
    text = _render({'text': '[MCS] 更新が中断され復旧しました: ' + result})
    if result in ('merge_aborted', 'interrupted_pre_merge', 'mixed_tree_reset'):
        _assert_code(text, 'recovery_completed')
    else:
        _assert_unknown(text)
    assert _PRIVATE not in text
