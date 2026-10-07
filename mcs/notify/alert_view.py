"""再確認候補の対象・根拠・時点を短い通知に表示する。"""
from datetime import datetime
import math

from mcs_adapter import project_url
from mcs_queries import JST
from mcs_requests import positive
import notify_render


def _when(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value < 253402214400:
        return "不明"
    return datetime.fromtimestamp(value, JST).strftime("%m-%d %H:%M")


def render_parts(checked, db=None):
    """検証済み候補を、見出し → 対象 → 理由の引用 → 発信者・時点 → 注記の順で表示する。

    患者名と投稿日時は1回だけ出し、理由（原文引用）を発信者より先に置く。"""
    pid, mid = checked.get("project_id"), checked.get("message_id")
    message = None
    if db is not None and positive(pid) and positive(mid):
        message = db.execute(
            "SELECT body_text,body_state,posted_at_ts,sender_name,organization,posted_at FROM messages "
            "WHERE project_id=? AND message_id=?", (pid, mid)).fetchone()
    subject = "患者本人" if checked.get("subject") == "patient" else "対象人物は未確認"
    name = notify_render.patient_heading(db, pid) if db is not None and positive(pid) else "患者記録"
    parts = {"containers": [
        {"type": "heading", "text": "🚨 緊急度高・再確認候補"},
        {"type": "text", "text": f"{name} · {subject}"}], "footer": []}
    reasons = checked.get("reasons")
    seen = set()
    if message is not None and message["body_state"] == "full" and isinstance(reasons, list):
        body = message["body_text"] or ""
        for quote in reasons:
            if (not isinstance(quote, str) or not quote.strip() or len(quote) > 180
                    or body.count(quote) != 1 or quote in seen):
                continue
            seen.add(quote)
            parts["containers"].append({"type": "quote", "text": quote})
            if len(seen) == 2:
                break
    if not seen:
        parts["containers"].append({"type": "text", "text": "緊急理由の引用は未取得"})
    sender = (notify_render._inline(message["sender_name"], 24) if message else "") or "発信者未取得"
    organization = (notify_render._inline(message["organization"], 24) if message else "") or "所属未取得"
    # the same stored post time the preview header shows
    posted = (f"{notify_render._mmdd(message['posted_at'])} {notify_render._hhmm(message['posted_at'])}"
              if message is not None and message["posted_at"] else "不明")
    parts["containers"].append({"type": "text", "text":
        f"{sender}（{organization}） · 投稿 {posted} · 判定 {_when(checked.get('observed_at'))}"})
    if message is not None and message["body_state"] == "full" and notify_render._structured_block(db, mid) is None:
        pending = "要約作成失敗" if notify_render._extraction_failed(db, mid) else "要約処理待ち"
        parts["containers"].append({"type": "text", "text": pending})
    if positive(pid):
        parts["footer"].append({"type": "text", "text":
            f"原本確認: MCSで開く {project_url(pid)} · 投稿 #{mid}"})
    header = notify_render._preview_header(db, pid, message) if db is not None and positive(pid) else "患者記録 / 発信者未取得（所属未取得） / 日時未取得"
    main = next((part["text"] for part in parts["containers"] if part["type"] == "quote"), "緊急理由の引用は未取得")
    # the push/notification line: urgency first, so a lock screen tells
    # it apart from a routine request
    parts["preview_text"] = "🚨緊急度高 " + notify_render._preview_line(
        header, f"再確認候補（{subject}）: {main}", limit=393)
    return parts


def render_text(checked, db=None):
    """同じ表示モデルから再確認候補のテキスト通知を作る。"""
    return notify_render.parts_text(render_parts(checked, db))
