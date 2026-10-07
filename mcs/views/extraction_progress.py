"""Read-only, bounded extraction progress counts for private patient summaries."""
from __future__ import annotations

import sqlite3

from mcs_requests import valid_hash
from semantic_policy import semantic_config, policy_fingerprint

LATEST_POSTS = 20


def message_progress(db, row, *, cfg=None):
    """Return only validated current counts for one source and configured backend."""
    row = dict(row)
    scfg, errors = semantic_config(cfg) if isinstance(cfg, dict) else ({}, ["config_unknown"])
    canonical = (not errors and scfg["mode"] != "off" and scfg["fact_source"] == "canonical"
                 and (scfg["project_ids"] is None or row["project_id"] in scfg["project_ids"]))
    progress = None
    if (not errors and row["body_state"] == "full" and isinstance(row["body_text"], str)
            and valid_hash(row["content_hash"])):
        try:
            if canonical:
                import semantic
                import semantic_extraction
                progress = semantic_extraction.progress_for_message(
                    db, row, model=semantic.llm_model(cfg),
                    policy_fingerprint=policy_fingerprint(scfg))
            else:
                import extract_llm
                import local_llm
                model = local_llm.resolve(cfg)[1]
                if extract_llm.MODEL != extract_llm._MODEL_PIN:
                    model = extract_llm.MODEL
                progress = extract_llm.progress_for_message(
                    db, row, model=model)
        except (sqlite3.DatabaseError, ValueError, TypeError, KeyError, AttributeError, RecursionError):
            progress = None
    if (not isinstance(progress, dict)
            or progress.get("state") not in ("processing", "complete", "attention")
            or type(progress.get("completed")) is not int
            or type(progress.get("total")) is not int
            or not 0 <= progress["completed"] <= progress["total"]
            or (progress["state"] == "complete" and progress["completed"] != progress["total"])):
        return None
    return {key: progress[key] for key in ("state", "completed", "total")}


def patient_progress(db, project_id, *, cfg=None):
    """Count only backend-validated current progress, never source text or extracted facts."""
    rows = db.execute("SELECT * FROM messages WHERE project_id=? AND body_state IS NOT 'deleted' "
                      "ORDER BY posted_at_ts DESC,message_id DESC LIMIT ?",
                      (project_id, LATEST_POSTS + 1)).fetchall()
    result = {"processing": 0, "complete": 0, "attention": 0,
              "completed": 0, "total": 0, "posts": min(len(rows), LATEST_POSTS),
              "limited": len(rows) > LATEST_POSTS}
    for raw in rows[:LATEST_POSTS]:
        progress = message_progress(db, raw, cfg=cfg)
        if progress is None:
            result["attention"] += 1
            continue
        result[progress["state"]] += 1
        result["completed"] += progress["completed"]
        result["total"] += progress["total"]
    return result


def summary_lines(db, project_id, *, cfg=None):
    """Two count-only lines; completion never means clinical or archive completeness."""
    progress = patient_progress(db, project_id, cfg=cfg)
    if not progress["posts"]:
        return ["抽出の処理状況: 要確認（表示対象の取得済み投稿なし）"]
    scope = f"最新{progress['posts']}投稿"
    line = (f"抽出の処理状況（{scope}）: 処理中{progress['processing']}・"
            f"完了{progress['complete']}・要確認{progress['attention']}")
    if progress["total"]:
        line += f"（{progress['total']}区間中{progress['completed']}区間完了）"
    caveat = "完了は抽出処理のみ。記録や臨床情報の完全性は保証しません。"
    if progress["limited"]:
        caveat = "以前の投稿は表示対象外。" + caveat
    return [line, caveat]
