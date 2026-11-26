"""Validate cloud scope and reserve bounded provider costs on the ledger owner thread."""
from __future__ import annotations

from contextlib import closing
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import os
import re
import sqlite3
import time
from collections.abc import Mapping
from types import MappingProxyType
from typing import NamedTuple
import uuid

import anthropic_client
import cloud_client
from anthropic_client import AnthropicError, MODEL
from anthropic_client import MAX_TOKENS as MAX_TOKENS, validate_usage as validate_usage
import openai_client  # noqa: F401  existing callers read cloud_policy.openai_client
import provider_registry
from llm_retry import CLOUD_MAX_ATTEMPTS

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
    current_price_version as current_price_version,
    price_info as price_info,
    _texts as _texts,
    input_bound as input_bound,
    _reservation_cost as _reservation_cost,
    max_cost as max_cost,
    usage_cost as usage_cost,
    _unbilled as _unbilled,
    _periods as _periods,
    CARRYOVER_KIND as CARRYOVER_KIND,
    carryover_entries as carryover_entries,
    carryover_marker as carryover_marker,
    _loads as _loads,
    _accounting_meta as _accounting_meta,
    _accounting_snapshot as _accounting_snapshot,
    _validate_accounting_timestamps as _validate_accounting_timestamps,
    _binding as _binding,
    _receipt_binding as _receipt_binding,
    _reserve_valid as _reserve_valid,
    PROFILE_MAX_TOKENS as PROFILE_MAX_TOKENS,
    PURPOSE_MAX_TOKENS as PURPOSE_MAX_TOKENS,
    profile_contract_valid as profile_contract_valid,
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
    _is_anthropic_model as _is_anthropic_model,
    _confirmed_anthropic_spend_limit as _confirmed_anthropic_spend_limit,
    _confirmed_anthropic_usage_limit as _confirmed_anthropic_usage_limit,
    _confirmed_anthropic_payment as _confirmed_anthropic_payment,
    result_evidence_error as result_evidence_error,
    _settled as _settled,
)
from cloud_history_scope import (
    capture_history_tx as capture_history_tx,
    _history_sources as _history_sources,
    _history_document as _history_document,
    _history_scope as _history_scope,
    history_target_ready as history_target_ready,
    history_thread_ready as history_thread_ready,
    history_thread_allowed as history_thread_allowed,
    _history_thread_allowed as _history_thread_allowed,
    history_thread_snapshot as history_thread_snapshot,
    history_scope_target_sql as history_scope_target_sql,
    history_selection_check as history_selection_check,
)
from llm_retry import http_error
from mcs_util import (canonical, private_env_problem, private_env_value, payload_hash, strict_json,
                      until_epoch, valid_hash)

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
           "provider": "auto", "provider_order": list(provider_registry.CLOUD_PROVIDERS), "provider_budgets": {},
           "max_concurrency": 4, "daily_cost_limit_usd": DEFAULT_DAILY_COST_LIMIT_USD,
           "monthly_cost_limit_usd": DEFAULT_MONTHLY_COST_LIMIT_USD,
           "usd_jpy": None, "until": None,
           "window_start": None, "window_budget_usd": None, "reextract_backlog_until": None,
           "semantic_backlog_until": None}
    errors = []
    # an absent block means "unset"; an explicit null is a config error, not an omission
    block = cfg.get("cloud_llm", {}) if isinstance(cfg, dict) else {}
    if not isinstance(block, dict):
        return out, ["config: cloud_llm_not_object"]
    if block.keys() - out.keys() - {"profiles", "provider_profiles"}:
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
        provider = provider_registry.DEFAULT_PROVIDER
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
        allowed = {model for module in provider_registry.PROVIDER_MODULES.values() for model in module.CONFIG_MODELS}
        try:
            owner = _price_definition(candidate)["provider"] if candidate in allowed else None
        except (AnthropicError, TypeError):
            owner = None
        if owner is None or provider != "auto" and owner != provider or provider == "auto" and owner not in out["provider_order"]:
            errors.append("config: cloud_llm_model_invalid")
        else:
            out["model"] = candidate
    legacy = provider_registry.LEGACY_PROFILES_PROVIDER
    if "profiles" in block:
        profiles = block["profiles"]
        if (not _profile_map_valid(provider_registry.PROVIDER_MODULES[legacy], profiles)
                or provider not in (legacy, "auto")):
            errors.append("config: cloud_llm_profiles_invalid")
        else:
            out["profiles"] = dict(profiles)
    if "provider_profiles" in block:
        value = block["provider_profiles"]
        if (not isinstance(value, dict) or value.keys() - provider_registry.PROVIDER_MODULES.keys()
                or any(not _profile_map_valid(provider_registry.PROVIDER_MODULES[name], mapping)
                       for name, mapping in value.items())
                or legacy in value and "profiles" in block and value[legacy] != block["profiles"]):
            # The older "profiles" may repeat its provider's provider_profiles entry, never contradict it.
            errors.append("config: cloud_llm_profiles_invalid")
        else:
            out["provider_profiles"] = {name: dict(mapping) for name, mapping in value.items()}
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
    # A retained period remains binding; removing it requires daily/monthly caps.
    for route in ("reextract", "semantic"):
        key = route + "_backlog_until"
        if block.get(key) is None:
            continue
        try:
            until_epoch(block[key])
            out[key] = block[key]
        except (ValueError, OverflowError):
            errors.append(f"config: cloud_llm_{route}_backlog_invalid")
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
    for route in ("reextract", "semantic"):
        key = route + "_backlog_until"
        if out[key] is not None and out["window_budget_usd"] is None and not _backlog_caps_configured(out):
            errors.append(f"config: cloud_llm_{route}_backlog_invalid")
            out[key] = None
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


def scope_generation(cfg, pin=None):
    """The settings that decide whether a unit may use the cloud at all; caps,
    rate and currency changes must not discard an in-flight paid result.

    A unit pinned to a contract (:func:`route_contract`) carries that contract
    in place of the provider choice (provider, provider_order, default model),
    so reordering providers never changes its scope or its saved answers' binding."""
    c = _cfg(cfg)
    if pin is None:
        return payload_hash({k: c[k] for k in ("mode", "routes", "project_ids", "exclude_project_ids", "model", "provider", "provider_order")})
    return payload_hash({**{k: c[k] for k in ("mode", "routes", "project_ids", "exclude_project_ids")},
                         "contract": {name: dict(contract) for name, contract in pin.items()}})


# The original contract's output limit, used when a selection carries none.
ORIGINAL_MAX_TOKENS = 1600


def selection_max_tokens(selection):
    """Return the selection's output limit, or the original contract's when it has none."""
    return selection["max_tokens"] if selection["max_tokens"] is not None else ORIGINAL_MAX_TOKENS


def api_key(root=None):
    """Read only this MCS installation's dotenv, never process or Hermes keys."""
    return private_env_value(anthropic_client.KEY_ENV, root)


def provider_key(provider, root=None):
    module = provider_registry.provider_module(provider)
    if module is None:
        return None
    # api_key stays the default provider's reader; existing callers and tests replace it.
    return api_key(root) if provider == provider_registry.DEFAULT_PROVIDER else private_env_value(module.KEY_ENV, root)


def resolve_settings(cfg, root=None, *, available_providers=None):
    """Choose a configured provider by key availability and facility approval; never retry a paid unit elsewhere."""
    return resolve_selection(cfg, root, available_providers=available_providers)[:2]


def resolve_selection(cfg, root=None, *, available_providers=None, pinned=None):
    """:func:`select_settings` on this installation's keys and review record."""
    if available_providers is not None and (
            type(available_providers) is not tuple
            or any(type(name) is not str or name not in PROVIDERS for name in available_providers)
            or len(set(available_providers)) != len(available_providers)):
        raise ValueError("cloud_provider_observation_invalid")
    def key_present(name):
        return (bool(provider_key(name, root)) if available_providers is None
                else name in available_providers)
    import external_transfer
    return select_settings(cfg, key_present, lambda: external_transfer.status(root), pinned=pinned)


def select_settings(cfg, key_present, transfer_status, *, pinned=None):
    """Pick the provider and mode from a key-presence check and the facility review.

    Returns ``(settings, errors, held)``. ``key_present(name)`` may read a key
    file; it is asked at most once per provider. ``transfer_status()`` returns
    :func:`external_transfer.status` and is read only when the cloud could be
    on. While a review record exists, a provider the facility has not
    approved never sends, whether the mode is omitted or explicit: its mode
    resolves to ``off`` and ``held`` names it. ``auto`` prefers the first
    provider in ``provider_order`` with both a key and approval and otherwise
    keeps the key-only pick. Without a record (installations from before the
    review) the review allows every provider, so nothing changes there; an
    explicit ``off`` always stays off. A changed review can therefore change
    the auto provider and the scope generation. ``pinned`` judges that
    provider in place of the configured or automatic choice
    (:func:`resolve_contract` decides whether the configuration offers it)."""
    import external_transfer
    c, errors = cloud_config(cfg)
    block = cfg.get("cloud_llm") if isinstance(cfg, dict) else None
    explicit = isinstance(block, dict) and "mode" in block
    seen, review = {}, []

    def keyed(name):
        if name not in seen:
            seen[name] = bool(key_present(name))
        return seen[name]

    def allowed(name):
        if not review:
            review.append(transfer_status())
        return external_transfer.service_allowed(review[0], name)
    automatic = not explicit and not errors
    wanted = automatic or explicit and c["mode"] != "off"
    provider = c["provider"] if pinned is None else pinned
    if provider == "auto":
        order = c["provider_order"]
        provider = None
        if wanted:
            provider = next((name for name in order if keyed(name) and allowed(name)), None)
        if provider is None:
            provider = next((name for name in order if keyed(name)), order[0])
    held = None
    if automatic:
        on = keyed(provider) and allowed(provider)
        c, errors = cloud_config(cfg, key_present=on)
        held = provider if keyed(provider) and not on else None
    elif wanted and not allowed(provider):
        c["mode"], held = "off", provider
    c["provider"] = provider
    if isinstance(block, dict) and "model" in block:
        try:
            if _price_definition(c["model"])["provider"] != provider:
                errors.append("config: cloud_llm_model_invalid")
        except AnthropicError:
            errors.append("config: cloud_llm_model_invalid")
    else:
        c["model"] = PROVIDERS[provider]
    return c, errors, held


