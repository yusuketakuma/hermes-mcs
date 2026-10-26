"""Report content-free cloud estimates and apply receipted human controls locally."""
from __future__ import annotations

from contextlib import contextmanager, closing
from datetime import date, datetime
from decimal import Decimal
import json
import math
import os
from pathlib import Path
import sqlite3
import time
from types import SimpleNamespace
import uuid

import cloud_policy as policy
import mcs_util
import semantic_jev as jev

READ_SECONDS = 5.0
READ_ROWS = 100000
READ_BYTES = 16 * 1024 * 1024


def pending_key_presence(root=None):
    """Preview only booleans for keys that init will save, never their values."""
    return {"jev": bool(os.environ.get("TYPESAFE_API_KEY") or jev.api_key(root)),
            "anthropic": bool(os.environ.get("MCS_ANTHROPIC_API_KEY") or policy.api_key(root))}


def _settings(cfg, root=None):
    key = bool(policy.api_key(root))
    # an unreadable config arrives as {}: fail closed like the runtime, never auto-enable
    settings, errors = policy.cloud_config(cfg, key_present=key and bool(cfg))
    return settings, errors, key


@contextmanager
def _reader(root=None):
    path = Path(root or mcs_util.HOME) / "data" / "ledger.db"
    if path.is_symlink() or not path.is_file():
        raise ValueError("ledger_unavailable")
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=.25)) as db:
        consumed_rows, consumed_bytes = 0, 0
        deadline = time.monotonic() + READ_SECONDS

        def bounded_row(cursor, values):
            nonlocal consumed_rows, consumed_bytes
            consumed_rows += 1
            consumed_bytes += sum(len(value) * 4 if isinstance(value, str) else len(value)
                                  if isinstance(value, bytes) else 8 for value in values)
            _checkpoint(deadline, consumed_rows)
            if consumed_bytes > READ_BYTES:
                raise ValueError("observation_budget_exceeded")
            return sqlite3.Row(cursor, values)

        db.row_factory = bounded_row
        db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 2 * 1024 * 1024)
        db.execute("PRAGMA query_only=ON")
        db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
        db.execute("BEGIN")
        yield db, deadline


def _checkpoint(deadline, rows=0):
    if time.monotonic() >= deadline or rows > READ_ROWS:
        raise ValueError("observation_budget_exceeded")


def _dryrun_report(db, deadline, now):
    fields = ("requests", "input_chars", "estimated_input_tokens", "estimated_output_tokens")
    start = now - 62 * 86400
    days = {}
    for n, row in enumerate(db.execute(
            "SELECT created_at,model,meta FROM artifacts WHERE kind='cloud_usage_dryrun' "
            "AND created_at>=? ORDER BY artifact_id LIMIT ?",
            (start, READ_ROWS + 1)), 1):
        _checkpoint(deadline, n)
        meta = mcs_util.loads_dict(row["meta"])
        if (not isinstance(meta, dict) or meta.get("route") not in policy.ROUTES
                or row["model"] != policy.MODEL
                or type(row["created_at"]) not in (int, float)
                or not math.isfinite(row["created_at"]) or not start <= row["created_at"] <= now
                or any(type(meta.get(k)) is not int or not 0 <= meta[k] <= 2**53 for k in fields)):
            raise ValueError("dryrun_observation_invalid")
        day = datetime.fromtimestamp(row["created_at"], policy.JST).date().isoformat()
        item = days.setdefault(day, {"day": day, "units": 0, **dict.fromkeys(fields, 0)})
        item["units"] += 1
        for field in fields:
            item[field] += meta[field]
    _checkpoint(deadline)
    return {"observed_at": now, "since": start, "timezone": "Asia/Tokyo",
            "input_estimate_basis": "character_proxy", "output_estimate_basis": "request_ceiling",
            "days": [days[day] for day in sorted(days)]}


