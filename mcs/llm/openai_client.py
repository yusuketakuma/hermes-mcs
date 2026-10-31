"""Call the pinned OpenAI Responses model with the shared guarded attempt lifecycle."""
from __future__ import annotations

from types import MappingProxyType

import bounded_http
from cloud_client import CloudClient, CloudError, MAX_TOKENS, SYSTEM  # noqa: F401
from mcs_util import strict_json

AnthropicError = CloudError
MODEL = "gpt-6-luna"
SOL_MODEL = "gpt-6.1-sol"
ENDPOINT = "https://api.openai.com/v1/responses"
KEY_ENV = "MCS_OPENAI_API_KEY"
PRICE_VERSION = "openai-gpt-6-luna-2026-10-29"
PRICE_CHECKED_ON = "2026-10-29"
PRICE_VALID_UNTIL = "2026-11-28"
PRICE_SOURCES = ("https://developers.openai.com/api/docs/models/gpt-6-luna",)
LONG_INPUT_THRESHOLD = 272000
PRICES_20261029 = MappingProxyType({"input": "0.1", "output": "0.5", "cached_input": "0.01",
    "cache_write_30m": "0.125", "long_input_multiplier": "2", "long_output": "0.75",
    "reservation_input": "0.25", "reservation_output": "0.75"})
PRICES = PRICES_20261029
SOL_PRICE_VERSION = "openai-gpt-6.1-sol-2026-10-31"
SOL_PRICES_20261031 = MappingProxyType({"input": "2", "output": "10", "cached_input": "0.10",
    "cache_write_30m": "2.50", "long_input_multiplier": "2", "long_output": "15",
    "reservation_input": "5", "reservation_output": "15"})
CURRENT_PRICE_VERSIONS = MappingProxyType({MODEL: PRICE_VERSION, SOL_MODEL: SOL_PRICE_VERSION})
PROFILE_DEFINITIONS = MappingProxyType({
    "luna-medium": (MODEL, "medium"),
    "sol-medium": (SOL_MODEL, "medium"),
    "sol-high": (SOL_MODEL, "high"),
})
PROFILE_PURPOSES = frozenset({"backlog", "patient_context", "quality_repair"})
DEFAULT_PROFILES = MappingProxyType({"backlog": "luna-medium", "patient_context": "sol-medium",
                                     "quality_repair": "sol-high"})
VALID_MODEL_EFFORTS = frozenset({(MODEL, "low"), *PROFILE_DEFINITIONS.values()})
QUOTA_ERROR_CODES = frozenset({"insufficient_quota", "credit_balance_exhausted",
                               "organization_spend_limit_exceeded", "project_spend_limit_exceeded",
                               "organization_usage_limit_exceeded"})
RATE_LIMIT_DIAGNOSTICS = frozenset({"rate_limit_exceeded", "slow_down", "rate_limit_error",
    "missing_code", "unknown_code", "unknown_type", "conflicting_fields", "invalid_field_type",
    "body_not_bytes", "body_too_large", "body_invalid_utf8", "body_invalid_json",
    "body_duplicate_keys", "body_nonfinite", "body_too_deep", "body_not_object",
    "error_missing", "error_not_object"})


def _error_429(raw):
    if not isinstance(raw, bytes):
        return None, "body_not_bytes"
    if len(raw) > bounded_http.MAX_RESPONSE_BYTES:
        return None, "body_too_large"
    try:
        obj = strict_json(raw.decode("utf-8"))
    except UnicodeError:
        return None, "body_invalid_utf8"
    except RecursionError:
        return None, "body_too_deep"
    except OverflowError:
        return None, "body_nonfinite"
    except (ValueError, TypeError) as error:
        reason = {"duplicate_json_key": "body_duplicate_keys", "nonfinite_json_number": "body_nonfinite"}
        return None, reason.get(str(error), "body_invalid_json")
    if not isinstance(obj, dict):
        return None, "body_not_object"
    if "error" not in obj:
        return None, "error_missing"
    if not isinstance(obj["error"], dict):
        return None, "error_not_object"
    return obj["error"], None


