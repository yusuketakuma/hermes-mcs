"""Validate cloud scope and reserve bounded provider costs on the ledger owner thread."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from contextlib import closing, contextmanager
from decimal import Decimal, InvalidOperation
import hashlib
from itertools import chain
import json
import math
import re
import time
from collections.abc import Mapping
from types import MappingProxyType
import uuid
from functools import lru_cache

import anthropic_client
from anthropic_client import AnthropicError, MODEL
from anthropic_client import MAX_TOKENS as MAX_TOKENS, validate_usage as validate_usage
import openai_client
from llm_retry import http_error
from mcs_util import canonical, private_env_value, payload_hash, strict_json, until_epoch

# Compatibility aliases: immutable versioned definitions live with providers.
PRICE_VERSION = anthropic_client.PRICE_VERSION
PRICE_CHECKED_ON = anthropic_client.PRICE_CHECKED_ON
PRICE_VALID_UNTIL = anthropic_client.PRICE_VALID_UNTIL
PRICE_SOURCES = anthropic_client.PRICE_SOURCES
# Conservative bound covering decimal/binary interpretations of the public 1M label.
# This is a reservation bound, not an advertised accepted input capacity.
INPUT_RESERVATION_BOUND = 1048576
# A byte-pair token never covers less than one UTF-8 byte, so a request's text
# bytes plus the framing tokens bound its billable input; a reservation at the
# 1M ceiling would hold ~$0.53 per attempt against a daily/monthly cap.
INPUT_RESERVATION_MARGIN = 256
PRICES = anthropic_client.PRICES
LONG_INPUT_THRESHOLD = anthropic_client.LONG_INPUT_THRESHOLD
JST = timezone(timedelta(hours=9))
KINDS = ("cloud_usage_reserve", "cloud_usage_result", "cloud_usage_dryrun")
ROUTES = ("patient_context", "reextract", "semantic_backlog")
DEFAULT_DAILY_COST_LIMIT_USD = 25
DEFAULT_MONTHLY_COST_LIMIT_USD = 75
OPENAI_PRICE_VERSION = openai_client.PRICE_VERSION
OPENAI_PRICE_CHECKED_ON = openai_client.PRICE_CHECKED_ON
OPENAI_PRICE_VALID_UNTIL = openai_client.PRICE_VALID_UNTIL
OPENAI_PRICE_SOURCES = openai_client.PRICE_SOURCES
PROVIDERS = {"anthropic": MODEL, "openai": openai_client.MODEL}
if ({model for model, _ in anthropic_client.PRICE_SNAPSHOTS}
        & {model for model, _ in openai_client.PRICE_SNAPSHOTS}):
    # Stored receipts predate an explicit provider field; ambiguous models fail closed.
    raise RuntimeError("duplicate_cloud_price_definition")
PRICE_DEFINITIONS = MappingProxyType({**anthropic_client.PRICE_SNAPSHOTS,
                                      **openai_client.PRICE_SNAPSHOTS})
_CURRENT_PRICE = object()


def _price_definition(model, price_version=_CURRENT_PRICE):
    if not isinstance(model, str):
        raise AnthropicError("pricing_unverified", "price_or_limit_invalid")
    if price_version is _CURRENT_PRICE:
        if model == PROVIDERS["anthropic"]:
            price_version = anthropic_client.PRICE_VERSION
        elif model in openai_client.CURRENT_PRICE_VERSIONS:
            price_version = openai_client.CURRENT_PRICE_VERSIONS[model]
        else:
            raise AnthropicError("pricing_unverified", "price_or_limit_invalid")
    if not isinstance(price_version, str):
        raise AnthropicError("pricing_unverified", "price_or_limit_invalid")
    definition = PRICE_DEFINITIONS.get((model, price_version))
    if (definition is None or definition.get("provider") not in ("anthropic", "openai")
            or definition.get("model") != model or definition.get("price_version") != price_version):
        raise AnthropicError("pricing_unverified", "price_or_limit_invalid")
    return definition


_BINDING_KEYS = ("reservation_id", "request_fp", "route", "project_id",
                 "unit_key", "message_id", "model", "price_version", "logical_request_fp", "logical_target_id", "logical_unit_fp",
                 "effort", "profile")


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


def _history_thread_allowed(db, project_id, message_id, sources, bindings=None):
    from inference_dependency_loss import source_binding
    target = db.execute("SELECT COALESCE(parent_id,message_id) FROM messages "
                        "WHERE project_id=? AND message_id=?", (project_id, message_id)).fetchone()
    if target is None:
        return False
    members = db.execute("SELECT message_id FROM messages WHERE project_id=? "
                         "AND (message_id=? OR parent_id=?)", (project_id, target[0], target[0])).fetchall()
    for row in members:
        binding = source_binding(db, project_id, row[0])
        if binding is None or sources is not None and sources.get(f"{project_id}:{row[0]}") != binding:
            return False
        if bindings is not None:
            bindings.append((row[0], binding))
    return bool(members)


def history_thread_snapshot(db, project_id, message_id):
    """Bind the active scope and every current thread occurrence to one read snapshot."""
    anchor = db.execute("SELECT artifact_id,content,meta FROM (SELECT artifact_id,content,meta FROM artifacts "
                        "WHERE kind='cloud_history_scope' ORDER BY artifact_id DESC LIMIT 1) "
                        "UNION ALL SELECT NULL,NULL,NULL UNION ALL SELECT NULL,NULL,NULL")
    try:
        scope = anchor.fetchone()
        if scope is None or scope[0] is None:
            return None
        sources = _history_scope(db, (scope[1], scope[2]))
        bindings = []
        if not _history_thread_allowed(db, project_id, message_id, sources, bindings):
            return None
        bindings.sort()
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
    if block.keys() - out.keys() - {"profiles"}:
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
    if "model" in block:
        candidate = block["model"]
        allowed = {PROVIDERS["anthropic"], *openai_client.CURRENT_PRICE_VERSIONS}
        try:
            owner = _price_definition(candidate)["provider"] if candidate in allowed else None
        except (AnthropicError, TypeError):
            owner = None
        if owner is None or provider != "auto" and owner != provider or provider == "auto" and owner not in out["provider_order"]:
            errors.append("config: cloud_llm_model_invalid")
        else:
            out["model"] = candidate
    if "profiles" in block:
        profiles = block["profiles"]
        if (not isinstance(profiles, dict) or profiles.keys() - openai_client.PROFILE_PURPOSES
                or any(not isinstance(role, str) or not isinstance(value, str)
                       or value not in openai_client.PROFILE_DEFINITIONS for role, value in profiles.items())
                or provider == "anthropic"):
            errors.append("config: cloud_llm_profiles_invalid")
        else:
            out["profiles"] = dict(profiles)
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
    return private_env_value(anthropic_client.KEY_ENV, root)


def provider_key(provider, root=None):
    return api_key(root) if provider == "anthropic" else private_env_value(openai_client.KEY_ENV, root) if provider == "openai" else None


def resolve_settings(cfg, root=None):
    """Choose a configured provider by key availability; never retry a paid unit elsewhere."""
    c, errors = cloud_config(cfg)
    provider = c["provider"]
    if provider == "auto":
        provider = next((name for name in c["provider_order"] if provider_key(name, root)), c["provider_order"][0])
    block = cfg.get("cloud_llm") if isinstance(cfg, dict) else None
    if (block is None or isinstance(block, dict) and "mode" not in block) and not errors:
        c, errors = cloud_config(cfg, key_present=bool(provider_key(provider, root)))
    c["provider"] = provider
    if isinstance(block, dict) and "model" in block:
        try:
            if _price_definition(c["model"])["provider"] != provider:
                errors.append("config: cloud_llm_model_invalid")
        except AnthropicError:
            errors.append("config: cloud_llm_model_invalid")
    else:
        c["model"] = PROVIDERS[provider]
    return c, errors


def resolve_profile(cfg, *, route, purpose, root=None):
    """Resolve one explicit use to a bounded, immutable provider selection."""
    settings, errors = resolve_settings(cfg, root)
    if errors:
        return None, errors
    return profile_for_settings(cfg, settings, route=route, purpose=purpose)


def profile_for_settings(cfg, settings, *, route, purpose):
    """Resolve a profile from a provider already selected by the caller."""
    valid_purposes = {"reextract": {"backlog", "quality_repair"},
                      "semantic_backlog": {"backlog", "quality_repair"},
                      "patient_context": {"patient_context", "quality_repair"}}
    if (not isinstance(route, str) or not isinstance(purpose, str)
            or route not in valid_purposes or purpose not in valid_purposes[route]):
        return None, ["config: cloud_profile_purpose_invalid"]
    provider = settings["provider"]
    block = cfg.get("cloud_llm") if isinstance(cfg, dict) else None
    block = block if isinstance(block, dict) else {}
    if provider != "openai":
        return MappingProxyType({"provider": provider, "model": settings["model"], "effort": None,
            "max_tokens": None, "price_version": _price_definition(settings["model"])["price_version"],
            "profile": None}), []
    profiles = block.get("profiles", {})
    if not isinstance(profiles, dict):
        return None, ["config: cloud_llm_profiles_invalid"]
    profile = profiles.get(purpose)
    if profile is None and block.get("model") == openai_client.MODEL:
        model, effort = openai_client.MODEL, "low"
        limit = {"backlog": 8192, "patient_context": 16384, "quality_repair": 32768}[purpose]
    else:
        profile = profile if profile is not None else (
            "sol-medium" if block.get("model") == openai_client.SOL_MODEL
            else openai_client.DEFAULT_PROFILES[purpose])
        if profile not in openai_client.PROFILE_DEFINITIONS:
            return None, ["config: cloud_llm_profiles_invalid"]
        model, effort = openai_client.PROFILE_DEFINITIONS[profile]
        limit = {"backlog": 8192, "patient_context": 16384, "quality_repair": 32768}[purpose]
    return MappingProxyType({"provider": provider, "model": model, "effort": effort,
        "max_tokens": limit, "price_version": _price_definition(model)["price_version"],
        "profile": profile}), []


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
    try:
        definition = _price_definition(model)
    except AnthropicError:
        return False
    day = datetime.fromtimestamp(time.time() if now is None else now, JST).date().isoformat()
    return definition["price_checked_on"] <= day <= definition["price_valid_until"]


def price_info(model):
    definition = _price_definition(model)
    return {"price_version": definition["price_version"],
            "price_checked_on": definition["price_checked_on"],
            "price_valid_until": definition["price_valid_until"],
            "price_valid": pricing_valid(model=model)}


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


def _reservation_cost(input_bound, max_tokens, *, legacy=False, model=MODEL, price_version=_CURRENT_PRICE):
    definition = _price_definition(model, price_version)
    prices = definition["prices"]
    if definition["provider"] == "openai":
        if legacy:
            raise AnthropicError("pricing_unverified", "reservation_bound_invalid")
        return (Decimal(input_bound) * Decimal(prices["reservation_input"])
                + Decimal(max_tokens) * Decimal(prices["reservation_output"])) / Decimal(1000000)
    rate = Decimal(prices["long_input"]) * (1 if legacy else max(
        Decimal(1), Decimal(prices["cache_write_5m_multiplier"]), Decimal(prices["cache_write_1h_multiplier"])))
    return (Decimal(input_bound) * rate + Decimal(max_tokens) * Decimal(prices["long_output"])) / Decimal(1000000)


def max_cost(max_tokens, *, input_bound=INPUT_RESERVATION_BOUND, now=None, model=MODEL):
    definition = _price_definition(model)
    if (not pricing_valid(now, model=model) or type(max_tokens) is not int or not 1 <= max_tokens <= definition["max_tokens"]
            or type(input_bound) is not int or not 1 <= input_bound <= INPUT_RESERVATION_BOUND):
        raise AnthropicError("pricing_unverified", "price_or_limit_invalid")
    return _reservation_cost(input_bound, max_tokens, model=model)


def usage_cost(usage, *, model=MODEL, price_version=_CURRENT_PRICE):
    definition = _price_definition(model, price_version)
    prices = definition["prices"]
    u = definition["validate_usage"](usage)
    if definition["provider"] == "openai":
        if u["input_tokens"] > INPUT_RESERVATION_BOUND or u["output_tokens"] > definition["max_tokens"]:
            raise AnthropicError("pricing_unverified", "usage_bounds")
        cached, written = (u["input_tokens_details"][k] for k in ("cached_tokens", "cache_write_tokens"))
        long = u["input_tokens"] > definition["long_input_threshold"]
        value = (Decimal(u["input_tokens"] - cached - written) * Decimal(prices["input"])
                 + Decimal(cached) * Decimal(prices["cached_input"]) + Decimal(written) * Decimal(prices["cache_write_30m"]))
        if long:
            value *= Decimal(prices["long_input_multiplier"])
        return (value + Decimal(u["output_tokens"]) * Decimal(prices["long_output"] if long else prices["output"])) / Decimal(1000000)
    total_input = u["input_tokens"] + u["cache_creation_input_tokens"] + u["cache_read_input_tokens"]
    if total_input > INPUT_RESERVATION_BOUND or u["output_tokens"] > definition["max_tokens"]:
        raise AnthropicError("pricing_unverified", "usage_bounds")
    long = total_input > definition["long_input_threshold"]
    input_price, output_price = (Decimal(prices["long_input"]), Decimal(prices["long_output"])) if long else (Decimal(prices["input"]), Decimal(prices["output"]))
    cache = u.get("cache_creation", {})
    cost = (u["input_tokens"] * input_price + u["output_tokens"] * output_price
            + cache.get("ephemeral_5m_input_tokens", 0) * input_price * Decimal(prices["cache_write_5m_multiplier"])
            + cache.get("ephemeral_1h_input_tokens", 0) * input_price * Decimal(prices["cache_write_1h_multiplier"])
            + u["cache_read_input_tokens"] * input_price * Decimal(prices["cache_read_multiplier"]))
    return cost / Decimal(1000000)


def _unbilled(http_class):
    """Only an attempt proven never sent settles at zero. Every HTTP outcome keeps
    the reserved maximum: the published error list does not state which statuses
    are unbilled."""
    return http_class == "not_sent"


def _periods(now):
    try:
        if type(now) not in (int, float) or not math.isfinite(now):
            raise ValueError
        date = datetime.fromtimestamp(now, JST)
        day = date.replace(hour=0, minute=0, second=0, microsecond=0)
        return day.timestamp(), day.replace(day=1).timestamp()
    except (TypeError, ValueError, OverflowError, OSError):
        raise AnthropicError("pricing_unverified", "usage_timestamp_invalid") from None


def _loads(value):
    try:
        result = json.loads(value)
        if not isinstance(result, dict):
            raise ValueError
        return result
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
        raise AnthropicError("pricing_unverified", "usage_invalid") from None


def _accounting_meta(value):
    try:
        result = strict_json(value)
        if not isinstance(result, dict):
            raise ValueError
        return result
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
        raise AnthropicError("pricing_unverified", "usage_invalid") from None


@contextmanager
def _accounting_snapshot(db):
    # A pending result keeps the SQLite read snapshot without committing a caller's transaction.
    anchor = db.execute("SELECT artifact_id FROM (SELECT artifact_id FROM artifacts "
        "WHERE kind IN ('cloud_usage_reserve','cloud_usage_result') LIMIT 1) "
        "UNION ALL SELECT NULL UNION ALL SELECT NULL")
    try:
        anchor.fetchone()
        yield
    finally:
        anchor.close()


def _validate_accounting_timestamps(db):
    # Values outside this conservative interior are checked precisely; no
    # otherwise valid historical timestamp is excluded by these scan bounds.
    candidates = db.execute("SELECT created_at FROM artifacts WHERE kind IN (?,?) AND CASE "
        "WHEN typeof(created_at) NOT IN ('integer','real') THEN 1 "
        "ELSE created_at < ? OR created_at > ? END", (KINDS[0], KINDS[1], -62135500000, 253402000000))
    for row in candidates:
        _periods(row[0])


def _binding(meta):
    return tuple(meta.get(k) for k in _BINDING_KEYS)


def _receipt_binding(reservation):
    return {key: reservation.get(key) for key in _BINDING_KEYS
            if key not in ("effort", "profile") or key in reservation}


def _reserve_valid(meta, row):
    model = meta.get("model")
    price_version = meta.get("price_version")
    if not isinstance(price_version, str):
        raise AnthropicError("pricing_unverified", "reservation_invalid")
    definition = _price_definition(model, price_version)
    if ("effort" in meta) != ("profile" in meta):
        raise AnthropicError("pricing_unverified", "reservation_invalid")
    if "effort" in meta:
        effort, profile = meta["effort"], meta["profile"]
        if definition["provider"] == "openai":
            if (not isinstance(effort, str) or profile is not None and not isinstance(profile, str)
                    or (model, effort) not in openai_client.VALID_MODEL_EFFORTS
                    or profile is not None and (openai_client.PROFILE_DEFINITIONS.get(profile) != (model, effort)
                        or meta.get("max_tokens") not in (8192, 16384, 32768))
                    or profile is None and (model != openai_client.MODEL or effort != "low")):
                raise AnthropicError("pricing_unverified", "reservation_invalid")
        elif effort is not None or profile is not None:
            raise AnthropicError("pricing_unverified", "reservation_invalid")
    if (not isinstance(meta.get("reservation_id"), str) or len(meta["reservation_id"]) != 32
            or not isinstance(meta.get("request_fp"), str) or len(meta["request_fp"]) != 64
            or meta.get('logical_request_fp') is not None and (not isinstance(meta['logical_request_fp'], str)
                or re.fullmatch(r'[0-9a-f]{64}', meta['logical_request_fp']) is None)
            or meta.get('logical_unit_fp') is not None and (not isinstance(meta['logical_unit_fp'], str)
                or re.fullmatch(r'[0-9a-f]{64}', meta['logical_unit_fp']) is None)
            or meta.get('logical_target_id') is not None and (type(meta['logical_target_id']) is not int
                or meta['logical_target_id'] <= 0 or meta['logical_target_id'] != row['message_id'])
            or row["model"] != model
            or meta.get("route") not in ROUTES
            or not definition["legacy_reservation"] and "input_bound" not in meta
            or type(meta.get("project_id")) is not int or meta["project_id"] <= 0
            or meta["project_id"] != row["project_id"]
            or meta.get("message_id") != row["message_id"]
            or not isinstance(meta.get("unit_key"), str) or not meta["unit_key"]):
        raise AnthropicError("pricing_unverified", "reservation_invalid")
    tokens = meta.get("max_tokens")
    bound = meta.get("input_bound", INPUT_RESERVATION_BOUND)   # rows before the request-sized bound
    maximum = _decimal(meta.get("max_cost_usd"))
    if (type(tokens) is not int or not 1 <= tokens <= definition["max_tokens"]
            or type(bound) is not int or not 1 <= bound <= INPUT_RESERVATION_BOUND
            or maximum != _reservation_cost(bound, tokens, legacy="input_bound" not in meta,
                                            model=model, price_version=price_version)):
        raise AnthropicError("pricing_unverified", "reservation_bound_invalid")
    return maximum


def _stored_reservation(ledger, reservation, *, bound_only=False):
    """The one retained reservation row matching ``reservation``, validated."""
    rid = reservation.get("reservation_id")
    rows = (_matching_bound_rows(ledger, KINDS[0], rid) if bound_only
            else _matching_rows(ledger, KINDS[0], rid))
    if len(rows) != 1:
        raise AnthropicError("pricing_unverified", "reservation_missing")
    if bound_only and rows[0]["created_at"] < time.time() - 62 * 86400:
        raise AnthropicError("pricing_unverified", "reservation_missing")
    stored = _accounting_meta(rows[0]["meta"])
    _reserve_valid(stored, rows[0])
    if stored != reservation:
        raise AnthropicError("pricing_unverified", "reservation_mismatch")
    return stored


def _matching_rows_many(ledger, kind, reservation_ids, *, now=None, _timestamps_checked=False):
    """Select retained reservation identities in SQL, preserving every duplicate match."""
    if not _timestamps_checked:
        with _accounting_snapshot(ledger.db):
            _validate_accounting_timestamps(ledger.db)
            return _matching_rows_many(ledger, kind, reservation_ids, now=now, _timestamps_checked=True)
    ids = list(dict.fromkeys(reservation_ids))
    cutoff = (time.time() if now is None else now) - 62 * 86400
    rows = []
    if ids and ledger.db.execute(
            "SELECT 1 FROM artifacts WHERE kind=? AND created_at>=? AND "
            "CASE WHEN typeof(meta)!='text' OR length(meta)>32768 OR NOT json_valid(meta) THEN 1 "
            "WHEN json_type(meta)!='object' THEN 1 "
            "WHEN EXISTS (SELECT 1 FROM json_tree(meta) WHERE key IS NOT NULL "
            "GROUP BY parent,key HAVING COUNT(*)>1) THEN 1 "
            "WHEN EXISTS (SELECT 1 FROM json_tree(meta) WHERE type='real' "
            "AND abs(value)>1.7976931348623157e308) THEN 1 ELSE 0 END LIMIT 1",
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


def _matching_bound_rows(ledger, kind, reservation_id):
    """Inspect all retained rows naming this reservation, without unrelated accounting rows."""
    if not isinstance(reservation_id, str) or len(reservation_id) != 32:
        raise AnthropicError("pricing_unverified", "reservation_invalid")
    rows = ledger.db.execute(
        "SELECT artifact_id,kind,project_id,message_id,model,meta,created_at FROM artifacts "
        "WHERE kind=? AND CASE WHEN json_valid(meta) THEN EXISTS "
        "(SELECT 1 FROM json_each(meta) WHERE key='reservation_id' AND type='text' AND value=?) "
        "WHEN typeof(meta)='text' THEN instr(meta,?)>0 "
        "WHEN typeof(meta)='blob' THEN instr(meta,CAST(? AS BLOB))>0 ELSE 0 END ORDER BY artifact_id",
        (kind, reservation_id, reservation_id, reservation_id)).fetchall()
    for row in rows:
        if not isinstance(row["meta"], str) or len(row["meta"]) > 32768:
            raise AnthropicError("pricing_unverified", "usage_invalid")
        _accounting_meta(row["meta"])
        _periods(row["created_at"])
    return rows


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
    return reservation_evidence_batch(db, [artifact_id])[artifact_id]


def reservation_evidence_batch(db, artifact_ids):
    # All financial rows are checked in one borrowed snapshot. No proof is
    # cached outside this call, so later changes must be checked again.
    try:
        ids = list(artifact_ids)
        if any(type(item) is not int or not 0 < item < 2**63 for item in ids):
            raise ValueError("cloud_receipt_invalid")
        ids = list(dict.fromkeys(ids))
        if not ids:
            return {}
        with closing(db.execute('SELECT artifact_id FROM (SELECT artifact_id FROM artifacts LIMIT 1) '
                                'UNION ALL SELECT NULL UNION ALL SELECT NULL')) as anchor:
            anchor.fetchone()
            return _reservation_evidence_batch_snapshot(db, ids)
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
        if "answer_text_sha256_v1" in result_meta:
            answer_hash = result_meta["answer_text_sha256_v1"]
            if not isinstance(answer_hash, str) or re.fullmatch(r"[0-9a-f]{64}", answer_hash) is None:
                raise ValueError("cloud_answer_hash_invalid")
        if ("answer_finish_reason_v1" in result_meta
                and result_meta["answer_finish_reason_v1"] not in ("stop", "length")):
            raise ValueError("cloud_answer_finish_reason_invalid")
        result[artifact_id] = {'reservation': meta, 'result': result_meta,
                               'witness': payload_hash([tuple(row) for row, _ in matched])}
    if (db.execute('PRAGMA data_version').fetchone()[0], db.total_changes) != before:
        raise ValueError("cloud_receipt_changed")
    return result


def _is_openai_model(model):
    return (isinstance(model, str)
            and any(key[0] == model and definition["provider"] == "openai"
                    for key, definition in PRICE_DEFINITIONS.items()))


def _confirmed_openai_quota(result, model):
    code = result.get("openai_error_code")
    return (isinstance(code, str) and code in openai_client.QUOTA_ERROR_CODES
            and _is_openai_model(model) and "openai_429_diagnostic" not in result
            and result.get("http_class") == "http_429"
            and result.get("error_kind") == "payment_required"
            and "usage" in result and result["usage"] is None)


def _confirmed_openai_rate_diagnostic(result, model):
    diagnostic = result.get("openai_429_diagnostic")
    return (isinstance(diagnostic, str) and diagnostic in openai_client.RATE_LIMIT_DIAGNOSTICS
            and _is_openai_model(model) and "openai_error_code" not in result
            and result.get("http_class") == "http_429"
            and result.get("error_kind") == "rate_limited"
            and "usage" in result and result["usage"] is None)


def _settled(m, maximum, matching):
    """(cost, settled?) of one reservation's single bound result; raises when
    the result is duplicated, unbound or its usage/cost disagrees."""
    if len(matching) != 1:
        raise ValueError
    r, row = matching[0]
    if (_binding(m) != _binding(r) or row["project_id"] != m["project_id"]
            or row["message_id"] != m["message_id"] or row["model"] != m["model"]):
        raise ValueError
    if "openai_error_code" in r and not _confirmed_openai_quota(r, m["model"]):
        raise ValueError
    if "openai_429_diagnostic" in r and not _confirmed_openai_rate_diagnostic(r, m["model"]):
        raise ValueError
    reported = _decimal(r.get("cost_usd"))
    if r.get("usage") is None:
        if reported != (Decimal(0) if _unbilled(r.get("http_class")) else maximum):
            raise ValueError
        return reported, _unbilled(r.get("http_class"))
    if (reported != usage_cost(r["usage"], model=m["model"], price_version=m["price_version"]) or reported > maximum
            or r["usage"].get("output_tokens", 0) > m["max_tokens"]):
        raise ValueError
    return reported, True


def _semantic_financial_target(meta):
    owner = meta.get('owner')
    target = meta.get('logical_target_id')
    if (not isinstance(owner, dict) or type(target) is not int or target <= 0
            or target != meta.get('message_id') or type(owner.get('semantic_target_id')) is not int
            or owner.get('semantic_target_id') != target):
        return None
    return target


def _semantic_financial_identity(meta):
    """Verify the historical unit fingerprint before comparing raw source and phase."""
    target = _semantic_financial_target(meta)
    if target is None:
        return None
    owner = meta['owner']
    source, generation, phase = (owner.get('semantic_source'), owner.get('semantic_generation'),
                                 owner.get('semantic_phase'))
    if (not isinstance(source, str) or re.fullmatch(r'[0-9a-f]{64}', source) is None
            or not isinstance(generation, str) or not generation or len(generation) > 1024
            or phase not in ('facts', 'summary')):
        return None
    expected = payload_hash({'target': target, 'source': source, 'generation': generation, 'phase': phase})
    if meta.get('logical_unit_fp') != expected:
        return None
    return target, source, phase


def _legacy_luna_low_proven(previous, current_body):
    """Prove the old low-effort Responses envelope without persisting the prompt."""
    if (previous.get('model') != openai_client.MODEL or 'effort' in previous or 'profile' in previous
            or not isinstance(current_body, dict)
            or set(current_body) != {'model', 'instructions', 'input', 'max_output_tokens', 'reasoning',
                                     'store', 'service_tier', 'prompt_cache_options', 'tools'}
            or current_body.get('model') not in openai_client.CURRENT_PRICE_VERSIONS
            or current_body.get('instructions') != openai_client.SYSTEM
            or not isinstance(current_body.get('input'), str)
            or current_body.get('store') is not False or current_body.get('service_tier') != 'default'
            or current_body.get('prompt_cache_options') != {'ttl': '30m'} or current_body.get('tools') != []):
        return False
    old_body = {**current_body, 'model': openai_client.MODEL,
                'max_output_tokens': previous['max_tokens'], 'reasoning': {'effort': 'low'}}
    return payload_hash(old_body) == previous.get('request_fp')


def _selection_differs(previous, candidate, current_body):
    if previous['model'] != candidate['model']:
        return True
    new_effort = candidate.get('effort')
    if not isinstance(new_effort, str):
        return False
    old_effort = previous.get('effort')
    if old_effort is None and _legacy_luna_low_proven(previous, current_body):
        old_effort = 'low'
    return isinstance(old_effort, str) and old_effort != new_effort


def _guard_repeated_request(ledger, candidate, *, current_body=None):
    """Keep an unresolved or already answered logical request from being sent again."""
    rows = ledger.db.execute("SELECT artifact_id,kind,project_id,message_id,model,meta,created_at "
        "FROM artifacts WHERE kind=? AND project_id=? AND (message_id IS ? OR ?='semantic_backlog') AND "
        "CASE WHEN typeof(meta)!='text' OR length(meta)>32768 OR NOT json_valid(meta) THEN 1 "
        "WHEN json_type(meta)!='object' THEN 1 "
        "WHEN json_extract(meta,'$.route')=? AND "
        "(json_extract(meta,'$.unit_key')=? OR ? IN ('semantic_backlog','patient_context','reextract')) THEN 1 "
        "ELSE EXISTS (SELECT 1 FROM json_tree(meta) "
        "WHERE key IS NOT NULL GROUP BY parent,key HAVING COUNT(*)>1) END ORDER BY artifact_id",
        (KINDS[0], candidate['project_id'], candidate['message_id'], candidate['route'],
         candidate['route'], candidate['unit_key'], candidate['route'])).fetchall()
    result_map = None
    disposition_ids = None
    for row in rows:
        try:
            previous = _receipt_meta(row['meta'])
            maximum = _reserve_valid(previous, row)
            if candidate['route'] == 'semantic_backlog':
                old_target = _semantic_financial_target(previous)
                new_target = _semantic_financial_target(candidate)
                if old_target is not None and new_target is not None and old_target != new_target:
                    continue
                old_identity = _semantic_financial_identity(previous)
                new_identity = _semantic_financial_identity(candidate)
                if old_identity is not None and new_identity is not None and old_identity != new_identity:
                    continue
            old_fp, new_fp = previous.get('logical_request_fp'), candidate.get('logical_request_fp')
            prompt_verified = False
            if old_fp is not None and new_fp is not None:
                if old_fp != new_fp:
                    continue
                prompt_verified = True
            elif _legacy_luna_low_proven(previous, current_body):
                prompt_verified = True
            # Legacy reservations contain only an envelope hash. A model or
            # token-limit change cannot prove that their prompt is different.
            if result_map is None:
                result_map = {}
                disposition_ids = set()
                results = ledger.db.execute("SELECT artifact_id,kind,project_id,message_id,model,meta,created_at "
                    "FROM artifacts WHERE kind IN (?,?) AND project_id=? AND (message_id IS ? OR ?='semantic_backlog') "
                    "ORDER BY artifact_id",
                    (KINDS[1], 'cloud_usage_disposition', candidate['project_id'],
                     candidate['message_id'], candidate['route']))
                for result_row in results:
                    result_meta = _receipt_meta(result_row['meta'])
                    reservation_id = result_meta.get('reservation_id')
                    if result_row['kind'] == 'cloud_usage_disposition':
                        if not isinstance(reservation_id, str) or re.fullmatch(r'[0-9a-f]{32}', reservation_id) is None:
                            raise ValueError('invalid_disposition')
                        disposition_ids.add(reservation_id)
                    else:
                        result_map.setdefault(reservation_id, []).append((result_meta, result_row))
            if previous['reservation_id'] in disposition_ids:
                raise ValueError('discarded')
            matches = result_map.get(previous['reservation_id'], [])
            _, settled = _settled(previous, maximum, matches)
            result = matches[0][0]
            if result.get('disposition'):
                raise ValueError('discarded')
            if settled and result.get('http_class') == 'not_sent':
                continue
            # A confirmed successful result may be deliberately reprocessed
            # with another model. An ambiguous send never grants that retry.
            if (prompt_verified and settled and result.get('usage') is not None and result.get('http_class') == 'http_200'
                    and result.get('error_kind') is None and _selection_differs(previous, candidate, current_body)):
                continue
            # A validated rate-limit response rejected this attempt. Its
            # reserved cost still remains counted under the existing policy.
            if (result.get('http_class') == 'http_429' and result.get('error_kind') == 'rate_limited'
                    and result.get('usage') is None):
                continue
            if _confirmed_openai_quota(result, previous['model']):
                continue
            raise ValueError('already_sent_or_unknown')
        except (AnthropicError, InvalidOperation, ValueError, TypeError, KeyError, OverflowError):
            raise AnthropicError('budget_exceeded', 'cloud_unit_unresolved') from None


def usage_report(ledger, *, now=None, period="month"):
    """Aggregate JST attempts; ambiguous results retain the full reserved amount.
    All retained accounting timestamps must be readable. With ``period="day"``,
    the count and cost fields cover only today's rows."""
    try:
        with _accounting_snapshot(ledger.db):
            _validate_accounting_timestamps(ledger.db)
            return _usage_report_snapshot(ledger, now=now, period=period)
    except (AnthropicError, ValueError, InvalidOperation, TypeError, OverflowError, OSError):
        return {"daily_requests": None, "monthly_cost_usd": None,
                "settled_cost_usd": None, "max_reserved_cost_usd": None,
                "unresolved_requests": None, "pricing_unverified": True}