def resolve_profile(cfg, *, route, purpose, root=None, ledger=None, ordered=False):
    """Resolve one explicit use to a bounded, immutable provider selection.

    With ``ordered`` the first provider in ``provider_order`` that may actually
    send wins: provider identity is not the inference generation, so the same
    unit answers to whichever configured provider can pay for it. A provider
    whose model is globally stopped or over budget falls through to the next
    entry. When every provider is blocked the first resolvable selection is
    still returned beside the block reason: a unit keeps the ordered contract
    it was reserved under, and send-time admission reports the reason."""
    settings, errors = resolve_settings(cfg, root)
    if errors:
        return None, errors
    if not ordered:
        return profile_for_settings(cfg, settings, route=route, purpose=purpose)
    reason = None
    first = None
    for provider in settings["provider_order"]:
        selection, provider_errors = profile_for_settings(
            cfg, settings, route=route, purpose=purpose, provider=provider)
        if provider_errors or selection is None:
            errors = provider_errors or errors
            continue
        if first is None:
            first = selection
        limit = selection_max_tokens(selection)
        reason = ledger is not None and (
            global_block_reason(ledger, model=selection["model"])
            or budget_block_reason(ledger, settings, max_tokens=limit, selection=selection))
        if reason:
            continue
        return selection, []
    return first, ([reason] if reason else []) + errors


def _profile_map_valid(module, mapping):
    return (isinstance(mapping, dict) and not mapping.keys() - module.PROFILE_PURPOSES
            and all(isinstance(role, str) and isinstance(value, str) and value in module.PROFILE_DEFINITIONS
                    for role, value in mapping.items()))


def profile_for_settings(cfg, settings, *, route, purpose, provider=None):
    """Resolve a profile from a provider already selected by the caller."""
    valid_purposes = {"reextract": {"backlog", "quality_repair"},
                      "semantic_backlog": {"backlog", "quality_repair"},
                      "patient_context": {"patient_context", "quality_repair"}}
    if (not isinstance(route, str) or not isinstance(purpose, str)
            or route not in valid_purposes or purpose not in valid_purposes[route]):
        return None, ["config: cloud_profile_purpose_invalid"]
    provider = provider or settings["provider"]
    block = cfg.get("cloud_llm") if isinstance(cfg, dict) else None
    block = block if isinstance(block, dict) else {}
    by_provider = block.get("provider_profiles", {})
    if not isinstance(by_provider, dict):
        return None, ["config: cloud_llm_profiles_invalid"]
    module = provider_registry.provider_module(provider)
    if module is None:
        return None, ["config: cloud_llm_provider_invalid"]
    # An unset provider or purpose takes the provider's own default.
    chosen = by_provider.get(provider, {})
    if provider == provider_registry.LEGACY_PROFILES_PROVIDER and "profiles" in block:
        if provider in by_provider and by_provider[provider] != block["profiles"]:
            return None, ["config: cloud_llm_profiles_invalid"]
        chosen = block["profiles"]
    selected = module.select_profile(purpose, chosen, block.get("model")) if _profile_map_valid(module, chosen) else None
    if selected is None:
        return None, ["config: cloud_llm_profiles_invalid"]
    profile, model, effort = selected
    limit = PURPOSE_MAX_TOKENS[purpose]
    return MappingProxyType({"provider": provider, "model": model, "effort": effort,
        "max_tokens": limit, "price_version": _price_definition(model)["price_version"],
        "profile": profile}), []


_ROUTE_PURPOSES = (("reextract", ("backlog", "quality_repair")),
                   ("semantic_backlog", ("backlog", "quality_repair")),
                   ("patient_context", ("patient_context", "quality_repair")))


# A selection's fields that decide what its requests send and to whom; the price
# table version is not one of them (a new table re-prices the same request).
CONTRACT_FIELDS = ("provider", "model", "effort", "profile", "max_tokens")


def selection_contract(selection):
    """The contract fields of one selection."""
    return {key: selection[key] for key in CONTRACT_FIELDS}


def _contract_config(cfg, provider, root=None, *, wait=True):
    """``cfg`` as the pinned ``provider`` reads it -> configuration, or None while unverified.

    The runtime view of split provider settings carries only the automatically
    selected provider's model and profiles, so a contract is always judged
    against its own provider's validated settings, read here; sending and
    ending an attempt therefore share one reading. A single config.json
    already holds every provider's settings. The empty fallback of an
    unreadable configuration and a view the loader synthesized after a
    provider read failed (``provider_unavailable``) are never used."""
    if not isinstance(cfg, dict) or not cfg or cfg.get("provider_unavailable"):
        return None   # the unreadable-config fallback ({}) or a synthesized view
    import mcs_util
    path = os.path.join(root if root is not None else mcs_util.HOME, "config.json")
    try:
        if not mcs_util._provider_config_active(path):
            return cfg
    except mcs_util.ProviderSettingsUnreadable:
        return None
    return mcs_util.load_config_checked(path, provider=provider, wait=wait)


def route_contract(cfg, selection, *, route, purpose, root=None):
    """The contract a unit pins when it starts on ``selection``'s provider -> {purpose: contract}, or None.

    Every purpose of the route (the selection's and its quality repair) takes
    that provider's own current profile (:func:`_contract_config`), so the
    unit and its repairs resume sending exactly what their paid answers
    belong to; the caller sends under :func:`resolve_contract`'s selection."""
    purposes = dict(_ROUTE_PURPOSES).get(route, ())
    provider = selection.get("provider") if isinstance(selection, Mapping) else None
    if purpose not in purposes or not isinstance(provider, str) or provider not in PROVIDERS:
        return None
    cfg = _contract_config(cfg, provider, root)
    if cfg is None:
        return None
    settings, errors, _held = resolve_selection(cfg, root, pinned=provider)
    if errors:
        return None
    pin = {}
    for name in purposes:
        chosen = profile_for_settings(cfg, settings, route=route, purpose=name, provider=provider)[0]
        if chosen is None:
            return None
        pin[name] = selection_contract(chosen)
    return pin


class ContractState(NamedTuple):
    """One pinned contract judged against the live configuration (:func:`resolve_contract`)."""
    state: str                  # "usable", "waiting" or "gone"
    reason: str | None
    selection: Mapping | None   # the pinned provider's live profile for the purpose
    settings: dict | None       # the pinned provider's settings
    key: str | None             # the pinned provider's key


def resolve_contract(cfg, pin, *, route, purpose, root=None, ledger=None, wait=True):
    """Whether a pinned contract may send now under ``cfg``, the facility review and the stop state.

    ``pin`` comes from :func:`route_contract`. ``usable``: send under the
    returned selection (the pinned provider's live profile, equal in every
    contract field), that provider's settings and its key. ``waiting``: the
    contract is still offered but nothing may send now -- the configuration,
    the review record or the key file is unverified (unreadable, a synthesized
    fallback view, or a review still pending), the mode is not ``on``, or
    (with ``ledger``) the run window, a stop, an open circuit, an unverified
    price or a spending cap holds it. ``gone``: a readable configuration,
    review or key file no longer offers it (another provider is named, it
    left ``provider_order``, the recorded review leaves it out, its key is
    absent, or one of its profiles changed). Profiles are always the pinned
    provider's own (:func:`_contract_config`). Which provider is preferred
    decides nothing here: a contract never moves to another provider or model.
    Without ``wait`` a provider-settings update in progress reads as
    unverified (``waiting``) at once instead of being waited out."""
    purposes = dict(_ROUTE_PURPOSES).get(route, ())
    provider = None
    if (purpose in purposes and isinstance(pin, Mapping) and set(pin) == set(purposes)
            and all(isinstance(pin[name], Mapping) and set(pin[name]) == set(CONTRACT_FIELDS)
                    for name in purposes)):
        named = [pin[name]["provider"] for name in purposes]
        # A malformed provider value (a list, an object, a number) is an invalid contract, never an error.
        if all(isinstance(name, str) for name in named) and len(set(named)) == 1:
            provider = named[0]
    if provider not in PROVIDERS:
        return ContractState("gone", "contract_invalid", None, None, None)
    view = _contract_config(cfg, provider, root, wait=wait)
    cfg = cfg if view is None else view
    # The configuration's own errors, without choosing an unpinned provider: which
    # provider ``auto`` would prefer now (and whether its model fits) is not this contract's.
    _general, errors = cloud_config(cfg)
    settings, pinned_errors, _held = resolve_selection(cfg, root, pinned=provider)
    key = provider_key(provider, root)

    def judged(state, reason, selection=None):
        return ContractState(state, reason, selection, settings, key)
    if view is None:
        return judged("waiting", "config_unavailable")
    if errors:
        return judged("waiting", "config_unverified")
    configured = cloud_config(cfg)[0]
    if (configured["provider"] not in ("auto", provider)
            or configured["provider"] == "auto" and provider not in configured["provider_order"]):
        return judged("gone", "provider_not_offered")
    # A missing cap only holds sending; every withdrawal below still ends the contract first.
    cap_missing = bool(pinned_errors) and all(error == COST_LIMIT_REQUIRED for error in pinned_errors)
    if pinned_errors and not cap_missing:
        return judged("gone", "contract_invalid")
    if not key:
        absent = private_env_problem(provider_registry.PROVIDER_MODULES[provider].KEY_ENV, root) == "key_missing"
        return judged("gone" if absent else "waiting", "key_missing" if absent else "key_unverified")
    import external_transfer
    review = external_transfer.status(root)
    if not external_transfer.service_allowed(review, provider):
        # Only a recorded review that leaves the provider out withdraws it.
        withdrawn = review.get("state") == "acknowledged"
        return judged("gone" if withdrawn else "waiting",
                      "provider_not_approved" if withdrawn else "provider_review_unverified")
    selection = None
    for name in purposes:
        chosen, problems = profile_for_settings(cfg, settings, route=route, purpose=name, provider=provider)
        if problems or chosen is None or selection_contract(chosen) != dict(pin[name]):
            return judged("gone", "contract_changed")
        if name == purpose:
            selection = chosen
    if cap_missing:
        return judged("waiting", "cost_limit_required", selection)
    if settings["mode"] != "on":
        return judged("waiting", "mode_off", selection)
    if ledger is not None:
        reason = (setting_block_reason(settings) or global_block_reason(ledger, model=selection["model"])
                  or budget_block_reason(ledger, settings, max_tokens=selection_max_tokens(selection),
                                         selection=selection))
        if reason:
            return judged("waiting", reason, selection)
    return judged("usable", None, selection)


