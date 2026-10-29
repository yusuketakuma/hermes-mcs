"""Call the pinned OpenAI Responses model with the shared guarded attempt lifecycle."""
from __future__ import annotations

import bounded_http
from anthropic_client import AnthropicClient, AnthropicError, SYSTEM

MODEL = "gpt-6-luna"
ENDPOINT = "https://api.openai.com/v1/responses"


def validate_usage(value):
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
        if (not isinstance(details, dict) or details.keys() - fields
                or key == "input_tokens_details" and fields - details.keys()
                or any(type(n) is not int or not 0 <= n <= 2**53 for n in details.values())):
            raise AnthropicError("pricing_unverified", "usage_details_invalid")
        details = {field: details.get(field, 0) for field in fields}
        if sum(details.values()) > out[key.removesuffix("_details")]:
            raise AnthropicError("pricing_unverified", "usage_details_mismatch")
        out[key] = details
    return out


class OpenAIClient(AnthropicClient):
    """Reuse bounded retries, reservations and uncertain-send handling for Responses."""
    def __init__(self, api_key, **kwargs):
        super().__init__(api_key, **kwargs)
        self.model = MODEL

    def request(self, method, body, timeout, *, deadline=None):
        return bounded_http.bounded_http_request(
            ENDPOINT, method, body, timeout, api_key=self.api_key,
            deadline=deadline, allowed_endpoints=(ENDPOINT,), auth="bearer")

    def body(self, prompt, max_tokens, cache_prefix):
        if cache_prefix and (not isinstance(cache_prefix, str) or not prompt.startswith(cache_prefix)):
            raise ValueError("cache_prefix_invalid")
        return {"model": MODEL, "instructions": SYSTEM, "input": prompt,
                "max_output_tokens": max_tokens, "reasoning": {"effort": "low"},
                "store": False, "service_tier": "default",
                "prompt_cache_options": {"ttl": "30m"}, "tools": []}

    def answer(self, obj, meta):
        if not isinstance(obj, dict) or obj.get("model") != MODEL:
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
        if (status != "completed" and not length) or not texts:
            raise AnthropicError("contract_error", "incomplete_response")
        return {"status": 200, "text": "".join(texts), "finish_reason": "length" if length else "stop",
                "model": MODEL, "usage": meta["usage"]}
