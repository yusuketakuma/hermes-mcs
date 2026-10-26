"""Validate cloud scope and reserve bounded Anthropic costs on the ledger owner thread."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import json
import re
import time
import uuid

from anthropic_client import AnthropicError, MODEL, MAX_TOKENS, validate_usage
from mcs_util import private_env_value, payload_hash

PRICE_VERSION = "anthropic-haiku-5-5-2026-10-26"
PRICE_CHECKED_ON = "2026-10-26"
# Local 30-day recheck policy, not a provider price guarantee.
PRICE_VALID_UNTIL = "2026-11-25"
PRICE_SOURCES = (
    "https://platform.claude.com/docs/en/models/haiku-5-5/overview",
    "https://platform.claude.com/docs/en/models/overview",
    "https://platform.claude.com/docs/en/about-claude/pricing",
)
# Conservative bound covering decimal/binary interpretations of the public 1M label.
# This is a reservation bound, not an advertised accepted input capacity.
INPUT_RESERVATION_BOUND = 1048576
PRICES = {"input": "0.1", "output": "0.5", "long_input": "0.5", "long_output": "2.5",
          "cache_write_5m_multiplier": "1.25", "cache_write_1h_multiplier": "2",
          "cache_read_multiplier": "0.1"}
LONG_INPUT_THRESHOLD = 100000
JST = timezone(timedelta(hours=9))
KINDS = ("cloud_usage_reserve", "cloud_usage_result", "cloud_usage_dryrun")
ROUTES = ("patient_context", "reextract")


def _decimal(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError("cost_invalid")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError("cost_invalid") from None
    if not result.is_finite() or result < 0 or result > Decimal("1e15"):
        raise ValueError("cost_invalid")
    return result


def cloud_config(cfg, *, key_present=False):
    """Normalize the optional cloud block, failing closed on every config error."""
    out = {"mode": "off", "routes": list(ROUTES), "project_ids": [],
           "exclude_project_ids": [], "model": MODEL, "daily_request_budget": None,
           "max_concurrency": 4, "monthly_cost_limit_usd": None, "usd_jpy": None}
    errors = []
    block = cfg.get("cloud_llm") if isinstance(cfg, dict) else None
    if block is None:
        block = {}
    if not isinstance(block, dict):
        return out, ["config: cloud_llm_not_object"]
    if block.keys() - out.keys():
        errors.append("config: cloud_llm_unknown_field")
    mode = block.get("mode", "on" if key_present is True else "off")
    if mode not in ("off", "dry_run", "shadow", "on"):
        errors.append("config: cloud_llm_mode_invalid")
    else:
        out["mode"] = mode
    if key_present is True and "mode" not in block and "project_ids" not in block:
        out["project_ids"] = None
    if "mode" in block and mode != "off" and "project_ids" not in block:
        errors.append("config: cloud_llm_project_scope_required")
    from mcs_config import project_scope
    for key in ("project_ids", "exclude_project_ids"):
        if key not in block:
            continue
        try:
            out[key] = project_scope(block[key])
            if key == "exclude_project_ids" and out[key] is None:
                raise ValueError
        except ValueError:
            errors.append(f"config: cloud_llm_{key}_invalid")
            out[key] = []
    if block.get("model", MODEL) != MODEL:
        errors.append("config: cloud_llm_model_invalid")
    routes = block.get("routes", list(ROUTES))
    if (not isinstance(routes, list)
            or any(not isinstance(r, str) or r not in ROUTES for r in routes)):
        errors.append("config: cloud_llm_routes_invalid")
    else:
        out["routes"] = sorted(set(routes))
    for key, low, high in (("daily_request_budget", 0, 100000), ("max_concurrency", 1, 32)):
        value = block.get(key, out[key])
        if key == "daily_request_budget" and value is None:
            continue
        if type(value) is not int or not low <= value <= high:
            errors.append(f"config: cloud_llm_{key}_invalid")
        else:
            out[key] = value
    for key in ("monthly_cost_limit_usd", "usd_jpy"):
        value = block.get(key)
        if value is None:
            continue
        try:
            if type(value) not in (int, float) or (key == "usd_jpy" and value <= 0):
                raise ValueError
            _decimal(value)
            out[key] = value
        except (ValueError, OverflowError):
            errors.append(f"config: cloud_llm_{key}_invalid")
    if errors:
        out["mode"] = "off"
    return out, errors


def _cfg(cfg):
    return cloud_config(cfg)[0] if "cloud_llm" in cfg else cloud_config({"cloud_llm": cfg})[0]


def config_generation(cfg):
    return payload_hash(_cfg(cfg))


def scope_generation(cfg):
    """The settings that decide whether a unit may use the cloud at all; caps,
    rate and currency changes must not discard an in-flight paid result."""
    c = _cfg(cfg)
    return payload_hash({k: c[k] for k in ("mode", "routes", "project_ids", "exclude_project_ids", "model")})


def api_key(root=None):
    """Read only this MCS installation's dotenv, never process or Hermes keys."""
    return private_env_value("MCS_ANTHROPIC_API_KEY", root)