def _usage_report_snapshot(ledger, *, now=None, period="month"):
    now = time.time() if now is None else now
    day, month = _periods(now)
    rows = list(ledger.db.execute(
        "SELECT artifact_id,kind,project_id,message_id,model,meta,created_at FROM artifacts "
        "WHERE kind IN ('cloud_usage_reserve','cloud_usage_result') "
        "AND created_at>=? ORDER BY artifact_id", (day if period == "day" else month,)))
    reserves, results = {}, {}
    daily, invalid = 0, False
    total, settled, unknown = Decimal(0), Decimal(0), 0
    provider_names = set(PROVIDERS) | {definition["provider"] for definition in PRICE_DEFINITIONS.values()}
    providers = {name: {"cost_usd": Decimal(0), "unresolved_requests": 0} for name in sorted(provider_names)}
    try:
        for row in rows:
            m = _accounting_meta(row["meta"])
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
            name = _price_definition(m["model"], m["price_version"])["provider"]
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
        for row in _matching_rows_many(ledger, KINDS[0], orphan_ids, now=now, _timestamps_checked=True):
            meta = _accounting_meta(row["meta"])
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
        return {"daily_requests": None, "monthly_cost_usd": None,
                "settled_cost_usd": None, "max_reserved_cost_usd": None,
                "unresolved_requests": None, "pricing_unverified": True}


