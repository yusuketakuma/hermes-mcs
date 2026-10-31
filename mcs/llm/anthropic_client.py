"""Call the fixed Anthropic Messages endpoint through bounded stdin workers."""
from __future__ import annotations

import time  # noqa: F401  retained for existing integrations that patch the shared clock
from types import MappingProxyType

import bounded_http
from cloud_client import CloudClient, CloudError, MAX_TOKENS, SYSTEM  # noqa: F401

AnthropicError = CloudError
MODEL = "claude-haiku-5-5"
ENDPOINT = bounded_http.ANTHROPIC_ENDPOINT
KEY_ENV = "MCS_ANTHROPIC_API_KEY"
PRICE_VERSION = "anthropic-haiku-5-5-2026-10-26"
PRICE_CHECKED_ON = "2026-10-26"
PRICE_VALID_UNTIL = "2026-11-25"
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


PRICE_SNAPSHOTS = MappingProxyType({
    ("claude-haiku-5-5", "anthropic-haiku-5-5-2026-10-26"): MappingProxyType({
        "provider": "anthropic", "model": "claude-haiku-5-5",
        "price_version": "anthropic-haiku-5-5-2026-10-26",
        "price_checked_on": "2026-10-26", "price_valid_until": "2026-11-25",
        "price_sources": ("https://platform.claude.com/docs/en/models/haiku-5-5/overview",
                          "https://platform.claude.com/docs/en/models/overview",
                          "https://platform.claude.com/docs/en/about-claude/pricing"),
        "prices": PRICES_20261026, "long_input_threshold": 100000,
        "validate_usage": _validate_usage_20261026, "max_tokens": 128000, "legacy_reservation": True,
    }),
})


class AnthropicClient(CloudClient):
    """Send Anthropic Messages through the shared guarded attempt loop."""
    MODEL = MODEL

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
        return {"model": MODEL, "max_tokens": max_tokens, "system": SYSTEM,
                "output_config": {"effort": "low"},
                "messages": [{"role": "user", "content": content}]}

    def answer(self, obj, meta):
        if not isinstance(obj, dict) or obj.get("model") != MODEL:
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
        answer = "".join(text)
        if (stop not in ("end_turn", "max_tokens") or not content
                or stop == "end_turn" and not answer.strip()):
            raise AnthropicError("contract_error", "incomplete_response")
        # a validated answer stays usable past the HTTP deadline; the caller's commit
        # guard decides. An answer cut at max_tokens is a paid "length" stop the
        # caller handles as truncation, not a failure the breaker counts.
        return {"status": 200, "text": answer,
                  "finish_reason": "length" if stop == "max_tokens" else "stop",
                  "model": MODEL, "usage": meta["usage"]}
