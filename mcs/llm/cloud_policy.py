"""Validate cloud scope and reserve bounded provider costs on the ledger owner thread."""
from __future__ import annotations

from contextlib import closing
from decimal import Decimal, InvalidOperation
import hashlib
from itertools import chain
import json
import math

from llm_retry import http_error
import re
import sqlite3
import time
from collections.abc import Mapping
from types import MappingProxyType
import uuid

import anthropic_client
import bounded_http
from anthropic_client import AnthropicError, MODEL
from anthropic_client import MAX_TOKENS as MAX_TOKENS, validate_usage as validate_usage
import openai_client

from cloud_usage_accounting import (
    PRICE_VERSION as PRICE_VERSION,
    PRICE_CHECKED_ON as PRICE_CHECKED_ON,
    PRICE_VALID_UNTIL as PRICE_VALID_UNTIL,
    PRICE_SOURCES as PRICE_SOURCES,
    INPUT_RESERVATION_BOUND as INPUT_RESERVATION_BOUND,
    INPUT_RESERVATION_MARGIN as INPUT_RESERVATION_MARGIN,
    PRICES as PRICES,
    LONG_INPUT_THRESHOLD as LONG_INPUT_THRESHOLD,
    JST as JST,
    KINDS as KINDS,
    ROUTES as ROUTES,
    OPENAI_PRICE_VERSION as OPENAI_PRICE_VERSION,
    OPENAI_PRICE_CHECKED_ON as OPENAI_PRICE_CHECKED_ON,
    OPENAI_PRICE_VALID_UNTIL as OPENAI_PRICE_VALID_UNTIL,
    OPENAI_PRICE_SOURCES as OPENAI_PRICE_SOURCES,
    PROVIDERS as PROVIDERS,
    PRICE_DEFINITIONS as PRICE_DEFINITIONS,
    _CURRENT_PRICE as _CURRENT_PRICE,
    _price_definition as _price_definition,
    _BINDING_KEYS as _BINDING_KEYS,
    _decimal as _decimal,
    pricing_valid as pricing_valid,
    price_info as price_info,
    _texts as _texts,
    input_bound as input_bound,
    _reservation_cost as _reservation_cost,
    max_cost as max_cost,
    usage_cost as usage_cost,
    _unbilled as _unbilled,
    _periods as _periods,
    _loads as _loads,
    _accounting_meta as _accounting_meta,
    _accounting_snapshot as _accounting_snapshot,
    _validate_accounting_timestamps as _validate_accounting_timestamps,
    _binding as _binding,
    _receipt_binding as _receipt_binding,
    _reserve_valid as _reserve_valid,
    _stored_reservation as _stored_reservation,
    _matching_rows_many as _matching_rows_many,
    _matching_rows as _matching_rows,
    _matching_bound_rows as _matching_bound_rows,
    reservation_evidence as reservation_evidence,
    _receipt_meta as _receipt_meta,
    _reservation_evidence as _reservation_evidence,
    reservation_evidence_batch as reservation_evidence_batch,
    _reservation_evidence_batch_snapshot as _reservation_evidence_batch_snapshot,
    _is_openai_model as _is_openai_model,
    _confirmed_openai_quota as _confirmed_openai_quota,
    _confirmed_openai_rate_diagnostic as _confirmed_openai_rate_diagnostic,
    _settled as _settled,
)
from cloud_history_scope import (
    capture_history_tx as capture_history_tx,
    _history_sources as _history_sources,
    _history_document as _history_document,
    _history_scope as _history_scope,
    history_target_ready as history_target_ready,
    history_thread_allowed as history_thread_allowed,
    _history_thread_allowed as _history_thread_allowed,
    history_thread_snapshot as history_thread_snapshot,
    history_scope_target_sql as history_scope_target_sql,
    history_selection_check as history_selection_check,
)
from llm_retry import http_error
from mcs_util import canonical, private_env_value, payload_hash, strict_json, until_epoch, valid_hash

# Compatibility aliases: immutable versioned definitions live with providers.
# Conservative bound covering decimal/binary interpretations of the public 1M label.
# This is a reservation bound, not an advertised accepted input capacity.
# A byte-pair token never covers less than one UTF-8 byte, so a request's text
# bytes plus the framing tokens bound its billable input; a reservation at the
# 1M ceiling would hold ~$0.53 per attempt against a daily/monthly cap.
DEFAULT_DAILY_COST_LIMIT_USD = 25
DEFAULT_MONTHLY_COST_LIMIT_USD = 75


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


