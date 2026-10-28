"""Card-interaction text shared by every transport — JA result
phrasing, bounded body chunking, and task-list rendering.

Nothing here touches a messaging SDK; each transport's actions layer
hands these results to its own ephemeral-reply path.
"""
from __future__ import annotations

import re

from . import forms

# kept verbatim: runner footer mentions (pings stay off via
# allowed_mentions) and URLs (a substitution would break the link)
_MD_KEEP = re.compile(r"(<@[!&]?\w+>|https?://\S+)")
# same-length look-alikes, so escaping never moves a text budget
_MD_INLINE = str.maketrans("*_~|`[]<\\", "＊＿～｜｀［］＜＼")
_MD_LINE = re.compile(r"^([ \t]*)(?:(\d+)\.|([#>+-]))", re.M)
_MD_LEAD = str.maketrans("#>+-", "＃＞＋－")


def escape_md(text: str) -> str:
    """User-authored text as literal Discord text with the same length:
    inline markup (``* _ ~ | ` [ ] < \\`` — no masked link, no tag)
    and the block markers at a line start (``#``/``-#`` headings, ``>``
    quote, ``-``/``+``/``1.`` lists) become fullwidth look-alikes."""
    text = "".join(part if i % 2 else part.translate(_MD_INLINE)
                   for i, part in enumerate(_MD_KEEP.split(text)))
    return _MD_LINE.sub(
        lambda m: m.group(1) + (f"{m.group(2)}．" if m.group(2)
                                else m.group(3).translate(_MD_LEAD)), text)


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
    "action_not_applicable": "この投稿では利用できない操作です。最新のカードでやり直してください。",
    "intervention_cursor_changed": "介入の履歴が変わったため、最新のカードから開き直してください。",
    "actor_mismatch": "この表示は操作した本人だけが使えます。元のカードから開き直してください。",
    "origin_mismatch": "このカードのボタンではありません。最新のカードから操作してください。",
    "card_missing": "対象カードが見つかりません。",
    "signal_not_open": "この候補はすでに閉じられています。最新のカードを確認してください。",
    "signal_not_found": "対象の候補が見つかりません。",
    "signal_corrupt": "候補の記録を読み取れないため処理できません。管理者に連絡してください。",
    "project_mismatch": "対象の患者が見つかりません。最新のカードでやり直してください。",
    "project_not_found": "対象の患者が見つかりません。最新のカードでやり直してください。",
    "payload_changed": "抽出結果が更新されたため報告できません。最新のカードでやり直してください。",
    "unsupported_inference_version": "この解析記録の版では再試行できません。管理者に連絡してください。",
    "semantic_intake_held": "解析対象を保留しています。記録を削除せず、管理者に連絡してください。",
    "stamp_not_allowed": "MCSスタンプは設定された担当者だけが操作できます。",
    "stamp_target_changed": "対象の投稿かスタンプの状態が変わりました。最新のカードからやり直してください。",
    "stamp_target_invalid": "このカードにはスタンプを付けられる投稿がありません。",
    "stamp_result_unknown": "この投稿には結果を確認中のスタンプ操作があります。確認が終わるまでお待ちください。",
    "stamp_witness_capacity": "この投稿のスタンプ操作の記録が上限に達しました。管理者に連絡してください。",
    "stamp_restore_hold": "データの復元を確認中のため、スタンプ操作を一時停止しています。",
    "stamp_witness_invalid": "スタンプ操作の記録を確認できないため停止しました。管理者に連絡してください。",
    "stamp_intent_expired": "確定から時間がたったため送信しませんでした。最新のカードからやり直してください。",
    "stamp_observation_required": "現在のスタンプ状態を確認できないため送信しませんでした。少し待ってからやり直してください。",
    "stamp_type_invalid": "選べないスタンプの種類です。",
    "stamp_operation_invalid": "選んだ操作はこの投稿では実行できません（取消するスタンプがありません）。",
    "stamp_account_mismatch": "MCSのログイン中のアカウントが設定と一致しないため送信しませんでした。",
    "stamp_server_rejected": "MCSがスタンプ操作を受け付けませんでした。MCSの画面で状態を確認してください。",
    "stamp_receipt_invalid": "承認の記録を確認できないため送信しませんでした。",
}
# a dispatch exception may land before or after the commit: say neither
CRASH_TEXT = ("処理中にエラーが発生しました。最新のカードで結果を確認してからやり直してください。"
              "続く場合は管理者に連絡してください。")
