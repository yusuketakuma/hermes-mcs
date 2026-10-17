"""Rollback layout floor: old code must never run against cards of a
newer layout (synthetic repo, temp ledger, no services)."""
import sqlite3
import subprocess

import pytest

from ops_testkit import _git, _make_repo
from test_mcs_update import updater  # noqa: F401


def _cards(path, *layouts):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE notification_cards(card_id INTEGER PRIMARY KEY,"
                " layout INTEGER NOT NULL DEFAULT 1)")
    con.executemany("INSERT INTO notification_cards(layout) VALUES(?)",
                    [(n,) for n in layouts])
    con.commit()
    con.close()


def _state(updater, repo, **entry):  # noqa: F811
    before = _git(repo, "rev-parse", "v1.0.0").stdout.strip()
    after = _git(repo, "rev-parse", "HEAD").stdout.strip()
    state = updater._default_state()
    state["applied"] = [{"tag": "v1.1.0", "sha": after, "prev_sha": before,
                         **entry}]
    updater.save_state(state)
    return before, after


def test_rollback_refuses_before_journal_when_target_lacks_the_layout(
        updater, tmp_path, monkeypatch):  # noqa: F811
    repo, _ = _make_repo(tmp_path)          # v1.0.0 has no notify_cards.py
    monkeypatch.setattr(updater, "REPO", str(repo))
    _, after = _state(updater, repo)
    _cards(updater.LEDGER, 1, 3)
    notices = []
    monkeypatch.setattr(updater, "load_config", lambda: {})
    monkeypatch.setattr(updater, "quiesce", lambda: pytest.fail("no quiesce"))
    monkeypatch.setattr(updater, "_enqueue_notice",
                        lambda text, **k: notices.append(text) or True)
    assert updater.rollback("rb-1") == 1
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == after
    st = updater.load_state()
    assert st["applying"] is None and st["applied"][-1]["sha"] == after
    assert st["executed"]["rb-1"]["result"] == "rollback_failed"
    assert "rollback_blocked_layout_floor" in st["executed"]["rb-1"]["detail"]
    assert any("layout 3 > target layout 1" in n for n in notices)


@pytest.mark.parametrize("ledger", ["absent", "no_table", "no_layout_column",
                                    "layout_ok"])
def test_rollback_layout_floor_passes_when_nothing_can_be_misread(
        updater, tmp_path, monkeypatch, ledger):  # noqa: F811
    repo, _ = _make_repo(tmp_path)
    monkeypatch.setattr(updater, "REPO", str(repo))
    before, _ = _state(updater, repo)
    if ledger == "no_table":
        sqlite3.connect(updater.LEDGER).close()
    elif ledger == "no_layout_column":          # schema older than layouts
        con = sqlite3.connect(updater.LEDGER)
        con.execute("CREATE TABLE notification_cards(card_id INTEGER PRIMARY KEY)")
        con.commit()
        con.close()
    elif ledger == "layout_ok":
        _cards(updater.LEDGER, 1, 1)
    assert updater._rollback_layout_error(
        updater.load_state()["applied"][-1]) is None


def test_rollback_layout_floor_treats_an_unreadable_ledger_as_unverified(
        updater, tmp_path, monkeypatch):  # noqa: F811
    """A DB that cannot answer MAX(layout) is not evidence of safety."""
    repo, _ = _make_repo(tmp_path)
    monkeypatch.setattr(updater, "REPO", str(repo))
    _state(updater, repo)
    with open(updater.LEDGER, "wb") as f:
        f.write(b"not a sqlite file" * 64)
    err = updater._rollback_layout_error(updater.load_state()["applied"][-1])
    assert err and err.startswith("rollback_layout_unverified:")


def test_rollback_layout_floor_defers_an_unreadable_backup_to_the_restore(
        updater, tmp_path, monkeypatch):  # noqa: F811
    """The bail path must still reset the tree so recovery classifies the
    journal as a pending restore; _restore_db then fails closed."""
    repo, _ = _make_repo(tmp_path)
    monkeypatch.setattr(updater, "REPO", str(repo))
    backup = tmp_path / "pre.db"
    backup.write_bytes(b"garbage" * 64)
    _state(updater, repo, schema_bump=True, backup_path=str(backup))
    _cards(updater.LEDGER, 3)
    assert updater._rollback_layout_error(
        updater.load_state()["applied"][-1]) is None


def test_rollback_layout_floor_reads_the_backup_a_schema_bump_restores(
        updater, tmp_path, monkeypatch):  # noqa: F811
    repo, _ = _make_repo(tmp_path)
    monkeypatch.setattr(updater, "REPO", str(repo))
    backup = tmp_path / "preupdate.db"
    _cards(str(backup), 1)                  # taken before any layout-3 card
    _state(updater, repo, schema_bump=True, backup_path=str(backup))
    _cards(updater.LEDGER, 3)
    assert updater._rollback_layout_error(
        updater.load_state()["applied"][-1]) is None


def test_target_card_layout_parses_the_constant(updater, monkeypatch):  # noqa: F811
    seen = []

    def git(argv):
        seen.append(argv)
        return subprocess.CompletedProcess(
            argv, 0, "X = 1\nCARD_LAYOUT = 2\nCARD_LAYOUT_OLD = 9\n", "")

    monkeypatch.setattr(updater, "_git", git)
    assert updater._target_card_layout("abc") == 2
    assert seen == [["show", "abc:mcs/notify/notify_cards.py"]]
    monkeypatch.setattr(updater, "_git", lambda argv: subprocess.CompletedProcess(
        argv, 128, "", ""))
    assert updater._target_card_layout("abc") == 1


def test_rollback_tree_refuses_before_any_reset(updater, tmp_path, monkeypatch):  # noqa: F811
    """Backstop for the apply-bail and recovery paths, which reach
    _rollback_tree without rollback()'s pre-journal check."""
    _cards(updater.LEDGER, 3)
    calls = []
    monkeypatch.setattr(updater, "load_config", lambda: {})
    monkeypatch.setattr(updater, "_restore_runtime", lambda entry: None)
    monkeypatch.setattr(updater, "_git", lambda argv: calls.append(argv)
                        or subprocess.CompletedProcess(argv, 1, "", ""))
    with pytest.raises(updater.UpdateError, match="rollback_blocked_layout_floor"):
        updater._rollback_tree({"prev_sha": "0" * 40})
    assert not any("reset" in call for call in calls)