def resolve_settings(cfg, root=None, *, available_providers=None):
    """Choose a configured provider by key availability; never retry a paid unit elsewhere."""
    if available_providers is not None and (
            type(available_providers) is not tuple
            or any(type(name) is not str or name not in PROVIDERS for name in available_providers)
            or len(set(available_providers)) != len(available_providers)):
        raise ValueError("cloud_provider_observation_invalid")
    def key_present(name):
        return (bool(provider_key(name, root)) if available_providers is None
                else name in available_providers)
    c, errors = cloud_config(cfg)
    provider = c["provider"]
    if provider == "auto":
        provider = next((name for name in c["provider_order"] if key_present(name)), c["provider_order"][0])
    block = cfg.get("cloud_llm") if isinstance(cfg, dict) else None
    if (block is None or isinstance(block, dict) and "mode" not in block) and not errors:
        c, errors = cloud_config(cfg, key_present=key_present(provider))
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


def send_route(route):
    """Routes still permitted to create new cloud requests."""
    return route == "patient_context"


def eligible(cfg, route, project_id):
    """Allow new cloud inference only for patient context within its scope."""
    return send_route(route) and in_scope(cfg, route, project_id)


def in_scope(cfg, route, project_id):
    """Keep historical response scope independent of permission for a new send."""
    c = _cfg(cfg)
    return (c["mode"] != "off" and route in c["routes"]
            and type(project_id) is int and project_id > 0
            and (c["project_ids"] is None or project_id in c["project_ids"])
            and project_id not in c["exclude_project_ids"])


def scope_revoked(cfg, route, project_id):
    """An existing unit's scope retires only on what the config declares:
    explicit off/dry_run, or a verifiable route/project exclusion. A
    computed 'off' — mode omitted, key unreadable — proves nothing about
    an existing reservation, so unverifiable config never revokes."""
    block = cfg.get("cloud_llm") if isinstance(cfg, dict) else None
    if not isinstance(block, dict):
        return False
    c, errors = cloud_config(cfg)
    if errors:
        return False
    if block.get("mode") in ("off", "dry_run"):
        return True
    if route not in c["routes"]:
        return True
    if type(project_id) is not int or project_id <= 0:
        # A malformed project can prove no exclusion — stay unverifiable.
        return False
    return ("project_ids" in block and c["project_ids"] is not None
            and project_id not in c["project_ids"]) \
        or ("exclude_project_ids" in block and project_id in c["exclude_project_ids"])


def owns_request(db, *, route, project_id, message_id):
    """A durable reservation already owns this message's cloud unit on `route`.

    Send closure retires new sends, never the ownership a prior reservation
    created: the unit must still reach recovery or stay unresolved."""
    return owns_any_request(db, route=route, project_id=project_id, message_ids=[message_id])


def owns_unit_request(db, *, route, project_id, unit_key):
    """owns_request keyed on a durable unit_key for targets without a message id —
    attachment pages reserve `attachment_ocr:<id>:<page>:cloud`. A reservation
    whose meta binds the key but cannot prove a different route stays a
    conservative hold."""
    return db.execute("SELECT 1 FROM artifacts WHERE kind=? AND project_id=? "
        "AND json_valid(meta) AND json_extract(meta,'$.unit_key')=? "
        "AND (json_extract(meta,'$.route')=? OR json_extract(meta,'$.route') IS NULL) "
        "LIMIT 1", (KINDS[0], project_id, unit_key, route)).fetchone() is not None


