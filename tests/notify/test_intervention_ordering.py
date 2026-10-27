"""Written intervention plans keep their scope and ordering across pages."""
from datetime import datetime

import notify_views
import pytest
from semantic_testkit import _message
from test_patient_context_v3_views import T0, ctx  # noqa: F401

__all__ = ["ctx"]


def test_unresolved_intervention_date_keeps_written_word_and_patient_scope(ctx):
    ctx.db.save_messages([_message(2001, body="看護師が明日本人を訪問する予定です。"),
                          _message(2002, body="薬剤師が11月3日に本人を服薬指導する予定です。")], project_id=1)
    ctx.db.ensure_patient(2)
    ctx.db.save_messages([_message(2003, pid=2, body="医師が本日本人を往診する予定です。")], project_id=2)
    result = notify_views.patient_context_list(ctx.db.db, 1, "previsit", cfg={}, since=T0, now=T0)
    items = list(result["items"])
    for page in range(1, result["pages"]):
        items.extend(notify_views.patient_context_list(ctx.db.db, 1, "previsit", cfg={}, since=T0, now=T0,
                                                       page=page)["items"])
    rows = [row for row in items if row["group"].startswith("介入予定の記載（日付未確認・変更未照合）")]
    assert {row["source_rest"] for row in rows} == {"#2001", "#2002"}
    assert all(row["project_id"] == 1 for row in rows)
    assert {quote for row in rows for quote in row["quotes"]} == {
        "看護師が明日本人を訪問する予定です。", "薬剤師が11月3日に本人を服薬指導する予定です。"}
    assert not any("#2001" in row["source_rest"] or "#2002" in row["source_rest"]
                   for row in items if row["group"].startswith("今後の介入予定の記載（変更未照合）"))


@pytest.mark.parametrize("fetch_state", ["complete", "incomplete", "pending"])
def test_empty_patient_history_remains_unconfirmed_without_completion_proof(ctx, fetch_state):
    ctx.db.ensure_patient(2)
    ctx.db.db.execute("UPDATE patients SET fetch_state=? WHERE project_id=2", (fetch_state,))
    result = notify_views.patient_context_list(ctx.db.db, 2, "previsit", cfg={}, since=T0, now=T0)
    assert "介入の記載: 投稿番号の降順で0件を対象に走査。定型の記載のみ対象、別投稿による変更は未照合" in result["head"]
    assert "介入の記載: 履歴の取得完了は未確認" in result["head"]
    assert all(row["project_id"] == 2 for row in result["items"])


@pytest.mark.parametrize("fetch_state,source_complete,warning", [
    ("complete", True, False), ("complete", None, True),
    ("complete", False, True), ("incomplete", True, True),
    ("unknown", None, True),
])
def test_history_warning_requires_both_fetch_and_source_completion(ctx, monkeypatch,
                                                                   fetch_state, source_complete, warning):
    import intervention_schedule

    ctx.db.ensure_patient(2)
    def source_schedule(_db, project_id):
        assert project_id == 2
        return {"project_id": project_id, "records": [], "coverage": {
            "state": "observed", "messages_examined": 0, "fetch_state": fetch_state,
            "source_complete": source_complete, "incomplete": 0, "deleted": 0, "invalid_source": 0,
            "oversize": 0, "truncated": False, "records_truncated": False}}
    monkeypatch.setattr(intervention_schedule, "read_source_schedule", source_schedule)
    result = notify_views.patient_context_list(ctx.db.db, 2, "previsit", cfg={}, since=T0, now=T0)
    assert ("介入の記載: 履歴の取得完了は未確認" in result["head"]) is warning


def test_scan_limit_does_not_hide_unconfirmed_history_or_cross_patient(ctx):
    ctx.db.ensure_patient(2)
    ctx.db.db.execute("UPDATE patients SET fetch_state='complete' WHERE project_id=1")
    ctx.db.save_messages([_message(mid, body="合成の一般連絡")
                          for mid in range(1000, 1201)], project_id=1)
    ctx.db.save_messages([_message(9999, pid=2, body="医師が2026年11月1日に本人を往診する予定です。")],
                         project_id=2)
    result = notify_views.patient_context_list(ctx.db.db, 1, "previsit", cfg={}, since=T0, now=T0)
    assert "介入の記載: 履歴の取得完了は未確認" in result["head"]
    assert "介入の記載: 表示対象を一部に限定" in result["head"]
    assert all(row["project_id"] == 1 for row in result["items"])
    assert all("往診する予定" not in row["text"] for row in result["items"])