def diagnostics(cfg, *, root=None, probe=False):
    """Return only effective modes, key presence, budgets and optional safe accounting."""
    cloud, errors, key = _settings(cfg, root)
    from semantic_policy import runtime_config
    semantic, sem_errors = runtime_config(cfg, root=root)
    jev_key = bool(jev.api_key(root))
    out = {
        "jev": {"mode": semantic["mode"], "key_present": jev_key,
                "status": "invalid" if sem_errors else "local" if not jev_key or semantic["mode"] == "off" else "configured",
                "daily_request_budget": semantic["daily_request_budget"],
                "scope": "all" if semantic["project_ids"] is None else "none" if not semantic["project_ids"] else "selected"},
        "anthropic": {"mode": cloud["mode"], "key_present": key,
                "status": "invalid" if errors else "local" if not key or cloud["mode"] in ("off", "dry_run") else "configured",
                "daily_request_budget": cloud["daily_request_budget"],
                "daily_cost_limit_usd": cloud["daily_cost_limit_usd"],
                "monthly_cost_limit_usd": cloud["monthly_cost_limit_usd"],
                "scope": "all" if cloud["project_ids"] is None else "none" if not cloud["project_ids"] else "selected",
                "price_version": policy.PRICE_VERSION, "price_valid": policy.pricing_valid(),
                "accounting": None, "dry_run": None, "block_reason": None}}
    eligible_scope = cloud["project_ids"] is None or bool(set(cloud["project_ids"]) - set(cloud["exclude_project_ids"]))
    if (not errors and not key and cloud["mode"] in ("on", "shadow")
            and cloud["routes"] and eligible_scope):
        # A plain doctor cannot observe the persisted pause without opening DB.
        out["anthropic"].update(status="warning" if probe else "local", block_reason="no_api_key")
    if probe:
        try:
            with _reader(root) as (db, deadline):
                borrowed = SimpleNamespace(db=db)
                out["anthropic"]["accounting"] = policy.usage_report(borrowed)
                out["anthropic"]["dry_run"] = _dryrun_report(db, deadline, time.time())
                reason = policy.global_block_reason(borrowed) or policy.budget_block_reason(borrowed, cloud)
                if reason == "paused":
                    out["anthropic"].update(status="local", block_reason=reason)
                elif reason:
                    out["anthropic"].update(status="blocked", block_reason=reason)
        except (OSError, ValueError, sqlite3.Error):
            out["anthropic"]["status"] = "unknown"
    return out


def _source_scope(settings, alias="m"):
    sql, params = "", []
    for key, exclude in (("project_ids", False), ("exclude_project_ids", True)):
        ids = settings[key]
        if ids is None or (exclude and not ids):
            continue
        if not ids:
            return " AND 0", []
        sql += f" AND {alias}.project_id {'NOT IN' if exclude else 'IN'} ({','.join('?' for _ in ids)})"
        params += ids
    return sql, params


