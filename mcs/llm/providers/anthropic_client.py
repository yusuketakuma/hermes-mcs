"""Call the fixed Anthropic Messages endpoint through bounded stdin workers."""
from __future__ import annotations

import time  # noqa: F401  retained for existing integrations that patch the shared clock
from decimal import Decimal
from types import MappingProxyType

import bounded_http
from cloud_client import CloudClient, CloudError, MAX_TOKENS, SYSTEM  # noqa: F401
from mcs_util import payload_hash

AnthropicError = CloudError
MODEL = "claude-haiku-5-5"
ENDPOINT = bounded_http.ANTHROPIC_ENDPOINT
KEY_ENV = "MCS_ANTHROPIC_API_KEY"
PRICE_VERSION = "anthropic-haiku-5-5-2026-11-15"
PRICE_CHECKED_ON = "2026-11-15"
PRICE_VALID_UNTIL = "2026-12-15"
PRICE_SOURCES = (
    "https://platform.claude.com/docs/en/models/haiku-5-5/overview",
    "https://platform.claude.com/docs/en/models/overview",
    "https://platform.claude.com/docs/en/about-claude/pricing",
)
LONG_INPUT_THRESHOLD = 100000
PRICES_20261026 = MappingProxyType({"input": "0.1", "output": "0.5", "long_input": "0.5",
    "long_output": "2.5", "cache_write_5m_multiplier": "1.25",
    "cache_write_1h_multiplier": "2", "cache_read_multiplier": "0.1"})
PRICES = PRICES_20261026


# Usage metadata known not to change the price at its listed values; any other
# field or value (a priority tier, a regional inference multiplier, a new
# counter) may be billed differently and stops the route as pricing_unverified.
_UNPRICED_USAGE_20261026 = MappingProxyType({"service_tier": frozenset({"standard"}),
    "inference_geo": frozenset({"global", "not_available"}), "speed": frozenset({"standard"})})


def _unpriced_20261026(key, value):
    if key in _UNPRICED_USAGE_20261026:
        return isinstance(value, str) and value in _UNPRICED_USAGE_20261026[key]
    if key == "server_tool_use":    # tools are never requested: every count must be zero
        return isinstance(value, dict) and all(type(v) is int and v == 0 for v in value.values())
    if key == "output_tokens_details":    # a breakdown of output_tokens, priced there
        return isinstance(value, dict) and all(type(v) is int and v >= 0 for v in value.values())
    return False


def _validate_usage_20261026(value):
    """Reject unpriced or malformed usage rather than charging zero."""
    allowed = {"input_tokens", "output_tokens", "cache_creation_input_tokens",
               "cache_read_input_tokens", "cache_creation"}
    if not isinstance(value, dict) or not all(_unpriced_20261026(k, value[k]) for k in value.keys() - allowed):
        raise AnthropicError("pricing_unverified", "usage_unknown")
    out = {}
    for key in allowed - {"cache_creation"}:
        v = value.get(key, 0 if key.startswith("cache_") else None)
        if type(v) is not int or not 0 <= v <= 2**53:
            raise AnthropicError("pricing_unverified", "usage_invalid")
        out[key] = v
    cache = value.get("cache_creation")
    if cache is not None:
        if (not isinstance(cache, dict)
                or cache.keys() - {"ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens"}):
            raise AnthropicError("pricing_unverified", "cache_usage_invalid")
        cache = dict(cache)
        for key in ("ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens"):
            v = cache.setdefault(key, 0)
            if type(v) is not int or not 0 <= v <= 2**53:
                raise AnthropicError("pricing_unverified", "cache_usage_invalid")
        if sum(cache.values()) != out["cache_creation_input_tokens"]:
            raise AnthropicError("pricing_unverified", "cache_usage_mismatch")
        out["cache_creation"] = cache
    elif out["cache_creation_input_tokens"]:
        # Unknown TTL is priced at the higher one-hour write rate.
        out["cache_creation"] = {"ephemeral_5m_input_tokens": 0,
                                  "ephemeral_1h_input_tokens": out["cache_creation_input_tokens"]}
    return out


validate_usage = _validate_usage_20261026