UNKNOWN_ERROR_TEXT = "この操作は完了できませんでした。最新のカードからやり直してください。"


RECEIPT_TEXT = "受け付けました。結果は反映後に表示されます。"
# Admission is not completion: only a fresh MCS readback says confirmed.
STAMP_STATE_TEXT = {
    "queued": "MCSスタンプを受け付けました。MCSでの反映を確認中です（自動では再送しません）。",
    "unknown": "MCSスタンプの送信結果を確認中です。自動では再送しません。",
    "confirmed": "MCSスタンプを反映しました（MCSで確認済み）。",
}
STAMP_REJECTED_TEXT = "MCSスタンプを反映できませんでした。最新のカードで状態を確認してください。"


def _applied_text(result: dict) -> str:
    """Say what was applied, so several receipts in one DM stay matchable."""
    rid, action = result.get("request_id"), result.get("action")
    if action == "ack":
        return "確認を取り消しました。" if result.get("withdrawn") else "確認済みにしました。"
    if action == "assign":
        return "担当を外しました。" if result.get("released") else "担当にしました。"
    if action == "stamp_press":
        # The card shows the stamp once MCS confirms it, as MCS's own row does.
        what = "取消" if result.get("stamp_op") == "cancel" else "スタンプ"
        return f"MCSへの{what}を受け付けました。MCSで反映を確認するとカードの表示が変わります（自動では再送しません）。"
    if result.get("stamp_state") is not None or action == "stamp":
        return STAMP_STATE_TEXT.get(result.get("stamp_state"), STAMP_STATE_TEXT["queued"])
    if result.get("field"):
        return "誤りの報告を記録しました。"
    if result.get("signal_key"):
        return "候補を却下しました。"
    if type(rid) is int:     # notification receipts carry a uuid request_id
        return f"タスク #{rid} を{'作成' if result.get('before') is None else '更新'}しました。"
    return "反映しました。"


def ja(result: dict | None) -> str:
    if not result:
        return "結果を取得できませんでした（処理中の可能性があります）。"
    if result.get("stamp_state") == "rejected":
        # The runner's rejection code says why; an unknown code keeps the generic line.
        reason = result.get("stamp_error") or result.get("error")
        if reason in {"source_changed", "stale_source", "stamp_target_changed"}:
            return ja({"outcome": "rejected", "error": reason, "stamp_reselect": True})
        known = str(reason).startswith("stamp_") and str(reason) in ERR_JA
        return STAMP_REJECTED_TEXT + ("\n" + ERR_JA[str(reason)] if known else "")
    if result.get("stamp_state") in STAMP_STATE_TEXT:
        return STAMP_STATE_TEXT[result["stamp_state"]]
    err = result.get("error")
    if result.get("stamp_reselect"):
        return (ERR_JA.get(str(err), STAMP_REJECTED_TEXT)
                + "\n最新のカードで「別の投稿」（メニュー内にある場合があります）を開き、"
                  "対象の投稿とスタンプを選び直してください。前の操作は再送しません。")
    if result.get("outcome") == "applied" or result.get("applied"):
        return _applied_text(result)
    if str(err).startswith("crash:"):
        return CRASH_TEXT
    return ERR_JA.get(str(err), UNKNOWN_ERROR_TEXT)


BODY_CHUNK = 1900          # under the 2000-char message ceiling
BODY_MAX_CHUNKS = 4        # ephemeral replies only — durable thread
                           # delivery passes max_chunks=None