def _route_counts(db, cfg, settings, deadline):
    out = {route: {"targets": 0, "requests_estimate": 0, "input_chars": 0,
                   "basic_requests_estimate": 0, "chunk_extra_requests_estimate": 0}
           for route in policy.ROUTES}
    if "patient_context" in settings["routes"]:
        import patient_context_refresh as context
        import extract_llm as extract
        # Resolve the semantic contract without consulting installation credentials.
        contract = context._contract({**cfg, "cloud_llm": {"mode": "off"}})
        if settings["mode"] == "on":
            contract["provider"] = extract._cloud_contract()
        scope, params = _source_scope(settings, "p")
        rows = db.execute("SELECT p.project_id FROM patients p WHERE COALESCE(p.is_archived,0)=0"
                          + scope + " ORDER BY p.project_id", params)
        for count, row in enumerate(rows, 1):
            _checkpoint(deadline, count)
            pending = context.pending_estimate(db, row["project_id"], cfg, now=time.time(), contract=contract)
            item = out["patient_context"]
            item["targets"] += pending["targets"]
            item["input_chars"] += pending["input_chars"]
            basic = pending["summary_units"] + pending["scan_sources"]
            chunks = pending["scan_chunks_estimate"]
            item["basic_requests_estimate"] += basic
            item["chunk_extra_requests_estimate"] += max(0, chunks - pending["scan_sources"])
            item["requests_estimate"] += pending["summary_units"] + chunks
    if "reextract" in settings["routes"]:
        import extract_llm as extract
        scope, params = _source_scope(settings)
        query = extract._selection_sql("DESC", scope, fresh=False).replace(
            "m.body_text,", "length(m.body_text) AS body_length,")
        now = time.time()
        rows = db.execute(query, (extract.KIND, extract.EXTRACT_VERSION, *params,
            None, None, None, now, extract.KIND, extract.EXTRACT_VERSION, now, READ_ROWS + 1))
        for count, row in enumerate(rows, 1):
            _checkpoint(deadline, count)
            if row["qc_src"] or row["thin_src"] or row["human_src"] or row["attempts"]:
                continue
            if db.execute(f"SELECT 1 FROM messages m WHERE m.message_id=? AND EXISTS ({extract._hold_sql()})",
                          (row["message_id"],)).fetchone():
                continue
            # Same source-bound prior as the worker, projecting metadata only.
            prior = db.execute("SELECT a.meta FROM artifacts a JOIN messages m ON a.message_id=m.message_id "
                f"WHERE a.kind=? AND m.message_id=? AND m.project_id=? AND m.content_hash=? {extract.current_extract_pred()} "
                "ORDER BY a.artifact_id DESC LIMIT 1", (extract.KIND, row["message_id"], row["project_id"], row["content_hash"])).fetchone()
            meta = mcs_util.loads_dict(prior[0]) if prior else None
            if not meta or type(meta.get("extract_version")) is not int:
                continue
            released = db.execute(f"SELECT 1 FROM messages m WHERE m.message_id=? "
                f"AND EXISTS ({extract._hold_sql(released=True)})", (row["message_id"],)).fetchone()
            if meta["extract_version"] >= extract.EXTRACT_VERSION and not released:
                continue
            item = out["reextract"]
            item["targets"] += 1
            item["input_chars"] += row["body_length"] or 0
            requests = max(1, math.ceil((row["body_length"] or 0) / extract._CHUNK_SIZE))
            item["basic_requests_estimate"] += 1
            item["chunk_extra_requests_estimate"] += requests - 1
            item["requests_estimate"] += requests
    return out


def _calibration(db, since):
    total_chars, total_tokens, output_tokens, samples = 0, 0, 0, 0
    reserves, seen = {}, set()
    for n, row in enumerate(db.execute("SELECT kind,meta,model,project_id,message_id FROM artifacts WHERE kind IN "
            "('cloud_usage_reserve','cloud_usage_result') AND created_at>=? ORDER BY artifact_id LIMIT ?",
            (since, READ_ROWS + 1)), 1):
        if n > READ_ROWS:
            raise ValueError("observation_budget_exceeded")
        meta = mcs_util.loads_dict(row["meta"])
        if not isinstance(meta, dict) or not isinstance(meta.get("reservation_id"), str):
            return None
        rid = meta["reservation_id"]
        if row["kind"] == "cloud_usage_reserve":
            if rid in reserves:
                return None
            policy._reserve_valid(meta, row)
            reserves[rid] = meta
        else:
            if rid in seen:
                return None
            seen.add(rid)
            reserve = reserves.get(rid)
            usage = meta.get("usage")
            if not reserve or usage is None:
                continue
            if (policy._binding(reserve) != policy._binding(meta)
                    or row["model"] != reserve["model"] or row["project_id"] != reserve["project_id"]
                    or row["message_id"] != reserve.get("message_id")):
                return None
            usage = policy.validate_usage(usage)
            if (policy.usage_cost(usage) != policy._decimal(meta.get("cost_usd"))
                    or policy.usage_cost(usage) > policy._decimal(reserve["max_cost_usd"])):
                return None
            chars = reserve.get("input_chars")
            if type(chars) is int and 0 < chars <= 2**53:
                total_chars += chars
                total_tokens += usage["input_tokens"] + usage["cache_creation_input_tokens"] + usage["cache_read_input_tokens"]
                output_tokens += usage["output_tokens"]
                samples += 1
    return {"settled_requests_with_characters": samples, "input_chars": total_chars,
            "input_tokens": total_tokens, "output_tokens": output_tokens,
            "tokens_per_char": Decimal(total_tokens) / total_chars if total_chars else None,
            "output_tokens_per_request": Decimal(output_tokens) / samples if samples else None}


