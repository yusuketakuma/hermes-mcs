"""Read-only, bounded extraction progress counts for private patient summaries."""
from __future__ import annotations
import semantic_transport

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
                import semantic_extraction
                progress = semantic_extraction.progress_for_message(
                    db, row, model=semantic_transport.llm_model(cfg),
                    policy_fingerprint=policy_fingerprint(scfg))
            else:
                import extract_llm
                progress = extract_llm.progress_for_message(
                    db, row, model=extract_llm.resolved_model(cfg))
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
    result = {"processing": 0, "complete": 0, "attention": 0, "unknown": 0,
              "completed": 0, "total": 0, "posts": min(len(rows), LATEST_POSTS),
              "limited": len(rows) > LATEST_POSTS}
    for raw in rows[:LATEST_POSTS]:
        progress = message_progress(db, raw, cfg=cfg)
        if progress is None:
            # not fetched / lookup failed: unknown, never a finding (要確認)
            result["unknown"] += 1
            continue
        result[progress["state"]] += 1
        result["completed"] += progress["completed"]
        result["total"] += progress["total"]
    return result


def summary_lines(db, project_id, *, cfg=None):
    """One count-only line about extraction progress for the patient."""
    progress = patient_progress(db, project_id, cfg=cfg)
    if not progress["posts"]:
        return ["抽出の処理状況: 取得済みの投稿はありません"]
    scope = f"最新{progress['posts']}投稿"
    line = (f"抽出の処理状況（{scope}）: 処理中{progress['processing']}・"
            f"完了{progress['complete']}・要確認{progress['attention']}")
    if progress["unknown"]:
        line += f"・状況未取得{progress['unknown']}"
    notes = []
    if progress["total"]:
        notes.append(f"{progress['total']}区間中{progress['completed']}区間完了")
    if progress["limited"]:
        notes.append("以前の投稿は対象外")
    if notes:
        line += f"（{'・'.join(notes)}）"
    return [line]