def notification_preview(parts: dict, limit: int = 600) -> str:
    """A sealed content-first preview, or visible text from an older spec."""
    preview = parts.get("preview_text")
    if not isinstance(preview, str) or not preview.strip():
        head, body = [], []
        for item in parts.get("containers") or []:
            kind = item.get("type")
            if kind == "meta":
                continue
            value = (f"{item['name']}: {item['value']}" if kind == "field"
                     else item.get("text") or "")
            (head if kind == "heading" else body).append(value)
        preview = "\n".join(body[:2] + head[:1]) or "MCS 通知"
    preview = " ".join(preview.split())
    return preview if len(preview) <= limit else preview[:limit - 1] + "…"


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



LONG_NOTICE = "\n…長文のためここまで表示しています。"
# only a post body has its full text in MCS; a list or summary has no MCS screen
BODY_LONG_NOTICE = LONG_NOTICE + "続きはMCSで確認してください。"
DIGEST_LONG_NOTICE = LONG_NOTICE + "絞込み（施設・患者・日数）で範囲を狭めてください。"


def _answer_chunks(value: str, notice: str = LONG_NOTICE) -> list:
    chunks = split_body(value, max_chunks=BODY_MAX_CHUNKS + 1)
    if len(chunks) <= BODY_MAX_CHUNKS:
        return chunks
    chunks = chunks[:BODY_MAX_CHUNKS]
    chunks[-1] = chunks[-1][:BODY_CHUNK - len(notice)] + notice
    return chunks


def _literal(value: str, plain: bool) -> str:
    """Source text in the Discord dialect (``plain`` false) as literal
    text — no masked link, heading or spoiler. ``escape_md`` keeps the
    length, so no chunk budget moves."""
    return value if plain else escape_md(value)


def body_messages(result: dict, *, plain: bool = False) -> list:
    """title + chunked body as sendable messages — shared by the live
    interaction path and the delayed followup sweep."""
    body = str(result.get("body") or "")
    title = str(result.get("title") or "本文")
    # The heading shares the 2000-character message budget with each chunk.
    # Titles are source-derived and may be arbitrarily long.
    if len(title) > 80:
        title = title[:79] + "…"
    # escaped per chunk: a chunk is its own message, so a split line's
    # remainder starts a line there
    chunks = [_literal(c, plain) for c in _answer_chunks(body, BODY_LONG_NOTICE)]
    heading = title if plain else f"**{_literal(title, plain)}**"
    return [f"{heading}（{i + 1}/{len(chunks)}）\n{c}"
            if len(chunks) > 1 else f"{heading}\n{c}"
            for i, c in enumerate(chunks)]


NO_TASKS_TEXT = "表示対象のタスクはありません。"


STAMP_FORM_PENDING = "スタンプの対象を準備中です。少し待ってからもう一度操作してください。"
SEARCH_EMPTY = "検索する語を入力してください。"


def followup_waiting(result) -> bool:
    """An admitted stamp is not finished: keep its followup for the terminal result."""
    return isinstance(result, dict) and result.get("stamp_state") == "queued"


LIST_SHOW = 15             # ephemeral rows before 「他N件」
SCOPE_CHANGED_TEXT = "表示できる患者の範囲が変わりました。もう一度操作してください。"


