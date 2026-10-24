"""One timeline read calls mentions once per stored full post, reuses a cached
None as unextracted, and still reads a page row the memo did not cover (synthetic)."""
from collections import Counter

import drug_timeline
from test_drug_timeline import corpus, current, view_of

__all__ = ["corpus"]


def _counted(monkeypatch):
    calls = Counter()
    real = drug_timeline.mentions

    def counted(reader, mid, *, cfg=None):
        calls[mid] += 1
        return real(reader, mid, cfg=cfg)
    monkeypatch.setattr(drug_timeline, "mentions", counted)
    return calls


def _seed(corpus):
    for mid in (1, 2, 3):
        current(corpus, mid, [{"name": f"キラナ{mid}", "subject": "patient"}])
    corpus.db.execute("UPDATE artifacts SET meta=json_set(meta,'$.plan_version',-1) "
                      "WHERE kind='extract_llm' AND message_id=2")     # stale: mentions() is None
    corpus.db.commit()


def test_each_full_post_is_read_once_across_coverage_and_page(corpus, monkeypatch):
    _seed(corpus)
    calls = _counted(monkeypatch)
    report = drug_timeline.timeline(view_of(corpus), 1, limit=2)
    assert calls == {1: 1, 2: 1, 3: 1}               # the page reuses the memo, None included
    assert report["coverage"]["no_current_extraction"] == 1
    assert report["coverage"]["stored_posts"] == 3 and report["next_cursor"] is not None


def test_a_page_row_outside_the_memo_falls_back_to_a_fresh_read(corpus, monkeypatch):
    _seed(corpus)
    view = view_of(corpus)
    real_page = view._page
    extra = dict(corpus.db.execute(
        "SELECT message_id,project_id,posted_at,posted_at_ts,body_state,content_hash "
        "FROM messages WHERE message_id=3").fetchone())

    def page(sql, params, *args):
        out = real_page(sql, params, *args)
        return {**out, "items": [*out["items"], extra]}
    view._page = page
    calls = _counted(monkeypatch)
    # since excludes post 3 from the covered rows, but the page still returns it
    corpus.db.execute("UPDATE messages SET posted_at_ts=0 WHERE message_id=3")
    corpus.db.commit()
    report = drug_timeline.timeline(view, 1, since=1)
    assert calls[3] == 1 and calls[1] == 1
    assert [item["message_id"] for item in report["items"]].count(3) == 1
    assert report["coverage"]["stored_posts"] == 2