def owns_member_request(db, *, route, project_id, target_id, identity=None):
    """Whether an unresolved reservation forbids member `target_id`'s local
    path — three verdicts, not strict equality. A candidate row already binds
    the target somewhere (row message_id or any member field); inside it:

    - Member fields (logical_target_id / owner.semantic_target_id) present:
      the reservation is a semantic member unit and it reached the target
      filter — hold, even when the fields contradict each other.
    - Member fields absent and the reservation is intact (valid meta with a
      unit_key and a message_id matching its row): provably a complete
      non-member unit — the member binding a member unit requires is missing
      — so it stays a send guard only and never forbids local.
    - Anything else (unparsable meta, missing unit/message identity):
      unproven — unknown paid state must never release the member to local.

    Only reservations on `route`, or unable to prove a different one, count.

    `identity` is the current request's verified unit tuple
    (target, semantic_source, phase) shared with the send guard. With it, a
    member-bound row still counts only after its own strict receipt and
    _reserve_valid checks pass and its _semantic_financial_identity proves
    the same input — a verified different unit stays a send guard, while an
    unverifiable or malformed member row still holds. A non-None identity
    that is malformed itself owns the member outright: an unproven current
    request never releases local. Generation and model differences inside a
    valid identity do not make a different input — same (target, source,
    phase) still owns."""
    if identity is not None and not (
            type(identity) is tuple and len(identity) == 3
            and type(identity[0]) is int and identity[0] > 0 and identity[0] == target_id
            and isinstance(identity[1], str)
            and re.fullmatch(r'[0-9a-f]{64}', identity[1]) is not None
            and identity[2] in ('facts', 'summary')):
        return True
    # model and project_id ride along so a member-bound row can run the full
    # _reserve_valid row checks before a verified different unit is skipped.
    rows = db.execute("SELECT message_id, project_id, model, meta FROM artifacts WHERE kind=? AND project_id=? AND ("
        "(message_id=? AND NOT json_valid(meta)) OR "
        "(json_valid(meta) AND (json_extract(meta,'$.route')=? OR json_extract(meta,'$.route') IS NULL) "
        "AND (message_id=? OR json_extract(meta,'$.message_id')=? "
        "OR json_extract(meta,'$.logical_target_id')=? "
        "OR json_extract(meta,'$.owner.semantic_target_id')=?)))",
        (KINDS[0], project_id, target_id, route,
         target_id, target_id, target_id, target_id)).fetchall()
    for row in rows:
        # With an identity every candidate is receipt-parsed before any
        # member/nonmember classification: json_extract reads the first of
        # duplicate keys while json.loads keeps the last, so a row that hides
        # member fields behind "owner":{...},"owner":null would otherwise skip
        # strict validation entirely via the non-member branch.
        try:
            meta = _receipt_meta(row["meta"]) if identity is not None else json.loads(row["meta"])
        except (TypeError, ValueError):
            return True
        if not isinstance(meta, dict):
            return True
        owner = meta.get("owner")
        if (meta.get("logical_target_id") is not None
                or isinstance(owner, dict) and owner.get("semantic_target_id") is not None):
            if identity is None:
                return True
            try:
                _reserve_valid(meta, row)
                old_identity = _semantic_financial_identity(meta)
            except (AnthropicError, ValueError, TypeError, KeyError):
                return True
            if old_identity is None or old_identity == identity:
                return True
            continue
        if (isinstance(meta.get("unit_key"), str) and meta["unit_key"]
                and meta.get("message_id") == row["message_id"]):
            continue
        return True
    return False


def owns_any_request(db, *, route, project_id, message_ids):
    """owns_request over a set of member targets: thread jobs reserve per
    member, so ownership is membership in the job's payload targets. A
    reservation whose meta cannot prove a different route stays a
    conservative hold — never an authorization to restart a unit locally."""
    ids = sorted({m for m in message_ids if type(m) is int and m > 0})
    if not ids:
        return False
    q = ",".join("?" * len(ids))
    return db.execute("SELECT 1 FROM artifacts WHERE kind=? AND project_id=? AND ("
        "(json_valid(meta) AND json_extract(meta,'$.route')=? AND "
        f"(message_id IN ({q}) OR json_extract(meta,'$.message_id') IN ({q}) "
        f"OR json_extract(meta,'$.unit_key') IN ({q}) "
        f"OR json_extract(meta,'$.logical_target_id') IN ({q}) "
        f"OR json_extract(meta,'$.owner.semantic_target_id') IN ({q}))) OR "
        f"(message_id IN ({q}) AND (NOT json_valid(meta) OR json_extract(meta,'$.route') IS NULL)) "
        ") LIMIT 1",
        (KINDS[0], project_id, route, *ids, *ids,
         *(str(m) for m in ids), *ids, *ids, *ids)).fetchone() is not None


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


