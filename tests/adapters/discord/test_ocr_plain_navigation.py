"""The actual plain OCR page list keeps all private controls on Discord's fallback path."""
import sys

from adapters.discord import actions
from discord_testkit import _fake_discord
from notify_testkit import NOW, _click, led, pinned_clock
from test_ocr_empty_report_navigation import _open_pages, _seed

__all__ = ['led', 'pinned_clock']


def test_actual_plain_ocr_middle_page_falls_back_with_five_reports_and_three_body_controls(led, monkeypatch, pinned_clock):
    monkeypatch.setitem(sys.modules, 'discord', _fake_discord())
    spec, _, _ = _seed(led, monkeypatch, count=11)
    first, _ = _open_pages(led, spec)
    next_page = next(button for button in first['navigation'] if button['label'] == '次の5ページ')
    result = _click(led, spec, 'body', token=next_page['token'], now=NOW + 4)
    assert result['action'] == 'list' and result['outcome'] == 'applied'
    assert 'layout' not in result['list']
    assert len(result['list']['items']) == 5
    assert [button['id'] for button in result['navigation']] == ['report'] * 5 + ['body'] * 3
    assert actions.context_layout(result, lambda pid: pid == 1) is None
    fallback = actions._answer_view(None, result, 0)
    assert [button.custom_id for button in fallback.items] == [
        'mcs:a:' + button['token'] for button in result['navigation']]
    assert actions._answer_view(None, result, 1) is None
