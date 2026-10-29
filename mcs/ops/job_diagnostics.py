"""Read bounded, non-sensitive evidence for failed semantic jobs."""
from __future__ import annotations

import sqlite3
import time

from mcs_util import payload_hash, positive, strict_json, until_epoch
from operation_contract import read_retry_payload, retry_error, retry_job_row
from semantic_runtime import payload_generation
import semantic_intake_hold
import cloud_policy
import semantic_policy
from mcs_queries import paused
from inference_dependency_loss import source_binding
from inference_version import is_current

# Only codes produced by the worker are exposed; remote text is never returned.
_REASONS = {
    "permission_refused": ("permission", "閲覧権限を確認してください。"),
    "off_or_scope": ("scope", "解析設定と対象範囲を確認してください。"),
    "source_changed": ("source", "最新の取得状態を確認してください。"),
    "generation_changed": ("source", "最新の取得状態を確認してください。"),
    "budget_short": ("budget", "処理に必要な予算と実行時間を確認してください。"),
    "long_output_defer_capped": ("budget", "処理に必要な予算と実行時間を確認してください。"),
    "audit_pending": ("quality", "監査の状態を確認してください。"),
    "audit_jev_None": ("investigate", "監査できなかった理由を確認してください。"),
    "audit_unevaluated": ("investigate", "監査できなかった理由を確認してください。"),
    "audit_claims_empty": ("quality", "要約の検証対象が空のため、元の記録と要約を確認してください。"),
    "audit_claims_invalid": ("quality", "要約の検証対象を確認してください。"),
    "audit_client_unavailable": ("jev", "照合サービスの設定と接続状態を確認してください。"),
    "audit_options_invalid": ("investigate", "監査設定を確認してください。"),
    "audit_response_invalid": ("jev", "照合結果の形式を確認してください。"),
    "loops_incomplete": ("quality", "解析が完了しなかった理由を確認してください。"),
    "loops_jev_rejected": ("quality", "監査の状態を確認してください。"),
    "llm_deadline": ("budget", "ローカルモデルの実行時間を確認してください。"),
    "llm_long_ceiling": ("model", "ローカルモデルの出力上限を確認してください。"),
    "llm_unreachable": ("model", "ローカルモデルの接続状態を確認してください。"),
    "llm_unavailable": ("model", "ローカルモデルの稼働状態を確認してください。"),
    "llm_busy": ("model", "ローカルモデルの利用状況を確認してください。"),
    "llm_prompt_rejected": ("model", "ローカルモデルが入力を受け付けない状態を確認してください。"),
    "llm_cloud_failed": ("investigate", "クラウド要求の送信結果と会計記録を確認してください。"),
    "internal_error": ("investigate", "処理中のエラーについて診断情報を確認してください。"),
}
for _phase in ("assess", "audit", "loops"):
    for _kind in ("resource", "retryable", "hard"):
        _REASONS[f"{_phase}_jev_{_kind}"] = ("jev", "照合サービスの状態と設定を確認してください。")


def _snapshot(db):
    if not db.in_transaction:
        raise sqlite3.DatabaseError("job_diagnostics_snapshot_required")


def _metrics(db):
    # Do not materialize oversized content in Python, and retain code fields only.
    rows = db.execute("SELECT artifact_id, CASE WHEN typeof(content)='text' "
                      "AND length(CAST(content AS BLOB))<=1048576 THEN content END "
                      "FROM main.artifacts WHERE kind='semantic_drain_run' "
                      "ORDER BY artifact_id DESC LIMIT 512")
    out = []
    for artifact_id, raw in rows:
        if raw is None:
            continue
        try:
            doc = strict_json(raw)
        except (ValueError, TypeError, RecursionError):
            continue
        metrics = doc.get("job_metrics") if isinstance(doc, dict) else None
        if not isinstance(metrics, list) or len(metrics) > 64:
            continue
        for metric in reversed(metrics):
            if not isinstance(metric, dict):
                continue
            fields = ("job_id", "project_id", "kind", "generation", "payload_digest", "attempts", "status")
            values = [metric.get(field) for field in fields]
            if (any(type(values[index]) is not int for index in (0, 1, 5))
                    or any(not isinstance(values[index], str) or len(values[index]) > 128
                           for index in (2, 3, 4, 6))):
                continue
            code = metric.get("retry_reason")
            if isinstance(code, str) and code.startswith("exception_") and len(code) > len("exception_"):
                code = "internal_error"
            if not isinstance(code, str) or code not in _REASONS:
                llm_error = metric.get("llm_error")
                code = "llm_" + llm_error if isinstance(llm_error, str) and len(llm_error) <= 64 else None
            out.append((artifact_id, *values, code if code in _REASONS else None))
    return out