def _verified_request_history(ledger, *, route, project_id, message_id, unit_key):
    """Enumerate the unit's reservation/result history and prove it well-formed:
    every receipt parses, every reservation validates, and each result settles
    against exactly one reservation. Unverifiable rows raise — they never
    pass silently. Returns the reservation rows for the caller's own scan."""
    records = ledger.db.execute("SELECT artifact_id,kind,project_id,message_id,model,meta,created_at "
        "FROM artifacts WHERE kind IN (?,?) AND project_id=? AND (message_id IS ? OR ?='semantic_backlog') AND "
        "CASE WHEN typeof(meta)!='text' OR length(meta)>32768 OR NOT json_valid(meta) THEN 1 "
        "WHEN json_type(meta)!='object' THEN 1 "
        "WHEN (json_extract(meta,'$.route')=? OR json_type(meta,'$.route') IS NULL) AND "
        "(json_extract(meta,'$.unit_key')=? OR ? IN ('semantic_backlog','patient_context','reextract')) THEN 1 "
        "ELSE EXISTS (SELECT 1 FROM json_tree(meta) "
        "WHERE key IS NOT NULL GROUP BY parent,key HAVING COUNT(*)>1) END ORDER BY artifact_id",
        (KINDS[0], KINDS[1], project_id, message_id, route,
         route, unit_key, route)).fetchall()
    rows = [row for row in records if row['kind'] == KINDS[0]]
    # Fiscal totals have a month window; same-input evidence does not. A
    # retained result without its exact reservation cannot prove a fresh input.
    reservations = {}
    try:
        for row in rows:
            previous = _receipt_meta(row['meta'])
            maximum = _reserve_valid(previous, row)
            reservations.setdefault(previous['reservation_id'], []).append((previous, maximum))
    except (AnthropicError, InvalidOperation, ValueError, TypeError, KeyError, OverflowError):
        raise AnthropicError('budget_exceeded', 'cloud_unit_unresolved') from None
    checked_results = {}
    for row in records:
        if row['kind'] != KINDS[1]:
            continue
        try:
            result = _receipt_meta(row['meta'])
            rid = result.get('reservation_id')
            if not isinstance(rid, str) or re.fullmatch(r'[0-9a-f]{32}', rid) is None:
                raise ValueError('result_invalid')
            checked_results.setdefault(rid, []).append((result, row))
        except (InvalidOperation, ValueError, TypeError, KeyError, OverflowError):
            raise AnthropicError('pricing_unverified', 'cloud_unit_unresolved') from None
    for rid, results in checked_results.items():
        matches = reservations.get(rid, [])
        if len(matches) != 1:
            raise AnthropicError('budget_exceeded', 'cloud_unit_unresolved')
        previous, maximum = matches[0]
        try:
            # Validate the complete result set: two individually valid rows
            # are still ambiguous accounting, including on a same-prompt retry.
            _settled(previous, maximum, results)
        except (AnthropicError, InvalidOperation, ValueError, TypeError, KeyError, OverflowError):
            raise AnthropicError('pricing_unverified', 'cloud_unit_unresolved') from None
    return rows


def unbound_member_history(ledger, *, route, project_id, message_id, unit_key, target_id,
                           identity=None):
    """Prove member `target_id` holds no paid unit on `route`: inside one
    accounting snapshot the fiscal tally is valid, the unit's whole
    reservation/result history verifies, and no reservation provably binds
    the member. A tainted or ambiguous history proves nothing — it never
    releases the member to a local path. `identity` scopes the ownership
    verdict to the current request's verified unit tuple; the history and
    tally checks are unchanged and run regardless."""
    try:
        with _accounting_snapshot(ledger.db):
            if _tally(ledger).invalid:
                return False
            _verified_request_history(ledger, route=route, project_id=project_id,
                                      message_id=message_id, unit_key=unit_key)
            return not owns_member_request(ledger.db, route=route,
                                           project_id=project_id, target_id=target_id,
                                           identity=identity)
    except (AnthropicError, sqlite3.Error):
        return False


