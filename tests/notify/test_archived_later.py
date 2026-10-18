"""Archived patients stay listed, after active ones and marked (synthetic ledger)."""
from __future__ import annotations

import notify_digest
import notify_render
import notify_views
from notify_testkit import (CFG, NOW, _add_request, _at, _delivered_card,
                            _msg, _patient, led, pinned_clock)

__all__ = ["led", "pinned_clock"]

T = _at("2026-10-01", 8, 5)
MARK = notify_views.ARCHIVED_MARK


def test_my_tasks_list_archived_patients_last_and_marked(led):
    _patient(led, 1, "合成 退所", archived=1)
    _patient(led, 2, "合成 在宅")
    _add_request(led, "退所者の確認", assignee="合成 担当", due="2026-09-01", pid=1)
    _add_request(led, "在宅の確認", assignee="合成 担当", due="2026-10-30", pid=2)
    items = notify_views.my_tasks_view(led.db, "合成 担当", NOW)["items"]
    assert [i["project_id"] for i in items] == [2, 1]   # overdue but archived: still last
    assert items[1]["text"].startswith(f"・合成 退所{MARK}: ")
    assert MARK not in items[0]["text"]


def test_unacked_marks_an_archived_patients_group(led):
    _delivered_card(led)
    led.db.execute("UPDATE patients SET is_archived=1 WHERE project_id=1")
    led.db.commit()
    view = notify_views.unacked_view(led.db, "discord", now=NOW)
    assert view["items"] and all(i["group"].endswith(MARK) for i in view["items"])


def test_digest_includes_archived_posts_after_active_ones(led):
    _patient(led, 1, "合成 退所", archived=1)
    _patient(led, 2, "合成 在宅")
    _msg(led, 100, pid=1, ts=int(T - 120))
    _msg(led, 101, pid=2, ts=int(T - 60))
    _msg(led, 102, pid=2, ts=int(T - 30))
    led.db.execute("UPDATE messages SET first_seen=?", (T - 60,))
    led.db.commit()
    view = notify_digest.view(led.db, CFG, "all", now=T)
    text = notify_render.parts_text(view["parts"], "plain")
    assert "新着 3件" in text
    new = text[text.index("■ 新着"):]
    assert new.index("合成 在宅") < new.index(f"合成 退所{MARK}")
    assert "スタンプ未取得 2投稿" in text      # archived stamps are never fetched