def _evidence(metrics, row, digest, generation):
    for artifact_id, job_id, project_id, kind, gen, fp, attempts, status, code in metrics:
        if (job_id == row["job_id"] and project_id == row["project_id"] and kind == row["kind"]
                and gen == generation and fp == digest and attempts + 1 == row["attempts"]
                and status in ("retry", "failed", "error")):
            return code, artifact_id if code else None
    return None, None


def _unsettled_cloud(db, row):
    """A possibly-sent attempt for this job stays held until its receipt settles."""
    rows = db.execute("SELECT artifact_id,kind,project_id,message_id,model, "
                      "CASE WHEN typeof(meta)='text' AND length(CAST(meta AS BLOB))<=32768 THEN meta END AS meta, "
                      "created_at FROM main.artifacts WHERE project_id=? AND message_id=? "
                      "AND kind IN ('cloud_usage_reserve','cloud_usage_result') "
                      "ORDER BY artifact_id DESC LIMIT 1025", (row["project_id"], row["message_id"])).fetchall()
    if len(rows) > 1024:
        return True
    reserves, results = [], {}
    for item in rows:
        try:
            meta = strict_json(item["meta"])
            if not isinstance(meta, dict):
                return True
        except (TypeError, ValueError, RecursionError):
            return True
        if item["kind"] == "cloud_usage_reserve":
            try:
                cloud_policy._reserve_valid(meta, item)
            except (ValueError, TypeError, KeyError, ArithmeticError, cloud_policy.AnthropicError):
                return True
            unit = meta.get("unit_key")
            if (meta.get("route") == "semantic_backlog" and isinstance(unit, str)
                    and unit.startswith(f"semantic:{row['job_id']}:")):
                reserves.append((meta, item))
        else:
            rid = meta.get("reservation_id")
            if not isinstance(rid, str):
                return True
            results.setdefault(rid, []).append((meta, item))
    try:
        if len({meta["reservation_id"] for meta, _ in reserves}) != len(reserves):
            return True
        for meta, item in reserves:
            maximum = cloud_policy._reserve_valid(meta, item)
            _, settled = cloud_policy._settled(meta, maximum, results.get(meta["reservation_id"], []))
            if not settled:
                return True
    except (ValueError, TypeError, KeyError, ArithmeticError, cloud_policy.AnthropicError):
        return True
    return False


def diagnose_job(db, cfg, job_id, now=None):
    """Describe a job without changing it or exposing its payload or source text."""
    _snapshot(db)
    return _diagnose(db, cfg, job_id, now, _metrics(db))


