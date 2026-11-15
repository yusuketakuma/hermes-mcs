"""Golden acceptance for the patient-context rebuild: posts in, published context and views out.

The rebuild may replace every intermediate record, worker and route.  This test
touches only the stable boundary: synthetic posts saved through the ledger, a
deterministic local model, ``run_due`` until the finite workload completes, and
the views that the notification transports render.  To accept an intended
change of the final product, delete the JSON file and run the test once: it
writes the new product and fails until the JSON diff has been reviewed.
"""
from datetime import datetime
import json
from pathlib import Path

import pytest

from mcs_queries import JST
import notify_views
import patient_context_refresh_readiness as readiness
from test_patient_context_refresh import CFG, finish, runtime, seed  # noqa: F401  (fixture)

GOLDEN = Path(__file__).resolve().parents[1] / "fixtures" / "patient_context_golden" / "local_three_posts.json"
SECTIONS = tuple(notify_views.CONTEXT_SECTIONS)
POSTS = (
    (1, "生活環境: 合成の居住記録。本人は自宅で生活しています。"),
    (2, "既往歴: 合成の既往として高血圧の記載があります。"),
    (3, "服薬: 合成の降圧薬を朝1回服用と記載されています。"),
)
# Values that depend on wall clock, row ids or hashes of the run, not on the product.
_VOLATILE = {"generated_at", "as_of", "created_at", "artifact_id", "reservation_id", "id", "token",
             "checkpoint", "cursor", "signature", "source_sig", "updated_at"}


def _normalized(value):
    if isinstance(value, dict):
        return {k: ("<volatile>" if k in _VOLATILE else _normalized(v)) for k, v in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_normalized(v) for v in value]
    return value


def _product(led):
    now = datetime(2026, 10, 18, 9, tzinfo=JST).timestamp()
    published = readiness.context_view_input(led.db, 1, cfg=CFG)
    views = {}
    for section in SECTIONS:
        if section == "measurements":
            continue    # rendered from a prepared measurement page, not from the context
        views[section] = notify_views.patient_context_list(
            led.db, 1, section, cfg=CFG, since=0, now=now)
    return _normalized({"published": published, "views": views})


def test_three_post_patient_reaches_the_reviewed_final_product(runtime):  # noqa: F811
    led, _, _ = runtime
    for mid, body in POSTS:
        seed(led, mid=mid, pid=1, body=body)
    finish(runtime)
    product = _product(led)
    assert product["published"] is not None, "the finite workload must publish a context"
    if not GOLDEN.exists():
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(json.dumps(product, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
                          encoding="utf-8")
        pytest.fail("golden written; review the JSON diff and run again")
    assert product == json.loads(GOLDEN.read_text(encoding="utf-8"))