def _costs(item, ratio, fx=None, *, output_limit=1600, output_per_request=None):
    tokens = int((Decimal(item["input_chars"]) * ratio).to_integral_value(rounding="ROUND_CEILING"))
    ceiling = item["requests_estimate"] * output_limit
    output = (min(ceiling, math.ceil(item["requests_estimate"] * output_per_request))
              if output_per_request is not None else None)
    low_output, high_output = (output, output) if output is not None else (0, ceiling)
    item.update(estimated_input_tokens=tokens, estimated_output_tokens=output,
        output_token_request_ceiling=ceiling,
        output_estimate_basis="observed_usage_mean" if output is not None else "unmeasured",
        estimated_cost_low_usd=str((tokens * Decimal(policy.PRICES["input"]) + low_output * Decimal(policy.PRICES["output"])) / 1000000),
        estimated_cost_high_usd=str((tokens * Decimal(policy.PRICES["long_input"]) + high_output * Decimal(policy.PRICES["long_output"])) / 1000000))
    if fx is not None:
        for bound in ("low", "high"):
            item[f"estimated_cost_{bound}_jpy"] = str(Decimal(item[f"estimated_cost_{bound}_usd"]) * Decimal(str(fx)))
    return item


def _safe_log(row, meta):
    result = {"kind": row["kind"], "at": row["created_at"]}
    if row["kind"] == "cloud_usage_disposition":
        if meta.get("disposition") in ("discarded_scope", "discarded_workday"):
            result["disposition"] = meta["disposition"]
        return result
    for key, allowed in (("route", policy.ROUTES), ("model", (policy.MODEL,)),
                         ("disposition", ("discarded_scope", "discarded_workday"))):
        if meta.get(key) in allowed:
            result[key] = meta[key]
    kinds = {"auth_error", "payment_required", "pricing_unverified", "budget_exceeded", "no_api_key",
             "rate_limited", "transport", "timeout", "contract_error", "protocol_error", "model_mismatch"}
    if isinstance(meta.get("error_kind"), str) and meta["error_kind"] in kinds:
        result["error_kind"] = meta["error_kind"]
    http = meta.get("http_class")
    if http in ("transport", "timeout") or (isinstance(http, str) and len(http) == 8
                                              and http.startswith("http_") and http[5:].isdigit()):
        result["http_class"] = http
    for key in ("cost_usd", "max_cost_usd"):
        if key in meta:
            result[key] = str(policy._decimal(meta[key]))
    if isinstance(meta.get("usage"), dict):
        result["usage"] = policy.validate_usage(meta["usage"])
    for key in ("requests", "input_chars", "estimated_input_tokens", "estimated_output_tokens",
                "local_facts", "cloud_facts", "matched", "rejected", "held", "repairs",
                "version", "units", "validated", "validated_units", "rejected_units",
                "rejection_observations", "held_observations", "repaired_units", "repair_observations"):
        value = meta.get(key)
        if type(value) is int and 0 <= value <= 2**53:
            result[key] = value
    return result


def _comparison_report(log):
    fields = ("local_facts", "cloud_facts", "matched", "validated", "rejected", "held", "repairs")
    totals = dict.fromkeys(fields, 0)
    observed = dict.fromkeys(fields, 0)
    units = validated_units = legacy = matched_local = matched_cloud = matched_total = 0
    rates = {key: [0, 0] for key in ("rejection", "held", "repair")}
    for row in log:
        if row["kind"] != "cloud_compare_v1":
            continue
        for key in fields:
            if key in row:
                totals[key] += row[key]
                observed[key] += 1
        if all(key in row for key in ("local_facts", "cloud_facts", "matched")):
            if row["matched"] > min(row["local_facts"], row["cloud_facts"]):
                raise ValueError("comparison_invalid")
            matched_local += row["local_facts"]
            matched_cloud += row["cloud_facts"]
            matched_total += row["matched"]
        if row.get("version") != 2:
            legacy += 1
            continue
        if row.get("units") != 1 or row.get("validated_units") not in (0, 1):
            raise ValueError("comparison_invalid")
        units += 1
        validated_units += row["validated_units"]
        for label, numerator, denominator in (("rejection", "rejected_units", "rejection_observations"),
                ("held", "held", "held_observations"), ("repair", "repaired_units", "repair_observations")):
            count = row.get(denominator)
            value = row.get(numerator)
            if count not in (0, 1) or (count and value not in (0, 1)):
                raise ValueError("comparison_invalid")
            rates[label][1] += count
            if count:
                rates[label][0] += value
    return {**{key: totals[key] if observed[key] else None for key in fields},
            "units": units, "validated_units": validated_units, "unvalidated_units": units - validated_units,
            "legacy_records_without_unit_denominators": legacy,
            "metric_records_observed": observed,
            "local_recall": matched_total / matched_local if matched_local else None,
            "local_recall_fact_denominator": matched_local,
            "cloud_only": matched_cloud - matched_total if observed["matched"] else None,
            **{label + "_rate": numerator / denominator if denominator else None
               for label, (numerator, denominator) in rates.items()},
            "rate_denominators": {label: pair[1] for label, pair in rates.items()},
            "rate_numerators": {label: pair[0] for label, pair in rates.items()}}


