"""Transport-neutral interaction forms and validation of submitted values.

Only runner-provided choices become command attributes. The caller owns the
pending form and the runner still authorizes each command before applying it.
"""
from __future__ import annotations


def valid_due(due: str) -> bool:
    """A due date must be a real calendar day spelled YYYY-MM-DD —
    the round trip rejects every other ISO spelling."""
    from datetime import date
    try:
        return date.fromisoformat(due).isoformat() == due
    except ValueError:
        return False


# ⚠ report target parts — values match mcs_operations
# EXTRACT_FEEDBACK_FIELDS (pinned by test_feedback_fields_match_the_runner_vocabulary)
FEEDBACK_FIELDS = (("summary", "要約"), ("urgency", "緊急度"), ("meds", "薬"),
                   ("symptoms", "症状"), ("requests", "依頼"),
                   ("vitals", "バイタル"), ("patient_context", "患者情報"),
                   ("labs", "検査値"), ("events", "出来事"), ("other", "その他"))
# 🚫 reason codes — values match mcs_operations DISMISS_REASON_CODES
DISMISS_REASONS = (("false_positive", "誤検知"), ("already_handled", "対応済み"),
                   ("duplicate", "重複"), ("out_of_scope", "対象外"),
                   ("other", "その他"))
TASK_REASON = "通知カードからタスク作成"
STAFF_OPTIONS = 25
MODAL_TITLES = {"request": "タスク作成", "dismiss": "候補を却下",
                "report": "抽出の誤りを報告", "search": "この患者を検索",
                "drugsearch": "薬剤を検索", "digest": "サマリー（絞込み）",
                "stamp": "MCSスタンプ"}
STAMP_REASON_MAX = 200
# card actions whose click opens a modal (forms.modal_fields) instead of
# answering directly
MODAL_ACTIONS = tuple(MODAL_TITLES)
# modal actions whose submit is a view click on the same token — no
# preview/confirm; the runner answers with the view
VIEW_FORMS = ("search", "drugsearch", "digest")
# view forms whose single "query" field is a free-text search term
QUERY_FORMS = ("search", "drugsearch")


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
    if action == "stamp":
        return stamp_fields(form)
    if action == "search":
        return [{"id": "query", "label": "検索語（AND）。絞込み: 資料:添付 開始:2026-01-01 分類:薬剤管理",
                 "required": True, "max": 500, "default": ""}]
    if action == "drugsearch":
        return [{"id": "query", "label": "薬名（一般名・製品名・コードの一部）",
                 "required": True, "max": 100, "default": ""}]
    if action == "digest":
        return [{"id": "query", "required": False, "max": 100,
                 "label": "絞込み（全体 / 担当 / 施設:名前 / 日数:1-7）",
                 "default": "全体"},
                {"id": "name", "label": "担当の名前（担当で絞る場合）",
                 "required": False, "max": 120, "default": clicker[:120]}]
    return []


def stamp_types(form: dict) -> dict:
    """Runner-sent canonical kind -> label pairs; the UI keeps no own table."""
    return {k: v for k, v in (form.get("types") or [])
            if isinstance(k, str) and isinstance(v, str)}


def _stamp_icons(form: dict) -> dict:
    """Runner-sent kind -> emoji; absent from older runners, then labels stand alone."""
    icons = form.get("emoji")
    return {k: v for k, v in icons.items() if isinstance(k, str) and isinstance(v, str)
            and 0 < len(v) <= 4} if isinstance(icons, dict) else {}


def _stamp_name(kind, types: dict, icons: dict) -> str:
    label = types.get(kind, "未知")
    return f"{icons[kind]}{label}" if kind in icons else label


def _stamp_own_text(own, types: dict, icons: dict | None = None) -> str:
    own = own if isinstance(own, dict) else {}
    if own.get("state") == "pending":
        return "（確認中）"
    if own.get("state") != "observed":
        return "（自分: 不明）"
    kind = own.get("type")
    return "（自分: なし）" if kind is None else f"（自分: {_stamp_name(kind, types, icons or {})}）"


def stamp_fields(form: dict | None) -> list:
    """🏷 modal: a post the runner offered, a stamp and a reason.

    Like MCS's own stamp row: the five stamps in MCS order, and choosing the
    stamp you already pressed takes it back (no separate cancel button)."""
    form = form if isinstance(form, dict) else {}
    types, icons = stamp_types(form), _stamp_icons(form)
    posts = [p for p in form.get("posts") or [] if isinstance(p, dict)
             and isinstance(p.get("opt"), str) and isinstance(p.get("label"), str)]
    ops = [(f"add:{k}", _stamp_name(k, types, icons)) for k in types]
    return [{"id": "post", "label": "対象の投稿", "required": True,
             "options": [(p["opt"], (p["label"] + _stamp_own_text(p.get("own"), types, icons))[:75])
                         for p in posts], "default": None},
            {"id": "op", "label": "スタンプ（押しているものを選ぶと取消）", "required": True,
             "options": ops, "default": None},
            {"id": "reason", "label": "理由", "required": True, "multiline": True,
             "max": STAMP_REASON_MAX, "default": ""}]


def stamp_attrs(fields: dict, form: dict | None):
    """🏷 values -> the selected runner post and operation, or an error string.
    Only the two contract refusals live here; the runner re-checks the rest."""
    form = form if isinstance(form, dict) else {}
    post = next((p for p in form.get("posts") or [] if isinstance(p, dict)
                 and p.get("opt") == fields.get("post")), None)
    if post is None:
        return "対象の投稿を選択してください。"
    own = post.get("own") if isinstance(post.get("own"), dict) else {}
    if own.get("state") == "pending":
        return "この投稿のMCSスタンプは確認中です。結果が出てからやり直してください。"
    if own.get("state") != "observed":
        return "この投稿の現在のMCSスタンプを確認できないため送信できません。"
    op = fields.get("op") or ""
    if op.startswith("add:") and op[4:] == own.get("type"):
        op = "cancel"           # MCS toggles: the pressed stamp again takes it back
    if op == "cancel":          # also the explicit choice of forms opened before this layout
        if own.get("type") is None:
            return "取消できる自分のスタンプがありません。"
        kind = None
    elif op.startswith("add:") and op[4:] in stamp_types(form):
        kind = op[4:]
    else:
        return "操作を選択してください。"
    reason = (fields.get("reason") or "").strip()
    if not reason:
        return "理由の入力が必要です。"
    if len(reason) > STAMP_REASON_MAX:
        return f"理由は{STAMP_REASON_MAX}文字以内で入力してください。"
    return {"post": post, "op": "cancel" if kind is None else "add", "kind": kind,
            "reason": reason, "expected_own": {"type": own.get("type"),
                                               "observed_at": own.get("observed_at")}}


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


def search_query(fields: dict) -> str | None:
    """🔎 modal value -> normalized keyword, or None when blank."""
    return " ".join((fields.get("query") or "").split())[:100] or None
