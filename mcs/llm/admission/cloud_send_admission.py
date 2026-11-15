"""Own the cloud-send refusal types and the spending-cap comparison shared by every send attempt."""
from __future__ import annotations

import mcs_util

CAP_KEYS = ("daily_request_budget", "daily_cost_limit_usd", "monthly_cost_limit_usd", "window_budget_usd")


class CloudFallback(Exception):
    """Discard this provider's unit and retry under the local contract."""


class CloudBusy(CloudFallback):
    """No shared cloud capacity was reserved or sent."""


class CloudObservationIncomplete(CloudFallback):
    """Defer adoption without discarding a received answer or starting a fallback."""


def caps(settings):
    """The spending limits one attempt is reserved under, comparable across settings reads."""
    out = {key: settings[key] for key in CAP_KEYS}
    # an earlier period start counts more spending, like a lower cap
    start = settings["window_start"]
    out["window_start"] = None if start is None else mcs_util.until_epoch(start)
    budgets = settings["provider_budgets"]
    provider_caps = budgets.get(settings["provider"], {})
    for key in ("daily_cost_limit_usd", "monthly_cost_limit_usd"):
        out["provider_" + key] = provider_caps.get(key, 0 if budgets else None)
    return out


def tightened(reserved, now):
    """True when a cap was set or lowered after the reservation: its unsent request must stop.

    The reservation itself stays counted at its maximum; only the send it has not made yet stops.
    """
    return any(reserved.get(key, now[key]) is None and now[key] is not None
               or None not in (reserved.get(key), now[key]) and now[key] < reserved[key]
               for key in now)
