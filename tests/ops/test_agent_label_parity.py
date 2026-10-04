"""Updater, setup and recovery keep their own label copies; they must agree."""
import mcs_setup
import mcs_update
from ops_testkit import _load


def test_agent_label_copies_match():
    rec = _load()
    for name in ("RESIDENT_LABELS", "WATCHER_LABELS"):
        expected = tuple(getattr(mcs_setup, name))
        assert tuple(getattr(mcs_update, name)) == expected, name
        assert tuple(getattr(rec, name)) == expected, name
    assert rec.EXCLUDED_LABELS == mcs_setup.EXCLUDED_LABELS