def _guard_repeated_request(ledger, candidate, *, current_body=None, local_fallback=False):
    """Keep an unresolved or already answered logical request from being sent again."""
    rows = _verified_request_history(ledger, route=candidate['route'],
        project_id=candidate['project_id'], message_id=candidate['message_id'],
        unit_key=candidate['unit_key'])
    result_map = None
    disposition_ids = None
    candidate_occurrence, occurrence_checked = None, False
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
            if (local_fallback and candidate['route'] == 'patient_context'
                    and candidate['unit_key'] == 'summary' and old_fp != new_fp):
                new_fp = _context_summary_fallback_fp(current_body, previous)
            prompt_verified = False
            if old_fp is not None and new_fp is not None:
                if old_fp != new_fp:
                    continue
                prompt_verified = True
            elif _legacy_luna_low_proven(previous, current_body):
                prompt_verified = True
            if (candidate['route'] == 'reextract' and candidate.get('message_id') is None
                    and isinstance(candidate.get('owner'), dict)
                    and candidate['owner'].get('source_type') == 'attachment_ocr'):
                from attachment_cloud_identity import occurrence
                if not occurrence_checked:
                    candidate_occurrence = occurrence(ledger.db, candidate)
                    occurrence_checked = True
                old_source = occurrence(ledger.db, previous) if candidate_occurrence is not None else None
                if (old_source is not None
                        and old_source[0]['source_key'] != candidate_occurrence[0]['source_key']):
                    continue
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
            http_class = result.get('http_class')
            if (local_fallback and isinstance(http_class, str)
                    and re.fullmatch(r'http_[45][0-9]{2}', http_class)
                    and result.get('error_kind') == http_error(int(http_class[5:]))[0]
                    and result.get('usage') is None):
                continue  # The failed HTTP result stays charged; this grants no new cloud send.
            if _confirmed_rejection(result, settled, previous['model']):
                continue
            # A confirmed successful result may be deliberately reprocessed
            # with another model. An ambiguous send never grants that retry.
            if (not local_fallback and prompt_verified and settled and result.get('usage') is not None and result.get('http_class') == 'http_200'
                    and result.get('error_kind') is None and _selection_differs(previous, candidate, current_body)):
                continue
            raise ValueError('already_sent_or_unknown')
        except (AnthropicError, InvalidOperation, ValueError, TypeError, KeyError, OverflowError):
            raise AnthropicError('budget_exceeded', 'cloud_unit_unresolved') from None



def _context_summary_fallback_fp(body, previous):
    # A backend/contract signature alone never makes the same reports a new input.
    owner = previous.get('owner')
    signature = owner.get('source_sig') if isinstance(owner, dict) else None
    if not isinstance(signature, str) or re.fullmatch(r'[0-9a-f]{64}', signature) is None:
        return None
    prompt = body.get('input') if isinstance(body, dict) else None
    if prompt is None and isinstance(body, dict):
        messages = body.get('messages')
        if isinstance(messages, list) and len(messages) == 1:
            prompt = messages[0].get('content')
    if not isinstance(prompt, str):
        return None
    try:
        payload = strict_json(prompt)
        if (not isinstance(payload, dict) or set(payload) != {'instruction', 'reports', 'source_identity'}
                or not isinstance(payload['source_identity'], dict)
                or set(payload['source_identity']) != {'signature'}):
            return None
        payload['source_identity']['signature'] = signature
        prompt = json.dumps(payload, ensure_ascii=False)
        changed = ({**body, 'input': prompt} if 'input' in body else
                   {**body, 'messages': [{**body['messages'][0], 'content': prompt}]})
        return _logical_request_fp(changed)
    except (ValueError, TypeError, KeyError, RecursionError):
        return None


def guard_local_fallback(ledger, *, route, project_id, message_id, unit_key, request_factory):
    """Prove local substitution safe without sending, reserving, or changing accounting."""
    with _accounting_snapshot(ledger.db):
        rows = _verified_request_history(ledger, route=route, project_id=project_id,
                                        message_id=message_id, unit_key=unit_key)
        if _tally(ledger).invalid:
            raise AnthropicError('budget_exceeded', 'cloud_unit_unresolved')
        if rows:
            candidate, body = request_factory()
            _guard_repeated_request(ledger, candidate, current_body=body, local_fallback=True)

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


