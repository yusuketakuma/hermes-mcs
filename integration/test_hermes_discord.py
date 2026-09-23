"""Run from the Hermes candidate with scripts/run_tests.sh <this absolute path>.

Real plugin discovery, native Discord event creation, gateway dispatch, snapshot,
and inbox processing. All data is synthetic and all network I/O is forbidden.
"""
import json
import socket
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'mcs'))
import _mcs_path  # noqa: F401  registers every subdir as import root

from ledger import Ledger, publish_snapshot
from mcs_adapter import Message
import job_ops
import semantic
import semantic_loops
from gateway.config import Platform, PlatformConfig
from gateway.run import GatewayRunner
from gateway.run_inbound import GatewayInboundMixin
from gateway.platforms.event import MessageEvent, MessageType
from plugins.platforms.discord.adapter import DiscordAdapter

MCS_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.asyncio
async def test_native_discord_confirmation_uses_snapshot_and_durable_inbox(tmp_path, monkeypatch):
    def no_network(*args, **kwargs):
        raise AssertionError('integration test must not contact any service')
    monkeypatch.setattr(socket.socket, 'connect', no_network)
    home = tmp_path / 'hermes'
    plugin_dir = home / 'plugins' / 'mcs-discord-commands'
    plugin_dir.parent.mkdir(parents=True)
    plugin_dir.symlink_to(MCS_ROOT / 'hermes_plugin', target_is_directory=True)
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setenv('HOME', str(tmp_path))
    inbox = tmp_path / 'inbox'
    inbox.mkdir()
    db_path = tmp_path / 'source.db'
    db = Ledger(str(db_path))
    db.ensure_patient(1)
    db.save_messages([Message(1, 1, None, 1, 'synthetic', 'user', '', '',
                             '2026-09-20T00:00:00+09:00', '<p>Confirm synthetic task.</p>',
                             'full', False, 0)])
    config, _ = semantic.semantic_config({'semantic': {'mode': 'shadow', 'project_ids': [1]}})
    db.artifact_add('semantic_policy', semantic.policy_fingerprint(config))
    bundle = semantic.thread_bundle(db, 1, 1, [1])
    member = bundle['members'][0]
    body = member['body_original']
    fact = {'kind': 'explicit_request', 'polarity': 'affirmed',
            'statement': 'Confirm synthetic task.', 'evidence_refs': ['e1'],
            '_evidence': {'evidence_id': 'e1', 'message_id': 1,
                          'revision_id': member['revision'], 'start_codepoint': 0,
                          'end_codepoint': len(body), 'quote': body}}
    semantic_loops.update_loops(db, 1, bundle, {1: [fact]}, None, config, time.monotonic() + 30)
    candidate = db.artifacts('loop_candidate', project_id=1)[0]
    snapshot = publish_snapshot(str(db_path), str(tmp_path / 'snapshots'))
    settings = dict(snapshot=snapshot, inbox=str(inbox), allowed_user_ids=['42'],
                    allowed_chat_ids=['123', '124'], project_ids=[1])
    config = {'plugins': {'enabled': ['mcs-discord-commands'], 'entries': {
        'mcs-discord-commands': {'settings': settings}}}}
    (home / 'config.yaml').write_text(yaml.safe_dump(config))
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token='synthetic-token'))
    interaction = SimpleNamespace(id=10, channel_id=123,
        channel=SimpleNamespace(id=123, name='synthetic', guild=None, topic=None),
        guild_id=None, user=SimpleNamespace(id=42, display_name='synthetic', bot=False))
    runner = object.__new__(GatewayRunner)
    runner._draining = False
    runner._hm_quick_commands = lambda: {}
    runner.adapters = {Platform.DISCORD: adapter}
    monkeypatch.setenv('DISCORD_ALLOWED_USERS', '42')
    monkeypatch.setenv('DISCORD_ALLOW_ALL_USERS', 'false')
    monkeypatch.setenv('GATEWAY_ALLOW_ALL_USERS', 'false')
    monkeypatch.delenv('GATEWAY_ALLOWED_USERS', raising=False)

    async def invoke(payload, *, internal=False, bot_message=False):
        text = '/mcs ' + json.dumps(payload)
        event = adapter._build_slash_event(interaction, text)
        if bot_message:
            # Normal Discord messages carry is_bot through build_source;
            # native slash interactions do not populate that field.
            event = MessageEvent(
                text=text, message_type=MessageType.COMMAND,
                message_id=str(interaction.id),
                source=adapter.build_source(
                    chat_id=str(interaction.channel_id), chat_type='group',
                    user_id=str(interaction.user.id), is_bot=True))
        event.internal = internal
        admitted = await GatewayInboundMixin._hm_admit_event(runner, event)
        if admitted is None:
            return {'ok': False, 'error': 'gateway_admission_rejected'}
        event, source, _ = admitted
        handled, result, _ = await GatewayInboundMixin._hm_dispatch_quick_and_plugin_commands(
            runner, event, source, 'mcs')
        assert handled, 'actual discovered plugin must handle the command'
        return json.loads(result)

    try:
        status = await invoke({'op': 'status', 'project_id': 1})
        assert status['ok'], status
        preview = await invoke({'op': 'request', 'phase': 'preview', 'action': 'create',
                                'project_id': 1, 'source_message_id': 1,
                                'title': 'Synthetic explicitly approved task',
                                'reason': 'Human reviewed the synthetic source',
                                'loop_artifact_id': candidate['artifact_id'],
                                'loop_match_confirmed': True})
        assert preview['ok'] and not list(inbox.iterdir())
        confirmation = {key: preview[key] for key in ('payload', 'payload_hash', 'origin')}
        confirmation.update(op='request', phase='confirm')
        assert not (await invoke(confirmation, internal=True))['ok']
        monkeypatch.setenv('DISCORD_ALLOWED_USERS', '43')
        assert not (await invoke(confirmation))['ok']  # current Hermes allowlist is rechecked
        monkeypatch.setenv('DISCORD_ALLOWED_USERS', '42')
        assert not (await invoke(confirmation, bot_message=True))['ok']
        interaction.channel_id = 124
        interaction.channel.id = 124
        assert not (await invoke(confirmation))['ok']  # both chats allowed, origin still bound
        interaction.channel_id = 123
        interaction.channel.id = 123
        interaction.id = 11  # confirmation is a separate human interaction
        assert (await invoke(confirmation))['ok']
        assert db.db.execute('SELECT count(*) FROM requests').fetchone()[0] == 0
        job_ops.drain_commands(db, {'errors': []}, str(inbox))
        assert db.db.execute('SELECT count(*) FROM requests').fetchone()[0] == 1
        receipt = json.loads(db.db.execute('SELECT receipt_json FROM command_receipts').fetchone()[0])
        assert receipt['reason'] == preview['payload']['reason']
        assert receipt['actor'] == 'discord:42'
        assert receipt['loop_ref']['artifact_id'] == candidate['artifact_id']
        # Re-delivery of the same confirmed operation must not create another request.
        assert (await invoke(confirmation))['ok']
        job_ops.drain_commands(db, {'errors': []}, str(inbox))
        assert db.db.execute('SELECT count(*) FROM requests').fetchone()[0] == 1
        assert len(db.artifacts('request_loop_link', project_id=1)) == 1
        publish_snapshot(str(db_path), str(tmp_path / 'snapshots'))
        loops = await invoke({'op': 'read', 'kind': 'loops', 'project_id': 1})
        linked = loops['result']['items'][0]['linked_requests'][0]
        assert linked['request_id'] == receipt['request_id'] and linked['status'] == 'open'
        completion = await invoke({'op': 'request', 'phase': 'preview', 'action': 'update', 'project_id': 1,
                                   'request_id': receipt['request_id'],
                                   'patch': {'status': 'done'}, 'reason': 'Human verified completion'})
        assert completion['ok']
        command = {key: completion[key] for key in ('payload', 'payload_hash', 'origin')}
        command.update(op='request', phase='confirm')
        assert (await invoke(command))['ok']
        job_ops.drain_commands(db, {'errors': []}, str(inbox))
        publish_snapshot(str(db_path), str(tmp_path / 'snapshots'))
        loops = await invoke({'op': 'read', 'kind': 'loops', 'project_id': 1})
        assert loops['result']['items'][0]['linked_requests'][0]['status'] == 'done'
        assert db.artifacts('loop_candidate', project_id=1)[0]['content'] == candidate['content']
        db.job_add('semantic', 1, 1, payload={'targets': [1], 'generation': 'synthetic'})
        job_id = db.db.execute("SELECT job_id FROM fetch_jobs WHERE kind='semantic'").fetchone()[0]
        for action, fields in (
                ('pause', {'feature': 'semantic'}), ('resume', {'feature': 'semantic'}),
                ('retry', {'job_id': job_id}), ('scan', {'days': 14, 'pages': 2})):
            publish_snapshot(str(db_path), str(tmp_path / 'snapshots'))
            control = await invoke({'op': 'control', 'phase': 'preview', 'action': action,
                                    'project_id': 1, **fields})
            assert control['ok'] and not list(inbox.iterdir())
            command = {key: control[key] for key in ('payload', 'payload_hash', 'origin')}
            command.update(op='control', phase='confirm')
            confirmed = await invoke(command)
            assert confirmed['ok']
            job_ops.drain_commands(db, {'errors': []}, str(inbox))
            publish_snapshot(str(db_path), str(tmp_path / 'snapshots'))
            receipt = await invoke({'op': 'read', 'kind': 'receipt', 'project_id': 1,
                                    'command_id': confirmed['receipt']['command_id'],
                                    'payload_hash': confirmed['receipt']['payload_hash']})
            assert receipt['ok'] and receipt['result']['outcome'] == 'applied'
            operations = await invoke({'op': 'read', 'kind': 'operations', 'project_id': 1})
            assert operations['ok']
            assert operations['result']['semantic_paused'] == (action == 'pause')
        assert db.db.execute("SELECT attempts FROM fetch_jobs WHERE job_id=?", (job_id,)).fetchone()[0] == 0
        assert db.db.execute("SELECT count(*) FROM fetch_jobs WHERE kind='history'").fetchone()[0] == 1
        with db.db:
            db.db.execute("UPDATE fetch_jobs SET state='failed',attempts=6 WHERE job_id=?", (job_id,))
        publish_snapshot(str(db_path), str(tmp_path / 'snapshots'))
        retry_input = {'op': 'control', 'phase': 'preview', 'action': 'retry',
                       'project_id': 1, 'job_id': job_id}
        assert (await invoke(retry_input))['error'] == 'attempt_limit'
        extension = await invoke({**retry_input, 'additional_attempts': 2,
                                  'reason': 'Human checked the failure and authorized two attempts'})
        assert extension['ok']
        command = {key: extension[key] for key in ('payload', 'payload_hash', 'origin')}
        command.update(op='control', phase='confirm')
        for _ in range(2):
            assert (await invoke(command))['ok']
            job_ops.drain_commands(db, {'errors': []}, str(inbox))
        publish_snapshot(str(db_path), str(tmp_path / 'snapshots'))
        operations = await invoke({'op': 'read', 'kind': 'operations', 'project_id': 1})
        retried = next(item for item in operations['result']['items'] if item['job_id'] == job_id)
        assert retried['attempts'] == 6 and retried['attempt_limit'] == 8
        assist, errors = semantic.semantic_config({'semantic': {
            'mode': 'assist', 'summary_mode': 'assist', 'project_ids': [1]}})
        assert not errors
        policy = semantic.policy_fingerprint(assist)
        db.artifact_add('semantic_policy', policy)
        db.artifact_add('extract_llm', json.dumps({'summary': 'Old synthetic summary', 'points': []}),
                        project_id=1, message_id=1, meta={'hash': member['revision']})
        with db.db:
            semantic._write_result(db, 1, 1, {
                'summary': {'target_message_id': 1, 'claims': [{'section': 'requests', 'text': body,
                                       'claim_kind': 'reported_fact', 'fact_refs': [0],
                                       'status': 'planned', 'polarity': 'affirmed'}],
                            'limitations': []},
                'findings': [], 'repaired': False},
                bundle['source_fingerprint'], {1: member}, 'PASS', policy, 'assist')
        publish_snapshot(str(db_path), str(tmp_path / 'snapshots'))
        comparison = await invoke({'op': 'read', 'kind': 'comparison', 'project_id': 1,
                                   'message_id': 1})
        assert comparison['ok'] and comparison['result']['adoptable']
        assert comparison['result']['diff']
        preview = await invoke({'op': 'control', 'phase': 'preview', 'action': 'adopt_summary',
                                'project_id': 1, 'message_id': 1,
                                'reason': 'Human compared both synthetic summaries'})
        assert preview['ok'] and not list(inbox.iterdir())
        command = {key: preview[key] for key in ('payload', 'payload_hash', 'origin')}
        command.update(op='control', phase='confirm')
        outbox_before = [tuple(row) for row in db.db.execute('SELECT * FROM notify_outbox')]
        for _ in range(2):
            assert (await invoke(command))['ok']
            job_ops.drain_commands(db, {'errors': []}, str(inbox))
        assert len(db.artifacts('semantic_adoption', project_id=1)) == 1
        assert [tuple(row) for row in db.db.execute('SELECT * FROM notify_outbox')] == outbox_before
        publish_snapshot(str(db_path), str(tmp_path / 'snapshots'))
        comparison = await invoke({'op': 'read', 'kind': 'comparison', 'project_id': 1,
                                   'message_id': 1})
        assert comparison['result']['candidate']['adopted']
    finally:
        db.close()
