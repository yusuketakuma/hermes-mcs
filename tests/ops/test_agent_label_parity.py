"""Updater and independent recovery labels agree with the service catalogue."""
import service_contract
import mcs_update
from ops_testkit import _load


def test_agent_labels_share_catalogue_and_match_independent_recovery():
    assert mcs_update.service_contract is service_contract
    rec = _load()
    for name in ("RESIDENT_LABELS", "WATCHER_LABELS"):
        expected = tuple(getattr(service_contract, name))
        assert tuple(getattr(rec, name)) == expected, name
    assert rec.EXCLUDED_LABELS == service_contract.EXCLUDED_LABELS
