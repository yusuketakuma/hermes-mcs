"""Core entrypoints resolve the same sealed Slack contracts outside the checkout cwd."""
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest


@pytest.mark.parametrize("operation", ["sweep", "mark_delivered", "mark_not_sent", "tampered"])
def test_core_only_entrypoint_uses_canonical_slack_contract(tmp_path, operation):
    root = Path(__file__).resolve().parents[2]
    code = textwrap.dedent("""
        import importlib.util
        import json
        from pathlib import Path
        import socket
        import sys
        def offline(*args, **kwargs):
            raise AssertionError('Synthetic child cannot use network sockets')
        socket.socket.connect = offline
        socket.socket.connect_ex = offline
        socket.socket.sendto = offline
        socket.create_connection = offline
        root, temp = map(Path, sys.argv[1:3])
        operation = sys.argv[3]
        assert str(root) not in sys.path
        sys.path.insert(0, str(root / 'mcs'))
        import _mcs_path
        assert str(root) in sys.path
        assert not any(n == 'adapters' or n.startswith('adapters.') for n in sys.modules)
        sys.path[:0] = [str(root / 'tests' / p) for p in ('support', 'notify', 'adapters/slack')]
        from common_testkit import make_ledger
        from notify_testkit import NOW, _attach, _dispatch, _intent, _latest_render, _seed_thread
        import notify_cards
        cfg = {'notify': {'interactive': 'slack', 'route_epoch': 1, 'card_thread': True,
                         'slack': {'profile': 'synthetic', 'application_id': 'A_SYNTHETIC',
                                   'team_id': 'T_SYNTHETIC', 'channel_id': 'C_SYNTHETIC'}},
               'signals': {'notify': True}}
        led = make_ledger(temp, data_dir=True)
        try:
            if operation == 'sweep':
                _seed_thread(led, mids=(100,))
                assert _dispatch(led, _intent(led, payload={'message_ids': [100]}), cfg)['dispatched']
                prior = _latest_render(led)['delivery_id']
                _attach(led, 100)
                result = notify_cards.sweep(led, cfg, now=NOW + 1)
                render = _latest_render(led)
                spec = json.loads(render['spec_json'])
                assert render['delivery_id'] != prior
                assert result['updated'] == 1
                assert spec['parts']['slack']['file_group'] is True
                group = spec['parts']['manifest'][-1]
                assert group['kind'] == 'file_group'
                saved = led.db.execute('SELECT payload_sha256 FROM notification_render_parts '
                                       'WHERE delivery_id=? AND part_id=?',
                                       (render['delivery_id'], 'file_group')).fetchone()
                from adapters.common.spec import file_group_sha256
                assert saved['payload_sha256'] == group['sha256'] == file_group_sha256(spec)
            else:
                from test_card_preflight_file_groups import group_unknown
                from slack_testkit import SLACK
                from adapters.slack import cards
                import notify_recovery
                import notify_spool
                from mcs_requests import canonical, payload_hash
                req, render, spec = group_unknown(led)
                published, validated = [], []
                notify_spool.publish_specs = lambda *args: published.extend(args[2])
                validate = cards.validate
                def canonical_validate(value):
                    validated.append(True)
                    return validate(value)
                cards.validate = canonical_validate
                old_files = {row[0]: row[1] for row in led.db.execute(
                    "SELECT part_id,remote_id FROM notification_render_parts WHERE delivery_id=? "
                    "AND kind='attachment_part'", (req['delivery_id'],))}
                if operation == 'tampered':
                    spec['parts']['manifest'][-1]['sha256'] = '0' * 64
                    render = {**dict(render), 'spec_json': canonical(spec).decode(),
                              'payload_hash': payload_hash(spec)}
                    card = dict(led.db.execute('SELECT * FROM notification_cards WHERE card_id=?',
                                               (render['card_id'],)).fetchone())
                    assert notify_recovery._file_group_resolution_error(
                        led.db, render, card, req, req) == 'file_group_identity_unverified'
                    assert len(validated) == 1 and not published
                else:
                    if operation == 'mark_not_sent':
                        req.update(result=operation, evidence={**req['evidence'], 'proof': 'no_journal_started'})
                        req.pop('remote_id')
                    receipt = notify_recovery.apply_card_part_resolve(led, req, SLACK, NOW + 1)
                    assert receipt['outcome'] == 'applied', receipt
                    assert len(validated) >= (2 if operation == 'mark_not_sent' else 1)
                    assert len(published) == (1 if operation == 'mark_not_sent' else 0)
                    if published:
                        stages = [p for p in published[0]['parts']['manifest'] if p['kind'] == 'attachment_part']
                        assert {p['part_id']: p.get('prior_remote_id') for p in stages} == old_files
                    assert notify_recovery.apply_card_part_resolve(led, req, SLACK, NOW + 2) == receipt
                    assert len(published) == (1 if operation == 'mark_not_sent' else 0)
            assert not any(n == 'slack_sdk' or n.startswith('slack_sdk.') or n == 'discord'
                           or n.startswith('discord.') for n in sys.modules)
        finally:
            led.close()
    """)
    result = subprocess.run([sys.executable, "-I", "-c", code, str(root), str(tmp_path), operation],
                            cwd=tmp_path, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