def eligible(cfg, route, project_id):
    """Evaluate explicit route/scope only; callers separately check key presence."""
    c = _cfg(cfg)
    return (c["mode"] != "off" and route in c["routes"]
            and type(project_id) is int and project_id > 0
            and (c["project_ids"] is None or project_id in c["project_ids"])
            and project_id not in c["exclude_project_ids"])


def pricing_valid(now=None):
    day = datetime.fromtimestamp(time.time() if now is None else now, JST).date().isoformat()
    return PRICE_CHECKED_ON <= day <= PRICE_VALID_UNTIL


def max_cost(max_tokens, *, now=None):
    if not pricing_valid(now) or type(max_tokens) is not int or not 1 <= max_tokens <= MAX_TOKENS:
        raise AnthropicError("pricing_unverified", "price_or_limit_invalid")
    return (Decimal(INPUT_RESERVATION_BOUND) * Decimal(PRICES["long_input"])
            + Decimal(max_tokens) * Decimal(PRICES["long_output"])) / Decimal(1000000)


def usage_cost(usage):
    u = validate_usage(usage)
    total_input = u["input_tokens"] + u["cache_creation_input_tokens"] + u["cache_read_input_tokens"]
    if total_input > INPUT_RESERVATION_BOUND or u["output_tokens"] > MAX_TOKENS:
        raise AnthropicError("pricing_unverified", "usage_bounds")
    long = total_input > LONG_INPUT_THRESHOLD
    input_price, output_price = (Decimal(PRICES["long_input"]), Decimal(PRICES["long_output"])) if long else (Decimal(PRICES["input"]), Decimal(PRICES["output"]))
    cache = u.get("cache_creation", {})
    cost = (u["input_tokens"] * input_price + u["output_tokens"] * output_price
            + cache.get("ephemeral_5m_input_tokens", 0) * input_price * Decimal(PRICES["cache_write_5m_multiplier"])
            + cache.get("ephemeral_1h_input_tokens", 0) * input_price * Decimal(PRICES["cache_write_1h_multiplier"])
            + u["cache_read_input_tokens"] * input_price * Decimal(PRICES["cache_read_multiplier"]))
    return cost / Decimal(1000000)


def _unbilled(http_class):
    """Attempts the API refused before any generation: never sent, or a 4xx/529
    rejection. 500/504 and transport/timeout keep the reserved maximum, since the
    published error list does not say whether processing had begun."""
    if http_class == "not_sent":
        return True
    m = re.fullmatch(r"http_(\d{3})", http_class or "")
    return bool(m) and (400 <= int(m.group(1)) <= 429 or m.group(1) == "529")


def _periods(now):
    date = datetime.fromtimestamp(now, JST)
    day = date.replace(hour=0, minute=0, second=0, microsecond=0)
    return day.timestamp(), day.replace(day=1).timestamp()


def _loads(value):
    try:
        result = json.loads(value)
        if not isinstance(result, dict):
            raise ValueError
        return result
    except (ValueError, TypeError, RecursionError):
        raise AnthropicError("pricing_unverified", "usage_invalid") from None


