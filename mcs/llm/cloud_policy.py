"""Validate cloud scope and reserve bounded provider costs on the ledger owner thread."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
import json
import math
import re
import sqlite3
import time
from types import MappingProxyType
import uuid
from functools import lru_cache

from anthropic_client import AnthropicError, MODEL, MAX_TOKENS, validate_usage
import openai_client
from llm_retry import http_error
from mcs_util import private_env_value, payload_hash, strict_json, until_epoch

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
# A byte-pair token never covers less than one UTF-8 byte, so a request's text
# bytes plus the framing tokens bound its billable input; a reservation at the
# 1M ceiling would hold ~$0.53 per attempt against a daily/monthly cap.
INPUT_RESERVATION_MARGIN = 256
PRICES = {"input": "0.1", "output": "0.5", "long_input": "0.5", "long_output": "2.5",
          "cache_write_5m_multiplier": "1.25", "cache_write_1h_multiplier": "2",
          "cache_read_multiplier": "0.1"}
LONG_INPUT_THRESHOLD = 100000
JST = timezone(timedelta(hours=9))
KINDS = ("cloud_usage_reserve", "cloud_usage_result", "cloud_usage_dryrun")
ROUTES = ("patient_context", "reextract", "semantic_backlog")
DEFAULT_DAILY_COST_LIMIT_USD = 25
DEFAULT_MONTHLY_COST_LIMIT_USD = 75
OPENAI_PRICE_VERSION = "openai-gpt-6-luna-2026-10-29"
OPENAI_PRICE_CHECKED_ON = "2026-10-29"
OPENAI_PRICE_VALID_UNTIL = "2026-11-28"
OPENAI_PRICE_SOURCES = ("https://developers.openai.com/api/docs/models/gpt-6-luna",)
PROVIDERS = {"anthropic": MODEL, "openai": openai_client.MODEL}
_BINDING_KEYS = ("reservation_id", "request_fp", "route", "project_id",
                 "unit_key", "message_id", "model", "price_version", "logical_request_fp", "logical_target_id", "logical_unit_fp")


def capture_history_tx(db, command_id, now, *, continuous=False):
    """Record the history scope within the human resume receipt transaction."""
    from inference_dependency_loss import source_binding
    if not db.in_transaction:
        raise RuntimeError("cloud_history_transaction_required")
    if continuous:
        if not isinstance(command_id, str) or not command_id or len(command_id) > 256:
            raise ValueError("cloud_history_command_invalid")
        db.execute("INSERT INTO artifacts(kind,content,model,meta,created_at) VALUES(?,?,?,?,?)",
            ("cloud_history_scope", json.dumps({"version": 2, "command_id": command_id}), MODEL,
             json.dumps({"command_id": command_id}), now))
        return
    sources = {}
    cutoff = now - 600
    for count, row in enumerate(db.execute("SELECT project_id,message_id FROM messages m "
            "WHERE m.first_seen IS NOT NULL AND m.first_seen<=? "
            "AND (m.notified_at IS NULL OR m.notified_at<=?) "
            "AND NOT EXISTS (SELECT 1 FROM message_revisions r "
            "WHERE r.message_id=m.message_id AND r.observed_at>?) LIMIT 100001",
            (cutoff, cutoff, cutoff)), 1):
        if count > 100000:
            raise ValueError("cloud_history_scope_too_large")
        binding = source_binding(db, row[0], row[1])
        if binding is not None:
            sources[f"{row[0]}:{row[1]}"] = binding
    db.execute("INSERT INTO artifacts(kind,content,model,meta,created_at) VALUES(?,?,?,?,?)",
        ("cloud_history_scope", json.dumps({"version": 1, "sources": sources}), MODEL,
         json.dumps({"command_id": command_id, "targets": len(sources)}), now))


@lru_cache(maxsize=2)
def _history_sources(raw):
    if not isinstance(raw, str) or len(raw) > 16 * 1024 * 1024:
        return {}
    try:
        doc = strict_json(raw)
        sources = doc.get("sources")
        if type(doc.get("version")) is not int or doc.get("version") != 1 or not isinstance(sources, dict):
            return {}
        if any(not isinstance(k, str) or re.fullmatch(r"[1-9][0-9]*:[1-9][0-9]*", k) is None
               or not isinstance(v, str) or re.fullmatch(r"[0-9a-f]{64}", v) is None
               for k, v in sources.items()):
            return {}
        return sources
    except (ValueError, TypeError, AttributeError, RecursionError):
        return {}


@lru_cache(maxsize=2)
def _history_document(raw):
    try:
        value = strict_json(raw)
        return value if isinstance(value, dict) else None
    except (ValueError, TypeError, AttributeError, RecursionError):
        return None


def _history_scope(db, scope):
    """Resolve a fixed scope or a continuous scope tied to its resume receipt."""
    if scope is None:
        return {}
    raw, meta = scope
    if not isinstance(raw, str) or len(raw) > 16 * 1024 * 1024:
        return {}
    try:
        doc = _history_document(raw)
        if isinstance(doc, dict) and type(doc.get("version")) is int and doc["version"] == 2:
            if not isinstance(meta, str) or len(meta) > 32768:
                return {}
            details = strict_json(meta)
            command_id = doc.get("command_id")
            if (set(doc) != {"version", "command_id"} or not isinstance(command_id, str)
                    or not command_id or len(command_id) > 256 or not isinstance(details, dict)
                    or details.get("command_id") != command_id):
                return {}
            control = db.execute("SELECT content,meta FROM artifacts WHERE kind='cloud_control' "
                "AND CASE WHEN json_valid(meta) THEN json_extract(meta,'$.command_id') END=? "
                "ORDER BY artifact_id DESC LIMIT 1", (command_id,)).fetchone()
            if control is None or control[0] != "running":
                return {}
            control_meta = strict_json(control[1])
            if (not isinstance(control_meta, dict) or control_meta.get("command_id") != command_id
                    or not isinstance(control_meta.get('actor'), str) or not control_meta['actor'].strip()
                    or not isinstance(control_meta.get('reason'), str) or not control_meta['reason'].strip()):
                return {}
            receipt = db.execute("SELECT outcome,receipt_json FROM command_receipts WHERE command_id=?",
                                 (command_id,)).fetchone()
            if receipt is None or receipt[0] != 'applied':
                return {}
            recorded = strict_json(receipt[1])
            if (not isinstance(recorded, dict) or recorded.get('command_id') != command_id
                    or recorded.get('outcome') != 'applied' or recorded.get('control') != 'running'
                    or recorded.get('actor') != control_meta.get('actor')
                    or recorded.get('reason') != control_meta.get('reason')):
                return {}
            return None  # continuous scope, bounded by route and live source checks
    except (ValueError, TypeError, AttributeError, RecursionError):
        return {}
    return _history_sources(raw)


def history_target_ready(db, project_id, message_id):
    """Keep a newly arrived or edited processing target in the realtime lane."""
    cutoff = time.time() - 600
    return db.execute("SELECT 1 FROM messages m WHERE m.project_id=? AND m.message_id=? "
        "AND m.first_seen IS NOT NULL AND m.first_seen<=? "
        "AND (m.notified_at IS NULL OR m.notified_at<=?) AND NOT EXISTS "
        "(SELECT 1 FROM message_revisions r WHERE r.message_id=m.message_id AND r.observed_at>?)",
        (project_id, message_id, cutoff, cutoff, cutoff)).fetchone() is not None


def history_thread_allowed(db, project_id, message_id):
    """Require every prompt member to match the receipted history snapshot occurrence."""
    scope = db.execute("SELECT content,meta FROM artifacts WHERE kind='cloud_history_scope' "
                       "ORDER BY artifact_id DESC LIMIT 1").fetchone()
    return _history_thread_allowed(db, project_id, message_id, _history_scope(db, scope))


def _history_thread_allowed(db, project_id, message_id, sources):
    from inference_dependency_loss import source_binding
    target = db.execute("SELECT COALESCE(parent_id,message_id) FROM messages "
                        "WHERE project_id=? AND message_id=?", (project_id, message_id)).fetchone()
    if target is None:
        return False
    members = db.execute("SELECT message_id FROM messages WHERE project_id=? "
                         "AND (message_id=? OR parent_id=?)", (project_id, target[0], target[0])).fetchall()
    if sources is None:
        return bool(members) and all(source_binding(db, project_id, row[0]) is not None for row in members)
    return bool(members) and all(
        sources.get(f"{project_id}:{row[0]}") == source_binding(db, project_id, row[0])
        and sources.get(f"{project_id}:{row[0]}") is not None for row in members)


def history_thread_snapshot(db, project_id, message_id):
    """Bind the active scope and every current thread occurrence to one read snapshot."""
    from inference_dependency_loss import source_binding
    anchor = db.execute("SELECT artifact_id,content,meta FROM (SELECT artifact_id,content,meta FROM artifacts "
                        "WHERE kind='cloud_history_scope' ORDER BY artifact_id DESC LIMIT 1) "
                        "UNION ALL SELECT NULL,NULL,NULL UNION ALL SELECT NULL,NULL,NULL")
    try:
        scope = anchor.fetchone()
        if scope is None or scope[0] is None:
            return None
        sources = _history_scope(db, (scope[1], scope[2]))
        if not _history_thread_allowed(db, project_id, message_id, sources):
            return None
        target = db.execute("SELECT COALESCE(parent_id,message_id) FROM messages "
                            "WHERE project_id=? AND message_id=?", (project_id, message_id)).fetchone()
        if target is None:
            return None
        members = db.execute("SELECT message_id FROM messages WHERE project_id=? "
                             "AND (message_id=? OR parent_id=?) ORDER BY message_id",
                             (project_id, target[0], target[0])).fetchall()
        bindings = [(row[0], source_binding(db, project_id, row[0])) for row in members]
        if not bindings or any(binding is None for _, binding in bindings):
            return None
        return payload_hash({'scope_id': scope[0], 'project_id': project_id,
                             'target_id': message_id, 'members': bindings})
    finally:
        anchor.close()


def history_scope_target_sql(column):
    """Pre-filter candidates; the receipt and occurrence check remains authoritative."""
    if column not in ('m.message_id', 'j.message_id'):
        raise ValueError('cloud_history_column_invalid')
    scope = ("SELECT CASE WHEN json_valid(content) THEN content ELSE '{}' END "
             "FROM artifacts WHERE kind='cloud_history_scope' ORDER BY artifact_id DESC LIMIT 1")
    return (f"(json_extract(({scope}),'$.version')=2 OR {column} IN "
            f"(SELECT CAST(substr(s.key,instr(s.key,':')+1) AS INTEGER) "
            f"FROM json_each(({scope}), '$.sources') s))")


@contextmanager
def history_selection_check(db):
    """Bind one validated receipt and selection to the same SQLite read snapshot."""
    # Keep a statement open to retain the read snapshot without creating a
    # savepoint that an active cancellation handler could prevent releasing.
    anchor = db.execute("SELECT content,meta FROM (SELECT content,meta FROM artifacts "
                        "WHERE kind='cloud_history_scope' ORDER BY artifact_id DESC LIMIT 1) "
                        "UNION ALL SELECT NULL,NULL UNION ALL SELECT NULL,NULL")
    active = [True]
    try:
        scope = anchor.fetchone()
        resolved = _history_scope(db, scope)
        sources = None if resolved is None else MappingProxyType(dict(resolved))
        thread_results = {}

        def check(pid, mid):
            if not active[0]:
                return 0
            target = db.execute("SELECT COALESCE(parent_id,message_id) FROM messages "
                                "WHERE project_id=? AND message_id=?", (pid, mid)).fetchone()
            if target is None:
                return 0
            key = (pid, target[0])
            if key not in thread_results:
                thread_results[key] = _history_thread_allowed(db, pid, mid, sources)
            return int(thread_results[key])

        yield check
    finally:
        active[0] = False
        anchor.close()


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


def cloud_config(cfg, *, key_present=False, require_cost_limit=True):
    """Normalize the optional cloud block, failing closed on every config error."""
    out = {"mode": "off", "routes": list(ROUTES), "project_ids": [],
           "exclude_project_ids": [], "model": MODEL, "daily_request_budget": None,
           "provider": "auto", "provider_order": ["anthropic", "openai"], "provider_budgets": {},
           "max_concurrency": 4, "daily_cost_limit_usd": DEFAULT_DAILY_COST_LIMIT_USD,
           "monthly_cost_limit_usd": DEFAULT_MONTHLY_COST_LIMIT_USD,
           "usd_jpy": None, "until": None,
           "window_start": None, "window_budget_usd": None}
    errors = []
    # an absent block means "unset"; an explicit null is a config error, not an omission
    block = cfg.get("cloud_llm", {}) if isinstance(cfg, dict) else {}
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
    provider = block.get("provider", "auto")
    if provider not in (*PROVIDERS, "auto"):
        errors.append("config: cloud_llm_provider_invalid")
        provider = "anthropic"
    else:
        out["provider"] = provider
    order = block.get("provider_order", out["provider_order"])
    if (not isinstance(order, list) or not order or len(order) != len(set(str(v) for v in order))
            or any(not isinstance(v, str) or v not in PROVIDERS for v in order)):
        errors.append("config: cloud_llm_provider_order_invalid")
    else:
        out["provider_order"] = list(order)
    out["model"] = PROVIDERS.get(provider, PROVIDERS[out["provider_order"][0]])
    if block.get("model", out["model"]) != out["model"]:
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
    for key in ("daily_cost_limit_usd", "monthly_cost_limit_usd", "usd_jpy"):
        value = block.get(key, out[key])
        if value is None:
            # Explicit null removes this cap; an omitted key follows the default.
            out[key] = None
            continue
        try:
            if type(value) not in (int, float) or (key == "usd_jpy" and value <= 0):
                raise ValueError
            _decimal(value)
            out[key] = value
        except (ValueError, OverflowError):
            errors.append(f"config: cloud_llm_{key}_invalid")
    if block.get("until") is not None:
        try:
            until_epoch(block["until"])
            out["until"] = block["until"]
        except (ValueError, OverflowError):
            errors.append("config: cloud_llm_until_invalid")
    # A period budget caps all spending from window_start, across days and months.
    if block.get("window_start") is not None or block.get("window_budget_usd") is not None:
        start, amount = block.get("window_start"), block.get("window_budget_usd")
        try:
            if type(amount) not in (int, float) or amount < 0:
                raise ValueError
            _decimal(amount)
            begin = until_epoch(start)
            if out["until"] is not None and until_epoch(out["until"]) <= begin:
                raise ValueError
            out["window_start"], out["window_budget_usd"] = start, amount
        except (ValueError, OverflowError):
            errors.append("config: cloud_llm_window_invalid")
    # A key alone may enable sending, but never without a spending cap.
    budgets = block.get("provider_budgets", {})
    cap_keys = ("daily_cost_limit_usd", "monthly_cost_limit_usd")
    try:
        if not isinstance(budgets, dict) or budgets.keys() - PROVIDERS.keys():
            raise ValueError
        for name, caps in budgets.items():
            if not isinstance(caps, dict) or set(caps) != set(cap_keys):
                raise ValueError
            if any(type(caps[k]) not in (int, float) for k in cap_keys):
                raise ValueError
            for k in cap_keys:
                _decimal(caps[k])
        for key in cap_keys:
            if out[key] is not None and sum((_decimal(v[key]) for v in budgets.values()), Decimal(0)) > _decimal(out[key]):
                raise ValueError
        out["provider_budgets"] = {name: dict(caps) for name, caps in budgets.items()}
    except (ValueError, OverflowError):
        errors.append("config: cloud_llm_provider_budgets_invalid")
    if (require_cost_limit and key_present is True and "mode" not in block and out["project_ids"] != []
            and out["daily_cost_limit_usd"] is None and out["monthly_cost_limit_usd"] is None
            and out["window_budget_usd"] is None):
        errors.append(COST_LIMIT_REQUIRED)
    if errors:
        out["mode"] = "off"
    return out, errors


COST_LIMIT_REQUIRED = "config: cloud_llm_cost_limit_required"


def blocking_errors(errors):
    """Config errors other than the missing spending cap, which only keeps sending off."""
    return [error for error in errors if error != COST_LIMIT_REQUIRED]


# A period budget sums cloud_usage rows since window_start, and maintenance prunes
# them after CLOUD_USAGE_KEEP_S (62 days); one day short of that keeps every counted row.
WINDOW_HISTORY_S = 61 * 86400


def setting_block_reason(settings, *, now=None):
    """A stop decided by normalized settings alone: outside the configured run window,
    or a period older than the usage history its budget is counted from."""
    now = time.time() if now is None else now
    until, start = settings.get("until"), settings.get("window_start")
    if until is not None and now >= until_epoch(until):
        return "expired"
    if start is not None and now < until_epoch(start):
        return "not_started"
    if (start is not None and settings.get("window_budget_usd") is not None
            and now - until_epoch(start) > WINDOW_HISTORY_S):
        return "window_history_unverified"
    return None


def _cfg(cfg):
    return cloud_config(cfg)[0] if "cloud_llm" in cfg else cloud_config({"cloud_llm": cfg})[0]


def config_generation(cfg):
    return payload_hash(_cfg(cfg))


def scope_generation(cfg):
    """The settings that decide whether a unit may use the cloud at all; caps,
    rate and currency changes must not discard an in-flight paid result."""
    c = _cfg(cfg)
    return payload_hash({k: c[k] for k in ("mode", "routes", "project_ids", "exclude_project_ids", "model", "provider", "provider_order")})


def api_key(root=None):
    """Read only this MCS installation's dotenv, never process or Hermes keys."""
    return private_env_value("MCS_ANTHROPIC_API_KEY", root)


