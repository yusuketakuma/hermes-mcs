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
    """検証済み候補を対象・一意な原文引用・観測時刻の区画へ変換する。"""
    pid, mid = checked.get("project_id"), checked.get("message_id")
    message = None
    if db is not None and positive(pid) and positive(mid):
        message = db.execute(
            "SELECT body_text,body_state,posted_at_ts,sender_name,organization,posted_at FROM messages "
            "WHERE project_id=? AND message_id=?", (pid, mid)).fetchone()
    subject = "患者本人" if checked.get("subject") == "patient" else "対象人物は未確認"
    name = notify_render.patient_heading(db, pid) if db is not None and positive(pid) else "患者記録"
    parts = {"containers": [
        {"type": "heading", "text": "⚠ 緊急度高・再確認候補（AI判定）"},
        {"type": "text", "text": f"{name} · {subject}"}], "footer": []}
    header = notify_render._preview_header(db, pid, message) if db is not None and positive(pid) else "患者記録 / 発信者未取得（所属未取得） / 日時未取得"
    parts["containers"].append({"type": "text", "text": header})
    if message is not None and message["body_state"] == "full" and notify_render._structured_block(db, mid) is None:
        pending = "要約作成失敗" if notify_render._extraction_failed(db, mid) else "要約処理待ち"
        parts["containers"].append({"type": "text", "text": pending})
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
        parts["containers"].append({"type": "text", "text": "緊急理由の引用は未取得。原本で確認してください。"})
    posted = _when(message["posted_at_ts"]) if message is not None else "不明"
    parts["containers"].append({"type": "text", "text":
        f"投稿日 {posted} · AI判定観測 {_when(checked.get('observed_at'))}"})
    parts["footer"].append({"type": "text", "text":
        "通知の確認・依頼登録等の記録が未確認です。未対応・業務完了の判定ではありません。"})
    if positive(pid):
        parts["footer"].append({"type": "text", "text":
            f"原本確認: MCSで開く {project_url(pid)} · 投稿 #{mid}"})
    main = next((part["text"] for part in parts["containers"] if part["type"] == "quote"), "緊急理由の引用は未取得")
    parts["preview_text"] = notify_render._preview_line(header, f"再確認候補（AI判定・{subject}）: {main}", limit=400)
    return parts


def render_text(checked, db=None):
    """同じ表示モデルから再確認候補のテキスト通知を作る。"""
    return notify_render.parts_text(render_parts(checked, db))