def _diagnose(db, cfg, job_id, now, metrics):
    if not positive(job_id):
        raise ValueError("job_id_invalid")
    row = db.execute("SELECT job_id,kind,project_id,message_id,state,attempts,reason_code "
                     "FROM main.fetch_jobs WHERE job_id=?", (job_id,)).fetchone()
    if row is None:
        raise ValueError("job_not_found")
    blockers = []
    if row["state"] != "failed":
        blockers.append("job_not_failed")
    if row["kind"] != "semantic":
        blockers.append("unsupported_kind")
    if type(row["attempts"]) is not int or row["attempts"] < 0:
        blockers.append("invalid_attempts")
    digest = None
    payload = None
    if row["kind"] == "semantic" and positive(row["project_id"]):
        source, error = retry_job_row(db, row["project_id"], job_id)
        if not error:
            raw = read_retry_payload(db, source)
            try:
                payload = strict_json(raw)
                if not isinstance(payload, dict):
                    raise ValueError
                digest = payload_hash(payload)
                if not semantic_intake_hold.supported_job(db, source, row["project_id"], row["message_id"]):
                    raise ValueError
                retry_problem = retry_error(source, payload, None)
                if retry_problem not in (None, "attempt_limit"):
                    blockers.append(retry_problem)
            except (TypeError, ValueError, RecursionError, OverflowError):
                payload, digest = None, None
    if payload is None:
        blockers.append("invalid_payload")
    elif not is_current(payload):
        blockers.append("needs_current_generation")
    if row["reason_code"] == "semantic_payload_invalid":
        blockers.append("invalid_payload")
    settings, errors = semantic_policy.runtime_config(cfg if isinstance(cfg, dict) else {})
    scope = {key: settings.get(key) for key in ("mode", "project_ids", "until")}
    if errors or settings["mode"] == "off":
        blockers.append("semantic_off_or_invalid")
    projects = settings["project_ids"]
    if projects is not None and row["project_id"] not in projects:
        blockers.append("out_of_scope")
    now = time.time() if now is None else now
    if settings["until"] is not None and now >= until_epoch(settings["until"]):
        blockers.append("window_expired")
    patient = db.execute("SELECT fetch_reason FROM main.patients WHERE project_id=?", (row["project_id"],)).fetchone()
    if patient is None:
        blockers.append("patient_unavailable")
    elif patient[0] in ("forbidden", "permission_denied", "access_revoked"):
        blockers.append("permission")
    if positive(row["project_id"]) and paused(db, row["project_id"]):
        blockers.append("paused")
    source_witness = []
    if row["kind"] == "semantic" and positive(row["project_id"]) and positive(row["message_id"]):
        if semantic_intake_hold.has_held_root(db, row["project_id"], row["message_id"]):
            blockers.append("intake_hold")
        if _unsettled_cloud(db, row):
            blockers.append("send_unverified")
        sources = db.execute("SELECT message_id, body_state, length(body_text) FROM main.messages "
                             "WHERE project_id=? AND (message_id=? OR parent_id=?) "
                             "ORDER BY message_id LIMIT 257", (row["project_id"], row["message_id"],
                                                               row["message_id"])).fetchall()
        ids = {source[0] for source in sources}
        targets = payload.get("targets", []) if payload else []
        targets_valid = isinstance(targets, list) and all(positive(target) for target in targets)
        if (not targets_valid or len(sources) > 256 or row["message_id"] not in ids
                or any(target not in ids for target in targets if targets_valid)
                or any(source[1] not in (None, "full") or not source[2] for source in sources)):
            blockers.append("source_unavailable")
        if len(sources) <= 256:
            source_witness = [source_binding(db, row["project_id"], source[0]) for source in sources]
    else:
        blockers.append("source_unavailable")
    code, evidence_id = (_evidence(metrics, row, digest, payload_generation(payload))
                         if payload is not None else (None, None))
    warnings = []
    if code is None:
        code, basis = "unknown", "unbound_or_missing"
        remedy = {"code": "investigate", "description": "失敗理由を確認してから対応してください。"}
        warnings.append("reason_unverified")
    else:
        basis = "bound_job_metric"
        if code == "llm_cloud_failed":
            blockers.append("send_unverified")
        remedy = {"code": _REASONS[code][0], "description": _REASONS[code][1]}
    blockers = sorted(set(blockers))
    result = {"job_id": row["job_id"], "kind": row["kind"] if row["kind"] == "semantic" else "other",
              "project_id": row["project_id"], "state": row["state"] if row["state"] in ("failed", "pending", "done") else "unknown",
              "attempts": row["attempts"] if type(row["attempts"]) is int else None,
              "reason_code": code, "reason_basis": basis, "remedy": remedy,
              "remediation_code": remedy["code"], "blockers": blockers, "warnings": warnings,
              "expected_payload_hash": digest}
    result["diagnosis_hash"] = payload_hash({**result, "scope": scope, "evidence_id": evidence_id, "source_witness": source_witness})
    return result


def list_failed(db, cfg, limit=50, now=None):
    """List a bounded number of failed jobs under the caller's snapshot."""
    _snapshot(db)
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("job_limit_invalid")
    ids = [row[0] for row in db.execute("SELECT job_id FROM main.fetch_jobs WHERE state='failed' "
                                         "ORDER BY job_id DESC LIMIT ?", (limit + 1,))]
    metrics = _metrics(db) if ids else []
    return {"jobs": [_diagnose(db, cfg, job_id, now, metrics) for job_id in ids[:limit]],
            "truncated": len(ids) > limit}
