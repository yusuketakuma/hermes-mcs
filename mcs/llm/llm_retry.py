"""Share bounded retry classification without changing provider attempt loops."""
import math


def retry_after(headers):
    try:
        value = next((v for k, v in headers.items() if str(k).lower() == "retry-after"), None)
        seconds = float(value)
        return seconds if math.isfinite(seconds) and seconds >= 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


def http_error(status):
    """Return a fixed error kind and retry flag, never provider response text."""
    if status in (401, 403):
        return "auth_error", False
    if status == 402:
        return "payment_required", False
    if status == 429:
        return "rate_limited", True
    if status >= 500:
        return "transport", True
    if 400 <= status < 500:
        return "contract_error", False
    return "protocol_error", False
