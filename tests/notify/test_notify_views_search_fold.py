"""🔎 search folds width/case variants, the snippet shows the hit, and
only current-revision loop candidates count. Synthetic data only."""
import json
import sqlite3

import pytest

import inference_version as inference
import notify_views
import rollup
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


def test_search_bounds_matches_without_fetching_or_counting_all_bodies(led, monkeypatch):
    import patient_search
    _patient(led)
    for mid in range(101, 122):
        _msg(led, mid, body=f"合成検索語 {mid} " + "x" * 2000)
    before = led.db.total_changes
    reads, original = [], patient_search._record
    def record(db, pid, key, cfg):
        reads.append(key)
        return original(db, pid, key, cfg)
    monkeypatch.setattr(patient_search, '_record', record)
    result = notify_views.patient_search_view(led.db, 1, "合成検索語")
    assert led.db.total_changes == before
    assert len(result["items"]) == patient_search.PAGE_SIZE and result['next_cursor']
    assert len(reads) == patient_search.PAGE_SIZE
    assert all(str(mid) in item["text"] for mid, item in zip(
        range(121, 121 - patient_search.PAGE_SIZE, -1), result["items"], strict=True))


def test_search_finishes_fetch_before_formatting_and_preserves_db_failure(led, monkeypatch):
    import patient_search
    _patient(led)
    _msg(led, 101, body='synthetic term')
    original = patient_search._candidates
    def interrupted(*args):
        yield next(iter(original(*args)))
        raise sqlite3.DatabaseError("synthetic row failure")
    formatted = []
    monkeypatch.setattr(patient_search, '_candidates', interrupted)
    monkeypatch.setattr(notify_views, "_snippet", lambda *a, **k: formatted.append(a) or "preview")
    with pytest.raises(sqlite3.DatabaseError, match="synthetic row failure"):
        notify_views.patient_search_view(led.db, 1, "synthetic")
    assert formatted == []


def test_loop_candidate_counts_only_current_revision(led):
    _patient(led)
    _msg(led, 101)
    reqs = [{"ctx": "処方変更", "at": "2026-09-29T09:00", "mid": 101}]
    # A current cache is still unverified when model configuration is absent.
    led.db.execute("INSERT INTO artifacts(kind,project_id,message_id,content,model,meta,"
                   "created_at) VALUES('patient_rollup',1,NULL,?,'t',?,?)",
                   (json.dumps({inference.VERSION_KEY: inference.CURRENT_VERSION, "project_id": 1, "recent_requests": reqs}),
                    json.dumps({inference.VERSION_KEY: inference.CURRENT_VERSION,
                                "period_check_version": rollup.PERIOD_CHECK_VERSION}), NOW))

    def mark(revision):
        led.db.execute("INSERT INTO artifacts(kind,project_id,message_id,content,model,"
                       "meta,created_at) VALUES('loop_candidate',1,101,?,'t','{}',?)",
                       (json.dumps({"origin": {"revision": revision}}), NOW))
        text = notify_views.patient_summary_text(led.db, 1)[1]
        assert "集約の現行性を確認できません" in text and "Loop候補" not in text
        return "Loop候補" in "\n".join(notify_views._request_reply_lines(led.db, 1, {"recent_requests": reqs}))

    assert mark("0" * 64) is False                     # stale generation
    assert mark(f"{101:064x}") is True                 # current content_hash
