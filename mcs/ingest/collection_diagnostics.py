"""Summarize bounded collection failures and incomplete parent bodies without identifying data."""
from __future__ import annotations

import re
from stats_math import finite_number

MAX_ERRORS = 256
MAX_ROWS = 1024
ERROR_LABELS = {
    "authentication": "ログイン・認証", "rate_limit": "取得制限",
    "deadline": "時間切れ", "network": "通信", "schema": "応答形式",
    "storage": "保存", "incomplete": "取得未完了", "command": "操作の取込み",
    "other": "その他・原因未分類",
}
FETCH_LABELS = {
    "parent_body_incomplete": "親本文未取得", "thread_incomplete": "返信取得未完了",
    "replies_missing": "返信不足", "unread_capped": "未読取得の上限",
    "other": "その他・理由未分類",
}
_CONTEXT = re.compile(r"^(?:(?:project|mark|thread|probe|reply|import|reconcile|attachment) [0-9]+|list_unread|unread|self_probe|discovery|thread_read|reply|import|reconcile|attachments|history|metadata):\s*")
_CODE = re.compile(r"[A-Za-z_][A-Za-z_0-9]*")


def error_counts(errors) -> dict:
    """Classify only bounded producer prefixes; never return their original text."""
    if not isinstance(errors, list):
        return {"state": "unknown", "total": None, "scanned": 0, "reasons": None}
    counts = dict.fromkeys(ERROR_LABELS, 0)
    for error in errors[:MAX_ERRORS]:
        text = _CONTEXT.sub("", error[:512], count=1) if isinstance(error, str) else ""
        match = _CODE.match(text)
        code = match.group() if match else ""
        if code in ("session_expired", "auth_required", "login_failed", "unauthorized"):
            reason = "authentication"
        elif code in ("rate_limited", "rate_limit") or text.startswith("http_error(status=429)"):
            reason = "rate_limit"
        elif code in ("deadline_exceeded", "timeout", "worker_timeout", "budget_exhausted"):
            reason = "deadline"
        elif code in ("http_error", "network_error", "connection_error", "worker_failed"):
            reason = "network"
        elif code in ("schema_error", "SchemaError", "invalid_payload"):
            reason = "schema"
        elif code in ("db_write_failed", "disk_low", "disk_full", "fs_OSError",
                      "fs_PermissionError", "fs_FileNotFoundError", "OperationalError"):
            reason = "storage"
        elif code in FETCH_LABELS.keys() - {"other"} or code in ("history_incomplete", "post_ack_gap", "window_stalled"):
            reason = "incomplete"
        elif code in ("cmd_invalid", "update_spawn_failed"):
            reason = "command"
        else:
            reason = "other"
        counts[reason] += 1
    return {"state": "known" if len(errors) <= MAX_ERRORS else "partial",
            "total": len(errors), "scanned": min(len(errors), MAX_ERRORS), "reasons": counts}


def unknown_incomplete() -> dict:
    return {"state": "unknown", "projects": None, "reasons": None,
            "parent_bodies": None, "oldest_observed_age_s": None, "age_unknown": None}


def incomplete_counts(db, now: float, checkpoint) -> dict:
    """Borrow the caller's bounded reader; age means first storage of a still-incomplete body."""
    counts = dict.fromkeys(FETCH_LABELS, 0)
    rows = db.execute(
        "SELECT fetch_reason FROM patients WHERE fetch_state='incomplete' "
        "AND COALESCE(is_archived,0)=0 ORDER BY project_id LIMIT ?", (MAX_ROWS + 1,))
    projects = 0
    for (reason,) in rows:
        checkpoint()
        projects += 1
        if projects > MAX_ROWS:
            raise RuntimeError("observation_limit")
        counts[reason if reason in FETCH_LABELS else "other"] += 1
    bodies, unknown_age, oldest = 0, 0, None
    # Keep incomplete patients outermost so unrelated root history cannot consume the read budget.
    rows = db.execute(
        "SELECT m.first_seen FROM patients p CROSS JOIN messages m ON m.project_id=p.project_id "
        "WHERE p.fetch_state='incomplete' AND COALESCE(p.is_archived,0)=0 "
        "AND m.parent_id IS NULL AND m.body_state IN ('snippet','unknown') "
        "LIMIT ?", (MAX_ROWS + 1,))
    for (first_seen,) in rows:
        checkpoint()
        bodies += 1
        if bodies > MAX_ROWS:
            raise RuntimeError("observation_limit")
        if not finite_number(first_seen) or not 0 <= first_seen <= now:
            unknown_age += 1
        else:
            age = now - first_seen
            oldest = age if oldest is None else max(oldest, age)
    return {"state": "known", "projects": projects, "reasons": counts,
            "parent_bodies": bodies, "oldest_observed_age_s": round(oldest, 1) if oldest is not None else None,
            "age_unknown": unknown_age}