def _circuit_result(ledger, error, *, reset=False):
    from semantic_runtime import record_circuit_result
    return record_circuit_result(ledger, error, kind="cloud_circuit", reset=reset)


def block_reason(ledger, cfg, route, project_id, *, model=None):
    if not eligible(cfg, route, project_id):
        return "scope"
    return global_block_reason(ledger, model=_cfg(cfg)["model"] if model is None else model)


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
    unchanged rows are hashed without decoding their metadata again; changed
    history rebuilds the tally, and unreadable history stops the gate."""

    def __init__(self, month, connection):
        self.month = month
        self.connection = connection
        self.precheck = None
        self.last_id = 0
        self.prefix = None   # fingerprint of the rows already tallied
        self.open = {}       # rid -> (meta, maximum, day): reserved, no result yet
        self.closed = {}     # rid -> (maximum, cost, settled, day): one result seen
        self.orphans = {}    # rid -> (prior-month reservation rows, results)
        self.checked_at = None
        self.history_valid_until = math.inf
        self.days = {}       # day -> [requests, cost]
        self.provider_days = {}
        self.provider_totals = {}
        self.total = Decimal(0)
        self.invalid = False

    def _add(self, day, cost, provider):
        self.days.setdefault(day, [0, Decimal(0)])[1] += cost
        self.total += cost
        self.provider_days[(provider, day)] = self.provider_days.get((provider, day), Decimal(0)) + cost
        self.provider_totals[provider] = self.provider_totals.get(provider, Decimal(0)) + cost

    def fingerprint(self, ledger, now):
        """Hash all accounting columns, including reservations outside the period."""
        digest = hashlib.sha256()
        with closing(ledger.db.execute(
            "SELECT artifact_id,kind,project_id,message_id,model,meta,created_at FROM artifacts "
            "WHERE kind IN ('cloud_usage_reserve','cloud_usage_result') AND created_at>=? "
            "AND artifact_id<=? ORDER BY artifact_id", (self.month, self.last_id))) as rows:
            prior = _matching_rows_many(ledger, KINDS[0], self.orphans, now=now, _timestamps_checked=True)
            try:
                for row in chain(rows, sorted(prior, key=lambda row: row['artifact_id'])):
                    digest.update(canonical(tuple(row)))
                    digest.update(b'\n')
            except (TypeError, ValueError, UnicodeError):
                raise AnthropicError('pricing_unverified', 'usage_invalid') from None
        return digest.digest()

    def update(self, ledger, now):
        last_id = self.last_id
        new_orphans = {}
        changed_orphans = set()
        with closing(ledger.db.execute(
            "SELECT artifact_id,kind,project_id,message_id,model,meta,created_at FROM artifacts "
            "WHERE kind IN ('cloud_usage_reserve','cloud_usage_result') AND created_at>=? "
            "AND artifact_id>? ORDER BY artifact_id", (self.month, self.last_id))) as rows:
            for row in rows:
                m = _accounting_meta(row["meta"])
                rid = m.get("reservation_id")
                if row["kind"] == "cloud_usage_reserve":
                    maximum = _reserve_valid(m, row)
                    if rid in self.open or rid in self.closed:
                        raise AnthropicError("pricing_unverified", "duplicate_reservation")
                    day = _periods(row["created_at"])[0]
                    self.open[rid] = (m, maximum, day)
                    self.days.setdefault(day, [0, Decimal(0)])[0] += 1
                    self._add(day, maximum, _price_definition(m["model"], m["price_version"])["provider"])
                elif not isinstance(rid, str):
                    raise AnthropicError("pricing_unverified", "result_invalid")
                elif rid in self.open:
                    meta, maximum, day = self.open.pop(rid)
                    try:
                        cost, settled = _settled(meta, maximum, [(m, row)])
                    except (AnthropicError, ValueError, InvalidOperation, TypeError):
                        self.invalid = True
                        cost, settled = maximum, False
                    provider = _price_definition(meta["model"], meta["price_version"])["provider"]
                    self._add(day, cost - maximum, provider)
                    self.closed[rid] = (maximum, cost, settled, day, provider)
                elif rid in self.closed:
                    # a second result row: the pair no longer settles (usage_report's _settled)
                    maximum, cost, settled, day, provider = self.closed[rid]
                    self.invalid = True
                    self._add(day, maximum - cost, provider)
                    self.closed[rid] = (maximum, maximum, False, day, provider)
                else:
                    entry = self.orphans.get(rid)
                    if entry is None:
                        entry = self.orphans[rid] = ([], [])
                        new_orphans[rid] = entry
                    entry[1].append((m, row))
                    changed_orphans.add(rid)
                self.last_id = row["artifact_id"]
        # Bind all late results together; each lookup validates retained history.
        for row in _matching_rows_many(ledger, KINDS[0], new_orphans, now=now, _timestamps_checked=True):
            meta = _accounting_meta(row["meta"])
            new_orphans[meta["reservation_id"]][0].append((meta, row))
            self.history_valid_until = min(self.history_valid_until, row['created_at'] + 62 * 86400)
        for rid in changed_orphans:
            prior, results = self.orphans[rid]
            try:
                if len(prior) != 1:
                    raise ValueError
                _settled(prior[0][0], _reserve_valid(*prior[0]), results)
            except (AnthropicError, ValueError, InvalidOperation, TypeError):
                self.invalid = True
        # The caller already verified this prefix in the same read snapshot.
        if self.prefix is None or self.last_id != last_id:
            self.prefix = self.fingerprint(ledger, now)
        self.checked_at = now


def _tally_precheck(db, start):
    if db.in_transaction:
        return None
    return db, start, db.execute('PRAGMA data_version').fetchone()[0], db.total_changes


def _current_tally(ledger, start, attribute, now):
    tally = getattr(ledger, attribute, None)
    before = _tally_precheck(ledger.db, start)
    previous = tally.precheck if tally is not None else None
    if (before is not None and before == previous and not tally.invalid
            and (not tally.orphans or tally.checked_at <= now <= tally.history_valid_until)):
        return tally
    if tally is not None:
        tally.precheck = None
    try:
        with _accounting_snapshot(ledger.db):
            _validate_accounting_timestamps(ledger.db)
            if (tally is None or tally.connection is not ledger.db or tally.month != start or tally.invalid
                    or (tally.prefix is not None and tally.prefix != tally.fingerprint(ledger, now))):
                tally = _Tally(start, ledger.db)
                setattr(ledger, attribute, tally)
            tally.update(ledger, now)
    except BaseException:
        setattr(ledger, attribute, None)
        raise
    if before is not None:
        if before == _tally_precheck(ledger.db, start) and not tally.invalid:
            tally.precheck = before
        else:
            # A concurrent write must not attach a fresh stamp to an older total.
            setattr(ledger, attribute, None)
    return tally


def _tally(ledger, now=None):
    now = time.time() if now is None else now
    return _current_tally(ledger, _periods(now)[1], "_cloud_usage_tally", now)


def _window_tally(ledger, start):
    """The same incremental tally as the monthly one, bounded below by the period start."""
    return _current_tally(ledger, start, "_cloud_window_tally", time.time())


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


def _selection_model(settings, selection, max_tokens):
    if selection is None:
        return settings["model"]
    if (not isinstance(selection, Mapping) or set(selection) !=
            {"provider", "model", "effort", "max_tokens", "price_version", "profile"}
            or selection["provider"] != settings["provider"]):
        raise AnthropicError("contract_error", "cloud_profile_invalid")
    model, effort, profile = selection["model"], selection["effort"], selection["profile"]
    if (not isinstance(model, str) or effort is not None and not isinstance(effort, str)
            or profile is not None and not isinstance(profile, str)
            or selection["max_tokens"] is not None and type(selection["max_tokens"]) is not int):
        raise AnthropicError("contract_error", "cloud_profile_invalid")
    definition = _price_definition(model)
    if (definition["provider"] != settings["provider"] or selection["price_version"] != definition["price_version"]
            or selection["max_tokens"] is not None and selection["max_tokens"] != max_tokens):
        raise AnthropicError("contract_error", "cloud_profile_invalid")
    if settings["provider"] == "openai":
        if ((model, effort) not in openai_client.VALID_MODEL_EFFORTS
                or profile is not None and (openai_client.PROFILE_DEFINITIONS.get(profile) != (model, effort)
                    or selection["max_tokens"] not in (8192, 16384, 32768))
                or profile is None and (model != openai_client.MODEL or effort != "low"
                    or selection["max_tokens"] not in (8192, 16384, 32768))):
            raise AnthropicError("contract_error", "cloud_profile_invalid")
    elif effort is not None or profile is not None or model != PROVIDERS["anthropic"]:
        raise AnthropicError("contract_error", "cloud_profile_invalid")
    return model


def budget_block_reason(ledger, cfg, *, max_tokens=1600, input_bound=INPUT_RESERVATION_MARGIN, selection=None):
    """Read the exact current JST reservation totals without spending or changing state.
    ``input_bound`` is the next request's reserved input (the margin alone for a
    pre-check without a request)."""
    c = _cfg(cfg)
    try:
        selected_model = _selection_model(c, selection, max_tokens)
    except (AnthropicError, TypeError, ValueError):
        return "profile_invalid"
    reason = setting_block_reason(c)
    if reason:
        return reason
    if not pricing_valid(model=selected_model):
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
    cost = max_cost(max_tokens, input_bound=input_bound, model=selected_model)
    if c["daily_cost_limit_usd"] is not None and today[1] + cost > _decimal(c["daily_cost_limit_usd"]):
        return "daily_cost_cap"
    limit = c["monthly_cost_limit_usd"]
    if limit is not None and tally.total + cost > _decimal(limit):
        return "monthly_cap"
    caps = c["provider_budgets"].get(c["provider"])
    if c["provider_budgets"] and caps is None:
        return "provider_budget_missing"
    if caps is not None:
        if tally.provider_days.get((c["provider"], _periods(time.time())[0]), Decimal(0)) + cost > _decimal(caps["daily_cost_limit_usd"]):
            return "provider_daily_cap"
        if tally.provider_totals.get(c["provider"], Decimal(0)) + cost > _decimal(caps["monthly_cost_limit_usd"]):
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


def usage_reserver(ledger, cfg, *, route, project_id, unit_key, message_id=None, owner=None, on_reserved_tx=None, selection=None):
    """Bind one unit's accounting; callers supply the same guard before and after this hook."""
    from semantic_runtime import reserve_artifact
    c = _cfg(cfg)
    if selection is not None:
        if not isinstance(selection, Mapping):
            raise AnthropicError("contract_error", "cloud_profile_invalid")
        selection = MappingProxyType(dict(selection))
    if on_reserved_tx is not None and not callable(on_reserved_tx):
        raise ValueError("cloud_reservation_hook_invalid")

    def reserve(body, timeout):
        if not isinstance(body, dict):
            raise AnthropicError("contract_error", "model_not_allowed")
        body_tokens = body.get("max_output_tokens") if c["provider"] == "openai" else body.get("max_tokens")
        model = _selection_model(c, selection, body_tokens)
        if body.get("model") != model:
            raise AnthropicError("contract_error", "model_not_allowed")
        if selection is not None and c["provider"] == "openai" and body.get("reasoning") != {"effort": selection["effort"]}:
            raise AnthropicError("contract_error", "cloud_profile_invalid")
        if c["provider"] == "openai" and (body.get("service_tier") != "default"
                or body.get("store") is not False or body.get("prompt_cache_options") != {"ttl": "30m"}):
            raise AnthropicError("contract_error", "openai_price_contract_invalid")
        if ledger.db.in_transaction:
            raise AnthropicError("budget_exceeded", "owner_transaction_active")
        bound = input_bound(body)
        tokens = body_tokens
        try:
            maximum = max_cost(tokens, input_bound=bound, model=model)
        except AnthropicError as error:
            _circuit_result(ledger, error)
            raise
        meta = {"reservation_id": uuid.uuid4().hex, "route": route, "project_id": project_id,
                "unit_key": str(unit_key), "message_id": message_id, "model": model,
                "price_version": _price_definition(model)["price_version"], "request_fp": payload_hash(body),
                "logical_request_fp": _logical_request_fp(body),
                "logical_target_id": (message_id if route == 'semantic_backlog' and isinstance(owner, dict)
                                      and owner.get('semantic_target_id') == message_id else None),
                "max_cost_usd": str(maximum), "max_tokens": tokens, "input_bound": bound,
                "input_chars": sum(len(text) for text in _texts(body)),
                "timeout_seconds": float(timeout),
                "owner": owner or {}}
        if selection is not None:
            meta.update(effort=selection["effort"], profile=selection["profile"])
        if (route == 'semantic_backlog' and isinstance(owner, dict)
                and owner.get('semantic_target_id') == message_id
                and all(isinstance(owner.get(k), str) and owner[k]
                        for k in ('semantic_source', 'semantic_generation', 'semantic_phase'))):
            meta['logical_unit_fp'] = payload_hash({
                'target': message_id, 'source': owner['semantic_source'],
                'generation': owner['semantic_generation'], 'phase': owner['semantic_phase']})

        def check():
            _guard_repeated_request(ledger, meta, current_body=body)
            reason = block_reason(ledger, c, route, project_id, model=model)
            if reason:
                raise AnthropicError(reason if reason == "pricing_unverified" else "budget_exceeded", reason)
            reason = budget_block_reason(ledger, c, max_tokens=tokens, input_bound=bound, selection=selection)
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
    from semantic_runtime import reserve_artifact, _record_cloud_circuit_result_tx
    error_kind = metadata.get("error_kind")
    http_class = metadata.get("http_class")
    if http_class == "http_200" and metadata.get("usage") is None:
        error_kind = "pricing_unverified"
    error = None
    if error_kind:
        # Rebuild the attempt's failure with its retry class; semantic_runtime
        # applies the cloud circuit's transient, contract and terminal policy.
        status = int(http_class[5:]) if isinstance(http_class, str) and http_class[5:].isdigit() else 0
        retryable = (False if error_kind in ("auth_error", "payment_required", "pricing_unverified")
                     else http_error(status)[1] if status and status != 200
                     else error_kind in ("transport", "timeout"))
        error = AnthropicError(error_kind, retryable=retryable, status=status)
    try:
        # Settle at the price table the reservation was made with: a result that arrives
        # after PRICE_VALID_UNTIL is still saved; only new sends stop (owner decision 2026-10-28).
        model = reservation["model"]
        if not isinstance(reservation.get("price_version"), str):
            raise AnthropicError("pricing_unverified", "result_mismatch")
        definition = _price_definition(model, reservation["price_version"])
        if metadata.get("model") != model or metadata.get("request_fp") != reservation["request_fp"]:
            raise AnthropicError("pricing_unverified", "result_mismatch")
        if "openai_error_code" in metadata and not _confirmed_openai_quota(metadata, model):
            raise AnthropicError("pricing_unverified", "result_mismatch")
        if "openai_429_diagnostic" in metadata and not _confirmed_openai_rate_diagnostic(metadata, model):
            raise AnthropicError("pricing_unverified", "result_mismatch")
        usage = definition["validate_usage"](metadata["usage"]) if metadata.get("usage") is not None else None
        maximum = _decimal(reservation["max_cost_usd"])
        cost = (usage_cost(usage, model=model, price_version=reservation["price_version"]) if usage is not None
                else Decimal(0) if _unbilled(metadata.get("http_class")) else maximum)
        if cost > maximum or (usage is not None and usage["output_tokens"] > reservation["max_tokens"]):
            raise AnthropicError("pricing_unverified", "cost_exceeds_reserve")
        result = _receipt_binding(reservation)
        request_id = metadata.get("request_id")
        if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", request_id):
            request_id = None
        result.update(usage=usage, cost_usd=str(cost), http_class=metadata.get("http_class"),
                      request_id=request_id, error_kind=error_kind)
        if "openai_error_code" in metadata:
            result["openai_error_code"] = metadata["openai_error_code"]
        if "openai_429_diagnostic" in metadata:
            result["openai_429_diagnostic"] = metadata["openai_429_diagnostic"]
        if "answer_text_sha256_v1" in metadata:
            answer_hash = metadata["answer_text_sha256_v1"]
            if (not isinstance(answer_hash, str) or re.fullmatch(r"[0-9a-f]{64}", answer_hash) is None
                    or http_class != "http_200" or usage is None or error_kind is not None):
                raise AnthropicError("pricing_unverified", "answer_hash_invalid")
            result["answer_text_sha256_v1"] = answer_hash
        if "answer_finish_reason_v1" in metadata:
            finish_reason = metadata["answer_finish_reason_v1"]
            if (finish_reason not in ("stop", "length") or http_class != "http_200"
                    or usage is None or error_kind is not None):
                raise AnthropicError("pricing_unverified", "finish_reason_invalid")
            result["answer_finish_reason_v1"] = finish_reason
        if "disposition" in metadata:
            if metadata["disposition"] not in ("discarded_scope", "discarded_workday"):
                raise AnthropicError("pricing_unverified", "disposition_invalid")
            result["disposition"] = metadata["disposition"]
    except (AnthropicError, ValueError, KeyError, TypeError, InvalidOperation):
        _circuit_result(ledger, AnthropicError("pricing_unverified"))
        raise AnthropicError("pricing_unverified", "result_invalid") from None

    invalid = []

    def check():
        try:
            _stored_reservation(ledger, reservation, bound_only=True)
        except (AnthropicError, ValueError, KeyError, TypeError, InvalidOperation):
            invalid.append(True)

    def write():
        rows, stored = [], None
        if not invalid:
            try:
                rows = _matching_bound_rows(ledger, KINDS[1], reservation["reservation_id"])
                if len(rows) == 1:
                    stored = _accounting_meta(rows[0]["meta"])
            except (AnthropicError, ValueError, KeyError, TypeError, InvalidOperation):
                invalid.append(True)
        if invalid:
            _record_cloud_circuit_result_tx(ledger, AnthropicError("pricing_unverified"))
            return "invalid"
        if rows:
            if len(rows) != 1:
                _record_cloud_circuit_result_tx(ledger, AnthropicError("pricing_unverified"))
                return "conflict"
            if stored == result:
                return "ok"
            # Preserve the conflict durably; accounting must never pick the cheaper result.
            ledger.artifact_add_tx(KINDS[1], "{}", project_id=reservation["project_id"],
                                   message_id=reservation.get("message_id"), model=model,
                                   meta={**result, "usage": None, "cost_usd": str(maximum),
                                         "error_kind": "pricing_unverified"})
            _record_cloud_circuit_result_tx(ledger, AnthropicError("pricing_unverified"))
            return "conflict"
        ledger.artifact_add_tx(KINDS[1], "{}", project_id=reservation["project_id"],
                               message_id=reservation.get("message_id"), model=model, meta=result)
        _record_cloud_circuit_result_tx(ledger, error)
        return "ok"

    outcome = reserve_artifact(ledger, check, write)
    if outcome != "ok":
        raise AnthropicError("pricing_unverified", "result_" + outcome)
    if error_kind == "pricing_unverified":
        raise AnthropicError("pricing_unverified", "usage_unknown")