def configured_models(cfg, settings):
    """Every model this configuration can send to: the default and each route purpose's profile."""
    models = {settings["model"]}
    for route, purposes in _ROUTE_PURPOSES:
        for purpose in purposes:
            selection, errors = profile_for_settings(cfg, settings, route=route, purpose=purpose)
            if selection is not None and not errors:
                models.add(selection["model"])
    return sorted(models)


def earliest_price_info(cfg, settings, now=None):
    """price_info of the configured model whose table runs out first (an expired one first)."""
    infos = [{**price_info(model, now), "price_model": model} for model in configured_models(cfg, settings)]
    return min(infos, key=lambda info: (info["price_valid"], info["price_valid_until"], info["price_model"]))


def selected_key(root=None, cfg=None):
    if cfg is None:
        from mcs_util import load_config
        cfg = load_config()
    settings, _ = resolve_settings(cfg, root)
    return provider_key(settings["provider"], root)


def selected_key_problem(root, cfg):
    """None when the selected provider's key is usable, else the fixed reason it is not."""
    settings, _ = resolve_settings(cfg, root)
    return private_env_problem(provider_registry.PROVIDER_MODULES[settings["provider"]].KEY_ENV, root)


# The send-block codes refusal_code names more precisely; every other code is already final.
REFINED_CODES = frozenset({"pricing_unverified", "window_history_unverified", "scope"})


def refusal_code(cfg, code, *, model, root=None, settings=None, route=None, project_id=None):
    """Name a send-block code the way the stop-reason classifier tells definitive from transient.

    ``cfg`` is the raw configuration (an explicit ``mode`` and the facility review are read
    from it); ``settings``, when given, are the resolved settings the code was judged under.
    An expired price table, an aged period, an explicit mode off and a provider the facility
    review has not approved are definitive. An unverifiable price or period history, a
    computed off (the key problem, else ``unknown``) and an unreadable review record are not.
    With ``route`` and ``project_id``, a scope stop that a second reading finds open and in
    scope was a key or review read that has since recovered: ``env_file_unreadable``.
    Every other code is returned as it is."""
    try:
        if code == "pricing_unverified":
            return "price_expired" if not pricing_valid(model=model) else code
        if code == "window_history_unverified":
            aged = setting_block_reason(_cfg(cfg) if settings is None else settings) == code
            return "window_history_aged" if aged else "window_history_pruned"
        if code == "scope":
            resolved, _, held = resolve_selection(cfg, root)
            if held is not None:
                import external_transfer
                unreadable = external_transfer.status(root).get("state") == "unreadable"
                return "transfer_review_unreadable" if unreadable else "provider_not_approved"
            block = cfg.get("cloud_llm") if isinstance(cfg, dict) else None
            if isinstance(block, dict) and block.get("mode") == "off" and not cfg.get("provider_unavailable"):
                return "mode_off"
            if resolved["mode"] == "off":
                # computed off: a mode left out with no usable key, or a configuration error
                return selected_key_problem(root, cfg) or "unknown"
            if route is not None and eligible(resolved, route, project_id):
                return "env_file_unreadable"
    except Exception:
        return {"window_history_unverified": "window_history_pruned", "scope": "unknown"}.get(code, code)
    return code


def send_route(route):
    """Routes still permitted to create new cloud requests."""
    return route == "patient_context"


# Set by the pool for its stored-message backlog workers only.
BACKLOG_WORKER_ENV = "MCS_REEXTRACT_BACKLOG_WORKER"


def _backlog_caps_configured(settings):
    """Require both aggregate and selected-provider daily/monthly spending caps."""
    providers = (settings["provider_order"] if settings["provider"] == "auto"
                 else [settings["provider"]])
    return (all(settings[key] is not None for key in ("daily_cost_limit_usd", "monthly_cost_limit_usd"))
            and all(name in settings["provider_budgets"] for name in providers))


def reextract_backlog_window(cfg, *, now=None):
    """The owner's bounded reopening of stored-message sends under spending caps."""
    return _backlog_window(cfg, "reextract_backlog_until", now)


def semantic_backlog_window(cfg, *, now=None):
    """The owner's separate bounded reopening of semantic backlog sends."""
    return _backlog_window(cfg, "semantic_backlog_until", now)


def _backlog_window(cfg, key, now):
    c, errors = cloud_config(cfg if "cloud_llm" in cfg else {"cloud_llm": cfg})
    until = c[key]
    return (not errors and until is not None
            and (time.time() if now is None else now) < until_epoch(until))


def semantic_planned(cfg):
    """Name the semantic cloud contract from raw config, retaining explicit null."""
    c, errors = cloud_config(cfg)
    block = cfg.get("cloud_llm") if isinstance(cfg, dict) else None
    # Explicit null stops sends but retains the declared paid-answer contract.
    return not errors and "semantic_backlog" in c["routes"] and (
        send_route("semantic_backlog") or isinstance(block, dict) and "semantic_backlog_until" in block)


def reextract_planned(cfg):
    """Whether stored-message re-extraction goes to the cloud at all, for
    spawning workers and estimates; route_open decides each send. Card display
    follows send_route alone, because the window opens in one process only.
    A route list without reextract keeps it closed, window or not."""
    return "reextract" in _cfg(cfg)["routes"] and (send_route("reextract") or reextract_backlog_window(cfg))


def route_open(route, cfg):
    """send_route, plus stored-message sends inside the bounded window. Only the
    backlog worker process may make them: its selection is the receipted
    history scope, so resident and tick passes stay local."""
    window = {"reextract": reextract_backlog_window, "semantic_backlog": semantic_backlog_window}.get(route)
    return send_route(route) or (window is not None and os.environ.get(BACKLOG_WORKER_ENV) == "1"
                               and window(cfg))


def semantic_summary_route_open(cfg):
    """Whether the bounded semantic window permits a summary-stage send.

    Unlike facts extraction, summary generation is phase-bound in the
    reservation owner, so non-worker backlog passes may use the same window
    without widening extraction or realtime traffic."""
    return send_route("semantic_backlog") or semantic_backlog_window(cfg)


def route_open_for_owner(route, cfg, owner=None):
    """Route permission for one owner-bound request."""
    return (route_open(route, cfg)
            or (route == "semantic_backlog" and isinstance(owner, dict)
                and owner.get("semantic_phase") == "summary"
                and semantic_summary_route_open(cfg)))


def eligible(cfg, route, project_id):
    """Allow new cloud inference only for an open route within its scope."""
    return route_open(route, cfg) and in_scope(cfg, route, project_id)


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
    if not isinstance(block, dict) or cfg.get("provider_unavailable"):
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


def _semantic_generation_identity(meta):
    """Verify a semantic unit including the interpretation generation it answered."""
    identity = _semantic_financial_identity(meta)
    if identity is None:
        return None
    generation = meta['owner'].get('semantic_generation')
    return (*identity[:2], generation, identity[2])


# Member-bound semantic reservations carry generation/source/phase from the
# first target-aware semantic rollout (commit a4d590c74, 2026-10-30 00:46:08 +09).
# Rows before that boundary cannot prove which member or interpretation they
# answered; their shape is nevertheless a complete, valid legacy receipt.
SEMANTIC_MEMBER_BINDING_START_EPOCH = 1793288768.0


def _prebinding_semantic_reservation(meta, row):
    """Whether a valid receipt is the pre-member-binding semantic format."""
    owner = meta.get('owner')
    return (meta.get('route') == 'semantic_backlog'
            and isinstance(meta.get('unit_key'), str)
            and re.fullmatch(r'semantic:[0-9]+:[0-9a-f]+', meta['unit_key']) is not None
            and type(row['created_at']) in (int, float)
            and row['created_at'] < SEMANTIC_MEMBER_BINDING_START_EPOCH
            and isinstance(owner, dict)
            and all(key not in owner for key in (
                'semantic_target_id', 'semantic_source', 'semantic_generation', 'semantic_phase'))
            and meta.get('logical_target_id') is None
            and meta.get('logical_unit_fp') is None
            and meta.get('logical_request_fp') is None
            and meta.get('input_binding') is None)


def _legacy_request_effort(previous, current_body):
    """The effort an older record without one was proven to have sent, else None.
    The prompt is not persisted; each provider knows its own old request shape."""
    return next((effort for module in provider_registry.PROVIDER_MODULES.values()
                 if (effort := module.legacy_request_effort(previous, current_body)) is not None), None)


def _selection_differs(previous, candidate, current_body):
    if previous['model'] != candidate['model']:
        return True
    new_effort = candidate.get('effort')
    if not isinstance(new_effort, str):
        return False
    old_effort = previous.get('effort')
    if old_effort is None:
        old_effort = _legacy_request_effort(previous, current_body)
    return isinstance(old_effort, str) and old_effort != new_effort