def _binding(meta):
    return tuple(meta.get(k) for k in ("reservation_id", "request_fp", "route", "project_id",
                                      "unit_key", "message_id", "model", "price_version"))


def _reserve_valid(meta, row):
    if (not isinstance(meta.get("reservation_id"), str) or len(meta["reservation_id"]) != 32
            or not isinstance(meta.get("request_fp"), str) or len(meta["request_fp"]) != 64
            or meta.get("model") != MODEL or row["model"] != MODEL
            or meta.get("price_version") != PRICE_VERSION
            or meta.get("route") not in ROUTES
            or type(meta.get("project_id")) is not int or meta["project_id"] <= 0
            or meta["project_id"] != row["project_id"]
            or meta.get("message_id") != row["message_id"]
            or not isinstance(meta.get("unit_key"), str) or not meta["unit_key"]):
        raise AnthropicError("pricing_unverified", "reservation_invalid")
    tokens = meta.get("max_tokens")
    maximum = _decimal(meta.get("max_cost_usd"))
    if (type(tokens) is not int or not 1 <= tokens <= MAX_TOKENS
            or maximum != (Decimal(INPUT_RESERVATION_BOUND) * Decimal(PRICES["long_input"])
                           + Decimal(tokens) * Decimal(PRICES["long_output"])) / Decimal(1000000)):
        raise AnthropicError("pricing_unverified", "reservation_bound_invalid")
    return maximum


def _matching_rows_many(ledger, kind, reservation_ids, *, now=None):
    """Select retained reservation identities in SQL, preserving every duplicate match."""
    ids = list(dict.fromkeys(reservation_ids))
    cutoff = (time.time() if now is None else now) - 62 * 86400
    rows = []
    if ids and ledger.db.execute(
            "SELECT 1 FROM artifacts WHERE kind=? AND created_at>=? AND "
            "CASE WHEN json_valid(meta) THEN json_type(meta)!='object' ELSE 1 END LIMIT 1",
            (kind, cutoff)).fetchone():
        raise AnthropicError("pricing_unverified", "usage_invalid")
    for offset in range(0, len(ids), 900):
        group = ids[offset:offset + 900]
        rows.extend(ledger.db.execute(
            "SELECT artifact_id,kind,project_id,message_id,model,meta,created_at FROM artifacts "
            "WHERE kind=? AND created_at>=? AND CASE WHEN json_valid(meta) "
            "THEN json_extract(meta,'$.reservation_id') END IN ("
            + ','.join('?' for _ in group) + ') ORDER BY artifact_id',
            (kind, cutoff, *group)))
    return rows


def _matching_rows(ledger, kind, reservation_id):
    return _matching_rows_many(ledger, kind, [reservation_id])