def record_disposition_tx(ledger, reservation, disposition):
    """Record a discarded unit in its owner's transaction without changing billing."""
    if not ledger.db.in_transaction:
        raise RuntimeError("cloud_disposition_transaction_required")
    if disposition not in ("discarded_scope", "discarded_workday"):
        raise ValueError("cloud_disposition_invalid")
    _stored_reservation(ledger, reservation)
    meta = _receipt_binding(reservation)
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


def dryrun_tx(ledger, *, route, project_id, unit_key, input_chars=0, max_tokens=8192, requests=1, model=MODEL):
    """Record aggregate estimates in the caller's active success transaction."""
    if not ledger.db.in_transaction:
        raise RuntimeError("cloud_dryrun_transaction_required")
    definition = _price_definition(model)
    if (route not in ROUTES or type(input_chars) is not int or input_chars < 0
            or type(requests) is not int or requests < 0
            or type(max_tokens) is not int or not 1 <= max_tokens <= definition["max_tokens"]):
        raise ValueError("dryrun_invalid")
    return ledger.artifact_add_tx(KINDS[2], "{}", model=model,
                        meta={"route": route, "requests": requests, "input_chars": input_chars,
                              "estimated_input_tokens": (input_chars + 2) // 3,
                              "estimated_output_tokens": max_tokens * requests,
                              "price_version": definition["price_version"]})


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