def provider_key(provider, root=None):
    return api_key(root) if provider == "anthropic" else private_env_value("MCS_OPENAI_API_KEY", root) if provider == "openai" else None


def resolve_settings(cfg, root=None):
    """Choose a configured provider by key availability; never retry a paid unit elsewhere."""
    c, errors = cloud_config(cfg)
    provider = c["provider"]
    if provider == "auto":
        provider = next((name for name in c["provider_order"] if provider_key(name, root)), c["provider_order"][0])
    block = cfg.get("cloud_llm") if isinstance(cfg, dict) else None
    if (block is None or isinstance(block, dict) and "mode" not in block) and not errors:
        c, errors = cloud_config(cfg, key_present=bool(provider_key(provider, root)))
    c["provider"], c["model"] = provider, PROVIDERS[provider]
    return c, errors


def selected_key(root=None, cfg=None):
    if cfg is None:
        from mcs_util import load_config
        cfg = load_config()
    settings, _ = resolve_settings(cfg, root)
    return provider_key(settings["provider"], root)


def eligible(cfg, route, project_id):
    """Evaluate explicit route/scope only; callers separately check key presence."""
    c = _cfg(cfg)
    return (c["mode"] != "off" and route in c["routes"]
            and type(project_id) is int and project_id > 0
            and (c["project_ids"] is None or project_id in c["project_ids"])
            and project_id not in c["exclude_project_ids"])