def _request_identity(c, body, *, route, project_id, unit_key, message_id, owner,
                      selection, input_binding):
    # A reservation and a pre-send history check must identify the same input.
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
    meta = {"route": route, "project_id": project_id, "message_id": message_id,
            "unit_key": str(unit_key), "model": model, "max_tokens": body_tokens,
            "request_fp": payload_hash(body), "logical_request_fp": _logical_request_fp(body),
            "logical_target_id": (message_id if route == 'semantic_backlog' and isinstance(owner, dict)
                                  and owner.get('semantic_target_id') == message_id else None),
            "owner": owner or {}}
    if selection is not None:
        meta.update(effort=selection["effort"], profile=selection["profile"])
    if input_binding is not None:
        if not isinstance(input_binding, str) or re.fullmatch(r'[0-9a-f]{64}', input_binding) is None:
            raise AnthropicError('contract_error', 'input_binding_invalid')
        meta['input_binding'] = input_binding
    if (route == 'semantic_backlog' and isinstance(owner, dict)
            and owner.get('semantic_target_id') == message_id
            and all(isinstance(owner.get(k), str) and owner[k]
                    for k in ('semantic_source', 'semantic_generation', 'semantic_phase'))):
        meta['logical_unit_fp'] = payload_hash({
            'target': message_id, 'source': owner['semantic_source'],
            'generation': owner['semantic_generation'], 'phase': owner['semantic_phase']})

    return meta


def request_send_block_reason(ledger, cfg, body, *, route, project_id, unit_key,
                              message_id=None, owner=None, selection=None, input_binding=None):
    """Check the same input's prior sends before deciding a fresh send is blocked."""
    c = _cfg(cfg)
    candidate = _request_identity(c, body, route=route, project_id=project_id, unit_key=unit_key,
        message_id=message_id, owner=owner, selection=selection, input_binding=input_binding)
    try:
        with _accounting_snapshot(ledger.db):
            _guard_repeated_request(ledger, candidate, current_body=body)
            # An orphan result cannot prove this is a fresh request. Validate the
            # existing accounting snapshot even when a circuit would short-circuit caps.
            if _tally(ledger).invalid:
                raise AnthropicError("pricing_unverified", "cloud_unit_unresolved")
            return (block_reason(ledger, c, route, project_id, model=candidate['model'])
                    or setting_block_reason(c)
                    or budget_block_reason(ledger, c, max_tokens=candidate['max_tokens'],
                                           input_bound=input_bound(body), selection=selection))
    except AnthropicError as error:
        if error.kind == 'pricing_unverified' and not ledger.db.in_transaction:
            try:
                _circuit_result(ledger, error)
            except Exception:
                # Failure to record the circuit must never turn an accounting
                # refusal into a fresh/local input or replace its fixed category.
                pass
        raise


def usage_reserver(ledger, cfg, *, route, project_id, unit_key, message_id=None, owner=None,
                   on_reserved_tx=None, selection=None, input_binding=None):
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
        meta = _request_identity(c, body, route=route, project_id=project_id, unit_key=unit_key,
            message_id=message_id, owner=owner, selection=selection, input_binding=input_binding)
        if ledger.db.in_transaction:
            raise AnthropicError("budget_exceeded", "owner_transaction_active")
        bound, tokens, model = input_bound(body), meta['max_tokens'], meta['model']
        try:
            maximum = max_cost(tokens, input_bound=bound, model=model)
        except AnthropicError as error:
            _circuit_result(ledger, error)
            raise
        meta.update(reservation_id=uuid.uuid4().hex,
                    price_version=_price_definition(model)["price_version"],
                    max_cost_usd=str(maximum), input_bound=bound,
                    input_chars=sum(len(text) for text in _texts(body)), timeout_seconds=float(timeout))

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


def _confirmed_rejection(result, settled, model):
    # A known rejection permits another attempt while retaining its reserved cost.
    return (settled and result.get('http_class') == 'not_sent'
            or result.get('http_class') == 'http_429' and result.get('error_kind') == 'rate_limited'
            and result.get('usage') is None or _confirmed_openai_quota(result, model))


def _saved_answer(answer, metadata):
    if (not isinstance(answer, dict) or not isinstance(answer.get('text'), str)
            or answer.get('finish_reason') not in ('stop', 'length')):
        raise ValueError('cloud_answer_invalid')
    encoded = answer['text'].encode('utf-8')
    if (len(encoded) > bounded_http.MAX_RESPONSE_BYTES
            or metadata.get('answer_text_sha256_v1') != hashlib.sha256(encoded).hexdigest()
            or metadata.get('answer_finish_reason_v1') != answer['finish_reason']):
        raise ValueError('cloud_answer_invalid')
    return {key: answer[key] for key in ('text', 'finish_reason')}