def list_messages(result: dict, allowed, markdown: bool = True, *,
                  plain: bool = False) -> list:
    """A runner list view (📋 / 🗂 / 🔎) as ephemeral messages. Items of
    projects outside this deployment's scope (``allowed(pid)`` false)
    are dropped before counting; the rest is capped with 「他N件」 (the
    runner's own overflow ``more`` is counted in, unfiltered). The title
    is bold in Discord markdown; ``markdown=False`` callers (Slack
    plain_text, LINE WORKS) get unmarked plain text, as with ``plain``."""
    plain = plain or not markdown
    view = result.get("list") or {}
    items = [i for i in view.get("items") or []
             if isinstance(i, dict) and allowed(i.get("project_id"))]
    bold = "" if plain else "**"

    def lit(value) -> str:
        return _literal(str(value), plain)

    lines = [f"{bold}{lit(view.get('title') or '一覧')}{bold}"]
    lines += [lit(x) for x in view.get("head") or []]
    group = None
    for i in items[:LIST_SHOW]:
        if i.get("group") and i["group"] != group:
            group = i["group"]
            lines.append(f"■ {lit(group)}")
        if not plain and i.get("url") and i.get("date") and i.get("quotes"):
            # markdown transports: the source date links to the patient's MCS room
            rest = f" {lit(i['source_rest'])}" if i.get("source_rest") else ""
            quotes = [lit(q) for q in i["quotes"]]
            quotes[-1] += f"　（[{lit(i['date'])}](<{i['url']}>){rest}）"
            lines.append("\n".join(quotes))
        else:
            lines.append(lit(i.get("text") or ""))
    if not items:
        lines.append(lit(view.get("empty") or "該当なし"))
    rest = len(items) - min(len(items), LIST_SHOW) + int(view.get("more") or 0)
    if rest > 0:
        shown = {i.get("group") for i in items[:LIST_SHOW]}
        hidden = [g for g in dict.fromkeys(i.get("group") for i in items[LIST_SHOW:])
                  if g and g not in shown]
        lines.append(f"他{rest}件（ここでは{min(len(items), LIST_SHOW)}件まで表示）"
                     + (f": {lit('・'.join(hidden))}" if hidden else ""))
    notes = [lit(x) for x in view.get("notes") or []]
    if notes and items:
        lines.append("")   # status notes stand apart from the last group's rows
    lines += notes
    return _answer_chunks("\n".join(lines))


_SCOPED_VIEWS = frozenset({"body", "summary", "list", "digest", "tasks"})


def view_scope_ok(result: dict, allowed) -> bool:
    """False when a view result was counted over a project this
    deployment no longer serves (a click queued before its scope
    narrowed): neither its text nor its card may be sent."""
    scope = result.get("projects")
    if not isinstance(scope, list):
        # a view answer without its counted scope fails closed
        return result.get("action") not in _SCOPED_VIEWS
    return all(allowed(p) for p in scope)


def view_answer(result: dict | None, allowed,
                markdown: bool = True, *, plain: bool = False) -> list | None:
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
    if not view_scope_ok(result, allowed):
        return [(SCOPE_CHANGED_TEXT, None)]
    # markdown=False callers send plain text (Slack plain_text / LINE
    # WORKS): no "**" or "*" markup may reach them
    plain = plain or not markdown
    action = result.get("action")
    if action == "digest" and isinstance(result.get("text"), str):
        # rendered by the runner from the shared display model in this
        # card's transport dialect (notify_render.parts_text)
        return [(m, None) for m in _answer_chunks(result["text"],
                                                  DIGEST_LONG_NOTICE)]
    if action in ("body", "summary") and result.get("body"):
        return [(m, None) for m in body_messages(result, plain=plain)]
    if action == "tasks":
        items = result.get("tasks") or []
        if items and not plain:
            return _task_answers(items)
        return ([(task_list_text(items, plain=plain), items)] if items
                else [(NO_TASKS_TEXT, None)])
    if action == "list":
        return [(m, None) for m in list_messages(result, allowed, markdown, plain=plain)]
    if action == "task_status":
        return [(task_done_text(result, plain=plain), None)]
    return None


# registry.CONFIRM_TTL_S (600 s) — the pending confirm's lifetime
CONFIRM_TTL_NOTE = "\nこの確認は10分で無効になります。"