def usage_report(ledger, *, now=None, period="month"):
    """Aggregate JST attempts; ambiguous results retain the full reserved amount.
    ``period="day"`` validates and counts only today's rows (any newly written
    result is today's); its cost fields then cover today only."""
    now = time.time() if now is None else now
    day, month = _periods(now)
    rows = list(ledger.db.execute(
        "SELECT artifact_id,kind,project_id,message_id,model,meta,created_at FROM artifacts "
        "WHERE kind IN ('cloud_usage_reserve','cloud_usage_result') "
        "AND created_at>=? ORDER BY artifact_id", (day if period == "day" else month,)))
    reserves, results = {}, {}
    daily, invalid = 0, False
    total, settled, unknown = Decimal(0), Decimal(0), 0
    try:
        for row in rows:
            m = _loads(row["meta"])
            rid = m.get("reservation_id")
            if row["kind"] == "cloud_usage_reserve":
                maximum = _reserve_valid(m, row)
                if rid in reserves:
                    raise AnthropicError("pricing_unverified", "duplicate_reservation")
                reserves[rid] = (m, maximum)
                daily += int(row["created_at"] >= day)
            else:
                if not isinstance(rid, str):
                    raise AnthropicError("pricing_unverified", "result_invalid")
                results.setdefault(rid, []).append((m, row))
        for rid, (m, maximum) in reserves.items():
            cost = maximum
            valid_usage = False
            matching = results.get(rid, [])
            if matching:
                try:
                    if len(matching) != 1:
                        raise ValueError
                    r, row = matching[0]
                    if (_binding(m) != _binding(r) or row["project_id"] != m["project_id"]
                            or row["message_id"] != m["message_id"] or row["model"] != MODEL):
                        raise ValueError
                    reported = _decimal(r.get("cost_usd"))
                    if r.get("usage") is None:
                        if reported != (Decimal(0) if _unbilled(r.get("http_class")) else maximum):
                            raise ValueError
                        if _unbilled(r.get("http_class")):
                            cost, valid_usage = reported, True
                    elif (reported != usage_cost(r["usage"]) or reported > maximum
                          or r["usage"].get("output_tokens", 0) > m["max_tokens"]):
                        raise ValueError
                    else:
                        cost, valid_usage = reported, True
                except (AnthropicError, ValueError, InvalidOperation):
                    invalid = True
            total += cost
            if valid_usage:
                settled += cost
            else:
                unknown += 1
        # Late results belong to the reservation's month; fetch their prior-month
        # bindings once per bounded ID group, rather than once per result.
        previous = {}
        orphan_ids = results.keys() - reserves.keys()
        for row in _matching_rows_many(ledger, KINDS[0], orphan_ids, now=now):
            meta = _loads(row["meta"])
            previous.setdefault(meta.get("reservation_id"), []).append(meta)
        for rid in orphan_ids:
            matching, prior = results[rid], previous.get(rid, [])
            if (len(matching) != 1 or len(prior) != 1
                    or _binding(prior[0]) != _binding(matching[0][0])):
                invalid = True
        return {"daily_requests": daily, "monthly_cost_usd": str(total),
                "settled_cost_usd": str(settled), "max_reserved_cost_usd": str(total - settled),
                "unresolved_requests": unknown, "pricing_unverified": invalid}
    except (AnthropicError, ValueError, InvalidOperation):
        return {"daily_requests": daily, "monthly_cost_usd": None,
                "settled_cost_usd": None, "max_reserved_cost_usd": None,
                "unresolved_requests": None, "pricing_unverified": True}


def _circuit_result(ledger, error, *, reset=False):
    from semantic_runtime import record_circuit_result
    return record_circuit_result(ledger, error, kind="cloud_circuit", reset=reset)


def block_reason(ledger, cfg, route, project_id):
    if not eligible(cfg, route, project_id):
        return "scope"
    return global_block_reason(ledger)


def global_block_reason(ledger):
    """Return a fixed global stop/pricing code without evaluating patient scope."""
    row = ledger.db.execute("SELECT content FROM artifacts WHERE kind='cloud_control' "
                            "AND project_id IS NULL AND message_id IS NULL "
                            "ORDER BY artifact_id DESC LIMIT 1").fetchone()
    if row is not None and row["content"] != "running":
        return "paused"
    from semantic_runtime import circuit_open, _circuit_state
    if circuit_open(ledger, kind="cloud_circuit"):
        return _circuit_state(ledger, kind="cloud_circuit").get("failure_class") or "circuit_open"
    if not pricing_valid():
        return "pricing_unverified"
    return None


def budget_block_reason(ledger, cfg, *, max_tokens=1600):
    """Read the exact current JST reservation totals without spending or changing state."""
    c = _cfg(cfg)
    if not pricing_valid():
        return "pricing_unverified"
    limit = c["monthly_cost_limit_usd"]
    # Without a monthly cap only today's rows need decoding under the write lock:
    # every newly written result row is today's, so corruption is still caught.
    report = usage_report(ledger, period="month" if limit is not None else "day")
    if report["pricing_unverified"]:
        return "pricing_unverified"
    if c["daily_request_budget"] is not None and report["daily_requests"] >= c["daily_request_budget"]:
        return "daily_cap"
    if limit is not None and _decimal(report["monthly_cost_usd"]) + max_cost(max_tokens) > _decimal(limit):
        return "monthly_cap"
    return None