def pricing_valid(now=None, *, model=MODEL):
    day = datetime.fromtimestamp(time.time() if now is None else now, JST).date().isoformat()
    if model == openai_client.MODEL:
        return OPENAI_PRICE_CHECKED_ON <= day <= OPENAI_PRICE_VALID_UNTIL
    return model == MODEL and PRICE_CHECKED_ON <= day <= PRICE_VALID_UNTIL


def price_info(model):
    if model == openai_client.MODEL:
        return {"price_version": OPENAI_PRICE_VERSION, "price_checked_on": OPENAI_PRICE_CHECKED_ON,
                "price_valid_until": OPENAI_PRICE_VALID_UNTIL, "price_valid": pricing_valid(model=model)}
    return {"price_version": PRICE_VERSION, "price_checked_on": PRICE_CHECKED_ON,
            "price_valid_until": PRICE_VALID_UNTIL, "price_valid": pricing_valid(model=model)}


def _texts(body):
    """The request's text parts: the system prompt and each message's string or text blocks."""
    yield str(body.get("system", ""))
    yield str(body.get("instructions", ""))
    if isinstance(body.get("input"), str):
        yield body["input"]
    for message in body.get("messages", []) if isinstance(body.get("messages"), list) else []:
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, str):
            yield content
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and isinstance(block.get("text"), str):
                    yield block["text"]


def _logical_request_fp(body):
    """Identify the same prompt across the two supported provider envelopes."""
    system = body.get('system', body.get('instructions'))
    prompt = body.get('input')
    if prompt is None:
        messages = body.get('messages')
        if not isinstance(messages, list) or len(messages) != 1 or not isinstance(messages[0], dict):
            return None
        if messages[0].get('role') != 'user':
            return None
        prompt = messages[0].get('content')
        if isinstance(prompt, list):
            if any(not isinstance(part, dict) or part.get('type') != 'text'
                   or not isinstance(part.get('text'), str) for part in prompt):
                return None
            prompt = ''.join(part['text'] for part in prompt)
    if not isinstance(system, str) or not isinstance(prompt, str):
        return None
    return payload_hash({'system': system, 'prompt': prompt})


def input_bound(body):
    """Input tokens to reserve for one request (never above the 1M ceiling)."""
    return min(INPUT_RESERVATION_BOUND,
               sum(len(text.encode("utf-8")) for text in _texts(body)) + INPUT_RESERVATION_MARGIN)


def _reservation_cost(input_bound, max_tokens, *, legacy=False, model=MODEL):
    if model == openai_client.MODEL:
        # Default service, global inference and the explicitly requested 30-minute cache.
        # Reserve the most expensive accepted input (cache write) and long-context output.
        return (Decimal(input_bound) * Decimal("0.25") + Decimal(max_tokens) * Decimal("0.75")) / Decimal(1000000)
    # usage_cost accepts cache writes up to the one-hour rate (an unknown TTL is
    # priced there too), so reserved input carries that rate to stay an upper
    # bound. legacy: rows reserved before input_bound was stored used plain input.
    rate = Decimal(PRICES["long_input"]) * (1 if legacy else max(
        Decimal(1), Decimal(PRICES["cache_write_5m_multiplier"]), Decimal(PRICES["cache_write_1h_multiplier"])))
    return (Decimal(input_bound) * rate
            + Decimal(max_tokens) * Decimal(PRICES["long_output"])) / Decimal(1000000)


