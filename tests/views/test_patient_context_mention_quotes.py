"""One post's several medication mentions must not repeat the whole post."""
import notify_views
from semantic_testkit import _message
from test_read_model import _artifact, _db, _hash_of


def test_background_medication_row_does_not_repeat_the_post_per_mention(tmp_path, monkeypatch):
    import patient_context_refresh
    monkeypatch.setattr(patient_context_refresh, "context_view_input", lambda *a, **k: None)
    store = _db(tmp_path, ())
    try:
        store.save_messages([_message(1, body="架空薬A、架空薬B、架空薬Cを継続")], project_id=1)
        _artifact(store, "extract_llm", 1, {"meds": [{"name": n, "subject": "patient"}
                                                     for n in ("架空薬A", "架空薬B", "架空薬C")]},
                  {"hash": _hash_of(store, 1)})
        view = notify_views.patient_context_list(store.db, 1, "background", cfg={}, since=0,
                                                 now=1_790_000_000)
        rows = [r for r in view["items"] if r["group"].startswith("薬剤の記載")]
        assert rows, view["items"]
        quotes = rows[0]["quotes"]
        assert len(quotes) == len(set(quotes)), quotes
    finally:
        store.close()