def _validate_usage_20261029(value):
    """Validate every billable counter, including automatic prompt-cache writes."""
    allowed = {"input_tokens", "input_tokens_details", "output_tokens", "output_tokens_details", "total_tokens"}
    if not isinstance(value, dict) or value.keys() - allowed:
        raise AnthropicError("pricing_unverified", "usage_unknown")
    out = dict(value)
    for key in ("input_tokens", "output_tokens", "total_tokens"):
        if type(out.get(key)) is not int or not 0 <= out[key] <= 2**53:
            raise AnthropicError("pricing_unverified", "usage_invalid")
    if out["input_tokens"] + out["output_tokens"] != out["total_tokens"]:
        raise AnthropicError("pricing_unverified", "usage_mismatch")
    for key, fields in (("input_tokens_details", {"cached_tokens", "cache_write_tokens"}),
                        ("output_tokens_details", {"reasoning_tokens"})):
        details = out.get(key, {})
        allowed_details = fields | ({"cache_write_tokens_details"} if key == "input_tokens_details" else set())
        if (not isinstance(details, dict) or details.keys() - allowed_details
                or key == "input_tokens_details" and fields - details.keys()
                or any(type(details.get(field, 0)) is not int or not 0 <= details.get(field, 0) <= 2**53
                       for field in fields)):
            raise AnthropicError("pricing_unverified", "usage_details_invalid")
        normalized = {field: details.get(field, 0) for field in fields}
        if sum(normalized.values()) > out[key.removesuffix("_details")]:
            raise AnthropicError("pricing_unverified", "usage_details_mismatch")
        if "cache_write_tokens_details" in details:
            writes = details["cache_write_tokens_details"]
            write_fields = {"ttl_30m_tokens", "ttl_12h_tokens"}
            if (not isinstance(writes, dict) or writes.keys() != write_fields
                    or any(type(n) is not int or not 0 <= n <= 2**53 for n in writes.values())
                    or sum(writes.values()) != normalized["cache_write_tokens"]
                    or writes["ttl_12h_tokens"] != 0):
                raise AnthropicError("pricing_unverified", "cache_write_details_invalid")
            normalized["cache_write_tokens_details"] = dict(writes)
        out[key] = normalized
    return out


validate_usage = _validate_usage_20261029


PRICE_SNAPSHOTS = MappingProxyType({
    ("gpt-6-luna", "openai-gpt-6-luna-2026-10-29"): MappingProxyType({
        "provider": "openai", "model": "gpt-6-luna",
        "price_version": "openai-gpt-6-luna-2026-10-29",
        "price_checked_on": "2026-10-29", "price_valid_until": "2026-11-28",
        "price_sources": ("https://developers.openai.com/api/docs/models/gpt-6-luna",),
        "prices": PRICES_20261029, "long_input_threshold": 272000,
        "validate_usage": _validate_usage_20261029, "max_tokens": 128000, "legacy_reservation": False,
    }),
    ("gpt-6.1-sol", "openai-gpt-6.1-sol-2026-10-31"): MappingProxyType({
        "provider": "openai", "model": "gpt-6.1-sol",
        "price_version": "openai-gpt-6.1-sol-2026-10-31",
        "price_checked_on": "2026-10-31", "price_valid_until": "2026-11-30",
        "price_sources": ("https://developers.openai.com/api/docs/models/gpt-6.1-sol",
                          "https://developers.openai.com/api/docs/pricing"),
        "prices": SOL_PRICES_20261031, "long_input_threshold": 272000,
        "validate_usage": _validate_usage_20261029, "max_tokens": 128000, "legacy_reservation": False,
    }),
})