def max_cost(max_tokens, *, input_bound=INPUT_RESERVATION_BOUND, now=None, model=MODEL):
    if (not pricing_valid(now, model=model) or type(max_tokens) is not int or not 1 <= max_tokens <= MAX_TOKENS
            or type(input_bound) is not int or not 1 <= input_bound <= INPUT_RESERVATION_BOUND):
        raise AnthropicError("pricing_unverified", "price_or_limit_invalid")
    return _reservation_cost(input_bound, max_tokens, model=model)


def usage_cost(usage, *, model=MODEL):
    if model == openai_client.MODEL:
        u = openai_client.validate_usage(usage)
        if u["input_tokens"] > INPUT_RESERVATION_BOUND or u["output_tokens"] > MAX_TOKENS:
            raise AnthropicError("pricing_unverified", "usage_bounds")
        cached, written = (u["input_tokens_details"][k] for k in ("cached_tokens", "cache_write_tokens"))
        long = u["input_tokens"] > 272000
        value = (Decimal(u["input_tokens"] - cached - written) * Decimal("0.1")
                 + Decimal(cached) * Decimal("0.01") + Decimal(written) * Decimal("0.125")) * (2 if long else 1)
        return (value + Decimal(u["output_tokens"]) * (Decimal("0.75") if long else Decimal("0.5"))) / Decimal(1000000)
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
    """Only an attempt proven never sent settles at zero. Every HTTP outcome keeps
    the reserved maximum: the published error list does not state which statuses
    are unbilled."""
    return http_class == "not_sent"


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
    return tuple(meta.get(k) for k in _BINDING_KEYS)


def _reserve_valid(meta, row):
    model = meta.get("model")
    price_version = OPENAI_PRICE_VERSION if model == openai_client.MODEL else PRICE_VERSION
    if (not isinstance(meta.get("reservation_id"), str) or len(meta["reservation_id"]) != 32
            or not isinstance(meta.get("request_fp"), str) or len(meta["request_fp"]) != 64
            or meta.get('logical_request_fp') is not None and (not isinstance(meta['logical_request_fp'], str)
                or re.fullmatch(r'[0-9a-f]{64}', meta['logical_request_fp']) is None)
            or meta.get('logical_unit_fp') is not None and (not isinstance(meta['logical_unit_fp'], str)
                or re.fullmatch(r'[0-9a-f]{64}', meta['logical_unit_fp']) is None)
            or meta.get('logical_target_id') is not None and (type(meta['logical_target_id']) is not int
                or meta['logical_target_id'] <= 0 or meta['logical_target_id'] != row['message_id'])
            or model not in PROVIDERS.values() or row["model"] != model
            or meta.get("price_version") != price_version
            or meta.get("route") not in ROUTES
            or model == openai_client.MODEL and "input_bound" not in meta
            or type(meta.get("project_id")) is not int or meta["project_id"] <= 0
            or meta["project_id"] != row["project_id"]
            or meta.get("message_id") != row["message_id"]
            or not isinstance(meta.get("unit_key"), str) or not meta["unit_key"]):
        raise AnthropicError("pricing_unverified", "reservation_invalid")
    tokens = meta.get("max_tokens")
    bound = meta.get("input_bound", INPUT_RESERVATION_BOUND)   # rows before the request-sized bound
    maximum = _decimal(meta.get("max_cost_usd"))
    if (type(tokens) is not int or not 1 <= tokens <= MAX_TOKENS
            or type(bound) is not int or not 1 <= bound <= INPUT_RESERVATION_BOUND
            or maximum != _reservation_cost(bound, tokens, legacy="input_bound" not in meta, model=model)):
        raise AnthropicError("pricing_unverified", "reservation_bound_invalid")
    return maximum


def _stored_reservation(ledger, reservation):
    """The one retained reservation row matching ``reservation``, validated."""
    rows = _matching_rows(ledger, KINDS[0], reservation.get("reservation_id"))
    if len(rows) != 1:
        raise AnthropicError("pricing_unverified", "reservation_missing")
    stored = _loads(rows[0]["meta"])
    _reserve_valid(stored, rows[0])
    if stored != reservation:
        raise AnthropicError("pricing_unverified", "reservation_mismatch")
    return stored


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


def reservation_evidence(db, artifact_id):
    """Validate a retained reservation's original successful result by exact reference."""
    try:
        return _reservation_evidence(db, artifact_id)
    except (AnthropicError, InvalidOperation, ValueError, TypeError, KeyError, OverflowError):
        raise ValueError("cloud_receipt_invalid") from None


def _receipt_meta(raw):
    if not isinstance(raw, str) or len(raw) > 32768:
        raise ValueError('cloud_receipt_invalid')
    try:
        value = strict_json(raw)
    except (TypeError, ValueError, UnicodeError, RecursionError, OverflowError):
        raise ValueError('cloud_receipt_invalid') from None
    if not isinstance(value, dict):
        raise ValueError('cloud_receipt_invalid')
    return value


def _reservation_evidence(db, artifact_id):
    return _reservation_evidence_batch(db, [artifact_id])[artifact_id]


def _reservation_evidence_batch(db, artifact_ids):
    # All financial rows are checked in one borrowed snapshot. No proof is
    # cached outside this call, so later changes must be checked again.
    try:
        ids = list(artifact_ids)
        if any(type(item) is not int or not 0 < item < 2**63 for item in ids):
            raise ValueError("cloud_receipt_invalid")
        ids = list(dict.fromkeys(ids))
        if not ids:
            return {}
        db.execute('SAVEPOINT cloud_receipt_batch')
        try:
            result = _reservation_evidence_batch_snapshot(db, ids)
        except BaseException:
            try:
                db.execute('ROLLBACK TO cloud_receipt_batch')
                db.execute('RELEASE cloud_receipt_batch')
            except sqlite3.Error:
                pass  # SQLite may already have unwound the savepoint.
            raise
        db.execute('RELEASE cloud_receipt_batch')
        return result
    except (AnthropicError, InvalidOperation, ValueError, TypeError, KeyError, OverflowError):
        raise ValueError("cloud_receipt_invalid") from None


