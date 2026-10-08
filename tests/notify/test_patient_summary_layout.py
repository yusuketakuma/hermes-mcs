"""🧾 患者まとめ: what needs action (open tasks) comes before the
extracted material, and an overdue task is marked."""
from notify_testkit import _add_request, _seed_thread, led  # noqa: F401
from notify_views import patient_summary_text

__all__ = ["led"]


def test_open_tasks_come_first_and_overdue_is_marked(led):
    _seed_thread(led)
    _add_request(led, title="合成の確認", due="2000-01-01")
    _add_request(led, title="合成の予定", due="2999-01-01")
    _, text = patient_summary_text(led.db, 1)
    lines = text.split("\n")
    tasks = lines.index("■ 未完了タスク")
    assert tasks < next(i for i, ln in enumerate(lines) if ln.startswith("■ 背景"))
    assert "合成の確認 — 期限 2000-01-01 ⚠期限切れ" in text
    assert "合成の予定 — 期限 2999-01-01" in text
    assert "2999-01-01 ⚠" not in text