class OpenAIClient(CloudClient):
    """Reuse bounded retries, reservations and uncertain-send handling for Responses."""
    MODEL = MODEL

    def __init__(self, api_key, *, model=MODEL, effort="low", **kwargs):
        if not isinstance(model, str) or not isinstance(effort, str) or (model, effort) not in VALID_MODEL_EFFORTS:
            raise ValueError("openai_profile_invalid")
        super().__init__(api_key, **kwargs)
        self.model = model
        self.effort = effort

    def request(self, method, body, timeout, *, deadline=None):
        return bounded_http.bounded_http_request(
            ENDPOINT, method, body, timeout, api_key=self.api_key,
            deadline=deadline, allowed_endpoints=(ENDPOINT,), auth="bearer")

    def response_error(self, status, raw):
        if status != 429:
            return super().response_error(status, raw)
        error, diagnostic = _error_429(raw)
        if diagnostic is None:
            code, kind = error.get("code"), error.get("type")
            if (code is not None and not isinstance(code, str)
                    or kind is not None and not isinstance(kind, str)):
                diagnostic = "invalid_field_type"
            elif kind is None or kind == "insufficient_quota":
                if code in QUOTA_ERROR_CODES:
                    return "payment_required", False, {"openai_error_code": code}
                if code is None and kind == "insufficient_quota":
                    return "payment_required", False, {"openai_error_code": "insufficient_quota"}
            if diagnostic is None:
                if code in QUOTA_ERROR_CODES or kind == "insufficient_quota":
                    diagnostic = "conflicting_fields"
                elif code in ("rate_limit_exceeded", "slow_down"):
                    diagnostic = code
                elif code is not None:
                    diagnostic = "unknown_code"
                elif kind in ("rate_limit_error", "slow_down"):
                    diagnostic = kind
                elif kind is not None:
                    diagnostic = "unknown_type"
                else:
                    diagnostic = "missing_code"
        kind, retryable, _ = super().response_error(status, raw)
        return kind, retryable, {"openai_429_diagnostic": diagnostic}

    def body(self, prompt, max_tokens, cache_prefix):
        if cache_prefix and (not isinstance(cache_prefix, str) or not prompt.startswith(cache_prefix)):
            raise ValueError("cache_prefix_invalid")
        return {"model": self.model, "instructions": SYSTEM, "input": prompt,
                "max_output_tokens": max_tokens, "reasoning": {"effort": self.effort},
                "store": False, "service_tier": "default",
                "prompt_cache_options": {"ttl": "30m"}, "tools": []}

    def answer(self, obj, meta):
        if not isinstance(obj, dict) or obj.get("model") != self.model:
            raise AnthropicError("model_mismatch")
        if obj.get("service_tier") != "default":
            raise AnthropicError("pricing_unverified", "service_tier_unknown")
        meta["usage"] = validate_usage(obj.get("usage"))
        output = obj.get("output")
        if not isinstance(output, list) or any(not isinstance(b, dict) for b in output):
            raise AnthropicError("protocol_error", "content_invalid")
        texts = []
        for item in output:
            if item.get("type") == "reasoning":
                continue
            if item.get("type") != "message" or item.get("role") != "assistant":
                raise AnthropicError("contract_error", "unexpected_output")
            content = item.get("content")
            if not isinstance(content, list):
                raise AnthropicError("protocol_error", "content_invalid")
            for part in content:
                if not isinstance(part, dict) or part.get("type") != "output_text" or not isinstance(part.get("text"), str):
                    raise AnthropicError("contract_error", "unexpected_output")
                texts.append(part["text"])
        status = obj.get("status")
        length = status == "incomplete" and obj.get("incomplete_details") == {"reason": "max_output_tokens"}
        answer = "".join(texts)
        reasoning_only = bool(output) and all(item.get("type") == "reasoning" for item in output)
        if ((status != "completed" and not length) or not output
                or not texts and not (length and reasoning_only)
                or status == "completed" and not answer.strip()):
            raise AnthropicError("contract_error", "incomplete_response")
        return {"status": 200, "text": answer, "finish_reason": "length" if length else "stop",
                "model": self.model, "usage": meta["usage"]}