def observe(cfg, *, root=None, days=14, since=None, project=None):
    """Compute a single bounded read snapshot without sending or persisting anything."""
    if type(days) is not int or not 1 <= days <= 62:
        raise ValueError("days_invalid")
    settings, errors, key = _settings(cfg, root)
    if errors:
        raise ValueError("cloud_config_invalid")
    # Keyless planning can still estimate the scope that auto mode would enable.
    planning = policy.cloud_config(cfg, key_present=True)[0]
    now = time.time()
    start = now - days * 86400
    if since is not None:
        start = datetime.combine(date.fromisoformat(since), datetime.min.time(), policy.JST).timestamp()
    if project is not None:
        if type(project) is not int or project <= 0:
            raise ValueError("project_invalid")
    with _reader(root) as (db, deadline):
        borrowed = SimpleNamespace(db=db)
        accounting = policy.usage_report(borrowed)
        reason = policy.global_block_reason(borrowed) or policy.budget_block_reason(borrowed, settings)
        calibration = _calibration(db, now - 62 * 86400) if not accounting["pricing_unverified"] else None
        routes = _route_counts(db, cfg, planning, deadline)
        counts = {}
        scope, params = _source_scope(planning)
        for n, row in enumerate(db.execute("SELECT CAST((m.posted_at_ts+32400)/86400 AS INTEGER) AS day,"
            "COUNT(*) AS posts,COUNT(DISTINCT m.project_id) AS patients,COALESCE(SUM(length(m.body_text)),0) AS chars,"
            "COALESCE(SUM(MAX(1,(COALESCE(length(m.body_text),0)+2999)/3000)),0) AS scan_requests "
            "FROM messages m WHERE m.posted_at_ts>=? AND m.posted_at_ts<=?" + scope + " GROUP BY day",
            (start, now, *params)), 1):
            _checkpoint(deadline, n)
            counts[row["day"]] = dict(row)
        log = []
        sql, args = "created_at>=?", [start]
        if project is not None:
            sql += " AND project_id=?"
            args.append(project)
        for n, row in enumerate(db.execute("SELECT kind,created_at,meta FROM artifacts WHERE kind IN "
                "('cloud_usage_reserve','cloud_usage_result','cloud_usage_dryrun','cloud_usage_disposition','cloud_compare_v1') AND "
                + sql + " ORDER BY artifact_id LIMIT ?", (*args, READ_ROWS + 1)), 1):
            _checkpoint(deadline, n)
            meta = mcs_util.loads_dict(row["meta"])
            if not isinstance(meta, dict):
                continue
            # Explicit projection: IDs, hashes, owner data and content never leave this query.
            log.append(_safe_log(row, meta))
        _checkpoint(deadline)
    calibrated = bool(calibration and calibration["tokens_per_char"] is not None)
    ratio = calibration["tokens_per_char"] if calibrated else Decimal(1) / 3
    output_mean = calibration["output_tokens_per_request"] if calibration else None
    import extract_llm as extract
    for route, item in routes.items():
        _costs(item, ratio, settings["usd_jpy"], output_limit=extract.MAX_TOKENS if route == "reextract" else 1600,
               output_per_request=output_mean)
    total_chars = sum(row["chars"] for row in counts.values())
    total_requests = sum(row["patients"] + row["scan_requests"] for row in counts.values())
    total_basic = sum(row["patients"] + row["posts"] for row in counts.values())
    if "patient_context" not in planning["routes"]:
        total_chars, total_requests, total_basic = 0, 0, 0
    recurring = {}
    for label, multiplier in (("daily", Decimal(1) / days), ("monthly_30_days", Decimal(30) / days)):
        recurring[label] = _costs({"input_chars": math.ceil(total_chars * multiplier),
            "requests_estimate": math.ceil(total_requests * multiplier),
            "basic_requests_estimate": math.ceil(total_basic * multiplier),
            "chunk_extra_requests_estimate": math.ceil(total_requests * multiplier) - math.ceil(total_basic * multiplier)},
            ratio, settings["usd_jpy"],
            output_per_request=output_mean)
    peak = max(max((row["patients"] + row["scan_requests"] for row in counts.values()), default=0),
               routes["reextract"]["requests_estimate"])
    decision = ("off" if settings["mode"] == "off" else "dry_run" if settings["mode"] == "dry_run"
                else "no_api_key" if not key else reason or
                ("eligible" if any(item["targets"] for item in routes.values()) else "no_targets"))
    return {"mode": settings["mode"], "key_present": key, "send_decision": decision, "routes": routes,
        "patient_context_recurring_estimate": recurring, "reextract_backlog_once": routes["reextract"],
        "new_posts_daily_estimate": sum(row["posts"] for row in counts.values()) / days,
        "additional_runtime_checks": "source_claim_deadline_and_current_config",
        "daily_request_budget": settings["daily_request_budget"],
        "daily_cost_limit_usd": settings["daily_cost_limit_usd"],
        "monthly_cost_limit_usd": settings["monthly_cost_limit_usd"],
        "max_concurrency_per_process": settings["max_concurrency"], "accounting": accounting,
        "days": days, "history_days_observed": len(counts),
        "new_posts": sum(row["posts"] for row in counts.values()),
        "daily_patient_context_requests_estimate": sum(row["patients"] for row in counts.values()) / days,
        "monthly_patient_context_requests_estimate": sum(row["patients"] for row in counts.values()) * 30 / days,
        "recommended_daily_request_budget": math.ceil(peak * 1.2),
        "input_estimate_basis": "usage_calibrated_character_proxy" if calibrated else "character_proxy",
        "patient_context_basis": "runtime_pending_source_contract",
        "recurring_basis": "stored_message_timestamp_proxy",
        "historical_collection_coverage": "unverified",
        "historical_average_denominator_days": days,
        "input_character_unit": "unicode_characters_not_tokens",
        "usage_calibration": {**{key: value for key, value in (calibration or {}).items()
            if key not in ("tokens_per_char", "output_tokens_per_request")},
            "status": "measured" if calibrated else "unmeasured",
            "scope": "pooled_routes_last_62_days",
            "tokens_per_character": str(ratio) if calibrated else None,
            "output_tokens_per_request": str(output_mean) if output_mean is not None else None},
        "shadow_comparison": _comparison_report(log),
        "estimate_components": {
            "current_backlog": routes,
            "recurring": recurring,
            "chunk_repair_retry_additions": {
                "chunk_requests_current": sum(item["chunk_extra_requests_estimate"] for item in routes.values()),
                "chunk_requests_recurring": {period: item["chunk_extra_requests_estimate"]
                                             for period, item in recurring.items()},
                "chunk_counts_included_in_current_and_recurring": True,
                "repair_requests_estimate": None, "retry_requests_estimate": None,
                "basis": "chunk_character_proxy_repair_retry_unmeasured"},
            "shadow_additions": {"enabled": settings["mode"] == "shadow",
                "cloud_work_current": routes if settings["mode"] == "shadow" else None,
                "cloud_work_recurring": recurring if settings["mode"] == "shadow" else None,
                "basis": "additional_to_local_execution" if settings["mode"] == "shadow" else "disabled"}},
        "estimate_excludes": ["unmeasured_repair_retry", "prompt_overhead_and_added_context"],
        "cost_basis": "reference_range_not_budget_reservation_or_invoice",
        "reservation_input_upper_bound_tokens": policy.INPUT_RESERVATION_BOUND,
        "price_version": policy.PRICE_VERSION, "price_checked_on": policy.PRICE_CHECKED_ON,
        "price_valid_until": policy.PRICE_VALID_UNTIL, "price_valid": policy.pricing_valid(),
        "usd_jpy": settings["usd_jpy"], "log": log}


