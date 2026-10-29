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
    "semantic_intake_unstored": "保存できず再取得を確認できない投稿",
    "other": "その他・理由未分類",
}
# Producer prefixes (collection_stages, job_ops, init_data, run_check) ahead of the code.
_CONTEXT = re.compile(
    r"^(?:(?:project|mark|thread|probe|reply|import|reconcile|attachment|attach|backfill|history|db)"
    r" [0-9]+(?:/[0-9]+)?(?: replies)?|import job"
    r"|list_unread|unread|self_probe|discovery|thread_read|reply|import|reconcile|attachments|history|metadata):\s*")
_CODE = re.compile(r"[A-Za-z_][A-Za-z_0-9]*")
_HTTP_STATUS = re.compile(r"^(?:http_error\(status=([0-9]{3})\)|http_([0-9]{3})\b)")

# category -> (label, legacy ERROR_LABELS bucket, transient, what happened, what to do).
# Fixed text only: producer details, ids and exception messages are never shown.
CATEGORIES = {
    "login": ("ログイン・認証", "authentication", False,
              "MCSへのログインが切れたか、認証に失敗しました",
              "MCSに手動でログインし直す（docs/guides/INSTALLATION.md の session_expired）"),
    "permission": ("閲覧権限", "authentication", False,
                   "MCSが閲覧を拒否しました（権限がない投稿・患者があります）",
                   "MCSの画面で対象の閲覧権限を確認"),
    "browser": ("ブラウザ接続の準備", "network", True,
                "MCSに接続するためのブラウザを準備できませんでした",
                "次回の収集で再試行されます。続く場合はChromeの起動とログイン状態を確認"),
    "rate_limit": ("取得制限", "rate_limit", True,
                   "MCSの取得制限にかかりました",
                   "対処不要。間隔をあけて自動で再試行します"),
    "deadline": ("時間切れ", "deadline", True,
                 "処理が制限時間内に終わりませんでした",
                 "次回の収集で続きから再試行されます。続く場合はネットワークとllama-serverの負荷を確認"),
    "deferred": ("時間内に終わらず先送り", "deadline", True,
                 "時間内に終わらなかった処理を次回に回しました（異常ではありません）",
                 "対処不要。次回以降に実行されます"),
    "network": ("通信", "network", True,
                "MCSとの通信に失敗しました",
                "次回の収集で再試行されます。続く場合はネットワーク接続を確認"),
    "server": ("MCS側の応答異常", "network", True,
               "MCSが異常な応答（エラー番号付き）を返しました",
               "次回の収集で再試行されます。続く場合はMCSの画面が開けるか確認"),
    "schema": ("応答形式", "schema", False,
               "MCSの応答が想定と違う形式でした（MCS側の仕様変更の可能性）",
               "続く場合は開発元へ連絡（python3 mcs/ops/mcs_setup.py doctor の結果を添える）"),
    "storage": ("保存", "storage", False,
                "取得した内容をこのMacに保存できませんでした",
                "ディスクの空き容量と data フォルダの書込み権限を確認"),
    "incomplete": ("取得未完了", "incomplete", True,
                   "一部の投稿・返信を取り切れませんでした",
                   "再試行待ちは次回以降に続きを取得します。window_stalledで停止した初回履歴は、"
                   "管理者がMCSの表示を確認し、既存の履歴取得操作で再開してください"
                   "（docs/guides/USER_GUIDE.md「新しく認識した患者の履歴」）"),
    "attachment": ("添付の取得", "incomplete", True,
                   "添付ファイルを取得できませんでした",
                   "次回の収集で再試行されます。続く場合はMCSの画面で添付が開けるか確認"),
    "auxiliary": ("補助情報の取得", "incomplete", True,
                  "職員一覧・自分の情報など補助的な情報を取得できませんでした",
                  "次回の収集で再試行されます。投稿の収集は続いています"),
    "command": ("操作の取込み", "command", False,
                "チャットからの操作・更新の指示の取込みや後片付けでエラーがありました",
                "操作が反映されたかを画面や受付の記録で確認し、反映されていないと確認できた場合だけやり直す"
                "（確認できない場合はやり直さず data/mcs.log の cmd を確認）"),
    "config": ("設定", "other", False,
               "設定に誤りがあり、一部の機能を使えません",
               "python3 mcs/ops/mcs_setup.py check で設定を確認"),
    "postprocess": ("収集後の処理", "other", True,
                    "要約・薬剤照合・集約などの収集後の処理が失敗しました（収集は完了しています）",
                    "次回の収集で再実行されます。続く場合は data/mcs.log を確認"),
    "runtime": ("実行基盤", "other", False,
                "収集の実行そのものに問題がありました（記録・ロック・異常終了など）",
                "python3 mcs/ops/mcs_setup.py check を実行。続く場合は data/mcs.log を確認"),
    "other": ("その他・原因未分類", "other", False,
              "分類できないエラーがありました",
              "続く場合は data/mcs.log を確認し、python3 mcs/ops/mcs_setup.py doctor の結果を開発元へ"),
}
# Every code the current producers emit after the prefix (see test_collection_diagnostics).
_CODES = {
    **dict.fromkeys(("session_expired", "auth_required", "login_failed", "unauthorized",
                     "no_token", "auto_login"), "login"),
    "forbidden": "permission",
    "bootstrap_error": "browser",
    **dict.fromkeys(("rate_limited", "rate_limit"), "rate_limit"),
    **dict.fromkeys(("deadline_exceeded", "timeout", "worker_timeout", "budget_exhausted"),
                    "deadline"),
    "deadline_deferred": "deferred",
    **dict.fromkeys(("http_error", "network_error", "connection_error", "worker_failed"),
                    "network"),
    **dict.fromkeys(("schema_error", "SchemaError", "invalid_payload", "bad_snapshot_ts"),
                    "schema"),
    **dict.fromkeys(("db_write_failed", "write_failed", "patient_write_failed", "disk_low",
                     "disk_full", "fs", "fs_OSError", "fs_PermissionError",
                     "fs_FileNotFoundError", "OperationalError", "health_write_failed",
                     "semantic_intake_unstored", "semantic_intake_diagnostic_unpersisted",
                     "semantic_intake_retry_unpersisted"),
                    "storage"),
    **dict.fromkeys(("parent_body_incomplete", "thread_incomplete", "replies_missing",
                     "unread_capped", "history_incomplete", "post_ack_gap", "window_stalled",
                     "pages_exceeded", "unread_cap_stuck", "mark_result_unknown",
                     "capacity_limit", "scope_changed", "reply_failed",
                     "reply_stored_for_other_patient"), "incomplete"),
    **dict.fromkeys(("download_failed", "url_not_allowed", "download_too_large",
                     "download_empty"), "attachment"),
    **dict.fromkeys(("self_profile_unavailable", "station_staffs_unavailable",
                     "reaction_request_failed", "self_profile"), "auxiliary"),
    **dict.fromkeys(("cmd_invalid", "update_spawn_failed", "consent_respawn_failed", "cmd",
                     "cmd_sweep", "restore_reconcile", "cmd_int", "cmd_int_tail", "stamps"),
                    "command"),
    "config": "config",
    **dict.fromkeys(("extract", "extract_llm", "drug_map", "rollup", "signals",
                     "semantic_init", "stall_check_failed"), "postprocess"),
    **dict.fromkeys(("run_lock_lost", "run_record_failed", "crash_site",
                     "snapshot_publish_failed", "code_changed"), "runtime"),
}


