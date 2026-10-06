"""🔎 search folds width/case variants, the snippet shows the hit, and
only current-revision loop candidates count. Synthetic data only."""
import json

import notify_views
from notify_testkit import NOW, _msg, _patient, led as led  # noqa: F401


def _items(led, query):
    return [i["text"] for i in notify_views.patient_search_view(led.db, 1, query)["items"]]


def test_search_matches_width_and_case_variants(led):
    _patient(led)
    _msg(led, 101, body="定時処方 ﾛｷｿﾆﾝ60mg 継続")
    _msg(led, 102, body="ＢＳ　１２０ 食前")
    assert len(_items(led, "ロキソニン")) == 1
    assert len(_items(led, "bs120")) == 1
    assert len(_items(led, "ＲＯＸ")) == 0


def test_snippet_shows_hit_found_across_whitespace(led):
    _patient(led)
    _msg(led, 101, body="あ" * 80 + " 発 熱 あり " + "い" * 80)
    (text,) = _items(led, "発熱")
    assert "発 熱" in text and text.split(": ", 1)[1].startswith("…")


def test_loop_candidate_counts_only_current_revision(led):
    _patient(led)
    _msg(led, 101)
    reqs = [{"ctx": "処方変更", "at": "2026-09-29T09:00", "mid": 101}]
    led.db.execute("INSERT INTO artifacts(kind,project_id,message_id,content,model,meta,"
                   "created_at) VALUES('patient_rollup',1,NULL,?,'t','{}',?)",
                   (json.dumps({"recent_requests": reqs}), NOW))

    def mark(revision):
        led.db.execute("INSERT INTO artifacts(kind,project_id,message_id,content,model,"
                       "meta,created_at) VALUES('loop_candidate',1,101,?,'t','{}',?)",
                       (json.dumps({"origin": {"revision": revision}}), NOW))
        return "Loop候補" in notify_views.patient_summary_text(led.db, 1)[1]

    assert mark("0" * 64) is False                     # stale generation
    assert mark(f"{101:064x}") is True                 # current content_hash
