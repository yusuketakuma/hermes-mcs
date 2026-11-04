"""Monitor wording separates normal deferral, current counts and reason streaks."""
import json

import pytest

import collection_diagnostics as diagnostics
import health_report
import health_watch
from test_collection_diagnostics import db as db, observe
from test_health_observation_budget import stored as stored
from test_health_watch_consecutive import _tick


@pytest.mark.parametrize('errors,has_error,status', [
    (['deadline_deferred:signals'], False, 'partial'),
    (['deadline_deferred:signals', 'rollup: ValueError'], True, 'partial'),
    (['unknown_fixed_code'], True, 'partial'),
    ([{'kind': 'deadline_deferred'}], False, 'partial'),
    (['deadline_deferred:signals'], False, 'failed'),
])
def test_only_normal_deferral_is_excluded_from_stage_errors(stored, errors, has_error, status):
    report = health_report.build_health(stored, {'errors': errors}, status,
                                        cfg={'backup': {'enabled': False}})
    assert ('stage_errors' in report['state_reasons']) is has_error
    assert report['run_status'] == status
    assert report['collection_diagnostics']['errors']['total'] == len(errors)
    if status == 'failed':
        assert report['overall'] == 'failed' and 'run_failed' in report['state_reasons']
    if has_error:
        assert report['overall'] != 'ok'


def test_error_counts_distinguish_deferrals_without_losing_information(db):
    block = observe(db, ['deadline_deferred:signals', 'rollup: ValueError'])
    assert diagnostics.detail_lines(block, 'errors') == [
        '処理異常1件 / 先送り1件: 時間内に終わらず先送り1件、収集後の処理1件']
    assert block['errors']['total'] == 2
    assert block['errors']['categories']['deferred'] == block['errors']['categories']['postprocess'] == 1


@pytest.mark.parametrize('jobs_only', [False, True])
def test_collection_verdict_does_not_claim_missing_patients_when_current_counts_are_zero(
        tmp_path, db, monkeypatch, jobs_only):
    previous = health_report.collection({'incomplete': [7]})
    monkeypatch.setattr(health_report, 'prev_health', lambda: previous)
    verdict = health_report.collection({'jobs_only': True} if jobs_only else {'incomplete': [7]})
    assert verdict['collection'] == 'incomplete'
    health = {'at': 3700, 'overall': 'degraded', 'state_reasons': ['collection_incomplete'],
              'collection_diagnostics': observe(db), **verdict}
    path = tmp_path / 'health.json'
    path.write_text(json.dumps(health))
    report = health_watch.classify_health(str(path), 3710, 600)
    lines = '\n'.join(health_watch._reason_rows(report, report['state_reasons']))
    assert report['state_reasons'] == ['collection_incomplete']
    assert '観測対象との差' in lines and '未取得は0件' in lines
    assert '収集全体の完了は断定できません' in lines
    assert '一部の患者の記録を取得できていません' not in lines
    assert '一部患者の記録取得に遅れや不具合' not in health_watch._impact(report, report['state_reasons'])


@pytest.mark.parametrize('intake', [None, 'present', 'unverified'])
def test_unknown_counts_or_unstored_intake_are_never_presented_as_zero(db, intake):
    block = observe(db)
    if intake is None:
        block['incomplete'] = diagnostics.unknown_incomplete()
    report = {'collection_diagnostics': block, 'semantic_intake_unstored': intake}
    rows = '\n'.join(health_watch._reason_rows(report, ['collection_incomplete']))
    assert '未取得は0件' not in rows
    if intake == 'present':
        assert '保存できず' in rows
    elif intake == 'unverified':
        assert '状態を確認できません' in rows
    else:
        assert '内訳を確認できません' in rows


def test_continuous_reason_count_does_not_claim_the_whole_reason_set_is_unchanged(tmp_path):
    _tick(tmp_path, 1000, reasons=['stage_errors', 'collection_incomplete'])
    _tick(tmp_path, 1600, reasons=['stage_errors', 'notification_pending'])
    report = _tick(tmp_path, 2200, reasons=['stage_errors', 'notification_pending'], errors=['rollup: ValueError'])
    state = json.loads((tmp_path / health_watch.STATE_REL).read_text())
    assert state['streak']['count'] == {'stage_errors': 3, 'notification_pending': 2}
    assert report['streak']['count'] == 3
    lines = '\n'.join(health_watch._alert_lines(report))
    assert '監視理由ごとの連続観測は最大3回' in lines and '同じ異常' not in lines


@pytest.mark.parametrize('damage', [None, 'bad_sum', 'bool', 'negative', 'missing', 'unknown'])
def test_notification_part_breakdown_is_explicit_or_unconfirmed(tmp_path, damage):
    states = {'pending': 5, 'unknown': 114, 'not_sent': 590, 'held': 0}
    parts = {'status': 'recorded', 'total': 709, 'by_state': states,
             'by_plan': {'current': 126, 'superseded': 583}}
    if damage == 'bad_sum':
        parts['by_plan']['current'] = 127
    elif damage == 'bool':
        parts['by_plan']['current'] = True
    elif damage == 'negative':
        parts['by_state']['held'] = -1
    elif damage == 'missing':
        del parts['by_plan']
    elif damage == 'unknown':
        parts['status'] = 'unknown'
    path = tmp_path / 'health.json'
    path.write_text(json.dumps({'at': 3700, 'overall': 'degraded',
        'state_reasons': ['notification_parts_incomplete'], 'notify': {'incomplete_parts': parts}}))
    report = health_watch.classify_health(str(path), 3710, 600)
    assert report['state_reasons'] == ['notification_parts_incomplete']
    rows = '\n'.join(health_watch._reason_rows(report, report['state_reasons']))
    if damage is None:
        assert '709件（現行126・過去583／結果不明114・未送信590・処理待ち5）' in rows
    else:
        assert '現行' not in rows and '結果不明114' not in rows
        assert report['notify_parts_breakdown'] is None


def test_partial_error_classification_never_calls_unscanned_items_abnormal(db):
    block = observe(db, ['deadline_deferred:signals'] * 257)
    assert diagnostics.detail_lines(block, 'errors')[0].startswith(
        '処理異常0件 / 先送り256件 / 未分類1件（先頭256件の内訳）')