def _reservation_evidence_batch_snapshot(db, ids):
    before = (db.execute('PRAGMA data_version').fetchone()[0], db.total_changes)
    fields = ('artifact_id', 'kind', 'project_id', 'message_id', 'model', 'meta', 'created_at', 'content')
    originals = {}
    for row in db.execute(
            "SELECT artifact_id,kind,project_id,message_id,model,"
            "CASE WHEN typeof(meta)='text' AND length(meta)<=32768 THEN meta END,created_at,content "
            "FROM artifacts WHERE kind='cloud_usage_reserve' "
            "AND artifact_id IN (SELECT value FROM json_each(?))", (json.dumps(ids),)):
        original = dict(zip(fields, row))
        meta = _receipt_meta(original['meta'])
        originals[original['artifact_id']] = (meta, _reserve_valid(meta, original))
    if len(originals) != len(ids):
        raise ValueError("cloud_receipt_missing")
    kinds = (KINDS[0], KINDS[1], 'cloud_usage_disposition')
    # A corrupt row may also have lost its patient/UUID.  It cannot be proved
    # unrelated to this source's disposition, so new adoption evidence holds.
    bad = db.execute("SELECT CASE WHEN typeof(meta)='text' AND length(meta)<=32768 THEN meta END "
                  "FROM artifacts WHERE kind IN (?,?,?) AND CASE "
                  "WHEN typeof(meta)!='text' OR length(meta)>32768 OR NOT json_valid(meta) THEN 1 "
                  "WHEN json_type(meta)!='object' THEN 1 "
                  "WHEN EXISTS (SELECT 1 FROM json_tree(meta) WHERE key IS NOT NULL "
                  "GROUP BY parent,key HAVING COUNT(*)>1) THEN 1 "
                  "WHEN EXISTS (SELECT 1 FROM json_tree(meta) WHERE type='real' AND abs(value)>1.7976931348623157e308) THEN 1 "
                  "ELSE 0 END LIMIT 1", kinds).fetchone()
    if bad is not None:
        _receipt_meta(bad[0])
        raise ValueError("cloud_receipt_invalid")
    wanted = {meta['reservation_id'] for meta, _ in originals.values()}
    rows_by_reservation = {rid: [] for rid in wanted}
    rows = db.execute("SELECT artifact_id,kind,project_id,message_id,model,"
        "CASE WHEN typeof(meta)='text' AND length(meta)<=32768 THEN meta END AS meta,created_at,content FROM artifacts "
        "WHERE kind IN (?,?,?) AND CASE WHEN json_valid(meta) THEN EXISTS "
        "(SELECT 1 FROM json_each(meta) m WHERE m.key='reservation_id' AND m.type='text' "
        "AND m.value IN (SELECT value FROM json_each(?))) END ORDER BY artifact_id",
        (*kinds, json.dumps(sorted(wanted))))
    for row in rows:
        meta = _receipt_meta(row['meta'])
        rows_by_reservation[meta['reservation_id']].append((row, meta))
    result = {}
    for artifact_id in ids:
        meta, maximum = originals[artifact_id]
        matched = rows_by_reservation[meta['reservation_id']]
        reservations = [row for row, _ in matched if row['kind'] == KINDS[0]]
        results = [(row, value) for row, value in matched if row['kind'] == KINDS[1]]
        if (len(reservations) != 1 or reservations[0]['artifact_id'] != artifact_id or len(results) != 1
                or any(row['kind'] == 'cloud_usage_disposition' for row, _ in matched)):
            raise ValueError("cloud_receipt_unresolved")
        result_row, result_meta = results[0]
        _, settled = _settled(meta, maximum, [(result_meta, result_row)])
        if not settled or result_meta.get('http_class') != 'http_200' or result_meta.get('error_kind') or result_meta.get('disposition'):
            raise ValueError("cloud_receipt_unresolved")
        result[artifact_id] = {'reservation': meta, 'result': result_meta,
                               'witness': payload_hash([tuple(row) for row, _ in matched])}
    if (db.execute('PRAGMA data_version').fetchone()[0], db.total_changes) != before:
        raise ValueError("cloud_receipt_changed")
    return result


def _settled(m, maximum, matching):
    """(cost, settled?) of one reservation's single bound result; raises when
    the result is duplicated, unbound or its usage/cost disagrees."""
    if len(matching) != 1:
        raise ValueError
    r, row = matching[0]
    if (_binding(m) != _binding(r) or row["project_id"] != m["project_id"]
            or row["message_id"] != m["message_id"] or row["model"] != m["model"]):
        raise ValueError
    reported = _decimal(r.get("cost_usd"))
    if r.get("usage") is None:
        if reported != (Decimal(0) if _unbilled(r.get("http_class")) else maximum):
            raise ValueError
        return reported, _unbilled(r.get("http_class"))
    if (reported != usage_cost(r["usage"], model=m["model"]) or reported > maximum
            or r["usage"].get("output_tokens", 0) > m["max_tokens"]):
        raise ValueError
    return reported, True


def _guard_repeated_request(ledger, candidate):
    """Keep an unresolved or already answered logical request from being sent again."""
    rows = ledger.db.execute("SELECT artifact_id,kind,project_id,message_id,model,meta,created_at "
        "FROM artifacts WHERE kind=? AND project_id=? AND (message_id IS ? OR ?='semantic_backlog') AND "
        "CASE WHEN typeof(meta)!='text' OR length(meta)>32768 OR NOT json_valid(meta) THEN 1 "
        "WHEN json_type(meta)!='object' OR EXISTS (SELECT 1 FROM json_tree(meta) "
        "WHERE key IS NOT NULL GROUP BY parent,key HAVING COUNT(*)>1) THEN 1 "
        "ELSE json_extract(meta,'$.route')=? AND "
        "(json_extract(meta,'$.unit_key')=? OR ?='semantic_backlog') END ORDER BY artifact_id",
        (KINDS[0], candidate['project_id'], candidate['message_id'], candidate['route'],
         candidate['route'], candidate['unit_key'], candidate['route'])).fetchall()
    result_map = None
    for row in rows:
        try:
            previous = _receipt_meta(row['meta'])
            maximum = _reserve_valid(previous, row)
            old_owner, new_owner = previous.get('owner'), candidate.get('owner')
            if candidate['route'] == 'semantic_backlog':
                old_target = previous.get('logical_target_id')
                if type(old_target) is int and old_target != candidate['message_id']:
                    continue
                old_unit, new_unit = previous.get('logical_unit_fp'), candidate.get('logical_unit_fp')
                if old_unit is not None and new_unit is not None:
                    if old_unit != new_unit:
                        continue
                elif previous.get('unit_key') != candidate.get('unit_key'):
                    root = new_owner.get('semantic_thread_root') if isinstance(new_owner, dict) else None
                    if (previous.get('message_id') not in (candidate.get('message_id'), root)
                            or previous.get('request_fp') != candidate.get('request_fp')):
                        continue
            if candidate['route'] == 'patient_context' and isinstance(old_owner, dict) and isinstance(new_owner, dict):
                if any(isinstance(old_owner.get(k), str) and isinstance(new_owner.get(k), str)
                       and old_owner[k] != new_owner[k] for k in ('night_id', 'source_sig')):
                    continue
            old_fp, new_fp = previous.get('logical_request_fp'), candidate.get('logical_request_fp')
            if old_fp is not None and new_fp is not None:
                if old_fp != new_fp:
                    continue
            # Legacy reservations contain only an envelope hash. A model or
            # token-limit change cannot prove that their prompt is different.
            if result_map is None:
                result_map = {}
                results = ledger.db.execute("SELECT artifact_id,kind,project_id,message_id,model,meta,created_at "
                    "FROM artifacts WHERE kind=? AND project_id=? AND (message_id IS ? OR ?='semantic_backlog') "
                    "ORDER BY artifact_id",
                    (KINDS[1], candidate['project_id'], candidate['message_id'], candidate['route']))
                for result_row in results:
                    result_meta = _receipt_meta(result_row['meta'])
                    result_map.setdefault(result_meta.get('reservation_id'), []).append((result_meta, result_row))
            matches = result_map.get(previous['reservation_id'], [])
            _, settled = _settled(previous, maximum, matches)
            result = matches[0][0]
            if result.get('disposition'):
                raise ValueError('discarded')
            if settled and result.get('http_class') == 'not_sent':
                continue
            # A confirmed successful result may be deliberately reprocessed
            # with another model. An ambiguous send never grants that retry.
            if (candidate['route'] == 'semantic_backlog' and old_unit is not None and new_unit is not None
                    and previous['model'] != candidate['model'] and settled
                    and result.get('http_class') == 'http_200' and result.get('error_kind') is None):
                continue
            # A validated rate-limit response rejected this attempt. Its
            # reserved cost still remains counted under the existing policy.
            if (result.get('http_class') == 'http_429' and result.get('error_kind') == 'rate_limited'
                    and result.get('usage') is None):
                continue
            raise ValueError('already_sent_or_unknown')
        except (AnthropicError, InvalidOperation, ValueError, TypeError, KeyError, OverflowError):
            raise AnthropicError('budget_exceeded', 'cloud_unit_unresolved') from None


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
    providers = {name: {"cost_usd": Decimal(0), "unresolved_requests": 0} for name in PROVIDERS}
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
            cost, valid_usage = maximum, False
            if results.get(rid):
                try:
                    cost, valid_usage = _settled(m, maximum, results[rid])
                except (AnthropicError, ValueError, InvalidOperation, TypeError):
                    invalid = True
            total += cost
            name = next(name for name, model in PROVIDERS.items() if model == m["model"])
            providers[name]["cost_usd"] += cost
            providers[name]["unresolved_requests"] += int(not valid_usage)
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
            previous.setdefault(meta.get("reservation_id"), []).append((meta, row))
        for rid in orphan_ids:
            prior = previous.get(rid, [])
            try:
                if len(prior) != 1:
                    raise ValueError
                # validated like an in-period result; its cost stays in its own period
                _settled(prior[0][0], _reserve_valid(*prior[0]), results[rid])
            except (AnthropicError, ValueError, InvalidOperation, TypeError):
                invalid = True
        return {"daily_requests": daily, "monthly_cost_usd": str(total),
                "settled_cost_usd": str(settled), "max_reserved_cost_usd": str(total - settled),
                "unresolved_requests": unknown, "pricing_unverified": invalid,
                "providers": {name: {**values, "cost_usd": str(values["cost_usd"])} for name, values in providers.items()}}
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
    return global_block_reason(ledger, model=_cfg(cfg)["model"])


