"""build_rollup looks up drug-map refs only for messages that carry a med_ref row."""

import drug_map
import rollup
from extract_testkit import _ledger
from test_drug_map import _dictionary, _seed


def test_current_refs_called_only_for_med_ref_messages(tmp_path, monkeypatch):
    store = _ledger(tmp_path)
    store.ensure_patient(1)
    _seed(store, mid=1)
    _seed(store, mid=2, names=("未登録架空",), posted="2026-09-20T00:00:00+09:00")
    calls = []
    real = rollup.current_refs
    monkeypatch.setattr(rollup, "current_refs",
                        lambda db, mid: calls.append(mid) or real(db, mid))
    rollup.build_rollup(store, 1)
    assert calls == []   # no med_ref rows: no per-message lookups
    drug_map.derive(store, _dictionary(tmp_path))
    ref_mids = {a["message_id"] for a in store.artifacts(drug_map.KIND)}
    rows = rollup.build_rollup(store, 1)["medications"]
    assert set(calls) == ref_mids and 1 in ref_mids
    assert {r["name"]: r["ref"]["status"] for r in rows if "ref" in r}["キラナ"] == "resolved"
