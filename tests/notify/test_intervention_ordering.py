"""Written intervention plans keep their scope and ordering across pages."""
from datetime import datetime

import notify_views
from semantic_testkit import _message
from test_patient_context_v3_views import T0, ctx  # noqa: F401

__all__ = ["ctx"]


def test_future_plans_are_sorted_by_event_day_then_written_role_without_dropping_rows(ctx):
    plans = [
        (80, "薬剤師", 5, "服薬指導"), (81, "看護師", 3, "訪問"),
        (82, "医師", 3, "往診"), (83, "薬剤師", 3, "訪問"),
        (84, "看護師", 3, "看護"), (85, "管理栄養士", 2, "栄養指導"),
        (86, "薬剤師", 1, "服薬指導"), (87, "医師", 7, "診察"),
        (88, "看護師", 6, "訪問"), (89, "介護福祉士", 4, "介護"),
    ]
    expected = []
    messages = []
    for mid, role, day, action in plans:
        body = f"{role}が2026年11月{day}日に本人を{action}する予定です。"
        messages.append(_message(mid, body=body))
        expected.append((f"2026-11-{day:02d}", role, mid, body))
    cancel = "薬剤師が2026年10月19日に本人への服薬指導を中止しました。"
    done = "看護師が2026年10月19日に本人を訪問しました。"
    uncertain = "先ほどの記載は誤りです。看護師が2026年11月3日に本人を訪問する予定です。"
    messages.extend([_message(90, body=cancel), _message(91, body=done),
                     _message(92, body=uncertain), _message(93, body="本文不完全")])
    ctx.db.save_messages(messages, project_id=1)
    ctx.db.ensure_patient(2)
    foreign = "医師が2026年11月1日に本人を訪問する予定です。"
    ctx.db.save_messages([_message(900, pid=2, body=foreign)], project_id=2)
    posted = datetime.fromisoformat("2026-10-20T09:00:00+09:00").timestamp()
    ctx.db.db.execute("UPDATE messages SET posted_at_ts=? WHERE project_id=1 AND message_id BETWEEN 80 AND 93", (posted,))
    ctx.db.db.execute("UPDATE messages SET body_state='snippet' WHERE message_id=93")
    ctx.db.db.execute("UPDATE patients SET fetch_state='incomplete' WHERE project_id=1")
    now = datetime.fromisoformat("2026-10-20T12:00:00+09:00").timestamp()
    first = notify_views.patient_context_list(ctx.db.db, 1, "previsit", cfg={}, since=T0, now=now)
    rows = list(first["items"])
    for page in range(1, first["pages"]):
        view = notify_views.patient_context_list(ctx.db.db, 1, "previsit", cfg={}, since=T0, now=now, page=page)
        rows.extend(view["items"])
    assert first["pages"] > 1
    assert all(row["project_id"] == 1 for row in rows)
    future = [quote for row in rows if row["group"].startswith("今後の介入予定の記載（変更未照合）")
              for quote in row["quotes"]]
    assert future == [body for _, _, _, body in sorted(expected, key=lambda row: (row[0], row[1], -row[2]))]
    rendered = "\n".join(row["text"] for row in rows)
    assert cancel in rendered and done in rendered
    assert "介入に関する記載あり（内容未確認）" in rendered
    assert uncertain not in rendered and foreign not in rendered
    assert "介入予定の記載: 日付順（同日は職種順、同じ職種は投稿番号の降順）" in first["head"]
    assert any("別投稿による変更は未照合" in line for line in first["head"])
    assert any("本文を確認できない投稿" in line for line in first["head"])
    assert any("履歴の取得完了は未確認" in line for line in first["head"])
