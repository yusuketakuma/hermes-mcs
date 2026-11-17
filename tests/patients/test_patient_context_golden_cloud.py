"""Golden acceptance for the cloud patient-context path: posts in, product and accounting out.

Only the HTTP boundary is synthetic (``AnthropicClient.request`` in
``real_cloud``); reservation, accounting, adoption and publication run for real.
The fixture clock starts on the first day the current price tables allow, so
times in the JSON are stored relative to that start (``<base+Ns>``, ``<base-day>``).
To accept an intended change of the final product, delete the JSON file and run
the test once: it writes the new product and fails until the diff is reviewed.
"""
from datetime import datetime
import json
from pathlib import Path

import pytest

from mcs_queries import JST
import notify_views
import patient_context_refresh_readiness as readiness
from anthropic_context_testkit import price_window
from test_context_cloud_update_identity import cloud, finish, real_cloud, runtime  # noqa: F401  (fixtures)
from test_patient_context_golden import _normalized
from test_patient_context_refresh import stamp

GOLDEN = Path(__file__).resolve().parents[1] / "fixtures" / "patient_context_golden" / "cloud_one_post.json"


def _accounting(ledger):
    """Content-free accounting shape: kinds, routes, models and settled request count."""
    rows = []
    for kind in ("cloud_usage_reserve", "cloud_usage_result"):
        for row in ledger.artifacts(kind):
            meta = json.loads(row["meta"])
            rows.append((kind, meta.get("route"), row["model"], meta.get("http_class")))
    return sorted(rows, key=lambda item: tuple(str(part) for part in item))


def _rebased(value, base):
    """Times relative to the fixture start, so a price-table rotation does not move the product."""
    day = datetime.fromtimestamp(base, JST)
    if isinstance(value, dict):
        return {k: _rebased(v, base) for k, v in value.items()}
    if isinstance(value, list):
        return [_rebased(v, base) for v in value]
    if isinstance(value, (int, float)) and not isinstance(value, bool) and abs(value - base) <= 31 * 86400:
        return f"<base{value - base:+g}s>"
    if isinstance(value, str):
        return (value.replace(day.date().isoformat(), "<base-day>")
                .replace(day.strftime("%m/%d "), "<base-day> "))
    return value


def test_cloud_patient_reaches_the_reviewed_final_product(runtime, real_cloud):  # noqa: F811
    ledger, _, local_calls = runtime
    cfg, posts = real_cloud
    finish(runtime, cfg)
    assert not local_calls, "the cloud path must not use the local model"
    base = stamp(f"{price_window()[0]}T12:00:00")
    now = base + 21 * 60 * 60
    product = _rebased(_normalized({
        "published": readiness.context_view_input(ledger.db, 1, cfg=cfg),
        "views": {section: notify_views.patient_context_list(ledger.db, 1, section, cfg=cfg, since=0, now=now)
                  for section in notify_views.CONTEXT_SECTIONS if section != "measurements"},
        "posts": len(posts),
        "accounting": _accounting(ledger),
    }), base)
    assert product["published"] is not None, "the finite workload must publish a context"
    if not GOLDEN.exists():
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(json.dumps(product, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
                          encoding="utf-8")
        pytest.fail("golden written; review the JSON diff and run again")
    assert product == json.loads(GOLDEN.read_text(encoding="utf-8"))
