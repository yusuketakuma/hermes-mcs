"""Private OCR report controls retain the exact page through the shared modal envelope."""
import copy

import attachment_feedback
from adapters.common import envelopes
import notify_actions
import notify_scope
from notify_testkit import CFG, NOW, _click, _delivered_card, led, pinned_clock

__all__ = ['led', 'pinned_clock']


def _open(ledger, monkeypatch):
    spec = _delivered_card(ledger)
    card = notify_scope.card_row(ledger.db, 1)
    ref = {'message_id': 100, 'artifact_id': 99, 'source_key': 'attachment_ocr:88:2',
           'attachment_id': 7, 'page': 2}
    monkeypatch.setattr(notify_actions.action_context, 'meds_messages', lambda *a: iter([100]))

    def view(*args, report_refs, **kwargs):
        report_refs.append(ref)
        return {'head': [], 'items': [], 'page': 0, 'pages': 1}

    monkeypatch.setattr(notify_actions, 'meds_view', view)
    monkeypatch.setattr(notify_actions, 'drug_search_available', lambda *a: False)
    base = {'actor': 'discord:1001', 'origin': {'thread_id': card['thread_id']}}
    with ledger.db:
        result = notify_actions._act_view(ledger.db, base, card, 'meds', {}, NOW, CFG,
                                          {'message_id': 100})
    button, = result['navigation']
    return spec, ref, button, result['token_ctx'][button['token']]


def test_report_page_survives_modal_context_and_builds_ocr_human_command(led, monkeypatch):
    spec, ref, button, token_ctx = _open(led, monkeypatch)
    seen = []
    monkeypatch.setattr(attachment_feedback, 'target', lambda db, req, **kw: seen.append(req) or ref)
    result = _click(led, spec, 'report', token=button['token'], now=NOW + 1)
    assert result['outcome'] == 'applied' and result['modal']
    # Slack/LINE WORKS store this nested context when opening their modal.
    modal_context = copy.deepcopy(token_ctx.get('context') or {})
    request = envelopes.human_payload('report', 'discord:1001', modal_context,
                                      result['params'], {'field': 'meds', 'note': '合成の訂正依頼'})
    assert request['source_key'] == ref['source_key']
    assert request['artifact_id'] == 99 and request['message_id'] == 100
    assert request['project_id'] == 1 and request['human_confirmed'] is True
    assert seen == [{'project_id': 1, **{key: ref[key] for key in ('message_id', 'artifact_id', 'source_key')}}]


def test_private_report_token_rejects_another_actor_and_stale_page(led, monkeypatch):
    spec, _, button, _ = _open(led, monkeypatch)
    other = _click(led, spec, 'report', token=button['token'], actor='discord:2002', now=NOW + 1)
    assert other['error'] == 'actor_mismatch'
    monkeypatch.setattr(attachment_feedback, 'target', lambda *a, **kw: None)
    stale = _click(led, spec, 'report', token=button['token'], now=NOW + 2)
    assert stale['error'] == 'extraction_changed'