SONNET_MODEL = "claude-sonnet-5-5"
SONNET_PRICE_VERSION = "anthropic-sonnet-5-5-2026-11-15"
# Sonnet 5.5 has one price tier (no long-input step): its long_* rates equal the
# base rates and its threshold is the 1M input bound, so no request is "long".
SONNET_PRICES_20261115 = MappingProxyType({"input": "2", "output": "10", "long_input": "2",
    "long_output": "10", "cache_write_5m_multiplier": "1.25",
    "cache_write_1h_multiplier": "2", "cache_read_multiplier": "0.05"})
CURRENT_PRICE_VERSIONS = MappingProxyType({MODEL: PRICE_VERSION, SONNET_MODEL: SONNET_PRICE_VERSION})
# Opt-in purpose profiles. A selection without a profile is the original
# Haiku contract: effort None on the record, "low" on the wire.
PROFILE_DEFINITIONS = MappingProxyType({
    "haiku-medium": (MODEL, "medium"),
    "sonnet-high": (SONNET_MODEL, "high"),
})
PROFILE_PURPOSES = frozenset({"backlog", "patient_context", "quality_repair"})
DEFAULT_PROFILES = MappingProxyType({"backlog": "haiku-medium", "patient_context": "sonnet-high",
                                     "quality_repair": "sonnet-high"})
ORIGINAL_CONTRACT = (MODEL, None)
VALID_MODEL_EFFORTS = frozenset({ORIGINAL_CONTRACT, *PROFILE_DEFINITIONS.values()})


def _haiku_snapshot(price_version, checked_on, valid_until):
    return MappingProxyType({
        "provider": "anthropic", "model": MODEL, "price_version": price_version,
        "price_checked_on": checked_on, "price_valid_until": valid_until, "price_sources": PRICE_SOURCES,
        "prices": PRICES_20261026, "long_input_threshold": LONG_INPUT_THRESHOLD,
        "validate_usage": _validate_usage_20261026, "max_tokens": 128000, "legacy_reservation": True,
    })


# A recheck with unchanged prices adds a version; older versions stay so stored
# reservations keep resolving to the definition they were priced with.
PRICE_SNAPSHOTS = MappingProxyType({
    (MODEL, "anthropic-haiku-5-5-2026-10-26"): _haiku_snapshot(
        "anthropic-haiku-5-5-2026-10-26", "2026-10-26", "2026-11-25"),
    (MODEL, PRICE_VERSION): _haiku_snapshot(PRICE_VERSION, PRICE_CHECKED_ON, PRICE_VALID_UNTIL),
    (SONNET_MODEL, SONNET_PRICE_VERSION): MappingProxyType({
        "provider": "anthropic", "model": SONNET_MODEL, "price_version": SONNET_PRICE_VERSION,
        "price_checked_on": "2026-11-15", "price_valid_until": "2026-12-15",
        "price_sources": ("https://platform.claude.com/docs/en/models/sonnet-5-5/overview",
                          "https://platform.claude.com/docs/en/about-claude/pricing"),
        "prices": SONNET_PRICES_20261115, "long_input_threshold": 1048576,
        "validate_usage": _validate_usage_20261026, "max_tokens": 128000, "legacy_reservation": False,
    }),
})


def provider_contract(model, effort):
    """Reuse key of one finite request shape. The original contract (effort None)
    hashes its wire effort "low", so its key is unchanged."""
    return {"backend": "anthropic", "model": model,
            "prompt": payload_hash({"system": SYSTEM, "effort": effort or "low"})[:16]}


def reservation_cost(definition, input_bound, max_tokens, *, legacy=False):
    """Worst-case cost of one request: the long-input rate, at the highest cache-write multiplier."""
    prices = definition["prices"]
    rate = Decimal(prices["long_input"]) * (1 if legacy else max(
        Decimal(1), Decimal(prices["cache_write_5m_multiplier"]), Decimal(prices["cache_write_1h_multiplier"])))
    return (Decimal(input_bound) * rate + Decimal(max_tokens) * Decimal(prices["long_output"])) / Decimal(1000000)


