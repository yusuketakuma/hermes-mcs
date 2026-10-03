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
    "action_retired": "保留ボタンは廃止されました。カードを最新の表示に更新します。",
    "extraction_changed": "抽出結果が更新されたため報告できません。最新のカードでやり直してください。",
}


def ja(result: dict | None) -> str:
    if not result:
        return "結果を取得できませんでした（処理中の可能性があります）。"
    err = result.get("error")
    if result.get("outcome") == "applied" or result.get("applied"):
        return "反映しました。"
    return ERR_JA.get(str(err), f"拒否されました: {err}")


BODY_CHUNK = 1900          # under the 2000-char message ceiling
BODY_MAX_CHUNKS = 4        # ephemeral replies only — durable thread
                           # delivery passes max_chunks=None


def split_body(text: str, limit: int = BODY_CHUNK,
               max_chunks: int | None = BODY_MAX_CHUNKS) -> list:
    """Split a full-text answer on line boundaries into <=limit chunks,
    hard-wrapping overlong lines.

    ``max_chunks`` bounds ephemeral interaction replies (a huge body
    stays a few messages). Durable part delivery passes ``None`` — the
    render must plan every chunk, never silently discard a tail."""
    if limit <= 0:
        raise ValueError("chunk_limit_must_be_positive")
    capped = max_chunks is not None
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
                if capped and len(out) == max_chunks:
                    return out
            out.append(text[start:start + limit])
            if capped and len(out) == max_chunks:
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
            if capped and len(out) == max_chunks:
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


NO_TASKS_TEXT = "このスレッドのタスクはありません。"


def valid_due(due: str) -> bool:
    """A due date must be a real calendar day spelled YYYY-MM-DD —
    the round trip rejects every other ISO spelling."""
    from datetime import date
    try:
        return date.fromisoformat(due).isoformat() == due
    except ValueError:
        return False


# ⚠ report target parts — values match mcs_operations
# EXTRACT_FEEDBACK_FIELDS
FEEDBACK_FIELDS = (("summary", "要約"), ("meds", "薬"), ("symptoms", "症状"),
                   ("requests", "依頼"), ("vitals", "バイタル"),
                   ("other", "その他"))
# 🚫 reason codes — values match mcs_operations DISMISS_REASON_CODES
DISMISS_REASONS = (("false_positive", "誤検知"), ("already_handled", "対応済み"),
                   ("duplicate", "重複"), ("out_of_scope", "対象外"),
                   ("other", "その他"))
TASK_REASON = "通知カードからタスク作成"
STAFF_OPTIONS = 25
MODAL_TITLES = {"request": "タスク作成", "dismiss": "候補を却下",
                "report": "抽出の誤りを報告", "search": "この患者を検索",
                "digest": "サマリー（絞込み）"}
# card actions whose click opens a modal (text.modal_fields) instead of
# answering directly
MODAL_ACTIONS = tuple(MODAL_TITLES)