def recover_response(ledger, *, route, project_id, message_id, unit_key, input_binding, request_fp,
                     owner=None, scope=None):
    """Read one exact paid answer; ambiguous, discarded or answerless attempts stay closed."""
    with _accounting_snapshot(ledger.db):
        rows = ledger.db.execute("SELECT artifact_id,kind,project_id,message_id,model,meta,created_at "
            "FROM artifacts WHERE kind=? AND project_id=? AND message_id IS ? AND "
            "CASE WHEN json_valid(meta) THEN json_extract(meta,'$.route')=? AND "
            "json_extract(meta,'$.unit_key')=? AND json_extract(meta,'$.input_binding')=? AND "
            "json_extract(meta,'$.request_fp')=? END ORDER BY artifact_id",
            (KINDS[0], project_id, message_id, route, unit_key, input_binding, request_fp)).fetchall()
        if route == 'reextract' and message_id is None and owner is not None and valid_hash(scope):
            from attachment_cloud_identity import recovery_rows
            rows.extend(recovery_rows(ledger.db, project_id=project_id, unit_key=unit_key,
                binding=input_binding, request_fp=request_fp, owner=owner, scope=scope))
        saved = None
        for row in rows:
            reservation = _receipt_meta(row['meta'])
            maximum = _reserve_valid(reservation, row)
            results = _matching_bound_rows(ledger, KINDS[1], reservation['reservation_id'])
            dispositions = _matching_bound_rows(ledger, 'cloud_usage_disposition',
                                                reservation['reservation_id'])
            # Reasons surfaced below are proven by checks recover_response
            # already performs; a bare ValueError from the shared settlement
            # proof stays an ambiguous receipt, never permission to resend.
            if not results:
                raise ValueError('cloud_result_discarded' if dispositions
                                 else 'cloud_result_missing')
            matches = [(_receipt_meta(r['meta']), r) for r in results]
            try:
                _, settled = _settled(reservation, maximum, matches)
            except ValueError:
                raise ValueError('cloud_receipt_invalid') from None
            if (len(matches) == 1 and not matches[0][0].get('disposition')
                    and _confirmed_rejection(matches[0][0], settled, reservation['model'])
                    and not dispositions):
                continue
            if dispositions:
                raise ValueError('cloud_result_discarded')
            result_meta = matches[0][0]
            if (not settled or result_meta.get('http_class') != 'http_200'
                    or result_meta.get('error_kind') or result_meta.get('disposition')):
                raise ValueError('cloud_result_unsettled')
            evidence = reservation_evidence(ledger.db, row['artifact_id'])
            raw = ledger.db.execute("SELECT CASE WHEN typeof(content)='text' AND length(content)<=? "
                                   "THEN content END FROM artifacts WHERE artifact_id=?",
                                   (6 * bounded_http.MAX_RESPONSE_BYTES + 128, results[0]['artifact_id'])).fetchone()[0]
            answer = strict_json(raw)
            if not isinstance(answer, dict) or set(answer) != {'text', 'finish_reason'} or saved is not None:
                raise ValueError('cloud_answer_unresolved')
            answer = _saved_answer(answer, evidence['result'])
            saved = (evidence, {'status': 200, **answer, 'model': reservation['model'],
                                'usage': evidence['result']['usage']})
        return saved


def record_result(ledger, reservation, metadata, *, answer=None):
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
        content = (json.dumps(_saved_answer(answer, result), ensure_ascii=False)
                   if answer is not None and reservation.get('input_binding') is not None else '{}')
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
            saved_content = ledger.db.execute('SELECT content FROM artifacts WHERE artifact_id=?',
                                               (rows[0]['artifact_id'],)).fetchone()[0]
            if stored == result and (answer is None or saved_content == content):
                return "ok"
            # Preserve the conflict durably; accounting must never pick the cheaper result.
            ledger.artifact_add_tx(KINDS[1], "{}", project_id=reservation["project_id"],
                                   message_id=reservation.get("message_id"), model=model,
                                   meta={**result, "usage": None, "cost_usd": str(maximum),
                                         "error_kind": "pricing_unverified"})
            _record_cloud_circuit_result_tx(ledger, AnthropicError("pricing_unverified"))
            return "conflict"
        ledger.artifact_add_tx(KINDS[1], content, project_id=reservation["project_id"],
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
