"""訪問時の確認用に、患者コンテキストの画面と未解消のシグナルを1つの読取り結果にまとめる。"""
from __future__ import annotations

from datetime import datetime

import notify_views
from mcs_queries import JST

# The visit sheet's default order: what to check today, what changed, who the patient is, open contacts.
VISIT_SECTIONS = ("previsit", "changes", "background", "replies")
ITEM_KEYS = ("group", "text", "quotes", "source", "message_id")


def section(db, project_id, key, *, cfg, now, since=None, role="", page=0) -> dict:
    """One card view (notify_views projection) reduced to plain, patient-scoped fields."""
    from patient_context_view import first_changes_baseline
    if key not in notify_views.CONTEXT_SECTIONS or key == "measurements":
        raise ValueError("unknown_section")
    if role not in notify_views.CONTEXT_ROLES or type(page) is not int or not 0 <= page <= 100000:
        raise ValueError("bad_arguments")
    baseline = first_changes_baseline(now) if since is None else since
    result = notify_views.patient_context_list(
        db, project_id, key, cfg=cfg, since=baseline, now=now, role=role, page=page,
        first_view=since is None, keep_message_id=True)
    items = [{k: item.get(k) for k in ITEM_KEYS}
             for item in result.get("items") or [] if item.get("project_id", project_id) == project_id]
    return {"key": key, "label": notify_views.CONTEXT_SECTIONS[key],
            **{k: result.get(k) for k in ("title", "head", "empty", "more", "notes", "page", "pages")},
            "items": items}


def open_signals(db, project_id, *, limit=50) -> dict:
    """Unresolved review candidates with their cited post; candidates, never findings."""
    import mcs_signals
    from notify_render import signal_evidence, signal_label

    found = mcs_signals.current_open(db, project_id=project_id, limit=limit)
    items = []
    for signal in found["items"]:
        message_id, _message = signal_evidence(db, signal)
        at = signal.get("detected_at")
        items.append({"group": signal_label(signal)[1:-1], "text": signal.get("note") or "",
                      "quotes": None, "message_id": message_id,
                      "source": datetime.fromtimestamp(at, JST).strftime("%m/%d %H:%M 検出")
                      if isinstance(at, (int, float)) else "検出時刻未取得"})
    notes = []
    # Without a completed run (or with unreadable states) an empty list is not proof that nothing is open.
    unconfirmed = found["pipeline_last_run_at"] is None or bool(found["errors"])
    if found["pipeline_last_run_at"] is None:
        notes.append("すべての種類の判定が終わったかを確認できません。表示は判定済みの分だけです。")
    if found["truncated"]:
        notes.append(f"新しい順に{found['returned']}件まで表示しています（全{found['total']}件）。")
    if found["errors"]:
        notes.append("一部のシグナルの状態を読み取れませんでした。")
    return {"key": "signals", "label": "未解消のシグナル", "title": "未解消のシグナル", "head": [],
            "items": items, "notes": notes,
            "empty": "判定済みの範囲に未解消のシグナルは見つかりませんでした。" if unconfirmed
            else "未解消のシグナルはありません。"}


def visit_brief(view, project_id, *, cfg, since=None, role="", sections=VISIT_SECTIONS) -> dict:
    """Everything a visit sheet reads from MCS for one patient, from one published snapshot.

    ``since`` is the previous visit time when the caller knows it, so 前回から変わったこと
    means 'since the last visit'; without it the 7-day first-view baseline applies."""
    if type(project_id) is not int or project_id <= 0:
        raise ValueError("bad_project")
    if since is not None and (isinstance(since, bool) or not isinstance(since, (int, float)) or since < 0):
        raise ValueError("bad_since")
    if view.db.execute("SELECT 1 FROM patients WHERE project_id=?", (project_id,)).fetchone() is None:
        raise ValueError("unknown_project")
    now = view.meta["generated_at"]
    return {"project_id": project_id, "snapshot": view.meta, "read_only": True,
            "since": since, "sections": [section(view.db, project_id, key, cfg=cfg, now=now, since=since, role=role)
                                         for key in sections],
            "signals": open_signals(view.db, project_id)}