def _count(value) -> bool:
    return type(value) is int and 0 <= value < 2**63


def _reasons(value, labels, total) -> dict | None:
    if not isinstance(value, dict) or not all(_count(value.get(key)) for key in labels):
        return None
    out = {key: value[key] for key in labels}
    return out if sum(out.values()) == total else None


def validated(value, observed_at=None) -> dict | None:
    """Whitelist persisted counts before doctor or monitoring can display them."""
    if not isinstance(value, dict):
        return None
    at = value.get("observed_at")
    if not finite_number(at) or at < 0 or (observed_at is not None and at != observed_at):
        return None
    errors, incomplete = value.get("errors"), value.get("incomplete")
    if not isinstance(errors, dict) or not isinstance(incomplete, dict):
        return None
    error_state = errors.get("state")
    total, scanned = errors.get("total"), errors.get("scanned")
    if error_state == "unknown":
        clean_errors = error_counts(None)
    elif error_state in ("known", "partial") and _count(total) and _count(scanned) \
            and scanned == min(total, MAX_ERRORS) \
            and (error_state == "known") == (total <= MAX_ERRORS):
        reasons = _reasons(errors.get("reasons"), ERROR_LABELS, scanned)
        if reasons is None:
            return None
        clean_errors = {"state": error_state, "total": total, "scanned": scanned, "reasons": reasons}
    else:
        return None
    if incomplete.get("state") == "unknown":
        clean_incomplete = unknown_incomplete()
    elif incomplete.get("state") == "known":
        projects, bodies = incomplete.get("projects"), incomplete.get("parent_bodies")
        missing_age, age = incomplete.get("age_unknown"), incomplete.get("oldest_observed_age_s")
        if not all(_count(v) and v <= MAX_ROWS for v in (projects, bodies, missing_age)) \
                or missing_age > bodies:
            return None
        reasons = _reasons(incomplete.get("reasons"), FETCH_LABELS, projects)
        if reasons is None or (age is not None and (not finite_number(age) or age < 0)) \
                or (age is None) != (bodies == missing_age):
            return None
        clean_incomplete = {"state": "known", "projects": projects, "reasons": reasons,
                            "parent_bodies": bodies, "oldest_observed_age_s": age, "age_unknown": missing_age}
    else:
        return None
    return {"observed_at": at, "errors": clean_errors, "incomplete": clean_incomplete}


def detail_lines(value, section: str) -> list[str]:
    """Render fixed Japanese labels and numeric counts only."""
    value = validated(value)
    if value is None or value[section]["state"] == "unknown":
        return ["収集の内訳を確認できません" if section == "errors" else "取得未完了の内訳を確認できません"]
    block = value[section]
    if section == "errors":
        details = "、".join(f"{ERROR_LABELS[k]}{n}件" for k, n in block["reasons"].items() if n)
        partial = "（先頭256件の内訳）" if block["state"] == "partial" else ""
        return [f"収集エラー{block['total']}件{partial}" + (f": {details}" if details else "")]
    details = "、".join(f"{FETCH_LABELS[k]}{n}件" for k, n in block["reasons"].items() if n)
    lines = [f"取得未完了{block['projects']}件" + (f": {details}" if details else "")]
    if block["parent_bodies"]:
        age = block["oldest_observed_age_s"]
        elapsed = f"初回観測から最長{age / 3600:.1f}時間" if age is not None else "経過時間不明"
        missing = f"、時刻不明{block['age_unknown']}件" if block["age_unknown"] else ""
        lines.append(f"未取得の親本文{block['parent_bodies']}件（{elapsed}{missing}）")
    return lines
