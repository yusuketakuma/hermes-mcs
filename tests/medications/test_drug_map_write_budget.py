"""A drug-map walk over unchanged messages must not commit once per message."""
import drug_map
from medications_testkit import _dictionary, _seed, store as store


def test_unchanged_rewalk_writes_only_progress_bookends(store, tmp_path):
    for mid in range(1, 41):
        _seed(store, mid=mid)
    dictionary = _dictionary(tmp_path)
    assert drug_map.derive(store, dictionary)["done"] == 40
    before = store.db.total_changes
    result = drug_map.derive(store, dictionary)
    assert result == {"status": "ok", "done": 0, "pids": []}
    # The synthetic unchanged rewalk writes only its start/end progress bookends;
    # it must not add one durable progress write for every unchanged message.
    assert store.db.total_changes - before <= 2


def test_cut_walk_still_persists_its_resume_cursor(store, tmp_path, monkeypatch):
    for mid in range(1, 21):
        _seed(store, mid=mid)
    dictionary = _dictionary(tmp_path)
    drug_map.derive(store, dictionary)
    clock = iter(range(1000))
    monkeypatch.setattr(drug_map.time, "monotonic", lambda: next(clock))
    assert drug_map.derive(store, dictionary, deadline=6)["status"] == "partial"
    cursor = drug_map.read_annotation_progress(store.db)["cursor"]
    assert 0 < cursor < 20