def preview_text(action: str, payload: dict, markdown: bool, target: str | None = None,
                 form: dict | None = None) -> str:
    """The human-confirm preview of a task/dismiss/report payload —
    Discord markdown (bold heading) or Slack plain
    text."""
    bold = "**" if markdown else ""

    def lit(value):
        return _literal(str(value), not markdown)

    # the render-pinned patient/post, display only (the command is pinned separately)
    about = (f"対象: {_literal(str(target)[:120], not markdown)}\n"
             if isinstance(target, str) and target.strip() else "")
    if action == "stamp":
        labels = forms.stamp_types(form if isinstance(form, dict) else {})
        # the chosen post from the pinned form, not only the card-level target
        post = next((p for p in (form or {}).get("posts") or [] if isinstance(p, dict)
                     and p.get("message_id") == payload.get("message_id")
                     and p.get("source_hash") == payload.get("source_hash")), None)
        chosen = (f"投稿: {lit(str(post.get('label') or '')[:75])}\n"
                  if post and isinstance(post.get("label"), str) else "投稿: 不明\n")
        if payload.get("op") == "cancel":
            kind = (payload.get("expected_own") or {}).get("type")
            what = f"取消（現在: {lit(labels.get(kind, '未知'))}）"
        else:
            what = f"{lit(labels.get(payload.get('reaction_type'), '未知'))}を押す"
        return (f"{bold}確認 — MCSスタンプ{bold}\n" + about + chosen
                + f"操作: {what}\n理由: {lit(payload['reason'])}\n"
                "確定すると、MCSの収集アカウント名義でスタンプを送信し、"
                "あなたの名前と理由を記録します。MCSで反映を確認するまで完了にしません。"
                + CONFIRM_TTL_NOTE)
    if action == "dismiss":
        code = dict(forms.DISMISS_REASONS).get(payload.get("reason_code"))
        reason = lit(payload["reason"])
        return (f"{bold}確認 — 候補の却下{bold}\n" + about
                + (f"区分: {code}\n" if code else "")
                + (f"理由: {reason}\n" if reason != code else "")
                + "確定すると、この候補を却下済みにし、あなたの名前と理由を記録します。"
                + CONFIRM_TTL_NOTE)
    if action == "report":
        return (f"{bold}確認 — 抽出の誤り報告{bold}\n" + about +
                f"箇所: {dict(forms.FEEDBACK_FIELDS).get(payload['field'], '?')}\n"
                f"メモ: {lit(payload['reason'])}\n"
                "確定すると、この投稿の要約の抽出を1回だけ再実行し、"
                "あなたの名前と理由を記録します。" + CONFIRM_TTL_NOTE)
    out = (f"{bold}確認 — タスク作成{bold}\n" + about
           + f"内容: {lit(payload['title'])}")
    if payload.get("assignee"):
        out += f"\n担当: {lit(payload['assignee'])}"
    if payload.get("due_date"):
        out += f"\n期限: {payload['due_date']}"
    return (out + f"\n理由: {lit(payload['reason'])}\n"
            "確定すると、この投稿にタスクを作成し、あなたの名前と理由を記録します。"
            + CONFIRM_TTL_NOTE)


def _task_answers(items: list) -> list:
    """Bound Discord task text and keep each group's controls on its final chunk."""
    answer, group = [], []

    def flush(tasks):
        chunks = split_body(task_list_text(tasks), max_chunks=None)
        answer.extend((chunk, tasks if i == len(chunks) - 1 else None)
                      for i, chunk in enumerate(chunks))

    for item in items:
        if group and len(task_list_text([*group, item])) > BODY_CHUNK:
            flush(group)
            group = []
        group.append(item)
    if group:
        flush(group)
    return answer


def task_list_text(items: list, *, plain: bool = False) -> str:
    """Ephemeral task list — one line per request, status mark first so
    the scan order matches the transition buttons below it."""
    marks = {"open": "⬜", "in_progress": "⏳", "done": "✅"}
    lines = ["📋 タスク一覧" if plain else "📋 **タスク一覧**"]
    for t in items:
        meta = []
        if t.get("assignee"):
            meta.append(f"担当: {t['assignee']}")
        if t.get("due_date"):
            meta.append(f"期限: {t['due_date']}")
        lines.append(f"{marks.get(t['status'], '⬜')} "
                     f"#{t['request_id']} "
                     + _literal(str(t['title']) + (" — " + "・".join(meta)
                                                   if meta else ""), plain))
    return "\n".join(lines)


def task_done_text(result: dict, *, plain: bool = False) -> str:
    status = "完了" if result.get("status") == "done" else "対応中"
    # the title is user-authored, as in task_list_text; the #id fallback is not
    title = (_literal(str(result["title"]), plain) if result.get("title")
             else f"#{result.get('request_id')}")
    if result.get("absorbed"):
        return f"タスク「{title}」は既に「{status}」です。"
    return f"タスク「{title}」を「{status}」にしました。"