def global_block_reason(ledger, *, model=MODEL):
    """Return a fixed global stop/pricing code without evaluating patient scope."""
    row = ledger.db.execute("SELECT content FROM artifacts WHERE kind='cloud_control' "
                            "AND project_id IS NULL AND message_id IS NULL "
                            "ORDER BY artifact_id DESC LIMIT 1").fetchone()
    if row is not None and row["content"] != "running":
        return "paused"
    from semantic_runtime import circuit_open, _circuit_state
    if circuit_open(ledger, kind="cloud_circuit"):
        return _circuit_state(ledger, kind="cloud_circuit").get("failure_class") or "circuit_open"
    if not pricing_valid(model=model):
        return "pricing_unverified"
    return None


class _Tally:
    """usage_report's month totals kept incrementally for the reservation gate:
    every usage row is decoded once per process instead of once per reservation
    (the gate runs under the write lock). Rows are append-only within a month;
    an unreadable row stops the gate and is read again next time."""

    def __init__(self, month):
        self.month = month
        self.last_id = 0
        self.prefix = None   # fingerprint of the rows already tallied
        self.open = {}       # rid -> (meta, maximum, day): reserved, no result yet
        self.closed = {}     # rid -> (maximum, cost, settled, day): one result seen
        self.orphans = {}    # rid -> (prior-month reservation rows, results)
        self.days = {}       # day -> [requests, cost]
        self.provider_days = {}
        self.provider_totals = {}
        self.total = Decimal(0)
        self.invalid = False

    def _add(self, day, cost, model):
        self.days.setdefault(day, [0, Decimal(0)])[1] += cost
        self.total += cost
        self.provider_days[(model, day)] = self.provider_days.get((model, day), Decimal(0)) + cost
        self.provider_totals[model] = self.provider_totals.get(model, Decimal(0)) + cost

    def fingerprint(self, ledger):
        """Cheap check that the tallied rows are still there unchanged (the
        runtime only appends; a same-length in-place edit is not detected)."""
        return tuple(ledger.db.execute(
            "SELECT count(*), total(length(meta)), total(created_at) FROM artifacts "
            "WHERE kind IN ('cloud_usage_reserve','cloud_usage_result') AND created_at>=? "
            "AND artifact_id<=?", (self.month, self.last_id)).fetchone())

    def update(self, ledger, now):
        rows = ledger.db.execute(
            "SELECT artifact_id,kind,project_id,message_id,model,meta,created_at FROM artifacts "
            "WHERE kind IN ('cloud_usage_reserve','cloud_usage_result') AND created_at>=? "
            "AND artifact_id>? ORDER BY artifact_id", (self.month, self.last_id)).fetchall()
        for row in rows:
            m = _loads(row["meta"])
            rid = m.get("reservation_id")
            if row["kind"] == "cloud_usage_reserve":
                maximum = _reserve_valid(m, row)
                if rid in self.open or rid in self.closed:
                    raise AnthropicError("pricing_unverified", "duplicate_reservation")
                day = _periods(row["created_at"])[0]
                self.open[rid] = (m, maximum, day)
                self.days.setdefault(day, [0, Decimal(0)])[0] += 1
                self._add(day, maximum, m["model"])
            elif not isinstance(rid, str):
                raise AnthropicError("pricing_unverified", "result_invalid")
            elif rid in self.open:
                meta, maximum, day = self.open.pop(rid)
                try:
                    cost, settled = _settled(meta, maximum, [(m, row)])
                except (AnthropicError, ValueError, InvalidOperation, TypeError):
                    self.invalid = True
                    cost, settled = maximum, False
                self._add(day, cost - maximum, meta["model"])
                self.closed[rid] = (maximum, cost, settled, day, meta["model"])
            elif rid in self.closed:
                # a second result row: the pair no longer settles (usage_report's _settled)
                maximum, cost, settled, day, model = self.closed[rid]
                self.invalid = True
                self._add(day, maximum - cost, model)
                self.closed[rid] = (maximum, maximum, False, day, model)
            else:
                entry = self.orphans.get(rid)
                if entry is None:
                    prior = [(_loads(r["meta"]), r) for r in _matching_rows_many(ledger, KINDS[0], [rid], now=now)]
                    entry = self.orphans[rid] = (prior, [])
                entry[1].append((m, row))
                try:
                    if len(entry[0]) != 1:
                        raise ValueError
                    _settled(entry[0][0][0], _reserve_valid(*entry[0][0]), entry[1])
                except (AnthropicError, ValueError, InvalidOperation, TypeError):
                    self.invalid = True
            self.last_id = row["artifact_id"]
        self.prefix = self.fingerprint(ledger)


def _tally(ledger, now=None):
    now = time.time() if now is None else now
    month = _periods(now)[1]
    tally = getattr(ledger, "_cloud_usage_tally", None)
    if (tally is None or tally.month != month
            or (tally.prefix is not None and tally.prefix != tally.fingerprint(ledger))):
        tally = ledger._cloud_usage_tally = _Tally(month)
    tally.update(ledger, now)
    return tally


def _window_tally(ledger, start):
    """The same incremental tally as the monthly one, bounded below by the period start."""
    tally = getattr(ledger, "_cloud_window_tally", None)
    if (tally is None or tally.month != start
            or (tally.prefix is not None and tally.prefix != tally.fingerprint(ledger))):
        tally = ledger._cloud_window_tally = _Tally(start)
    tally.update(ledger, time.time())
    return tally


