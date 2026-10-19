"""Own card action vocabulary, token lifetimes and minting on the caller's transaction."""
from __future__ import annotations

import json
import secrets

# Vocabulary -> (label, style, class). Retired defer tokens stay readable.
ACTIONS = {
    "ack":     ("確認する", "secondary", "write"),
    "assign":  ("担当する", "secondary", "write"),
    "defer":   ("保留", "secondary", "write"),
    "body":    ("本文表示", "secondary", "view"),
    "tasks":   ("タスク一覧", "secondary", "view"),
    "request": ("タスク作成", "secondary", "write"),
    "summary": ("患者の記録まとめ", "secondary", "view"),
    "report":  ("誤りを報告", "secondary", "write"),
    "meds":    ("薬剤を確認", "secondary", "view"),
    "drugsearch": ("薬剤を検索", "secondary", "view"),
    "dismiss": ("却下", "danger", "write"),
    "prev":    ("◀ 前", "secondary", "view"),
    "next":    ("次 ▶", "secondary", "view"),
    "mytasks": ("自分のタスク", "secondary", "view"),
    "unacked": ("未確認一覧", "secondary", "view"),
    "search":  ("この患者を検索", "secondary", "view"),
    "digest":  ("全体の新着集計", "secondary", "view"),
    # LINE WORKS only: the adapter answers it locally with a 1:1 menu
    # of the card's other buttons — it never reaches the runner
    "more":    ("その他の操作", "secondary", "view"),
    # minted only inside a tasks view — never a card button; the plugin
    # supplies its own labels from the view's transitions
    "task_status": ("", "secondary", "write"),
}

WRITE_ACTIONS = frozenset(
    a for a, (_, _, cls) in ACTIONS.items() if cls == "write")


# Live responses are recomputed after current authorization even on replay.
LIVE_VIEWS = frozenset({"body", "summary", "request", "dismiss", "report",
                         "mytasks", "unacked", "search", "digest", "tasks",
                         "meds", "drugsearch"})

TOKEN_VIEW_S = 30 * 86400

TOKEN_WRITE_S = 7 * 86400


def mint(db, card_id, action, params, need, now) -> str:
    """Mint on borrowed db without commit/close or changing params/need inputs."""
    token = secrets.token_hex(16)
    ttl = TOKEN_VIEW_S if ACTIONS[action][2] == "view" else TOKEN_WRITE_S
    db.execute(
        """INSERT INTO notification_action_tokens(
             token,card_id,action,params,need_source_gen,
             need_manifest_id,need_ui_rev,need_request_rev,
             expires_at,created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (token, card_id, action,
         json.dumps(params, ensure_ascii=False, sort_keys=True)
         if params else None,
         need.get("source_gen"), need.get("manifest_id"),
         need.get("ui_rev"), need.get("request_rev"),
         now + ttl, now))
    return token
