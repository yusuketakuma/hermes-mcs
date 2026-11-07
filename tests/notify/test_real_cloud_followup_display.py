"""Real cloud context receipts and QA feed a newly sealed Slack progress view.

Local/semantic prerequisites are synthetic proofs from the context fixture;
real local completion and the closed individual cloud lane are tested separately
in test_cloud_after_local. This is not a combined local-to-cloud end-to-end run.
"""
import json

import notify_cards
import patient_context_progress
from adapters.slack.cards import validate
from adapters.slack.presentation import render_parts
from anthropic_context_testkit import cloud, runtime
from test_context_cloud_update_identity import real_cloud, finish, receipts
from test_patient_context_refresh import document
from notify_testkit import _dispatch, _intent, _latest_render
from slack_testkit import SLACK

__all__ = ['runtime', 'cloud', 'real_cloud']


def test_real_context_adoption_drives_new_slack_card_progress(runtime, real_cloud):
    ledger, clock, _ = runtime
    cfg, posts = real_cloud
    finish(runtime, cfg)
    assert posts and ledger.synthetic_jev_requests
    assert len(receipts(ledger)) == 2 * len(posts)
    assert document(ledger)
    progress = patient_context_progress.read(ledger.db, 1, cfg)
    assert progress['percent'] == 100
    assert not progress['held']
    financial = [tuple(row) for row in ledger.db.execute(
        "SELECT * FROM artifacts WHERE kind IN ('cloud_usage_reserve','cloud_usage_result',"
        "'cloud_usage_disposition','cloud_usage_dryrun') ORDER BY artifact_id")]
    post_count = len(posts)
    notify_cfg = {**cfg, **SLACK}
    assert _dispatch(ledger, _intent(ledger, payload={'message_ids': [1]}),
                     notify_cfg, now=clock.wall)['dispatched']
    render = _latest_render(ledger)
    spec = json.loads(render['spec_json'])
    assert validate(spec) is spec
    header = next(c for c in spec['parts']['containers'] if c.get('provider_scope') == 'header')
    assert spec['parts']['processing_display_version'] == 3
    assert header['context_progress'] == {**progress, 'observed_at': clock.wall}
    assert header['visible_providers'] == ['jev', 'local']
    blocks = render_parts(spec['parts'], card_kind='thread')
    shown = json.dumps(blocks, ensure_ascii=False)
    assert '👤 100%' in shown
    images = [element['alt_text'].split()[0] for block in blocks
              for element in block.get('elements', []) if element.get('type') == 'image']
    assert 'Anthropic' not in images and 'OpenAI' not in images
    count = ledger.db.execute('SELECT COUNT(*) FROM notification_renders').fetchone()[0]
    notify_cards.sweep(ledger, notify_cfg, now=clock.wall + 1)
    assert ledger.db.execute('SELECT COUNT(*) FROM notification_renders').fetchone()[0] == count
    assert _latest_render(ledger)['delivery_id'] == render['delivery_id']
    assert len(posts) == post_count
    assert [tuple(row) for row in ledger.db.execute(
        "SELECT * FROM artifacts WHERE kind IN ('cloud_usage_reserve','cloud_usage_result',"
        "'cloud_usage_disposition','cloud_usage_dryrun') ORDER BY artifact_id")] == financial