PRUNE_MARK_KIND = "cloud_usage_pruned_before"


def window_history_pruned(ledger, cfg):
    """True when maintenance has already deleted usage rows from inside the period
    (judged by its durable cutoff, not the clock), or that mark is unreadable."""
    marks = [row[0] for row in ledger.db.execute(
        "SELECT content FROM artifacts WHERE kind=?", (PRUNE_MARK_KIND,))]
    try:
        cutoffs = [float(mark) for mark in marks]
    except (TypeError, ValueError):
        return True
    if len(cutoffs) > 1 or not all(math.isfinite(cutoff) for cutoff in cutoffs):
        return True   # maintenance stops advancing duplicates, so they cannot vouch for the period
    return bool(cutoffs) and until_epoch(_cfg(cfg)["window_start"]) < cutoffs[0]


def window_spend(ledger, cfg):
    """Spending reserved or settled since window_start (unsettled at their maximum), or None."""
    c = _cfg(cfg)
    if c["window_budget_usd"] is None:
        return None
    tally = _window_tally(ledger, until_epoch(c["window_start"]))
    if tally.invalid:
        raise AnthropicError("pricing_unverified", "window_tally_invalid")
    return tally.total


def budget_block_reason(ledger, cfg, *, max_tokens=1600, input_bound=INPUT_RESERVATION_MARGIN):
    """Read the exact current JST reservation totals without spending or changing state.
    ``input_bound`` is the next request's reserved input (the margin alone for a
    pre-check without a request)."""
    c = _cfg(cfg)
    reason = setting_block_reason(c)
    if reason:
        return reason
    if not pricing_valid(model=c["model"]):
        return "pricing_unverified"
    try:
        tally = _tally(ledger)
    except (AnthropicError, ValueError, InvalidOperation):
        return "pricing_unverified"
    if tally.invalid:
        return "pricing_unverified"
    today = tally.days.get(_periods(time.time())[0], [0, Decimal(0)])
    if c["daily_request_budget"] is not None and today[0] >= c["daily_request_budget"]:
        return "daily_cap"
    cost = max_cost(max_tokens, input_bound=input_bound, model=c["model"])
    if c["daily_cost_limit_usd"] is not None and today[1] + cost > _decimal(c["daily_cost_limit_usd"]):
        return "daily_cost_cap"
    limit = c["monthly_cost_limit_usd"]
    if limit is not None and tally.total + cost > _decimal(limit):
        return "monthly_cap"
    caps = c["provider_budgets"].get(c["provider"])
    if c["provider_budgets"] and caps is None:
        return "provider_budget_missing"
    if caps is not None:
        if tally.provider_days.get((c["model"], _periods(time.time())[0]), Decimal(0)) + cost > _decimal(caps["daily_cost_limit_usd"]):
            return "provider_daily_cap"
        if tally.provider_totals.get(c["model"], Decimal(0)) + cost > _decimal(caps["monthly_cost_limit_usd"]):
            return "provider_monthly_cap"
    if c["window_budget_usd"] is not None:
        if window_history_pruned(ledger, c):
            return "window_history_unverified"
        try:
            spent = window_spend(ledger, c)
        except (AnthropicError, ValueError, InvalidOperation):
            return "pricing_unverified"
        if spent + cost > _decimal(c["window_budget_usd"]):
            return "window_cap"
    return None


def stop(ledger):
    ledger.artifact_add("cloud_control", "paused")


def resume(ledger):
    _circuit_result(ledger, None, reset=True)
    ledger.artifact_add("cloud_control", "running")