def apply_control_tx(db, req, now):
    """Write global control/circuit and its receipt in the caller's existing transaction."""
    if req["cmd"] == "ops.cloud_resume":
        from mcs_config import validate_config
        from safe_io import read_regular_nofollow
        try:
            raw = read_regular_nofollow(Path(mcs_util.HOME) / "config.json", mcs_util.CONFIG_MAX_BYTES, ValueError)
            if len(raw) > mcs_util.CONFIG_MAX_BYTES:
                raise ValueError
            cfg = mcs_util.parse_config(raw)
            if not isinstance(cfg, dict):
                raise ValueError
            errors, _ = validate_config(cfg)
        except (OSError, ValueError, UnicodeError, RecursionError):
            return "cloud_config_invalid", {}
        key = policy.api_key()
        _, cloud_errors = policy.cloud_config(cfg, key_present=bool(key))
        if errors or cloud_errors:
            return "cloud_config_invalid", {}
        if not key:
            return "no_api_key", {}
        if not policy.pricing_valid() or policy.usage_report(SimpleNamespace(db=db))["pricing_unverified"]:
            return "pricing_unverified", {}
        state = {"consecutive_failures": 0, "open_until": 0.0,
                 "failure_class": None, "updated_at": now}
        db.execute("INSERT INTO artifacts(kind,content,model,meta,created_at) VALUES(?,?,?,?,?)",
            ("cloud_circuit", json.dumps(state), policy.MODEL, '{"state":"closed"}', now))
    content = "paused" if req["cmd"] == "ops.cloud_stop" else "running"
    meta = {"command_id": req["command_id"], "actor": req["actor"], "reason": req["reason"]}
    db.execute("INSERT INTO artifacts(kind,content,model,meta,created_at) VALUES(?,?,?,?,?)",
        ("cloud_control", content, policy.MODEL, json.dumps(meta, ensure_ascii=False), now))
    return None, {"reason": req["reason"], "control": content}