def classify(error) -> str:
    """The fixed category of one producer error (a string or a {"kind": ...} row)."""
    if isinstance(error, dict):
        error = error.get("kind")
    if not isinstance(error, str):
        return "other"
    text = _CONTEXT.sub("", error[:512], count=1)
    status = _HTTP_STATUS.match(text)
    if status:
        code = int(status.group(1) or status.group(2))
        return ("rate_limit" if code == 429 else "login" if code == 401
                else "permission" if code == 403 else "server")
    match = _CODE.match(text)
    code = match.group() if match else ""
    if code.startswith("coverage_stalled_"):
        return "incomplete"
    return _CODES.get(code, "other")


def error_counts(errors) -> dict:
    """Classify only bounded producer prefixes; never return their original text."""
    if not isinstance(errors, list):
        return {"state": "unknown", "total": None, "scanned": 0, "reasons": None}
    counts = dict.fromkeys(ERROR_LABELS, 0)
    categories = dict.fromkeys(CATEGORIES, 0)
    for error in errors[:MAX_ERRORS]:
        category = classify(error)
        categories[category] += 1
        counts[CATEGORIES[category][1]] += 1
    return {"state": "known" if len(errors) <= MAX_ERRORS else "partial",
            "total": len(errors), "scanned": min(len(errors), MAX_ERRORS), "reasons": counts,
            "categories": categories}


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


def validated(value, observed_at=None, *, fine=False) -> dict | None:
    """Whitelist persisted counts before doctor or monitoring can display them.

    fine=False keeps the legacy 9-reason shape that the support diagnostics DTO carries.
    """
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
        if "categories" in errors:      # absent in health written before 1.1.0
            categories = _reasons(errors["categories"], CATEGORIES, scanned)
            projected = dict.fromkeys(ERROR_LABELS, 0)
            for key, n in (categories or {}).items():
                projected[CATEGORIES[key][1]] += n
            if categories is None or projected != reasons:
                return None
            if fine:
                clean_errors["categories"] = categories
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
    value = validated(value, fine=True)
    if value is None or value[section]["state"] == "unknown":
        return ["収集の内訳を確認できません" if section == "errors" else "取得未完了の内訳を確認できません"]
    block = value[section]
    if section == "errors":
        details = ("、".join(f"{CATEGORIES[k][0]}{n}件" for k, n in block["categories"].items() if n)
                   if "categories" in block else
                   "、".join(f"{ERROR_LABELS[k]}{n}件" for k, n in block["reasons"].items() if n))
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


def guidance(value) -> list[tuple[str, int, str, str]] | None:
    """(label, count, what happened, what to do) per observed category, most first; None when unknown."""
    value = validated(value, fine=True)
    if value is None or "categories" not in value["errors"]:
        return None
    rows = [(CATEGORIES[k][0], n, CATEGORIES[k][3], CATEGORIES[k][4])
            for k, n in value["errors"]["categories"].items() if n]
    return sorted(rows, key=lambda row: -row[1])