def usage_reserver(ledger, cfg, *, route, project_id, unit_key, message_id=None, owner=None, on_reserved_tx=None):
    """Bind one unit's accounting; callers supply the same guard before and after this hook."""
    from semantic_runtime import reserve_artifact
    c = _cfg(cfg)
    if on_reserved_tx is not None and not callable(on_reserved_tx):
        raise ValueError("cloud_reservation_hook_invalid")

    def reserve(body, timeout):
        model = c["model"]
        if not isinstance(body, dict) or body.get("model") != model:
            raise AnthropicError("contract_error", "model_not_allowed")
        if model == openai_client.MODEL and (body.get("service_tier") != "default"
                or body.get("store") is not False or body.get("prompt_cache_options") != {"ttl": "30m"}):
            raise AnthropicError("contract_error", "openai_price_contract_invalid")
        if ledger.db.in_transaction:
            raise AnthropicError("budget_exceeded", "owner_transaction_active")
        bound = input_bound(body)
        tokens = body.get("max_output_tokens") if model == openai_client.MODEL else body.get("max_tokens")
        try:
            maximum = max_cost(tokens, input_bound=bound, model=model)
        except AnthropicError as error:
            _circuit_result(ledger, error)
            raise
        meta = {"reservation_id": uuid.uuid4().hex, "route": route, "project_id": project_id,
                "unit_key": str(unit_key), "message_id": message_id, "model": model,
                "price_version": OPENAI_PRICE_VERSION if model == openai_client.MODEL else PRICE_VERSION, "request_fp": payload_hash(body),
                "logical_request_fp": _logical_request_fp(body),
                "logical_target_id": (message_id if route == 'semantic_backlog' and isinstance(owner, dict)
                                      and owner.get('semantic_target_id') == message_id else None),
                "max_cost_usd": str(maximum), "max_tokens": tokens, "input_bound": bound,
                "input_chars": sum(len(text) for text in _texts(body)),
                "timeout_seconds": float(timeout),
                "owner": owner or {}}
        if (route == 'semantic_backlog' and isinstance(owner, dict)
                and owner.get('semantic_target_id') == message_id
                and all(isinstance(owner.get(k), str) and owner[k]
                        for k in ('semantic_source', 'semantic_generation', 'semantic_phase'))):
            meta['logical_unit_fp'] = payload_hash({
                'target': message_id, 'source': owner['semantic_source'],
                'generation': owner['semantic_generation'], 'phase': owner['semantic_phase']})

        def check():
            _guard_repeated_request(ledger, meta)
            reason = block_reason(ledger, c, route, project_id)
            if reason:
                raise AnthropicError(reason if reason == "pricing_unverified" else "budget_exceeded", reason)
            reason = budget_block_reason(ledger, c, max_tokens=tokens, input_bound=bound)
            if reason:
                raise AnthropicError("pricing_unverified" if reason == "pricing_unverified" else "budget_exceeded", reason)

        def write():
            artifact_id = ledger.artifact_add_tx(KINDS[0], "{}", project_id=project_id, message_id=message_id,
                                                  model=model, meta=meta)
            if on_reserved_tx is not None:
                from copy import deepcopy
                row = ledger.db.execute("SELECT kind,project_id,message_id,model,content,meta FROM artifacts "
                                        "WHERE artifact_id=?", (artifact_id,)).fetchone()
                if row is None:
                    raise RuntimeError("cloud_reservation_hook_invalid")
                original = tuple(row)
                before = ledger.db.total_changes
                writes = on_reserved_tx(ledger, deepcopy(meta), artifact_id)
                row = ledger.db.execute("SELECT kind,project_id,message_id,model,content,meta FROM artifacts "
                                        "WHERE artifact_id=?", (artifact_id,)).fetchone()
                if (not ledger.db.in_transaction or type(writes) is not int or writes <= 0
                        or ledger.db.total_changes - before != writes or row is None or tuple(row) != original):
                    raise RuntimeError("cloud_reservation_hook_invalid")
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
    # Accounting owns its transaction.  In particular, a result callback can
    # run while a caller's transaction is open; do not commit or roll it back.
    if ledger.db.in_transaction:
        raise RuntimeError("cloud_result_owner_transaction_active")
    from semantic_runtime import reserve_artifact
    error_kind = metadata.get("error_kind")
    http_class = metadata.get("http_class")
    if http_class == "http_200" and metadata.get("usage") is None:
        error_kind = "pricing_unverified"
    error = None
    if error_kind:
        # Rebuild the attempt's failure with its retry class: the breaker counts
        # only retryable (429/5xx/transport/timeout) failures (semantic_runtime).
        status = int(http_class[5:]) if isinstance(http_class, str) and http_class[5:].isdigit() else 0
        retryable = (http_error(status)[1] if status and status != 200
                     else error_kind in ("transport", "timeout"))
        error = AnthropicError(error_kind, retryable=retryable, status=status)
    try:
        # Settle at the price table the reservation was made with: a result that arrives
        # after PRICE_VALID_UNTIL is still saved; only new sends stop (owner decision 2026-10-28).
        model = reservation["model"]
        if (model not in PROVIDERS.values() or metadata.get("model") != model or metadata.get("request_fp") != reservation["request_fp"]
                or reservation.get("price_version") != (OPENAI_PRICE_VERSION if model == openai_client.MODEL else PRICE_VERSION)):
            raise AnthropicError("pricing_unverified", "result_mismatch")
        validator = openai_client.validate_usage if model == openai_client.MODEL else validate_usage
        usage = validator(metadata["usage"]) if metadata.get("usage") is not None else None
        maximum = _decimal(reservation["max_cost_usd"])
        cost = (usage_cost(usage, model=model) if usage is not None
                else Decimal(0) if _unbilled(metadata.get("http_class")) else maximum)
        if cost > maximum or (usage is not None and usage["output_tokens"] > reservation["max_tokens"]):
            raise AnthropicError("pricing_unverified", "cost_exceeds_reserve")
        result = {k: reservation.get(k) for k in _BINDING_KEYS}
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
            _stored_reservation(ledger, reservation)

        def write():
            rows = _matching_rows(ledger, KINDS[1], reservation["reservation_id"])
            if rows:
                if len(rows) == 1 and _loads(rows[0]["meta"]) == result:
                    return False
                # Preserve the conflict durably; accounting must never pick the cheaper result.
                ledger.artifact_add_tx(KINDS[1], "{}", project_id=reservation["project_id"],
                                       message_id=reservation.get("message_id"), model=model,
                                       meta={**result, "usage": None, "cost_usd": str(maximum),
                                             "error_kind": "pricing_unverified"})
                return True
            ledger.artifact_add_tx(KINDS[1], "{}", project_id=reservation["project_id"],
                                   message_id=reservation.get("message_id"), model=model, meta=result)
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
    _stored_reservation(ledger, reservation)
    meta = {key: reservation.get(key) for key in _BINDING_KEYS}
    meta["disposition"] = disposition
    existing = _matching_rows(ledger, "cloud_usage_disposition", reservation["reservation_id"])
    if existing:
        if (len(existing) != 1 or _loads(existing[0]["meta"]) != meta
                or existing[0]["project_id"] != reservation["project_id"]
                or existing[0]["message_id"] != reservation.get("message_id")
                or existing[0]["model"] != reservation["model"]):
            raise AnthropicError("pricing_unverified", "disposition_conflict")
        return existing[0]["artifact_id"]
    return ledger.artifact_add_tx("cloud_usage_disposition", "{}",
        project_id=reservation["project_id"], message_id=reservation.get("message_id"),
        model=reservation["model"], meta=meta)


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


# Observations only: a reserved slot older than this may still be in flight
# (claim leases can outlast it), so it is not proof that a send was interrupted.
RESERVED_AGE_S = 600
# Rows read per category at most; above it the count is reported as unknown.
EVIDENCE_SCAN_LIMIT = 20000
_UNREADABLE_JSON = ("typeof({c})!='text' OR length({c})>32768 OR NOT json_valid({c}) "
                    "OR json_type({c})!='object' OR EXISTS (SELECT 1 FROM json_tree({c}) "
                    "WHERE key IS NOT NULL GROUP BY parent,key HAVING COUNT(*)>1)")


def evidence_hold_counts(db, *, now, scan_limit=EVIDENCE_SCAN_LIMIT):
    """Observed counts, without IDs, behind source-repair holds; None where not fully counted.

    unattributable_accounting_rows: accounting rows failing the global structure
    check that holds every repair adoption. Per-reservation evidence failures
    (missing, duplicate, altered or disposed results) are not counted here.
    old_reserved_repair_slots: repair slots, across all patients, scopes and
    history, still marked reserved after RESERVED_AGE_S; not proof of an
    interrupted send. unparseable_repair_slots: slots whose content is not one
    JSON object without duplicate keys, or whose creation time is not a finite number.
    Other faults the repair reader rejects are not counted, so zero is not
    evidence that nothing holds a repair.
    """
    from patient_context_artifacts import CANDIDATE_KIND
    receipt_kinds = ('cloud_usage_reserve', 'cloud_usage_result', 'cloud_usage_disposition')
    out = {"unattributable_accounting_rows": None, "old_reserved_repair_slots": None,
           "unparseable_repair_slots": None, "reserved_age_s": RESERVED_AGE_S, "scan_limit": scan_limit}
    # The bound applies before counting: a full COUNT(*) would itself read every row.
    rows = db.execute("SELECT COUNT(*) FROM (SELECT 1 FROM artifacts WHERE kind IN (?,?,?) LIMIT ?)",
                      (*receipt_kinds, scan_limit + 1)).fetchone()[0]
    if rows <= scan_limit:
        out["unattributable_accounting_rows"] = db.execute(
            "SELECT COUNT(*) FROM artifacts WHERE kind IN (?,?,?) AND CASE WHEN "
            + _UNREADABLE_JSON.format(c="meta") + " THEN 1 WHEN EXISTS (SELECT 1 FROM json_tree(meta) "
            "WHERE type='real' AND abs(value)>1.7976931348623157e308) THEN 1 ELSE 0 END",
            receipt_kinds).fetchone()[0]
    slots = db.execute("SELECT COUNT(*) FROM (SELECT 1 FROM artifacts WHERE kind=? LIMIT ?)",
                       (CANDIDATE_KIND, scan_limit + 1)).fetchone()[0]
    if slots <= scan_limit:
        unreadable = ("typeof(created_at) NOT IN ('integer','real') "
                      "OR abs(created_at)>1.7976931348623157e308 OR "
                      + _UNREADABLE_JSON.format(c="content"))
        bad, old = db.execute(
            "SELECT COALESCE(SUM(CASE WHEN " + unreadable + " THEN 1 ELSE 0 END),0),"
            "COALESCE(SUM(CASE WHEN " + unreadable + " THEN 0 WHEN created_at<? "
            "AND json_extract(content,'$.phase')='reserved' THEN 1 ELSE 0 END),0) "
            "FROM artifacts WHERE kind=?", (now - RESERVED_AGE_S, CANDIDATE_KIND)).fetchone()
        out["unparseable_repair_slots"], out["old_reserved_repair_slots"] = bad, old
    return out