def command(args):
    action = args.cloud_action
    cfg = mcs_util.load_config()
    try:
        if action in ("stop", "resume"):
            from writer_lock import acquire_run_lock, wait_run_lock, code_stamp
            expected_code = code_stamp()
            if not args.confirm_human or not mcs_util.valid_text(args.reason, 2000):
                print("--confirm-human と --reason による人の確認が必要です")
                return 1
            from ledger import Ledger
            import mcs_requests
            req = {"version": 1, "cmd": "ops.cloud_" + action,
                "command_id": args.command_id or str(uuid.uuid4()), "actor": args.actor,
                "human_confirmed": True, "project_id": None, "reason": args.reason}
            # Validate before opening a writer; malformed CLI input never creates a ledger.
            error = mcs_requests.validate(req)
            if error:
                print("クラウド操作: 指定を確認してください（" + error + "）")
                return 1
            path = Path(mcs_util.HOME) / "data" / "ledger.db"
            if path.is_symlink() or not path.is_file():
                raise ValueError("ledger_unavailable")
            lock_fd = acquire_run_lock(str(path.parent / "run.lock"))
            if lock_fd is None:
                lock_fd = wait_run_lock(str(path.parent / "run.lock"), wait_s=5.0,
                    expected_code_stamp=expected_code,
                    marker_path=str(path.parent / "update_in_progress.marker"))
            if lock_fd is None:
                print("クラウド操作: ほかの保存処理が終わってから再試行してください")
                return 3
            try:
                if (path.parent / "update_in_progress.marker").exists() or code_stamp() != expected_code:
                    raise ValueError("update_in_progress")
                with closing(Ledger(str(path))) as ledger:
                    receipt = mcs_requests.apply_command(ledger, req)
            finally:
                os.close(lock_fd)
            print(json.dumps({k: receipt.get(k) for k in ("command_id", "outcome", "error", "control")}, ensure_ascii=False))
            return 0 if receipt["outcome"] == "applied" else 1
        report = observe(cfg, days=getattr(args, "days", 14), since=getattr(args, "since", None), project=getattr(args, "project", None))
        if action == "log":
            result = {"entries": report["log"]}
        elif action == "compare":
            result = _comparison_report(report["log"])
        else:
            result = {k: v for k, v in report.items() if k != "log"}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        print("件数・文字数からの試算です。Unicode文字数は実トークン数ではなく、修復・再試行・追加文脈の量は確定していません。")
        return 0
    except (OSError, ValueError, sqlite3.Error, policy.AnthropicError):
        print("クラウド処理: 設定または保存記録を確認できません。本文や認証情報は表示していません")
        return 1