def usage_cost(definition, u, *, input_limit):
    """Price validated usage; the long tier is chosen by the total input including cache."""
    prices = definition["prices"]
    total_input = u["input_tokens"] + u["cache_creation_input_tokens"] + u["cache_read_input_tokens"]
    if total_input > input_limit or u["output_tokens"] > definition["max_tokens"]:
        raise AnthropicError("pricing_unverified", "usage_bounds")
    long = total_input > definition["long_input_threshold"]
    input_price, output_price = (Decimal(prices["long_input"]), Decimal(prices["long_output"])) if long else (Decimal(prices["input"]), Decimal(prices["output"]))
    cache = u.get("cache_creation", {})
    cost = (u["input_tokens"] * input_price + u["output_tokens"] * output_price
            + cache.get("ephemeral_5m_input_tokens", 0) * input_price * Decimal(prices["cache_write_5m_multiplier"])
            + cache.get("ephemeral_1h_input_tokens", 0) * input_price * Decimal(prices["cache_write_1h_multiplier"])
            + u["cache_read_input_tokens"] * input_price * Decimal(prices["cache_read_multiplier"]))
    return cost / Decimal(1000000)


class AnthropicClient(CloudClient):
    """Send Anthropic Messages through the shared guarded attempt loop."""
    MODEL = MODEL
    # Class defaults keep the original contract for subclasses that replace __init__.
    model = MODEL
    effort = None

    def __init__(self, api_key, *, model=MODEL, effort=None, **kwargs):
        if not isinstance(model, str) or effort is not None and not isinstance(effort, str) \
                or (model, effort) not in VALID_MODEL_EFFORTS:
            raise ValueError("anthropic_profile_invalid")
        super().__init__(api_key, **kwargs)
        self.model = model
        self.effort = effort

    def request(self, method, body, timeout, *, deadline=None):
        return bounded_http.bounded_http_request(
            ENDPOINT, method, body, timeout, api_key=self.api_key,
            deadline=deadline, allowed_endpoints=(ENDPOINT,), auth="anthropic")

    def body(self, prompt, max_tokens, cache_prefix):
        content = prompt
        if cache_prefix:
            if not isinstance(cache_prefix, str) or not prompt.startswith(cache_prefix):
                raise ValueError("cache_prefix_invalid")
            content = [{"type": "text", "text": cache_prefix, "cache_control": {"type": "ephemeral"}}]
            if prompt[len(cache_prefix):]:
                content.append({"type": "text", "text": prompt[len(cache_prefix):]})
        # The original contract (effort None) keeps its exact "low" wire body.
        return {"model": self.model, "max_tokens": max_tokens, "system": SYSTEM,
                "output_config": {"effort": self.effort or "low"},
                "messages": [{"role": "user", "content": content}]}

    def answer(self, obj, meta):
        if not isinstance(obj, dict) or obj.get("model") != self.model:
            raise AnthropicError("model_mismatch")
        meta["usage"] = validate_usage(obj.get("usage"))
        content = obj.get("content")
        if not isinstance(content, list) or any(not isinstance(b, dict) for b in content):
            raise AnthropicError("protocol_error", "content_invalid")
        text = []
        for block in content:
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                text.append(block["text"])
            elif (block.get("type") == "thinking" and isinstance(block.get("thinking"), str)
                  and (block["thinking"].strip() or block["thinking"] == ""
                       and isinstance(block.get("signature"), str) and block["signature"])):
                pass
            elif (block.get("type") == "redacted_thinking" and isinstance(block.get("data"), str)
                  and block["data"].strip()):
                pass
            else:
                raise AnthropicError("protocol_error", "content_invalid")
        stop = obj.get("stop_reason")
        if stop == "refusal":
            # A 200 refusal may be billed: its validated usage is already in meta.
            # It is never an answer and is not retried or sent elsewhere.
            raise AnthropicError("contract_error", "refusal")
        answer = "".join(text)
        if (stop not in ("end_turn", "max_tokens") or not content
                or stop == "end_turn" and not answer.strip()):
            raise AnthropicError("contract_error", "incomplete_response")
        # a validated answer stays usable past the HTTP deadline; the caller's commit
        # guard decides. An answer cut at max_tokens is a paid "length" stop the
        # caller handles as truncation, not a failure the breaker counts.
        return {"status": 200, "text": answer,
                  "finish_reason": "length" if stop == "max_tokens" else "stop",
                  "model": self.model, "usage": meta["usage"]}


def client_class():
    """This provider's client, read at call time so a replaced AnthropicClient is used."""
    return AnthropicClient