def _verified_request_history(ledger, *, route, project_id, message_id, unit_key):
    """Enumerate the unit's reservation/result history and prove it well-formed:
    every receipt parses, every reservation validates, and each result settles
    against exactly one reservation. Unverifiable rows raise — they never
    pass silently. Returns the reservation rows for the caller's own scan."""
    # Each kind is processed separately. Ordering within kind lets SQLite seek by kind and patient.
    records = ledger.db.execute("SELECT artifact_id,kind,project_id,message_id,model,meta,created_at "
        "FROM artifacts WHERE kind IN (?,?) AND project_id=? AND (message_id IS ? OR ?='semantic_backlog') AND "
        "CASE WHEN typeof(meta)!='text' OR length(meta)>32768 OR NOT json_valid(meta) THEN 1 "
        "WHEN json_type(meta)!='object' THEN 1 "
        "WHEN (json_extract(meta,'$.route')=? OR json_type(meta,'$.route') IS NULL) AND "
        "(json_extract(meta,'$.unit_key')=? OR ? IN ('semantic_backlog','patient_context','reextract')) THEN 1 "
        "ELSE EXISTS (SELECT 1 FROM json_tree(meta) "
        "WHERE key IS NOT NULL GROUP BY parent,key HAVING COUNT(*)>1) END ORDER BY kind,artifact_id",
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
        raise _classified(AnthropicError('budget_exceeded', 'cloud_unit_unresolved'),
                          'receipt_inconsistent') from None
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
            raise _classified(AnthropicError('pricing_unverified', 'cloud_unit_unresolved'),
                              'receipt_inconsistent') from None
    for rid, results in checked_results.items():
        matches = reservations.get(rid, [])
        if len(matches) != 1:
            raise _classified(AnthropicError('budget_exceeded', 'cloud_unit_unresolved'), 'receipt_inconsistent')
        previous, maximum = matches[0]
        try:
            # Validate the complete result set: two individually valid rows
            # are still ambiguous accounting, including on a same-prompt retry.
            _settled(previous, maximum, results)
        except (AnthropicError, InvalidOperation, ValueError, TypeError, KeyError, OverflowError):
            raise _classified(AnthropicError('pricing_unverified', 'cloud_unit_unresolved'),
                              'receipt_inconsistent') from None
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


# Fixed classification of an accounting refusal, set where it is decided and read only by
# the history observation. It never changes the exception, its detail or any decision.
HISTORY_REFUSALS = frozenset({"repeat_success", "repeat_failed", "repeat_unsettled",
                              "result_missing", "disposition", "receipt_inconsistent"})
# The guard's final branch after the one validated result of the same request. Only
# _classified sets verified_repeat, so a converted or foreign exception never carries it.
REPEAT_REFUSALS = frozenset({"repeat_success", "repeat_failed", "repeat_unsettled"})
# Owner decision 2026-11-25: the same logical request may be billed for the first
# send plus five failure retries; the next verified repeat refuses like any other.
# A constant, not a setting.
FAILED_SEND_BILLED_LIMIT = CLOUD_MAX_ATTEMPTS


def _classified(error, refusal):
    error.history_refusal = refusal
    error.verified_repeat = refusal in REPEAT_REFUSALS
    return error


def _repeat_comparison(previous, candidate, field):
    nullable = field in ('effort', 'profile')
    def valid(value):
        if field == 'max_tokens':
            return type(value) is int and value > 0
        return (value is None and nullable) or type(value) is str
    if field not in previous and field not in candidate:
        return 'unknown'
    if field not in previous:
        return 'stored_absent' if valid(candidate[field]) else 'unknown'
    if field not in candidate:
        return 'current_absent' if valid(previous[field]) else 'unknown'
    old, new = previous[field], candidate[field]
    if not valid(old) or not valid(new):
        return 'unknown'
    return 'same' if type(old) is type(new) and old == new else 'different'


def _repeat_observation(previous, candidate, match):
    values = {label + '_cmp': _repeat_comparison(previous, candidate, field) for label, field in (
        ('unit_key', 'unit_key'), ('binding', 'input_binding'), ('request_fp', 'request_fp'),
        ('model', 'model'), ('effort', 'effort'), ('profile', 'profile'), ('max_tokens', 'max_tokens'))}
    exact = ('unit_key_cmp', 'binding_cmp', 'request_fp_cmp')
    # Precedence groups observations; it does not establish why recovery missed.
    reason = ('recovery_not_requested' if 'input_binding' not in candidate else
              'unit_key_mismatch' if values['unit_key_cmp'] == 'different' else
              'stored_binding_missing' if values['binding_cmp'] == 'stored_absent' else
              'input_binding_mismatch' if values['binding_cmp'] == 'different' else
              'request_envelope_mismatch' if values['request_fp_cmp'] == 'different' else
              'exact_keys_same_unexplained' if all(values[key] == 'same' for key in exact) else
              'observation_unknown')
    return {**values, 'guard_match': match, 'primary_reason': reason}


def _observe_recovery(observation, path):
    try:
        if type(observation) is dict:
            observation['recovery_path'] = path
    except Exception:
        pass


# A request without its result is replaced by local work only after the longest live send and
# adoption could still land it: the attempt's own wait plus the 900 s unit deadline and the
# commit margin. The adoption fence (_published_version) covers a suspended clock.
RESULT_STALE_MARGIN_S = 905
# Dispositions that end a reservation without an adopted answer; none of them permits a resend.
SUPERSEDED_LOCAL = "superseded_local"
_INLINE_DISPOSITIONS = frozenset({"discarded_scope", "discarded_workday"})
_ENDED_DISPOSITIONS = _INLINE_DISPOSITIONS | {SUPERSEDED_LOCAL}
# A provider answer can be financially successful yet unusable after local
# schema/source validation. This receipt-bound annotation lets accounting treat
# it as a settled failure without mutating the immutable paid result receipt.
VALIDATION_FAILURE_KIND = "cloud_usage_validation_failure"


def _result_stale(row, previous, now):
    """True once a reservation without its result is past the replacement threshold; never raises."""
    try:
        timeout = previous.get('timeout_seconds')
        if type(timeout) not in (int, float) or not math.isfinite(timeout) \
                or not 0 < timeout <= cloud_client.ATTEMPT_TIMEOUT_CEILING:
            timeout = cloud_client.ATTEMPT_TIMEOUT_CEILING
        created = row['created_at']
        current = time.time() if now is None else now
        if (type(created) not in (int, float) or not math.isfinite(created)
                or type(current) not in (int, float) or not math.isfinite(current)):
            return False
        return created + timeout + RESULT_STALE_MARGIN_S <= current
    except Exception:
        return False


def _validation_failures(ledger, reservation_id, reservation=None):
    rows = _matching_bound_rows(ledger, VALIDATION_FAILURE_KIND, reservation_id)
    if reservation is None:
        return rows
    bound = _receipt_binding(reservation)
    for row in rows:
        meta = _receipt_meta(row["meta"])
        if (any(meta.get(key) != bound[key] for key in bound)
                or any(key in meta and key not in bound for key in _BINDING_KEYS)
                or row["project_id"] != reservation["project_id"]
                or row["message_id"] != reservation.get("message_id")
                or row["model"] != reservation["model"]):
            raise AnthropicError("pricing_unverified", "usage_invalid")
    return rows


def _fixed_failure_code(value):
    return (value if type(value) is str and len(value) <= 64
            and all(c in "abcdefghijklmnopqrstuvwxyz0123456789_" for c in value) else None)


def record_validation_failure_tx(ledger, reservation, *, failure_reason=None, repair_issues=None):
    """Annotate a paid answer rejected by local validation; billing receipts stay immutable."""
    if not ledger.db.in_transaction:
        raise RuntimeError("cloud_validation_failure_transaction_required")
    if not isinstance(reservation, dict) or type(reservation.get("reservation_id")) is not str:
        raise ValueError("cloud_validation_failure_invalid")
    existing = _validation_failures(ledger, reservation["reservation_id"], reservation)
    if existing:
        return existing[0]["artifact_id"]
    meta = _receipt_binding(reservation)
    meta["validation_failure"] = True
    reason = _fixed_failure_code(failure_reason)
    if reason is not None:
        meta["failure_reason"] = reason
    if isinstance(repair_issues, dict) and len(repair_issues) <= 32:
        issues = {}
        for key, count in repair_issues.items():
            key = _fixed_failure_code(key)
            if key is not None and type(count) is int and 0 <= count <= 1000000:
                issues[key] = count
        if issues:
            meta["repair_issues"] = issues
    return ledger.artifact_add_tx(VALIDATION_FAILURE_KIND, "{}",
        project_id=reservation["project_id"], message_id=reservation.get("message_id"),
        model=reservation["model"], meta=meta)


def _guard_repeated_request(ledger, candidate, *, current_body=None, local_fallback=False,
                            supersede=None, now=None):
    """Keep an unresolved or already answered logical request from being sent again.

    With ``supersede`` (a list; patient-context local fallback only) the guard also judges
    which paid requests local work may replace. A reservation already ended by a disposition,
    or whose result carries a discarded one, passes without a new row. A verified same request
    of this unit key (a settled repeat, or a result still missing past
    :data:`RESULT_STALE_MARGIN_S`) is appended to the list instead of refusing. On the
    send path (``local_fallback`` false) a proven same request whose verified results
    are retryable cloud failures may be sent again as a separate billed attempt, at most
    :data:`FAILED_SEND_BILLED_LIMIT` billed attempts in total; a provider-proven
    refusal (``contract_error_detail``) is never resent or counted; local fallback
    never passes a settled failure without a disposition. Everything else refuses exactly as
    without it. Without ``local_fallback`` the clock is never read."""
    if supersede is not None and (type(supersede) is not list or not local_fallback
                                  or candidate.get('route') != 'patient_context'):
        raise ValueError('cloud_supersede_scope_invalid')
    rows = _verified_request_history(ledger, route=candidate['route'],
        project_id=candidate['project_id'], message_id=candidate['message_id'],
        unit_key=candidate['unit_key'])
    result_map = None
    disposition_ids = None
    candidate_occurrence, occurrence_checked = None, False
    failed_sends = 0
    for row in rows:
        refusal = None
        guard_match = None
        superseded = stale = None
        try:
            previous = _receipt_meta(row['meta'])
            maximum = _reserve_valid(previous, row)
            if candidate['route'] == 'semantic_backlog':
                old_target = _semantic_financial_target(previous)
                new_target = _semantic_financial_target(candidate)
                if old_target is not None and new_target is not None and old_target != new_target:
                    continue
                old_identity = _semantic_generation_identity(previous)
                new_identity = _semantic_generation_identity(candidate)
                if old_identity is not None and new_identity is not None and old_identity != new_identity:
                    continue
                if new_identity is not None and _prebinding_semantic_reservation(previous, row):
                    # The first target-aware rollout made interpretation generation part of
                    # the semantic unit. Earlier receipts can neither be reused for a reprocess
                    # nor prove that they answered this member's current generation; keeping them
                    # as duplicate evidence wedged every later send in the project. They remain
                    # fiscally valid, but a generation-bound candidate is allowed one fresh unit.
                    continue
            old_fp, new_fp = previous.get('logical_request_fp'), candidate.get('logical_request_fp')
            if (local_fallback and candidate['route'] == 'patient_context'
                    and candidate['unit_key'] == 'summary' and old_fp != new_fp):
                new_fp = _context_summary_fallback_fp(current_body, previous)
            prompt_verified = False
            # Only this unit key's own requests are replaced: another unit (another source,
            # contract or provider) holding the same prompt keeps its refusal.
            own_unit = previous.get('unit_key') == candidate['unit_key']
            if old_fp is not None and new_fp is not None:
                if old_fp != new_fp:
                    continue
                prompt_verified = True
                guard_match = 'logical_equal'
            elif _legacy_request_effort(previous, current_body) is not None:
                prompt_verified = True
                guard_match = 'legacy_low_verified'
            else:
                guard_match = 'legacy_unproven'
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
                disposition_ids = {}
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
                        value = result_meta.get('disposition')
                        # an unreadable value is kept as None: it blocks and never raises here
                        disposition_ids.setdefault(reservation_id, set()).add(
                            value if type(value) is str else None)
                    else:
                        result_map.setdefault(reservation_id, []).append((result_meta, result_row))
            if previous['reservation_id'] in disposition_ids:
                ended = disposition_ids[previous['reservation_id']]
                if local_fallback and candidate['route'] == 'patient_context':
                    # Local work already replaced this request; it is never sent again.
                    if ended == {SUPERSEDED_LOCAL}:
                        continue
                    # First disposition wins: an ended request needs no new row.
                    if supersede is not None and ended <= _ENDED_DISPOSITIONS:
                        continue
                superseded = SUPERSEDED_LOCAL in ended
                refusal = 'disposition'
                raise ValueError('discarded')
            matches = result_map.get(previous['reservation_id'], [])
            if not matches:
                refusal = 'result_missing'   # no result yet: the send's outcome is unknown, not a broken receipt
                if local_fallback:
                    stale = _result_stale(row, previous, now)
                    if supersede is not None and prompt_verified and own_unit and stale:
                        supersede.append(_accounting_meta(row['meta']))
                        continue
            _, settled = _settled(previous, maximum, matches)
            result = matches[0][0]
            validation_failed = bool(_validation_failures(ledger, previous['reservation_id'], previous))
            if result.get('disposition'):
                # record_result writes only a discarded value inline; anything else blocks
                if supersede is not None and result['disposition'] in _INLINE_DISPOSITIONS:
                    continue
                refusal = 'disposition'
                raise ValueError('discarded')
            http_class = result.get('http_class')
            if (local_fallback and isinstance(http_class, str)
                    and re.fullmatch(r'http_[45][0-9]{2}', http_class)
                    and result.get('error_kind') == http_error(int(http_class[5:]))[0]
                    and result.get('usage') is None):
                continue  # The failed HTTP result stays charged; this grants no new cloud send.
            if (_never_sent(result, settled) or _confirmed_openai_quota(result, previous['model'])
                    or _confirmed_anthropic_payment(result, previous['model'])):
                # Never-sent attempts are free; provider-proven account/payment refusals
                # reopen only through their explicit human-resume contract.
                continue
            # A confirmed successful result may be deliberately reprocessed
            # with a different selection (model, output bound or effort).
            # An ambiguous send never grants that retry.
            if (not local_fallback and prompt_verified and settled and not validation_failed
                    and result.get('usage') is not None and result.get('http_class') == 'http_200'
                    and result.get('error_kind') is None and _selection_differs(previous, candidate, current_body)):
                continue
            # The one validated result of the same request: a proven success, a settled
            # failure, or an unsettled one. A verified settled failure permits a resend on
            # the send path only (the unit stayed unanswered and fully accounted; the new
            # send is a separate billed attempt, capped at FAILED_SEND_BILLED_LIMIT
            # billed attempts). A provider-proven refusal detail is excluded: the provider
            # already declined this content, so it refuses and is never counted. Local
            # fallback never passes a settled failure without a disposition, and success,
            # unproven and unknown outcomes still refuse duplicates.
            successful = (not validation_failed
                          and result.get('usage') is not None and result.get('http_class') == 'http_200'
                          and result.get('error_kind') is None)
            refusal = ('repeat_unsettled' if not settled else
                       'repeat_success' if successful else 'repeat_failed')
            if supersede is not None and prompt_verified and own_unit:
                supersede.append(_accounting_meta(row['meta']))
                continue
            failed_send = (not successful and result.get('http_class') != 'not_sent'
                           and result.get('contract_error_detail') not in cloud_client.CONTRACT_ERROR_DETAILS
                           and (validation_failed
                                or result.get('error_kind') in ('rate_limited', 'transport', 'timeout')
                                or (settled and result.get('http_class') == 'http_200'
                                    and result.get('error_kind') in ('contract_error', 'protocol_error'))))
            if failed_send and not local_fallback and prompt_verified:
                failed_sends += 1
                if failed_sends < FAILED_SEND_BILLED_LIMIT:
                    continue
            raise ValueError('already_sent_or_unknown')
        except (AnthropicError, InvalidOperation, ValueError, TypeError, KeyError, OverflowError):
            error = _classified(AnthropicError('budget_exceeded', 'cloud_unit_unresolved'),
                                refusal or 'receipt_inconsistent')
            if refusal == 'disposition':
                error.history_superseded = superseded is True
            elif refusal == 'result_missing' and local_fallback:
                error.history_stale = stale is True
            if candidate['route'] == 'reextract' and candidate.get('message_id') is not None and guard_match:
                try:
                    error.history_observation = _repeat_observation(previous, candidate, guard_match)
                except Exception:
                    error.history_observation = {}
            raise error from None



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



def guard_context_origins(ledger, project_id, origins):
    """Verify exact retained context-answer receipts without changing their accounting."""
    expected, legacy = {}, {}
    with _accounting_snapshot(ledger.db):
        for mid, unit, model, metadata in origins:
            binding = mid, unit, model
            legacy_refs = 'cloud_receipts' not in metadata
            refs = metadata.get('cloud_receipts')
            if legacy_refs:
                # Without a stored pointer, this source's retained sends must all
                # verify; a missing history cannot establish an adopted answer.
                if binding not in legacy:
                    history = _verified_request_history(ledger, route='patient_context',
                        project_id=project_id, message_id=mid, unit_key=unit)
                    legacy[binding] = [row['artifact_id'] for row in history
                        if _receipt_meta(row['meta']).get('route') == 'patient_context'
                        and _receipt_meta(row['meta']).get('unit_key') == unit]
                refs = [{'artifact_id': aid, 'witness': None} for aid in legacy[binding]]
            if (type(refs) is not list or not 1 <= len(refs) <= 128
                    or any(type(ref) is not dict or set(ref) != {'artifact_id', 'witness'}
                           or type(ref['artifact_id']) is not int or ref['artifact_id'] <= 0
                           or (not legacy_refs or ref['witness'] is not None)
                           and not valid_hash(ref['witness']) for ref in refs)
                    or len({ref['artifact_id'] for ref in refs}) != len(refs)):
                raise ValueError('patient_context_cloud_source_unverified')
            for ref in refs:
                proof = binding, ref['witness']
                if ref['artifact_id'] in expected and expected[ref['artifact_id']] != proof:
                    raise ValueError('patient_context_cloud_source_unverified')
                expected[ref['artifact_id']] = proof
        evidence = reservation_evidence_batch(ledger.db, expected)
        for aid, ((mid, unit, model), witness) in expected.items():
            proof = evidence[aid]
            reservation = proof['reservation']
            if (reservation.get('route') != 'patient_context' or reservation['project_id'] != project_id
                    or reservation.get('message_id') != mid or reservation['unit_key'] != unit
                    or reservation['model'] != model or witness is not None and proof['witness'] != witness):
                raise ValueError('patient_context_cloud_source_unverified')

def guard_local_fallback(ledger, *, route, project_id, message_id, unit_key, request_factory,
                         allow_supersede=False):
    """Prove local substitution safe without sending, reserving, or changing accounting.

    ``allow_supersede`` (patient context only) also passes the paid requests that
    :func:`supersede_local_tx` would record as replaced; it still writes nothing. Returns the
    reservation ids of those requests: empty when there is nothing left to record, as after
    that function ran, and always empty without ``allow_supersede``."""
    if allow_supersede and route != 'patient_context':
        raise ValueError('cloud_supersede_scope_invalid')
    targets = []
    with _accounting_snapshot(ledger.db):
        rows = _verified_request_history(ledger, route=route, project_id=project_id,
                                        message_id=message_id, unit_key=unit_key)
        if _tally(ledger).invalid:
            raise AnthropicError('budget_exceeded', 'cloud_unit_unresolved')
        if rows:
            candidate, body = request_factory()
            _guard_repeated_request(ledger, candidate, current_body=body, local_fallback=True,
                                    **({'supersede': targets} if allow_supersede else {}))
    return [reservation['reservation_id'] for reservation in targets]


def supersede_local_tx(ledger, *, route, project_id, message_id, unit_key, request_factory, now=None):
    """Record this unit key's verified paid requests as replaced by local work, in the caller's transaction.

    Appends one ``superseded_local`` disposition per replaced reservation and returns their
    artifact ids, each a newly written row; reservation and result rows, and so every total,
    stay as they are. The targets are what ``guard_local_fallback(allow_supersede=True)``
    passes with a replacement: same-prompt requests whose stored ``unit_key`` is this one, at
    any age. Any refusal, by the guard or by a write, raises with none of this call's rows
    left in the transaction. The caller commits them together with the local result."""
    if not ledger.db.in_transaction:
        raise RuntimeError('cloud_disposition_transaction_required')
    if route != 'patient_context':
        raise ValueError('cloud_supersede_route_invalid')
    with _accounting_snapshot(ledger.db):
        rows = _verified_request_history(ledger, route=route, project_id=project_id,
                                        message_id=message_id, unit_key=unit_key)
        if _tally(ledger).invalid:
            raise AnthropicError('budget_exceeded', 'cloud_unit_unresolved')
        if not rows:
            return []
        candidate, body = request_factory()
        targets = []
        _guard_repeated_request(ledger, candidate, current_body=body, local_fallback=True,
                                supersede=targets, now=now)
        written, seen = [], set()
        ledger.db.execute('SAVEPOINT cloud_supersede_local')
        try:
            for reservation in targets:
                if reservation['reservation_id'] not in seen:
                    seen.add(reservation['reservation_id'])
                    written.append(record_disposition_tx(ledger, reservation, SUPERSEDED_LOCAL))
        except BaseException:
            ledger.db.execute('ROLLBACK TO cloud_supersede_local')
            ledger.db.execute('RELEASE cloud_supersede_local')
            raise
        ledger.db.execute('RELEASE cloud_supersede_local')
        return written


def _proven_unsent(ledger, row):
    """True when a reservation row is proven to have left nothing with the provider: its one
    result settles it as never sent and no disposition names it. Anything missing, repeated
    or unreadable is not proof."""
    try:
        reservation = _receipt_meta(row['meta'])
        maximum = _reserve_valid(reservation, row)
        rid = reservation['reservation_id']
        results = _matching_bound_rows(ledger, KINDS[1], rid)
        if (len(results) != 1 or len(_matching_bound_rows(ledger, KINDS[0], rid)) != 1
                or _matching_bound_rows(ledger, 'cloud_usage_disposition', rid)):
            return False
        result = _receipt_meta(results[0]['meta'])
        _, settled = _settled(reservation, maximum, [(result, results[0])])
        return bool(_never_sent(result, settled) and result.get('usage') is None
                    and not result.get('disposition'))
    except (AnthropicError, InvalidOperation, ValueError, TypeError, KeyError, OverflowError):
        return False


def unit_reserved(ledger, *, route, project_id, message_id, unit_key, source_sig=None, sent_only=False):
    """True when this unit key has any reservation for the message; an unreadable one counts.

    With ``source_sig`` only a reservation made for that input (its owner's source signature)
    counts: one unit key serves every later input of the same message or patient, and a request
    made for an earlier input can never be taken up by the current one.

    With ``sent_only`` a reservation proven never sent does not count. A request is reserved
    before the last checks ahead of its send; one they refuse is settled as ``not_sent``, costs
    nothing and holds no answer, now or later. Every other reservation holds or may hold paid
    work and still counts, as does one whose rows cannot prove it."""
    with _accounting_snapshot(ledger.db):
        rows = ledger.db.execute(
            "SELECT artifact_id,kind,project_id,message_id,model,meta,created_at FROM artifacts "
            "WHERE kind=? AND project_id=? AND message_id IS ? AND "
            "CASE WHEN typeof(meta)!='text' OR length(meta)>32768 OR NOT json_valid(meta) THEN 1 "
            "WHEN json_type(meta)!='object' THEN 1 "
            "ELSE json_extract(meta,'$.route') IS ? AND json_extract(meta,'$.unit_key') IS ? "
            "AND (? IS NULL OR json_extract(meta,'$.owner.source_sig') IS ?) END ORDER BY artifact_id"
            + ("" if sent_only else " LIMIT 1"),
            (KINDS[0], project_id, message_id, route, unit_key, source_sig, source_sig)).fetchall()
        return any(not sent_only or not _proven_unsent(ledger, row) for row in rows)


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


def _accounting_order_source(db) -> str:
    """The table expression for the accounting reads ordered by artifact_id.

    Inside the caller's read snapshot, the exact order index (ledger_schema) lets
    these reads skip a temporary sort; without it they keep their original plan."""
    import ledger_schema
    if ledger_schema.cloud_usage_order_index_state(db) == "present":
        return f"artifacts INDEXED BY {ledger_schema.CLOUD_USAGE_ORDER_INDEX}"
    return "artifacts"


def _usage_report_snapshot(ledger, *, now=None, period="month"):
    now = time.time() if now is None else now
    day, month = _periods(now)
    rows = list(ledger.db.execute(
        "SELECT artifact_id,kind,project_id,message_id,model,meta,created_at "
        f"FROM {_accounting_order_source(ledger.db)} "
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
        for at, provider, requests, cost, done in carryover_entries(ledger.db, day if period == "day" else month):
            daily += requests * int(at >= day)
            total += cost
            providers.setdefault(provider, {"cost_usd": Decimal(0), "unresolved_requests": 0})
            providers[provider]["cost_usd"] += cost
            if done:
                settled += cost
            else:
                unknown += requests
                providers[provider]["unresolved_requests"] += requests
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


def _circuit_on_pricing_error(ledger, error, model, since):
    """Hold the circuit for an unverifiable price or accounting; an expired price table only refuses.

    ``since`` is the instant the attempt began: a price table that was not valid then (a day
    change between the pre-check and the reservation) is a refusal, not an accounting fault."""
    if error.kind == "pricing_unverified" and pricing_valid(since, model=model) and pricing_valid(model=model):
        _circuit_result(ledger, error)


def block_reason(ledger, cfg, route, project_id, *, model=None, owner=None):
    if not route_open_for_owner(route, cfg, owner) or not in_scope(cfg, route, project_id):
        return "scope"
    return global_block_reason(ledger, model=_cfg(cfg)["model"] if model is None else model)


def stopped(ledger) -> bool:
    """Whether the standing `cloud-llm stop` switch is set (paid cloud and Jev sends)."""
    row = ledger.db.execute("SELECT content FROM artifacts WHERE kind='cloud_control' "
                            "AND project_id IS NULL AND message_id IS NULL "
                            "ORDER BY artifact_id DESC LIMIT 1").fetchone()
    return row is not None and row["content"] != "running"


def global_block_reason(ledger, *, model=MODEL):
    """Return a fixed global stop/pricing code without evaluating patient scope."""
    if stopped(ledger):
        return "paused"
    from semantic_runtime import circuit_open, _circuit_state
    if circuit_open(ledger, kind="cloud_circuit"):
        return _circuit_state(ledger, kind="cloud_circuit").get("failure_class") or "circuit_open"
    if not pricing_valid(model=model):
        return "pricing_unverified"
    return None


def _hash_row(digest, row):
    try:
        digest.update(canonical(tuple(row)))
    except (TypeError, ValueError, UnicodeError):
        raise AnthropicError('pricing_unverified', 'usage_invalid') from None
    digest.update(b'\n')


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
        # sha256 state over the in-period rows up to _main_id (fingerprint's
        # first part): appended rows extend it instead of re-reading history
        self._main = hashlib.sha256()
        self._main_id = 0
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
        self.carried = None   # carry-over marker the totals include; None until first read
        # (guard mutation count, artifact id, digest state) of committed rows hashed
        # outside any transaction while the write-time guard was installed, else None.
        self._guarded = None

    def _add(self, day, cost, provider):
        self.days.setdefault(day, [0, Decimal(0)])[1] += cost
        self.total += cost
        self.provider_days[(provider, day)] = self.provider_days.get((provider, day), Decimal(0)) + cost
        self.provider_totals[provider] = self.provider_totals.get(provider, Decimal(0)) + cost

    def fingerprint(self, ledger, now):
        """Hash all accounting columns, including reservations outside the period.

        Re-reads every in-period row (the same-length mutation check) and keeps
        the verified state so update() can extend it with appended rows only."""
        digest = hashlib.sha256()
        with closing(ledger.db.execute(
            "SELECT artifact_id,kind,project_id,message_id,model,meta,created_at "
            f"FROM {_accounting_order_source(ledger.db)} "
            "WHERE kind IN ('cloud_usage_reserve','cloud_usage_result') AND created_at>=? "
            "AND artifact_id<=? ORDER BY artifact_id", (self.month, self.last_id))) as rows:
            for row in rows:
                _hash_row(digest, row)
        self._main, self._main_id = digest.copy(), self.last_id
        return self._with_prior(digest, ledger, now)

    def unchanged(self, ledger, now):
        """The rows this tally already hashed are unchanged in the caller's read snapshot.

        The installed write-time guard moves its mutation count whenever an accounting
        row is changed, removed, replaced or inserted below a newer id; appends leave it
        alone. While it still matches the count seen when committed rows were hashed,
        those rows hold and only the rows tallied after them (which a rollback may have
        removed or an id reuse replaced) are hashed again. Otherwise everything is."""
        guarded, mutations = self._guarded, _guard_mutations(ledger.db)
        if guarded is not None and mutations == guarded[0] and guarded[1] <= self.last_id:
            digest = guarded[2].copy()
            if guarded[1] < self.last_id:
                # The tail is an artifact_id range: read it through the accounting-only
                # order index when present, else by rowid, never by the kind/created_at
                # index that would walk the whole period.
                source = _accounting_order_source(ledger.db)
                with closing(ledger.db.execute(_GUARDED_TAIL_SQL.format(
                        source=source if source != "artifacts" else "artifacts NOT INDEXED"),
                        (guarded[1] + 1, self.last_id, self.month))) as rows:
                    for row in rows:
                        _hash_row(digest, row)
            if self._with_prior(digest.copy(), ledger, now) == self.prefix:
                self._main, self._main_id = digest, self.last_id
                return True
        return self.prefix == self.fingerprint(ledger, now)

    def _mark_guarded(self, ledger):
        """Remember the verified digest of committed rows for unchanged()'s guarded check."""
        mutations = None if ledger.db.in_transaction else _guard_mutations(ledger.db)
        if mutations is not None and self._main_id == self.last_id:
            self._guarded = (mutations, self._main_id, self._main.copy())

    def _with_prior(self, digest, ledger, now):
        """Finish a main-row digest with the prior-period reservations, in fingerprint's order."""
        prior = _matching_rows_many(ledger, KINDS[0], self.orphans, now=now, _timestamps_checked=True)
        for row in sorted(prior, key=lambda row: row['artifact_id']):
            _hash_row(digest, row)
        return digest.digest()

    def update(self, ledger, now):
        if self.carried is None:
            # Rows a reset deleted after this tally's last_id leave no fingerprint
            # change; _current_tally rebuilds on a changed carry-over marker instead.
            self.carried = carryover_marker(ledger.db)
            for at, provider, requests, cost, _settled_group in carryover_entries(ledger.db, self.month):
                day = _periods(at)[0]
                self.days.setdefault(day, [0, Decimal(0)])[0] += requests
                self._add(day, cost, provider)
        last_id = self.last_id
        new_orphans = {}
        changed_orphans = set()
        # Extend only a state that covers exactly the rows tallied so far.
        extend = self._main.copy() if self._main_id == last_id else None
        with closing(ledger.db.execute(
            "SELECT artifact_id,kind,project_id,message_id,model,meta,created_at "
            f"FROM {_accounting_order_source(ledger.db)} "
            "WHERE kind IN ('cloud_usage_reserve','cloud_usage_result') AND created_at>=? "
            "AND artifact_id>? ORDER BY artifact_id", (self.month, self.last_id))) as rows:
            for row in rows:
                if extend is not None:
                    _hash_row(extend, row)
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
            if extend is not None:
                self._main, self._main_id = extend.copy(), self.last_id
                self.prefix = self._with_prior(extend, ledger, now)
            else:
                self.prefix = self.fingerprint(ledger, now)
        self._mark_guarded(ledger)
        self.checked_at = now


_GUARDED_TAIL_SQL = (
    "SELECT artifact_id,kind,project_id,message_id,model,meta,created_at FROM {source} "
    "WHERE artifact_id BETWEEN ? AND ? AND kind IN ('cloud_usage_reserve','cloud_usage_result') "
    "AND created_at>=? ORDER BY artifact_id")


def _guard_mutations(db):
    """The accounting guard's mutation count while its exact index and triggers are installed, else None."""
    import ledger_schema
    state = ledger_schema.cloud_usage_guard_state(db)
    return None if state is None else state[1]


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
                    or tally.carried != carryover_marker(ledger.db)
                    or (tally.prefix is not None and not tally.unchanged(ledger, now))):
                tally = _Tally(start, ledger.db)
                setattr(ledger, attribute, tally)
            tally.update(ledger, now)
    except BaseException:
        setattr(ledger, attribute, None)
        raise
    if before is not None:
        if before == _tally_precheck(ledger.db, start) and not tally.invalid:
            tally.precheck = before
        # Otherwise a concurrent write must not attach a fresh stamp to an older total. The
        # tally itself stays: unstamped, its next use re-verifies the hashed rows in a new
        # snapshot (through the write-time guard when installed) before extending it.
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
    module = provider_registry.PROVIDER_MODULES[settings["provider"]]
    if (not profile_contract_valid(settings["provider"], model, effort, profile, selection["max_tokens"])
            or profile is None and not module.original_selection_valid(
                model, selection["max_tokens"], default_model=PROVIDERS[settings["provider"]],
                purpose_limits=PROFILE_MAX_TOKENS)):
        raise AnthropicError("contract_error", "cloud_profile_invalid")
    return model


def budget_block_reason(ledger, cfg, *, max_tokens=ORIGINAL_MAX_TOKENS, input_bound=INPUT_RESERVATION_MARGIN, selection=None):
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
        now = time.time()
        tally = _tally(ledger, now)
    except (AnthropicError, ValueError, InvalidOperation):
        return "pricing_unverified"
    if tally.invalid:
        return "pricing_unverified"
    day = _periods(now)[0]
    today = tally.days.get(day, [0, Decimal(0)])
    if c["daily_request_budget"] is not None and today[0] >= c["daily_request_budget"]:
        return "daily_cap"
    cost = max_cost(max_tokens, input_bound=input_bound, model=selected_model)
    if c["daily_cost_limit_usd"] is not None and today[1] + cost > _decimal(c["daily_cost_limit_usd"]):
        return "daily_cost_cap"
    limit = c["monthly_cost_limit_usd"]
    if limit is not None and tally.total + cost > _decimal(limit):
        return "monthly_cap"
    provider = _price_definition(selected_model)["provider"]
    caps = c["provider_budgets"].get(provider)
    if c["provider_budgets"] and caps is None:
        return "provider_budget_missing"
    if caps is not None:
        if tally.provider_days.get((provider, day), Decimal(0)) + cost > _decimal(caps["daily_cost_limit_usd"]):
            return "provider_daily_cap"
        if tally.provider_totals.get(provider, Decimal(0)) + cost > _decimal(caps["monthly_cost_limit_usd"]):
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


RESTORE_PAUSE = "database_restored"


def pause_for_restore(db, restored_at, cfg, now=None) -> bool:
    """Stop new cloud sends once for one database restore, inside the caller's transaction.

    The replaced database lost every reservation, result and saved answer made
    after its backup, and the repeat-request guard and the spending caps read
    nothing else: a paid request could be sent and billed again. Resuming is
    the existing human-confirmed operation. Only an installation with no
    reservation, no cloud block and no provider key has nothing to protect."""
    if type(restored_at) not in (int, float) or not math.isfinite(restored_at):
        raise ValueError("restore_time_invalid")
    restore = repr(float(restored_at))      # text: the lookup must not depend on float round-trips
    if db.execute("SELECT 1 FROM artifacts WHERE kind='cloud_control' AND CASE WHEN json_valid(meta) "
                  "THEN json_extract(meta,'$.restore') END=? LIMIT 1", (restore,)).fetchone():
        return False       # already paused for this restore; a later human resume stands
    if (isinstance(cfg, dict) and "cloud_llm" not in cfg and resolve_settings(cfg)[0]["mode"] == "off"
            and not db.execute("SELECT 1 FROM artifacts WHERE kind=? LIMIT 1", (KINDS[0],)).fetchone()):
        return False
    db.execute("INSERT INTO artifacts(kind,content,model,meta,created_at) VALUES(?,?,?,?,?)",
               ("cloud_control", "paused", "",
                json.dumps({"reason": RESTORE_PAUSE, "restore": restore}),
                time.time() if now is None else now))
    return True


def restore_paused(db) -> bool:
    """Whether the standing stop is the automatic one a database restore left."""
    row = db.execute("SELECT content,meta FROM artifacts WHERE kind='cloud_control' "
                     "AND project_id IS NULL AND message_id IS NULL "
                     "ORDER BY artifact_id DESC LIMIT 1").fetchone()
    if row is None or row[0] != "paused":
        return False
    try:
        meta = strict_json(row[1])
    except (ValueError, TypeError, RecursionError):
        return False
    return isinstance(meta, dict) and meta.get("reason") == RESTORE_PAUSE


def resume(ledger):
    _circuit_result(ledger, None, reset=True)
    ledger.artifact_add("cloud_control", "running")


def _request_identity(c, body, *, route, project_id, unit_key, message_id, owner,
                      selection, input_binding):
    # A reservation and a pre-send history check must identify the same input.
    if not isinstance(body, dict):
        raise AnthropicError("contract_error", "model_not_allowed")
    # Unresolved settings ("auto") reserve under the default provider's request shape.
    module = provider_registry.PROVIDER_MODULES.get(c["provider"],
                                                    provider_registry.PROVIDER_MODULES[provider_registry.DEFAULT_PROVIDER])
    body_tokens = body.get(module.MAX_TOKENS_FIELD)
    model = _selection_model(c, selection, body_tokens)
    if body.get("model") != model:
        raise AnthropicError("contract_error", "model_not_allowed")
    refusal = module.request_contract_error(body, selection)
    if refusal is not None:
        raise AnthropicError("contract_error", refusal)
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
    started = time.time()
    c = _cfg(cfg)
    candidate = _request_identity(c, body, route=route, project_id=project_id, unit_key=unit_key,
        message_id=message_id, owner=owner, selection=selection, input_binding=input_binding)
    try:
        with _accounting_snapshot(ledger.db):
            _guard_repeated_request(ledger, candidate, current_body=body)
            # An orphan result cannot prove this is a fresh request. Validate the
            # existing accounting snapshot even when a circuit would short-circuit caps.
            if _tally(ledger).invalid:
                raise _classified(AnthropicError("pricing_unverified", "cloud_unit_unresolved"),
                                  "receipt_inconsistent")
            return (block_reason(ledger, c, route, project_id, model=candidate['model'], owner=owner)
                    or setting_block_reason(c)
                    or budget_block_reason(ledger, c, max_tokens=candidate['max_tokens'],
                                           input_bound=input_bound(body), selection=selection))
    except AnthropicError as error:
        if error.kind == 'pricing_unverified' and not ledger.db.in_transaction:
            try:
                _circuit_on_pricing_error(ledger, error, candidate['model'], started)
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
        # one clock read: the maximum and the recorded table must come from the same JST day
        now = time.time()
        try:
            maximum = max_cost(tokens, input_bound=bound, model=model, now=now)
        except AnthropicError as error:
            _circuit_on_pricing_error(ledger, error, model, now)
            raise
        meta.update(reservation_id=uuid.uuid4().hex,
                    price_version=_price_definition(model, now=now)["price_version"],
                    max_cost_usd=str(maximum), input_bound=bound,
                    input_chars=sum(len(text) for text in _texts(body)), timeout_seconds=float(timeout))

        def check():
            _guard_repeated_request(ledger, meta, current_body=body)
            reason = block_reason(ledger, c, route, project_id, model=model, owner=owner)
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
            _circuit_on_pricing_error(ledger, error, model, now)
            raise
    return reserve


def _never_sent(result, settled):
    return settled and result.get('http_class') == 'not_sent'


def _confirmed_rejection(result, settled, model):
    # A known rejection permits another attempt while retaining its reserved cost.
    return (_never_sent(result, settled)
            or result.get('http_class') == 'http_429' and result.get('error_kind') == 'rate_limited'
            and result.get('usage') is None or _confirmed_openai_quota(result, model)
            or _confirmed_anthropic_payment(result, model))


def _saved_answer(answer, metadata, reservation):
    if (not isinstance(answer, dict) or not isinstance(answer.get('text'), str)
            or answer.get('finish_reason') not in ('stop', 'length')):
        raise ValueError('cloud_answer_invalid')
    encoded = answer['text'].encode('utf-8')
    if (len(encoded) > cloud_client.response_limit(reservation["max_tokens"])
            or metadata.get('answer_text_sha256_v1') != hashlib.sha256(encoded).hexdigest()
            or metadata.get('answer_finish_reason_v1') != answer['finish_reason']):
        raise ValueError('cloud_answer_invalid')
    return {key: answer[key] for key in ('text', 'finish_reason')}


def recover_response(ledger, *, route, project_id, message_id, unit_key, input_binding, request_fp,
                     logical_request_fp=None, model=None, effort=None,
                     owner=None, scope=None, observation=None):
    """Read one exact paid answer; ambiguous, discarded or answerless attempts stay closed."""
    with _accounting_snapshot(ledger.db):
        rows = ledger.db.execute("SELECT artifact_id,kind,project_id,message_id,model,meta,created_at "
            "FROM artifacts WHERE kind=? AND project_id=? AND message_id IS ? AND "
            "CASE WHEN json_valid(meta) THEN json_extract(meta,'$.route')=? AND "
            "json_extract(meta,'$.unit_key')=? AND json_extract(meta,'$.input_binding')=? AND "
            "json_extract(meta,'$.request_fp')=? END ORDER BY artifact_id",
            (KINDS[0], project_id, message_id, route, unit_key, input_binding, request_fp)).fetchall()
        if not rows and logical_request_fp is not None:
            # An output-bound change rewrites the envelope fingerprint while the
            # prompt itself stays identical. A settled answer for the same prompt,
            # model and effort is reused; the send guard still refuses the resend.
            rows = ledger.db.execute("SELECT artifact_id,kind,project_id,message_id,model,meta,created_at "
                "FROM artifacts WHERE kind=? AND project_id=? AND message_id IS ? AND "
                "CASE WHEN json_valid(meta) THEN json_extract(meta,'$.route')=? AND "
                "json_extract(meta,'$.unit_key')=? AND json_extract(meta,'$.input_binding')=? AND "
                "json_extract(meta,'$.logical_request_fp')=? AND json_extract(meta,'$.model')=? AND "
                "json_extract(meta,'$.effort') IS ? END ORDER BY artifact_id",
                (KINDS[0], project_id, message_id, route, unit_key, input_binding,
                 logical_request_fp, model, effort)).fetchall()
        if route == 'reextract' and message_id is None and owner is not None and valid_hash(scope):
            from attachment_cloud_identity import recovery_rows
            rows.extend(recovery_rows(ledger.db, project_id=project_id, unit_key=unit_key,
                binding=input_binding, request_fp=request_fp, owner=owner, scope=scope))
        observed = observation if route == 'reextract' and message_id is not None else None
        _observe_recovery(observed, 'unknown' if rows else 'no_exact_rows')
        saved = None
        settled_failure = False
        for row in rows:
            reservation = _receipt_meta(row['meta'])
            maximum = _reserve_valid(reservation, row)
            results = _matching_bound_rows(ledger, KINDS[1], reservation['reservation_id'])
            dispositions = _matching_bound_rows(ledger, 'cloud_usage_disposition',
                                                reservation['reservation_id'])
            # Reasons surfaced below are proven by checks recover_response
            # already performs; a bare ValueError from the shared settlement
            # proof stays an ambiguous receipt, never permission to resend.
            ended = ('cloud_result_superseded' if dispositions and all(
                         _accounting_meta(item['meta']).get('disposition') == SUPERSEDED_LOCAL
                         for item in dispositions) else 'cloud_result_discarded')
            if not results:
                raise ValueError(ended if dispositions else 'cloud_result_missing')
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
                raise ValueError(ended)
            result_meta = matches[0][0]
            if not settled or result_meta.get('disposition'):
                raise ValueError('cloud_result_unsettled')
            if (result_meta.get('http_class') != 'http_200'
                    or result_meta.get('error_kind')
                    or _validation_failures(ledger, reservation['reservation_id'], reservation)):
                # A settled failure is fully accounted and answered nothing: like a
                # confirmed rejection it holds no answer to reuse. The reservation
                # guard alone decides whether a new billed attempt is allowed.
                settled_failure = True
                continue
            evidence = reservation_evidence(ledger.db, row['artifact_id'])
            raw = ledger.db.execute("SELECT CASE WHEN typeof(content)='text' AND length(content)<=? "
                                   "THEN content END FROM artifacts WHERE artifact_id=?",
                                   (6 * cloud_client.response_limit(reservation["max_tokens"]) + 128, results[0]['artifact_id'])).fetchone()[0]
            answer = strict_json(raw)
            if not isinstance(answer, dict) or set(answer) != {'text', 'finish_reason'} or saved is not None:
                raise ValueError('cloud_answer_unresolved')
            answer = _saved_answer(answer, evidence['result'], reservation)
            saved = (evidence, {'status': 200, **answer, 'model': reservation['model'],
                                'usage': evidence['result']['usage']})
        if rows and saved is None:
            _observe_recovery(observed, 'settled_failure_only' if settled_failure
                              else 'confirmed_rejection_only')
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
    if (http_class == "http_200" and metadata.get("usage") is None
            and error_kind != cloud_client.RESPONSE_TOO_LARGE):
        # An oversized response is the reader's own bound, not a failed price check: it is
        # charged at the reserved maximum and stays unsettled without stopping other sends.
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
        if "anthropic_error_code" in metadata and not _confirmed_anthropic_spend_limit(metadata, model):
            raise AnthropicError("pricing_unverified", "result_mismatch")
        if "anthropic_400_diagnostic" in metadata and not _confirmed_anthropic_usage_limit(metadata, model):
            raise AnthropicError("pricing_unverified", "result_mismatch")
        usage = definition["validate_usage"](metadata["usage"]) if metadata.get("usage") is not None else None
        evidence_error = result_evidence_error(metadata)
        if evidence_error is not None:
            raise AnthropicError("pricing_unverified", evidence_error)
        if "contract_error_detail" in metadata:
            error.detail = metadata["contract_error_detail"]
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
        if "anthropic_error_code" in metadata:
            result["anthropic_error_code"] = metadata["anthropic_error_code"]
        if "anthropic_400_diagnostic" in metadata:
            result["anthropic_400_diagnostic"] = metadata["anthropic_400_diagnostic"]
        if "contract_error_detail" in metadata:
            result["contract_error_detail"] = metadata["contract_error_detail"]
        if "answer_text_sha256_v1" in metadata:
            result["answer_text_sha256_v1"] = metadata["answer_text_sha256_v1"]
        if "answer_finish_reason_v1" in metadata:
            result["answer_finish_reason_v1"] = metadata["answer_finish_reason_v1"]
        if "disposition" in metadata:
            if metadata["disposition"] not in ("discarded_scope", "discarded_workday"):
                raise AnthropicError("pricing_unverified", "disposition_invalid")
            result["disposition"] = metadata["disposition"]
        content = (json.dumps(_saved_answer(answer, result, reservation), ensure_ascii=False)
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


def _superseded_disposition(ledger, reservation):
    """True when the reservation's one disposition row is a local replacement; never raises."""
    try:
        rows = _matching_bound_rows(ledger, "cloud_usage_disposition", reservation["reservation_id"])
        return len(rows) == 1 and _loads(rows[0]["meta"]).get("disposition") == SUPERSEDED_LOCAL
    except Exception:
        return False


def record_disposition_tx(ledger, reservation, disposition):
    """Record a discarded or locally replaced unit in its owner's transaction without changing billing.

    The first disposition wins: discarding a reservation already replaced by local work
    writes nothing and returns the existing row. A replacement by local work finds its
    reservation at any age, as the guard that chose it does (maintenance keeps receipts a
    current patient context cites past the usage window); a discard needs a retained one."""
    if not ledger.db.in_transaction:
        raise RuntimeError("cloud_disposition_transaction_required")
    if disposition not in _ENDED_DISPOSITIONS:
        raise ValueError("cloud_disposition_invalid")
    if disposition == SUPERSEDED_LOCAL or _superseded_disposition(ledger, reservation):
        _stored_reservation(ledger, reservation, bound_only=True, retained=False)
        existing = _matching_bound_rows(ledger, "cloud_usage_disposition", reservation["reservation_id"])
    else:
        _stored_reservation(ledger, reservation)
        existing = _matching_rows(ledger, "cloud_usage_disposition", reservation["reservation_id"])
    meta = _receipt_binding(reservation)
    meta["disposition"] = disposition
    if existing:
        if (len(existing) == 1 and disposition != SUPERSEDED_LOCAL
                and _loads(existing[0]["meta"]) == {**meta, "disposition": SUPERSEDED_LOCAL}):
            meta["disposition"] = SUPERSEDED_LOCAL
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