_BUDGET_KEYS = {"daily_usd": "daily_cost_limit_usd", "monthly_usd": "monthly_cost_limit_usd"}


def _budget_value(text):
    """A dollar amount >= 0, or none/off to remove that cap."""
    if text.strip().lower() in ("none", "off", "null"):
        return None
    value = float(text)
    if not math.isfinite(value) or value < 0:
        raise ValueError
    return value


def budget_command(args):
    """Show or set the Anthropic daily/monthly cost caps in config.json."""
    try:
        changes = {}
        for option, key in _BUDGET_KEYS.items():
            text = getattr(args, option, None)
            if text is not None:
                changes[key] = _budget_value(text)
    except ValueError:
        print("予算: 金額はドル単位の0以上の数値、または none で指定してください")
        return 1
    import config_edit
    try:
        if changes:
            cfg, cloud_errors = config_edit.apply({"cloud_llm." + k: v for k, v in changes.items()})
            if cloud_errors:
                print("設定に誤りがあるため予算を保存していません。mcs_setup.py check で確認してください")
                return 1
        else:
            cfg = config_edit.read_strict()
            from mcs_config import validate_config
            if validate_config(cfg)[0]:
                print("設定に誤りがあるため予算を表示していません。mcs_setup.py check で確認してください")
                return 1
    except config_edit.ConfigConflict:
        print("予算: ほかの設定保存と競合したため変更していません。設定を再確認して再試行してください")
        return 1
    except FileNotFoundError:
        print("予算: 設定ファイルがないため変更していません（先に mcs_setup.py init で設定してください）")
        return 1
    except (OSError, ValueError, RecursionError):
        print("予算: 設定ファイルを読み取れないため変更していません。mcs_setup.py check で確認してください")
        return 1
    settings, errors = policy.cloud_config(cfg)
    if errors:
        print("設定に誤りがあるため予算を表示していません。mcs_setup.py check で確認してください")
        return 1
    print(json.dumps({"daily_cost_limit_usd": settings["daily_cost_limit_usd"],
                      "monthly_cost_limit_usd": settings["monthly_cost_limit_usd"],
                      "saved": bool(changes)}, ensure_ascii=False))
    return 0


def add_parsers(sub):
    p = sub.add_parser("cloud-llm", help="外部AIの件数・費用を送信せずに試算、照会します")
    actions = p.add_subparsers(dest="cloud_action", required=True)
    for action in ("plan", "estimate", "log", "compare"):
        q = actions.add_parser(action)
        q.add_argument("--days", type=int, default=14)
        q.add_argument("--since", help="日本時間の日付 YYYY-MM-DD")
        if action == "log":
            q.add_argument("--project", type=int)
        q.set_defaults(fn=command)
    q = actions.add_parser("budget", help="Anthropicの1日・1か月の費用上限（ドル）を表示・設定します")
    q.add_argument("--daily-usd", dest="daily_usd", help="1日の上限（ドル）。none で解除")
    q.add_argument("--monthly-usd", dest="monthly_usd", help="1か月の上限（ドル）。none で解除")
    q.set_defaults(fn=budget_command, cloud_action="budget")
    for action in ("stop", "resume"):
        q = actions.add_parser(action, help="人の確認と記録を伴うクラウド処理の" + ("停止" if action == "stop" else "再開"))
        q.add_argument("--confirm-human", action="store_true")
        q.add_argument("--reason", required=True)
        q.add_argument("--actor", default="local-human")
        q.add_argument("--command-id", help="同じ操作を再試行する場合の承認記録UUID")
        q.set_defaults(fn=command, cloud_action=action)