def stop(ledger):
    ledger.artifact_add("cloud_control", "paused")


def resume(ledger):
    _circuit_result(ledger, None, reset=True)
    ledger.artifact_add("cloud_control", "running")


def usage_reserver(ledger, cfg, *, route, project_id, unit_key, message_id=None, owner=None):
    """Bind one unit's accounting; callers supply the same guard before and after this hook."""
    from semantic_runtime import reserve_artifact
    c = _cfg(cfg)

    def reserve(body, timeout):
        if not isinstance(body, dict) or body.get("model") != MODEL:
            raise AnthropicError("contract_error", "model_not_allowed")
        if ledger.db.in_transaction:
            raise AnthropicError("budget_exceeded", "owner_transaction_active")
        try:
            maximum = max_cost(body.get("max_tokens"))
        except AnthropicError as error:
            _circuit_result(ledger, error)
            raise
        meta = {"reservation_id": uuid.uuid4().hex, "route": route, "project_id": project_id,
                "unit_key": str(unit_key), "message_id": message_id, "model": MODEL,
                "price_version": PRICE_VERSION, "request_fp": payload_hash(body),
                "max_cost_usd": str(maximum), "max_tokens": body["max_tokens"],
                "input_chars": len(body.get("system", "")) + sum(
                    len(message.get("content", "")) for message in body.get("messages", [])
                    if isinstance(message, dict) and isinstance(message.get("content"), str)),
                "timeout_seconds": float(timeout),
                "owner": owner or {}}

        def check():
            reason = block_reason(ledger, c, route, project_id)
            if reason:
                raise AnthropicError(reason if reason == "pricing_unverified" else "budget_exceeded", reason)
            reason = budget_block_reason(ledger, c, max_tokens=body["max_tokens"])
            if reason:
                raise AnthropicError("pricing_unverified" if reason == "pricing_unverified" else "budget_exceeded", reason)


        def write():
            ledger.artifact_add_tx(KINDS[0], "{}", project_id=project_id, message_id=message_id,
                                   model=MODEL, meta=meta)
            return meta
        try:
            return reserve_artifact(ledger, check, write)
        except AnthropicError as error:
            if error.kind == "pricing_unverified":
                _circuit_result(ledger, error)
            raise
    return reserve


def record_result(ledger, reservation, metadata):
    """Settle only a matching, validated result; repeat delivery is idempotent."""
    from semantic_runtime import reserve_artifact
    error_kind = metadata.get("error_kind")
    if metadata.get("http_class") == "http_200" and metadata.get("usage") is None:
        error_kind = "pricing_unverified"
    error = AnthropicError(error_kind) if error_kind else None
    try:
        if (metadata.get("model") != MODEL or metadata.get("request_fp") != reservation["request_fp"]
                or not pricing_valid()):
            raise AnthropicError("pricing_unverified", "result_mismatch")
        usage = validate_usage(metadata["usage"]) if metadata.get("usage") is not None else None
        maximum = _decimal(reservation["max_cost_usd"])
        cost = (usage_cost(usage) if usage is not None
                else Decimal(0) if _unbilled(metadata.get("http_class")) else maximum)
        if cost > maximum or (usage is not None and usage["output_tokens"] > reservation["max_tokens"]):
            raise AnthropicError("pricing_unverified", "cost_exceeds_reserve")
        result = {k: reservation.get(k) for k in ("reservation_id", "request_fp", "route", "project_id",
                  "unit_key", "message_id", "model", "price_version")}
        request_id = metadata.get("request_id")
        if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", request_id):
            request_id = None
        result.update(usage=usage, cost_usd=str(cost), http_class=metadata.get("http_class"),
                      request_id=request_id, error_kind=error_kind)
        if "disposition" in metadata:
            if metadata["disposition"] not in ("discarded_scope", "discarded_workday"):
                raise AnthropicError("pricing_unverified", "disposition_invalid")
            result["disposition"] = metadata["disposition"]

        def check():
            rows = _matching_rows(ledger, KINDS[0], reservation["reservation_id"])
            if len(rows) != 1:
                raise AnthropicError("pricing_unverified", "reservation_missing")
            meta = _loads(rows[0]["meta"])
            _reserve_valid(meta, rows[0])
            if meta != reservation:
                raise AnthropicError("pricing_unverified", "reservation_mismatch")

        def write():
            rows = _matching_rows(ledger, KINDS[1], reservation["reservation_id"])
            if rows:
                if len(rows) == 1 and _loads(rows[0]["meta"]) == result:
                    return False
                # Preserve the conflict durably; accounting must never pick the cheaper result.
                ledger.artifact_add_tx(KINDS[1], "{}", project_id=reservation["project_id"],
                                       message_id=reservation.get("message_id"), model=MODEL,
                                       meta={**result, "usage": None, "cost_usd": str(maximum),
                                             "error_kind": "pricing_unverified"})
                return True
            ledger.artifact_add_tx(KINDS[1], "{}", project_id=reservation["project_id"],
                                   message_id=reservation.get("message_id"), model=MODEL, meta=result)
        conflict = reserve_artifact(ledger, check, write)
        if conflict:
            raise AnthropicError("pricing_unverified", "result_conflict")
    except (AnthropicError, ValueError, KeyError, TypeError, InvalidOperation):
        _circuit_result(ledger, AnthropicError("pricing_unverified"))
        raise AnthropicError("pricing_unverified", "result_invalid") from None
    _circuit_result(ledger, error)
    if error_kind == "pricing_unverified":
        raise AnthropicError("pricing_unverified", "usage_unknown")


