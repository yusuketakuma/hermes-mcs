"""An active candidate dictionary requires approval even if progress is inconsistent."""
from copy import deepcopy

import pytest

import drug_map
import drug_reconciliation
import drug_timeline
import notify_views
from medications_testkit import DOCUMENT, _dictionary
from test_drug_timeline import corpus, current, view_of

__all__ = ["corpus"]


@pytest.mark.parametrize("approved", [False, True])
def test_matching_progress_cannot_activate_unapproved_dictionary(corpus, tmp_path, approved):
    document = deepcopy(DOCUMENT)
    if not approved:
        document["source"].pop("approved_by")
    dictionary = _dictionary(tmp_path, document)
    cfg = {"drug_map": {"path": str(tmp_path / "fictional-drug-map.json"),
                        "sha256": dictionary.sha256}}
    current(corpus, 1, [{"name": "キラナ", "subject": "patient", "status": "current",
                         "action": "none", "negated": False}])
    # Model an inconsistent persisted progress row, not a normal derive:
    # derive must never mint a production generation for an unapproved map.
    with corpus.db:
        drug_map._save_progress(corpus.db, {"dictionary": [dictionary.dict_id, dictionary.sha256,
                                                            False, drug_map.RESOLVER_VERSION],
                                            "cursor": 0})
    selected, state = drug_map.active_dictionary(corpus.db, cfg)
    assert state == ("active" if approved else "inactive")
    assert (selected is not None) is approved
    view = notify_views.drug_search_view(corpus.db, cfg, 1, "キラナ")
    assert bool(view["items"]) is approved
    if not approved:
        assert "有効化されていない" in view["empty"]
    timeline = drug_timeline.timeline(view_of(corpus), 1, cfg=cfg)
    assert len(timeline["items"]) == 1
    candidate = timeline["items"][0]["candidate"]
    assert (candidate is not None) is approved
    assert drug_reconciliation.reconcile(view_of(corpus), 1, cfg=cfg)["dictionary_state"] == state
    # Explicit standalone reference lookup/search still accept an unapproved map.
    assert dictionary.lookup("キラナ")["status"] == "resolved"
    assert dictionary.search("キラナ")["items"]
    assert dictionary.lookup("キラナ")["dictionary"]["approved"] is approved


def test_active_dictionary_keeps_existing_pin_and_generation_guards(corpus, tmp_path):
    dictionary = _dictionary(tmp_path)
    cfg = {"drug_map": {"path": str(tmp_path / "fictional-drug-map.json"),
                        "sha256": dictionary.sha256}}
    assert drug_map.active_dictionary(corpus.db, {}) == (None, "unconfigured")
    assert drug_map.active_dictionary(corpus.db, cfg) == (None, "inactive")
    with corpus.db:
        drug_map._save_progress(corpus.db, {"dictionary": [dictionary.dict_id, dictionary.sha256,
                                                            True, drug_map.RESOLVER_VERSION],
                                            "cursor": 0})
    assert drug_map.active_dictionary(corpus.db, cfg) == (None, "inactive")
    bad = {"drug_map": {**cfg["drug_map"], "sha256": "0" * 64}}
    assert drug_map.active_dictionary(corpus.db, bad) == (None, "invalid")
