"""A drug-map walk over unchanged messages must not commit once per message."""
import drug_map
from test_drug_map import _dictionary, _seed, store as store


def test_unchanged_rewalk_writes_only_progress_bookends(store, tmp_path):
    for mid in range(1, 41):
        _seed(store, mid=mid)
    dictionary = _dictionary(tmp_path)
    assert drug_map.derive(store, dictionary)["done"] == 40
    before = store.db.total_changes
    result = drug_map.derive(store, dictionary)
    assert result == {"status": "ok", "done": 0, "pids": []}
    # start-of-call save + completion save; per-row commits were fsyncs
    # under synchronous=FULL and cost ~90 s per production tick
    assert store.db.total_changes - before <= 2


def test_cut_walk_still_persists_its_resume_cursor(store, tmp_path, monkeypatch):
    for mid in range(1, 21):
        _seed(store, mid=mid)
    dictionary = _dictionary(tmp_path)
    drug_map.derive(store, dictionary)
    clock = iter(range(1000))
    monkeypatch.setattr(drug_map.time, "monotonic", lambda: next(clock))
    assert drug_map.derive(store, dictionary, deadline=6)["status"] == "partial"
    cursor = drug_map._progress(store.db)["cursor"]
    assert 0 < cursor < 20