def record_disposition_tx(ledger, reservation, disposition):
    """Record a discarded unit in its owner's transaction without changing billing."""
    if not ledger.db.in_transaction:
        raise RuntimeError("cloud_disposition_transaction_required")
    if disposition not in ("discarded_scope", "discarded_workday"):
        raise ValueError("cloud_disposition_invalid")
    rows = _matching_rows(ledger, KINDS[0], reservation.get("reservation_id"))
    if len(rows) != 1:
        raise AnthropicError("pricing_unverified", "reservation_missing")
    stored = _loads(rows[0]["meta"])
    _reserve_valid(stored, rows[0])
    if stored != reservation:
        raise AnthropicError("pricing_unverified", "reservation_mismatch")
    meta = {key: reservation.get(key) for key in (
        "reservation_id", "request_fp", "route", "project_id", "unit_key",
        "message_id", "model", "price_version")}
    meta["disposition"] = disposition
    existing = _matching_rows(ledger, "cloud_usage_disposition", reservation["reservation_id"])
    if existing:
        if (len(existing) != 1 or _loads(existing[0]["meta"]) != meta
                or existing[0]["project_id"] != reservation["project_id"]
                or existing[0]["message_id"] != reservation.get("message_id")
                or existing[0]["model"] != MODEL):
            raise AnthropicError("pricing_unverified", "disposition_conflict")
        return existing[0]["artifact_id"]
    return ledger.artifact_add_tx("cloud_usage_disposition", "{}",
        project_id=reservation["project_id"], message_id=reservation.get("message_id"),
        model=MODEL, meta=meta)


def dryrun_tx(ledger, *, route, project_id, unit_key, input_chars=0, max_tokens=8192, requests=1):
    """Record aggregate estimates in the caller's active success transaction."""
    if not ledger.db.in_transaction:
        raise RuntimeError("cloud_dryrun_transaction_required")
    if (route not in ROUTES or type(input_chars) is not int or input_chars < 0
            or type(requests) is not int or requests < 0
            or type(max_tokens) is not int or not 1 <= max_tokens <= MAX_TOKENS):
        raise ValueError("dryrun_invalid")
    return ledger.artifact_add_tx(KINDS[2], "{}", model=MODEL,
                        meta={"route": route, "requests": requests, "input_chars": input_chars,
                              "estimated_input_tokens": (input_chars + 2) // 3,
                              "estimated_output_tokens": max_tokens * requests,
                              "price_version": PRICE_VERSION})