def test_deleted_and_incomplete_posts_are_counted_separately_within_patient_scope(ctx):
    ctx.db.ensure_patient(2)
    ctx.db.save_messages([_message(110, pid=2, body="削除された合成投稿"),
                          _message(111, pid=2, body="未取得の合成投稿"),
                          _message(112, pid=2, body="合成の一般連絡")], project_id=2)
    ctx.db.ensure_patient(3)
    ctx.db.save_messages([_message(900, pid=3, body="別患者の削除済み投稿")], project_id=3)
    ctx.db.db.execute("UPDATE messages SET body_state='deleted' WHERE message_id IN (110, 900)")
    ctx.db.db.execute("UPDATE messages SET body_state='snippet' WHERE message_id=111")
    ctx.db.db.execute("UPDATE patients SET fetch_state='complete' WHERE project_id=2")

    result = notify_views.patient_context_list(ctx.db.db, 2, "previsit", cfg={}, since=T0, now=T0)
    assert "介入の記載: 投稿番号の降順で3件を対象に走査。定型の記載のみ対象、別投稿による変更は未照合" in result["head"]
    assert "介入の記載: 本文を確認できない投稿 1件" in result["head"]
    assert "介入の記載: 削除済みの投稿 1件" in result["head"]
    assert "介入の記載: 履歴の取得完了は未確認" in result["head"]
    assert all(row["project_id"] == 2 for row in result["items"])


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


def test_cancel_notice_is_a_scoped_unconfirmed_cue_across_pages(ctx):
    plans = [
        "看護師が2026年11月3日に本人を訪問する予定です。",
        "看護師が2026年11月3日に本人を看護する予定です。",
        "看護師が2026年11月4日に本人を訪問する予定です。",
        "医師が2026年11月3日に本人を往診する予定です。",
        "薬剤師が2026年11月3日に本人を服薬指導する予定です。",
    ] + [f"介護職員が2026年11月{day}日に本人を介護する予定です。" for day in range(5, 12)]
    cancellations = [
        "看護師が2026年11月3日の本人への訪問を中止しました。",
        "看護師が2026年11月3日に本人を訪問する予定を取消しました。",
        "医師が2026年11月4日の本人への往診を中止しました。",
        "薬剤師が2026年10月19日に本人への服薬指導を中止しました。",
    ]
    unknown = "予定を確認します。看護師が2026年11月3日に本人を訪問する予定です。"
    messages = [_message(100 + index, body=body) for index, body in enumerate(plans + cancellations + [unknown])]
    ctx.db.save_messages(messages, project_id=1)
    ctx.db.ensure_patient(2)
    foreign = "薬剤師が2026年11月3日の本人への服薬指導を中止しました。"
    ctx.db.save_messages([_message(900, pid=2, body=foreign)], project_id=2)
    posted = datetime.fromisoformat("2026-10-20T09:00:00+09:00").timestamp()
    ctx.db.db.execute("UPDATE messages SET posted_at_ts=? WHERE project_id=1 AND message_id BETWEEN 100 AND 200", (posted,))
    ctx.db.db.execute("UPDATE patients SET fetch_state='incomplete' WHERE project_id=1")
    now = datetime.fromisoformat("2026-10-20T12:00:00+09:00").timestamp()
    first = notify_views.patient_context_list(ctx.db.db, 1, "previsit", cfg={}, since=T0, now=now)
    rows = list(first["items"])
    for page in range(1, first["pages"]):
        view = notify_views.patient_context_list(ctx.db.db, 1, "previsit", cfg={}, since=T0, now=now, page=page)
        rows.extend(view["items"])
    assert first["pages"] > 1 and all(row["project_id"] == 1 for row in rows)
    future = {row["quotes"][0]: row for row in rows
              if row["group"].startswith("今後の介入予定の記載（変更未照合）") and row["quotes"]}
    assert set(future) == set(plans)
    cue = "同じ職種・同じ日の取消記録あり（対応未確認）"
    assert all(future[body]["quotes"].count(cue) == 1 for body in plans[:2])
    assert all(cue not in future[body]["quotes"] for body in plans[2:])
    rendered = "\n".join(row["text"] for row in rows)
    assert all(body in rendered for body in cancellations)
    assert "介入に関する記載あり（内容未確認）" in rendered
    assert unknown not in rendered and foreign not in rendered
    assert any("履歴の取得完了は未確認" in line for line in first["head"])
    assert all("取消済み" not in row["text"] for row in future.values())