def modal_fields(action: str, form: dict | None = None,
                 clicker: str = "") -> list:
    """Transport-neutral modal definition — each transport renders these
    as Discord inputs/selects or Slack input blocks. A field with
    ``options`` [(value, label)] is a single select; others are text.

    📝 assignee: the runner-sent roster (``form["staff"]``) becomes a
    select plus a free-text 'other' field; without a roster the free
    text defaults to the clicker's display name. The roster source is
    the runner's assignee_choices() — swap it there, not here."""
    form = form or {}
    if action == "request":
        out = [{"id": "task", "label": "タスク内容", "required": True,
                "multiline": True, "max": 1000,
                "default": str(form.get("hint") or "")[:1000]}]
        staff = [s for s in form.get("staff") or []
                 if isinstance(s, str) and 0 < len(s) <= 75][:STAFF_OPTIONS]
        if staff:
            mine = next((s for s in staff if clicker and (
                s == clicker or s.startswith(clicker + "（"))), None)
            out.append({"id": "assignee_pick", "label": "担当者（一覧から）",
                        "required": False,
                        "options": [(s, s) for s in staff],
                        "default": mine})
            out.append({"id": "assignee", "label": "担当者（その他・手入力）",
                        "required": False, "max": 120, "default": ""})
        else:
            out.append({"id": "assignee", "label": "担当者（任意）",
                        "required": False, "max": 120,
                        "default": clicker[:120]})
        out.append({"id": "due_date", "label": "期限 YYYY-MM-DD（任意）",
                    "required": False, "max": 10, "default": ""})
        # the human gate records why — empty falls back to TASK_REASON
        out.append({"id": "reason", "label": "理由（任意）",
                    "required": False, "multiline": True, "max": 2000,
                    "default": ""})
        return out
    if action == "report":
        return [{"id": "field", "label": "誤っている箇所", "required": True,
                 "options": list(FEEDBACK_FIELDS), "default": None},
                {"id": "note", "label": "メモ（任意）", "required": False,
                 "multiline": True, "max": 500, "default": ""}]
    if action == "dismiss":
        return [{"id": "reason_code", "label": "却下理由", "required": True,
                 "options": list(DISMISS_REASONS), "default": None},
                {"id": "note", "label": "メモ（任意）", "required": False,
                 "multiline": True, "max": 2000, "default": ""}]
    if action == "search":
        return [{"id": "query", "label": "キーワード（空白区切りで AND）",
                 "required": True, "max": 100, "default": ""}]
    if action == "digest":
        return [{"id": "query", "required": False, "max": 100,
                 "label": "絞込み（all / mine / station:名前 / days:1-7）",
                 "default": "all"},
                {"id": "name", "label": "担当の名前（mine用）",
                 "required": False, "max": 120, "default": clicker[:120]}]
    return []


def digest_inputs(fields: dict, clicker: str = "") -> dict:
    """📊 modal values -> view input: the scope text (blank = all) and
    the name ``mine`` matches (blank = the clicker's display name)."""
    name = " ".join((fields.get("name") or "").split())[:120] or clicker[:120]
    out = {"query": " ".join((fields.get("query") or "").split())[:100]
           or "all"}
    if name:
        out["name"] = name
    return out


def task_attrs(fields: dict):
    """📝 modal values -> request.create attrs, or an error string.
    A typed assignee wins over the list pick. The old 依頼 form's
    title/reason keys still validate (a modal open across a restart)."""
    title = (fields.get("task") or fields.get("title") or "").strip()
    if not title:
        return "タスク内容の入力が必要です。"
    if len(title) > 1000:
        return "タスク内容は1000文字以内で入力してください。"
    assignee = ((fields.get("assignee") or "").strip()
                or (fields.get("assignee_pick") or "").strip())
    if len(assignee) > 120:
        return "担当者は120文字以内で入力してください。"
    due = (fields.get("due_date") or "").strip()
    if due and not valid_due(due):
        return "期限は YYYY-MM-DD 形式で入力してください。"
    reason = (fields.get("reason") or "").strip() or TASK_REASON
    if len(reason) > 2000:
        return "理由は2000文字以内で入力してください。"
    attrs = {"title": title, "reason": reason}
    if assignee:
        attrs["assignee"] = assignee
    if due:
        attrs["due_date"] = due
    return attrs


def feedback_attrs(fields: dict):
    """⚠ modal values -> (field, reason) or an error string."""
    field = (fields.get("field") or "").strip()
    labels = dict(FEEDBACK_FIELDS)
    if field not in labels:
        return "誤っている箇所を選択してください。"
    note = (fields.get("note") or "").strip()
    if len(note) > 500:
        return "メモは500文字以内で入力してください。"
    return field, note or f"抽出の誤り報告（{labels[field]}）"


def dismiss_attrs(fields: dict):
    """🚫 modal values -> (reason, reason_code) or an error string. A
    modal opened before the upgrade still submits the old free-text
    ``reason`` alone — accepted without a code."""
    labels = dict(DISMISS_REASONS)
    code = (fields.get("reason_code") or "").strip()
    note = (fields.get("note") or fields.get("reason") or "").strip()
    if len(note) > 2000:
        return "理由は2000文字以内で入力してください。"
    if code:
        if code not in labels:
            return "却下理由を選択してください。"
        return note or labels[code], code
    if "reason_code" in fields:
        return "却下理由を選択してください。"
    return (note, None) if note else "理由の入力が必要です。"


