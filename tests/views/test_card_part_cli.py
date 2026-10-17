"""Local operator CLI plans and enqueues only synthetic sealed recovery envelopes."""
import io
import json

import pytest

import ledger
import mcs_view
from notify_testkit import SCOPE, _uuid, led as led
from test_card_part_resolve import prepare


def snapshot(store, tmp_path):
    store.db.commit()
    path = store.db.execute('PRAGMA database_list').fetchone()[2]
    return ledger.publish_snapshot(path, str(tmp_path / 'snapshots'))


def stdin(monkeypatch, value):
    monkeypatch.setattr(mcs_view.sys, 'stdin', io.TextIOWrapper(io.BytesIO(json.dumps(value).encode())))


def envelope(req):
    return {k: v for k, v in req.items() if k not in ('cmd', 'version', 'human_confirmed')}


def test_cli_plan_readonly_then_enqueue(led, tmp_path, monkeypatch, capsys):
    req = prepare(led)
    snap = snapshot(led, tmp_path)
    inbox = tmp_path / 'cmd'
    inbox.mkdir()
    common = ['--snapshot', snap, '--cmd-dir', str(inbox)]
    before = led.db.total_changes
    assert mcs_view.main(common + ['card_part_plan', '--delivery-id', req['delivery_id'],
                                   '--part-id', req['part_id']]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan['cmd'] == 'ops.card_part_resolve'
    assert plan['manifest_sha256'] == req['manifest_sha256']
    assert plan['payload_sha256'] == req['payload_sha256']
    assert not set(plan) & {'body', 'text', 'human_confirmed', 'reason', 'result'}
    assert led.db.total_changes == before and not list(inbox.iterdir())
    stdin(monkeypatch, envelope(req))
    assert mcs_view.main(common + ['control', 'card_part_resolve', '--confirm-human']) == 0
    assert json.loads(capsys.readouterr().out)['outcome'] == 'queued'
    queued = json.loads(next(inbox.glob('*.json')).read_text())
    assert queued == req
    assert 'project_id' not in queued
    assert led.db.total_changes == before


@pytest.mark.parametrize('failure', ['confirmation', 'reason', 'worker', 'envelope'])
def test_cli_does_not_bypass_recovery_validator(led, tmp_path, monkeypatch, capsys, failure):
    req = prepare(led)
    snap = snapshot(led, tmp_path)
    inbox = tmp_path / 'cmd'
    inbox.mkdir()
    command = ['--snapshot', snap, '--cmd-dir', str(inbox), 'control', 'card_part_resolve']
    body = envelope(req)
    if failure != 'confirmation':
        command.append('--confirm-human')
    if failure == 'reason':
        del body['reason']
    elif failure == 'worker':
        body['evidence']['worker_stopped'] = False
    elif failure == 'envelope':
        body['human_confirmed'] = True
    stdin(monkeypatch, body)
    if failure == 'confirmation':
        with pytest.raises(SystemExit) as exc:
            mcs_view.main(command)
        assert exc.value.code == 2
    else:
        assert mcs_view.main(command) == 1
    assert not list(inbox.iterdir())


def test_existing_card_resolve_still_requires_no_project(led, tmp_path, monkeypatch, capsys):
    req = prepare(led)
    snap = snapshot(led, tmp_path)
    inbox = tmp_path / 'cmd'
    inbox.mkdir()
    body = {'command_id': _uuid(990), 'actor': 'synthetic-operator', 'reason': 'synthetic remote check',
            'delivery_id': req['delivery_id'], 'attempt_id': req['attempt_id'],
            'result': 'mark_delivered', 'message_id': 'synthetic-post', **SCOPE,
            'evidence': {'method': 'remote_check', 'ref': 'synthetic-proof'}}
    stdin(monkeypatch, body)
    assert mcs_view.main(['--snapshot', snap, '--cmd-dir', str(inbox), 'control',
                          'card_resolve', '--confirm-human']) == 0
    assert json.loads(capsys.readouterr().out)['outcome'] == 'queued'
    queued = json.loads(next(inbox.glob('*.json')).read_text())
    assert queued['cmd'] == 'ops.card_resolve' and 'project_id' not in queued


def test_unavailable_plan_has_nonzero_exit_and_not_a_chat_view(led, tmp_path, capsys):
    req = prepare(led)
    snap = snapshot(led, tmp_path)
    assert mcs_view.main(['--snapshot', snap, 'card_part_plan', '--delivery-id', req['delivery_id'],
                          '--part-id', 'missing']) == 1
    assert json.loads(capsys.readouterr().out)['error'] == 'unknown_part'
    view = mcs_view.View(snap)
    try:
        with pytest.raises(ValueError, match='unknown_view'):
            view.read('card_part_plan', project=1)
    finally:
        view.close()


def test_lineworks_absence_keeps_duplicate_risk_gate(led, tmp_path, monkeypatch, capsys):
    req = prepare(led)
    snap = snapshot(led, tmp_path)
    inbox = tmp_path / 'cmd'
    inbox.mkdir()
    body = envelope(req)
    body.update(transport='lineworks', team_id='synthetic-team', result='mark_not_sent')
    del body['guild_id']
    body['evidence']['proof'] = 'remote_absent'
    stdin(monkeypatch, body)
    assert mcs_view.main(['--snapshot', snap, '--cmd-dir', str(inbox), 'control',
                          'card_part_resolve', '--confirm-human']) == 1
    assert json.loads(capsys.readouterr().out)['error'] == 'duplicate_risk_acknowledgement_required'
    assert not list(inbox.iterdir())
