"""Real synthetic extraction proofs drive compact provider progress."""
import processing_route
from adapters.slack.presentation import _provider_context
from test_cloud_after_local import local_then_cloud, routed, _local_pass, _cloud_pass

__all__ = ['local_then_cloud', 'routed']


def test_icons_follow_real_local_and_cloud_commits(local_then_cloud):
    ledger, cfg, _, calls, _ = local_then_cloud
    assert _local_pass(ledger)['done'] == 1
    cfg['cloud_llm']['mode'] = 'on'
    before = processing_route.provider_states(ledger.db, 1, 1, cfg, followup=True)
    assert before['local'] == 'done' and before['anthropic'] == 'incomplete'
    block = _provider_context(before, visible=['anthropic', 'jev', 'local'], compact=True)
    assert any(item.get('text') == '💻 ✓' for item in block['elements'])
    assert any(item.get('text') == '…' for item in block['elements'])
    assert _cloud_pass(ledger)['done'] == 1
    after = processing_route.provider_states(ledger.db, 1, 1, cfg, followup=True)
    assert after['anthropic'] == 'done' and after['local'] != 'done'
    block = _provider_context(after, visible=['anthropic', 'jev', 'local'], compact=True)
    assert any(item.get('text') == '✓' for item in block['elements'])
    assert not any(item.get('text') in ('…', '💻 ✓') for item in block['elements'])
    assert len(calls) == 1