SEARCH_EMPTY = "キーワードを入力してください。"


def search_query(fields: dict) -> str | None:
    """🔎 modal value -> normalized keyword, or None when blank."""
    return " ".join((fields.get("query") or "").split())[:100] or None


LIST_SHOW = 15             # ephemeral rows before 「他N件」


def list_messages(result: dict, allowed, markdown: bool = True) -> list:
    """A runner list view (📋 / 🗂 / 🔎) as ephemeral messages. Items of
    projects outside this deployment's scope (``allowed(pid)`` false)
    are dropped before counting; the rest is capped with 「他N件」 (the
    runner's own overflow ``more`` is counted in, unfiltered). The title
    is bold in Discord markdown, or in Slack mrkdwn (``*``) when
    ``markdown`` is false."""
    view = result.get("list") or {}
    items = [i for i in view.get("items") or []
             if isinstance(i, dict) and allowed(i.get("project_id"))]
    bold = "**" if markdown else "*"
    lines = [f"{bold}{view.get('title') or '一覧'}{bold}"]
    lines += [str(x) for x in view.get("head") or []]
    group = None
    for i in items[:LIST_SHOW]:
        if i.get("group") and i["group"] != group:
            group = i["group"]
            lines.append(f"■ {group}")
        lines.append(str(i.get("text") or ""))
    if not items:
        lines.append(str(view.get("empty") or "該当なし"))
    rest = len(items) - min(len(items), LIST_SHOW) + int(view.get("more") or 0)
    if rest > 0:
        lines.append(f"他{rest}件")
    lines += [str(x) for x in view.get("notes") or []]
    return split_body("\n".join(lines))


def view_answer(result: dict | None, allowed,
                markdown: bool = True) -> list | None:
    """An applied view click's ephemeral answer as ``[(text, tasks)]``
    — ``tasks`` is the task list whose transition buttons ride that
    message (the caller registers ``result["token_ctx"]`` first), else
    None. Returns None when the result is no view answer (a write
    click, a rejection, a modal open): each transport then answers
    with ``ja(result)`` or stays silent. Shared by the live interaction
    path and the delayed followup sweep of every transport."""
    if not result or result.get("outcome") != "applied" \
            or result.get("modal"):
        return None
    action = result.get("action")
    if action == "digest" and isinstance(result.get("text"), str):
        # rendered by the runner from the shared display model in this
        # card's transport dialect (notify_render.parts_text)
        return [(m, None) for m in split_body(result["text"])]
    if action in ("body", "summary") and result.get("body"):
        return [(m, None) for m in body_messages(result)]
    if action == "tasks":
        items = result.get("tasks") or []
        return ([(task_list_text(items), items)] if items
                else [(NO_TASKS_TEXT, None)])
    if action == "list":
        return [(m, None) for m in list_messages(result, allowed, markdown)]
    if action == "task_status":
        return [(task_done_text(result), None)]
    return None


def preview_text(action: str, payload: dict, markdown: bool) -> str:
    """The human-confirm preview of a task/dismiss/report payload —
    Discord markdown (bold heading, code-quoted signal) or Slack plain
    text."""
    bold = "**" if markdown else ""
    if action == "dismiss":
        key = payload["signal_key"]
        code = dict(DISMISS_REASONS).get(payload.get("reason_code"))
        return (f"{bold}確認 — 候補の却下{bold}\n"
                f"signal: {f'`{key}`' if markdown else key}\n"
                + (f"区分: {code}\n" if code else "")
                + f"理由: {payload['reason'][:400]}")
    if action == "report":
        return (f"{bold}確認 — 抽出の誤り報告{bold}\n"
                f"箇所: {dict(FEEDBACK_FIELDS).get(payload['field'], '?')}\n"
                f"メモ: {payload['reason'][:400]}\n"
                "確定すると、この投稿の構造化抽出を1回だけ再実行します。")
    out = (f"{bold}確認 — タスク作成{bold}\n"
           f"内容: {payload['title'][:200]}")
    if payload.get("assignee"):
        out += f"\n担当: {payload['assignee'][:120]}"
    if payload.get("due_date"):
        out += f"\n期限: {payload['due_date']}"
    return out + f"\n理由: {payload['reason'][:400]}"


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
