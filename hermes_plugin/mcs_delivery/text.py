"""Card-interaction text shared by every transport — JA result
phrasing, bounded body chunking, and task-list rendering.

Nothing here touches a messaging SDK; each transport's actions layer
hands these results to its own ephemeral-reply path.
"""
from __future__ import annotations

ERR_JA = {
    "unknown_token": "この操作は無効化されました（カードが更新された可能性があります）。",
    "token_expired": "操作の有効期限が切れています。最新のカードでやり直してください。",
    "stale_ui": "カードが更新されました。最新の表示で操作してください。",
    "stale_source": "原資料が更新されました。最新の表示で操作してください。",
    "card_revoked": "このカードは取り下げ済みです。",
    "card_not_found": "対象カードが見つかりません。",
    "manifest_invalid": "表示内容が変わったため確定できません。最新の表示で操作してください。",
    "scope_mismatch": "この環境のカードではありません。",
    "stale_task": "タスクが更新されました。最新の一覧でやり直してください。",
    "request_not_found": "対象のタスクが見つかりません。",
    "request_not_open": "このタスクは既に終了しています。",
    "interactive_off": "現在インタラクティブ通知は停止中です。",
    "signal_changed": "対象の候補が更新されました。最新のカードでやり直してください。",
    "source_changed": "元の投稿が更新されました。最新のカードでやり直してください。",
    "source_missing": "対象の投稿が見つかりません。",
    "source_incomplete": "対象の投稿データが不完全です。",
    "command_id_conflict": "同じIDで内容の異なる要求が検出されました。",
    "bad_page": "そのページは存在しません。",
    "reason_required": "理由の入力が必要です。",
}


def ja(result: dict | None) -> str:
    if not result:
        return "結果を取得できませんでした（処理中の可能性があります）。"
    err = result.get("error")
    if result.get("outcome") == "applied" or result.get("applied"):
        return "反映しました。"
    return ERR_JA.get(str(err), f"拒否されました: {err}")


BODY_CHUNK = 1900          # under the 2000-char message ceiling
BODY_MAX_CHUNKS = 4        # runner caps ~6k chars; never spam a channel


def split_body(text: str, limit: int = BODY_CHUNK) -> list:
    """Split a full-text answer on line boundaries into <=limit chunks,
    hard-wrapping overlong lines. Bounded so a huge body stays a few
    ephemeral messages, never a flood."""
    if limit <= 0:
        raise ValueError("chunk_limit_must_be_positive")
    out, cur = [], ""
    start = 0
    while start <= len(text):
        # Look only as far as one chunk. Long lines and discarded tails
        # must not be copied or split after the output budget is filled.
        end = text.find("\n", start, start + limit + 1)
        if end < 0 and len(text) - start > limit:
            if cur:
                out.append(cur)
                cur = ""
                if len(out) == BODY_MAX_CHUNKS:
                    return out
            out.append(text[start:start + limit])
            if len(out) == BODY_MAX_CHUNKS:
                return out
            start += limit
            continue
        if end < 0:
            end = len(text)
        line = text[start:end]
        start = end + 1
        cand = (cur + "\n" + line) if cur else line
        if len(cand) > limit:
            out.append(cur)
            if len(out) == BODY_MAX_CHUNKS:
                return out
            cur = line
        else:
            cur = cand
    if cur or not out:
        out.append(cur)
    return out



def body_messages(result: dict) -> list:
    """title + chunked body as sendable messages — shared by the live
    interaction path and the delayed followup sweep."""
    body = str(result.get("body") or "")
    title = str(result.get("title") or "本文")
    # The heading shares the 2000-character message budget with each chunk.
    # Titles are source-derived and may be arbitrarily long.
    if len(title) > 80:
        title = title[:79] + "…"
    chunks = split_body(body)
    return [f"**{title}**（{i + 1}/{len(chunks)}）\n{c}"
            if len(chunks) > 1 else f"**{title}**\n{c}"
            for i, c in enumerate(chunks)]


def task_list_text(items: list) -> str:
    """Ephemeral task list — one line per request, status mark first so
    the scan order matches the transition buttons below it."""
    marks = {"open": "⬜", "in_progress": "⏳", "done": "✅"}
    lines = ["📋 **タスク**（このスレッド）"]
    for t in items:
        meta = []
        if t.get("assignee"):
            meta.append(f"担当: {t['assignee']}")
        if t.get("due_date"):
            meta.append(f"期限: {t['due_date']}")
        lines.append(f"{marks.get(t['status'], '⬜')} "
                     f"#{t['request_id']} {t['title']}"
                     + (" — " + "・".join(meta) if meta else ""))
    return "\n".join(lines)


def task_done_text(result: dict) -> str:
    status = "完了" if result.get("status") == "done" else "対応中"
    title = result.get("title") or f"#{result.get('request_id')}"
    if result.get("absorbed"):
        return f"タスク「{title}」は既に「{status}」です。"
    return f"タスク「{title}」を「{status}」にしました。"
