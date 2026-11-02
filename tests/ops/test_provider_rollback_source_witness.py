"""Synthetic fixed-tree rollback witnesses retain the ordinary source guards."""
import hashlib
import json
import os

import pytest

from ops_testkit import _git
from test_mcs_update import updater  # noqa: F401
from test_update_rollback_guards import _head, _source_repo


def _witness(tree):
    return hashlib.sha256(json.dumps(
        tree, ensure_ascii=True, separators=(",", ":")).encode("ascii")).hexdigest()


@pytest.fixture
def reviewed_source(updater, tmp_path, monkeypatch):  # noqa: F811
    repo, previous = _source_repo(tmp_path)
    monkeypatch.setattr(updater, "REPO", str(repo))
    monkeypatch.setattr(updater, "_PROVIDER_ROLLBACK_SOURCE_WITNESSES",
                        frozenset({_witness(updater._split_source_tree(previous))}))
    path = repo / "mcs/ingest/consumer.py"
    path.write_text("# synthetic newer reader\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "synthetic newer runtime")
    return updater, repo, previous


def test_reviewed_prior_is_rollback_only(reviewed_source):
    update, _, previous = reviewed_source
    assert not update._split_target_source_matches(previous)
    assert update._split_target_source_matches(previous, allow_reviewed_rollback=True)


@pytest.mark.parametrize("reader", ["provider_settings.py", "mcs_config.py"])
def test_reviewed_prior_rejects_changed_current_reader(reviewed_source, reader):
    update, repo, previous = reviewed_source
    (repo / "mcs/core" / reader).write_text("# synthetic changed settings contract\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "synthetic changed settings reader")
    assert not update._split_target_source_matches(previous, allow_reviewed_rollback=True)


@pytest.mark.parametrize("damage", ["dirty", "hidden", "staged", "untracked", "hardlink", "mode"])
def test_reviewed_prior_keeps_current_source_checks(reviewed_source, tmp_path, damage):
    update, repo, previous = reviewed_source
    path = repo / "mcs/ingest/consumer.py"
    if damage == "untracked":
        (repo / "mcs/shadow.py").write_text("# synthetic shadow\n")
    elif damage == "hardlink":
        os.link(path, tmp_path / "synthetic-link")
    elif damage == "mode":
        path.chmod(0o755)
    else:
        path.write_text("# synthetic unsafe change\n")
        if damage == "hidden":
            _git(repo, "update-index", "--assume-unchanged", str(path.relative_to(repo)))
        elif damage == "staged":
            _git(repo, "add", ".")
    assert not update._split_target_source_matches(previous, allow_reviewed_rollback=True)


def test_reviewed_prior_rejects_unknown_changed_target(reviewed_source):
    update, repo, previous = reviewed_source
    path = repo / "mcs/ingest/consumer.py"
    path.write_text("# synthetic unreviewed target\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "synthetic unknown runtime")
    unknown = _head(repo)
    _git(repo, "checkout", "-q", "HEAD~1")
    assert not update._split_target_source_matches(unknown, allow_reviewed_rollback=True)
    assert update._split_target_source_matches(previous, allow_reviewed_rollback=True)


def test_reviewed_prior_still_requires_target_readers(reviewed_source, monkeypatch):
    update, _, previous = reviewed_source
    real_tree = update._split_source_tree
    missing = [entry for entry in real_tree(previous) if entry[3] != "mcs/core/provider_settings.py"]
    monkeypatch.setattr(update, "_PROVIDER_ROLLBACK_SOURCE_WITNESSES", frozenset({_witness(missing)}))
    monkeypatch.setattr(update, "_split_source_tree", lambda ref: missing if ref == previous else real_tree(ref))
    assert not update._split_target_source_matches(previous, allow_reviewed_rollback=True)


def test_reviewed_prior_rechecks_target_tree(reviewed_source, monkeypatch):
    update, _, previous = reviewed_source
    real_tree = update._split_source_tree
    calls = []

    def changed(ref):
        tree = real_tree(ref)
        if ref == previous:
            calls.append(ref)
            if len(calls) > 1:
                return tree[:-1]
        return tree

    monkeypatch.setattr(update, "_split_source_tree", changed)
    assert not update._split_target_source_matches(previous, allow_reviewed_rollback=True)


@pytest.mark.parametrize("state", ["pending", "authority_invalid"])
def test_reviewed_prior_never_bypasses_settings_hold(reviewed_source, monkeypatch, state):
    update, _, previous = reviewed_source
    monkeypatch.setattr(update.provider_settings, "probe", lambda _: {"state": state})
    with pytest.raises(update.ProviderRollbackUnverified, match="settings_unverified"):
        update._provider_rollback_preflight(previous)


def test_reviewed_prior_preflight_keeps_split_snapshot_guard(reviewed_source, monkeypatch):
    update, _, previous = reviewed_source
    monkeypatch.setattr(update.provider_settings, "probe", lambda _: {"state": "requires_checked_read"})
    monkeypatch.setattr(update.provider_settings, "read", lambda *a, **k: {"mode": "split", "snapshot": 1})
    assert update._provider_rollback_preflight(previous) is True
    snapshots = iter([1, 2])
    monkeypatch.setattr(update.provider_settings, "read", lambda *a, **k: {
        "mode": "split", "snapshot": next(snapshots)})
    with pytest.raises(update.ProviderRollbackUnverified, match="settings_changed"):
        update._provider_rollback_preflight(previous)
