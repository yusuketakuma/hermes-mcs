"""Compact labels never invent execution or publication state."""
import pytest
from extraction_progress import compact_label


@pytest.mark.parametrize("progress,expected", [
    (None, "? 状況未取得"),
    ({"state": "complete", "completed": 2, "total": 3}, "? 状況未取得"),
    ({"state": "processing", "completed": True, "total": 3}, "? 状況未取得"),
    ({"state": "complete", "completed": 3, "total": 3}, "✓ 完了"),
    ({"state": "processing", "completed": 2, "total": 3}, "◐ 一部 2/3"),
    ({"state": "processing", "completed": 0, "total": 3}, "◐ 未完了"),
    ({"state": "processing", "completed": 3, "total": 3}, "◐ 未完了"),
    ({"state": "attention", "completed": 3, "total": 3}, "! 要確認"),
])
def test_evidence_driven_label(progress, expected):
    assert compact_label(progress) == expected
