"""Synthetic checks for the access review summary and the receipted external-AI review."""
from contextlib import closing
import json
import os
import sqlite3
from types import SimpleNamespace
import uuid

import pytest

import external_transfer
import mcs_compliance
import mcs_requests
import mcs_util
from ledger import Ledger


def test_review_flags_breadth_denials_and_off_hours():
    day = 1_790_000_000 - 1_790_000_000 % 86400 + 3 * 3600  # 12:00 JST
    entries = [{"via": "slack", "actor": "a", "ts": day, "outcome": "ok", "project_id": n}
               for n in range(3)]
    entries += [{"via": "slack", "actor": "b", "ts": day + 10 * 3600, "outcome": "user_not_allowed",
                 "project_id": None}]
    rows = {row["actor"]: row for row in mcs_compliance.review(entries, max_patients=2)}
    assert rows["a"]["patients"] == 3 and rows["a"]["flags"] == ["many_patients"]
    assert rows["b"]["flags"] == ["denied", "off_hours"]


def test_review_counts_patients_a_list_view_returned():
    day = 1_790_000_000 - 1_790_000_000 % 86400 + 3 * 3600
    entries = [{"via": "slack", "actor": "c", "ts": day, "outcome": "ok", "project_id": None,
                "patients": [1, 2], "patients_more": 1}]
    row, = mcs_compliance.review(entries, max_patients=2)
    assert row["patients"] == 3 and row["flags"] == ["many_patients"]


# --- receipted facility review (ops.external_transfer_acknowledge) -----------------------------


@pytest.fixture
def install(tmp_path, monkeypatch):
    """A synthetic new install: pending review record beside an initialised ledger."""
    monkeypatch.setattr(mcs_util, "HOME", str(tmp_path))
    (tmp_path / "data").mkdir()
    path = tmp_path / "data" / "ledger.db"
    Ledger(str(path)).close()
    assert external_transfer.require(tmp_path) is True
    return tmp_path, path


def _req(**changes):
    req = {"version": 1, "cmd": external_transfer.CMD, "command_id": str(uuid.uuid4()),
           "actor": "管理薬剤師 合成", "human_confirmed": True, "project_id": None,
           "reason": "合成契約v1を確認", "services": ["anthropic", "jev"]}
    return {**req, **changes}


def _apply(path, req, *, wrap=None):
    with closing(Ledger(str(path))) as ledger:
        return mcs_requests.apply_command(ledger if wrap is None else SimpleNamespace(db=wrap(ledger.db)), req)


def _receipts(path):
    with closing(Ledger(str(path))) as ledger:
        return {row["command_id"]: json.loads(row["receipt_json"])
                for row in ledger.db.execute("SELECT command_id,receipt_json FROM command_receipts")}


def _file(root):
    target = external_transfer.path(root)
    return open(target, "rb").read(), os.stat(target).st_mtime_ns


def _cli(capsys, *argv):
    code = mcs_compliance.main(list(argv))
    out = capsys.readouterr().out.strip().splitlines()
    return code, out[-1] if out else ""


def test_approval_leaves_a_receipt_naming_the_published_record(install):
    root, path = install
    req = _req()
    receipt = _apply(path, req)
    assert receipt["outcome"] == "applied" and receipt["error"] is None
    stored = _receipts(path)[req["command_id"]]
    assert stored == receipt
    assert (stored["actor"], stored["reason"], stored["services"]) == (
        req["actor"], req["reason"], ["anthropic", "jev"])
    ack = external_transfer.status(root)["acknowledged"]
    assert ack["command_id"] == req["command_id"] and ack["at"] == stored["acknowledged_at"]
    assert external_transfer.receipt_state(stored, root) == "matched"
    # nothing but the operator's own text and the service names is kept
    assert "synthetic-secret" not in json.dumps(stored)


def test_root_follows_the_ledger_not_the_process_home(install, tmp_path, monkeypatch):
    root, path = install
    elsewhere = tmp_path / "other-home"
    elsewhere.mkdir()
    monkeypatch.setattr(mcs_util, "HOME", str(elsewhere))
    assert _apply(path, _req())["outcome"] == "applied"
    assert external_transfer.status(root)["state"] == "acknowledged"
    assert not os.path.exists(external_transfer.path(elsewhere))


def test_replay_returns_the_stored_receipt_without_rewriting(install):
    root, path = install
    req = _req()
    first = _apply(path, req)
    before = _file(root)
    assert _apply(path, dict(req)) == first
    assert _file(root) == before


@pytest.mark.parametrize("changes", [{"reason": "別の根拠"}, {"actor": "別の責任者"}, {"services": ["openai"]}])
def test_same_command_id_with_another_payload_is_a_conflict(install, changes):
    root, path = install
    req = _req()
    _apply(path, req)
    before = _file(root)
    assert _apply(path, {**req, **changes}) == {"outcome": "rejected", "error": "command_id_conflict"}
    assert _file(root) == before


@pytest.mark.parametrize("changes, error", [
    ({"human_confirmed": False}, "human_confirmation_required"),
    ({"services": ["jev", "anthropic"]}, "bad_services"),
    ({"services": []}, "bad_services"),
    ({"services": ["other"]}, "bad_services"),
    ({"reason": "確" * 501}, "bad_reason"),
    ({"project_id": 1}, "bad_project_id"),
])
def test_rejected_before_the_record_is_touched(install, changes, error):
    root, path = install
    before = _file(root)
    receipt = _apply(path, _req(**changes))
    assert (receipt["outcome"], receipt["error"]) == ("rejected", error)
    assert _file(root) == before


def test_installs_without_the_record_get_a_rejected_receipt(install):
    root, path = install
    os.unlink(external_transfer.path(root))
    receipt = _apply(path, _req())
    assert (receipt["outcome"], receipt["error"]) == ("rejected", "external_transfer_not_required")
    assert not os.path.exists(external_transfer.path(root))
    assert external_transfer.status(root)["state"] == "not_required"


def test_unreadable_record_is_a_rejected_receipt_not_an_exception(install):
    root, path = install
    with open(external_transfer.path(root), "w") as stream:
        stream.write("{")
    receipt = _apply(path, _req())
    assert (receipt["outcome"], receipt["error"]) == ("rejected", "external_transfer_record_invalid")


def test_failed_receipt_never_reaches_the_record(install, monkeypatch):
    root, path = install
    before = open(external_transfer.path(root), "rb").read()
    original = mcs_requests.canonical

    def failing(value):
        if isinstance(value, dict) and value.get("outcome") == "applied":
            assert open(external_transfer.path(root), "rb").read() == before  # nothing before COMMIT
            raise RuntimeError("synthetic receipt failure")
        return original(value)

    monkeypatch.setattr(mcs_requests, "canonical", failing)
    req = _req()
    with pytest.raises(RuntimeError):
        _apply(path, req)
    assert open(external_transfer.path(root), "rb").read() == before
    assert req["command_id"] not in _receipts(path)


class _CommitFails:
    """Connection stand-in whose COMMIT fails after the receipt INSERT."""

    def __init__(self, db):
        self._db = db

    def __getattr__(self, name):
        return getattr(self._db, name)

    def __enter__(self):
        return self._db.__enter__()

    def __exit__(self, kind, value, traceback):
        if kind is None:
            self._db.rollback()
            raise sqlite3.OperationalError("synthetic commit failure")
        return self._db.__exit__(kind, value, traceback)


def test_failing_commit_leaves_the_record_untouched(install):
    root, path = install
    before = open(external_transfer.path(root), "rb").read()
    req = _req()
    with pytest.raises(sqlite3.OperationalError):
        _apply(path, req, wrap=_CommitFails)
    assert open(external_transfer.path(root), "rb").read() == before
    assert external_transfer.status(root)["state"] == "pending"
    assert req["command_id"] not in _receipts(path)


def test_cli_records_through_the_receipt_and_replays_any_service_order(install, capsys):
    root, path = install
    command_id = str(uuid.uuid4())
    args = ["acknowledge", "--by", "管理薬剤師 合成", "--reason", "合成契約v1を確認",
            "--confirm-human", "--command-id", command_id]
    code, line = _cli(capsys, *args, "--services", "jev,anthropic")
    assert code == 0
    printed = json.loads(line)
    assert printed["outcome"] == "applied" and printed["file_check"] == "matched"
    assert printed["services"] == ["anthropic", "jev"] and printed["command_id"] == command_id
    assert _receipts(path)[command_id]["services"] == ["anthropic", "jev"]
    before = _file(root)
    code, line = _cli(capsys, *args, "--services", "anthropic, jev,jev")
    assert code == 0 and json.loads(line)["outcome"] == "applied"
    assert _file(root) == before
    code, line = _cli(capsys, *(a if a != "合成契約v1を確認" else "合成契約v2を確認" for a in args),
                      "--services", "anthropic,jev")
    assert code == 1 and json.loads(line)["error"] == "command_id_conflict"
    assert _file(root) == before
    assert mcs_compliance.transfer_status()["receipt"] == "matched"


def test_cli_without_confirmation_or_with_long_name_writes_nothing(install, capsys):
    root, path = install
    before = _file(root)
    with pytest.raises(SystemExit):
        mcs_compliance.main(["acknowledge", "--by", "合成", "--reason", "合成", "--services", "jev"])
    code, line = _cli(capsys, "acknowledge", "--by", "合" * 121, "--reason", "合成", "--services", "jev",
                      "--confirm-human")
    assert code == 1 and json.loads(line)["error"] == "bad_actor"
    code, line = _cli(capsys, "acknowledge", "--by", "合成", "--reason", "合成", "--services", "other",
                      "--confirm-human")
    assert code == 1 and json.loads(line)["error"] == "bad_services"
    assert _file(root) == before and _receipts(path) == {}


def test_cli_before_the_first_run_explains_the_order(install, capsys):
    root, path = install
    path.unlink()
    code, line = _cli(capsys, "acknowledge", "--by", "合成", "--reason", "合成", "--services", "jev",
                      "--confirm-human")
    assert code == 1 and "初回収集" in line
    assert external_transfer.status(root)["state"] == "pending" and not path.exists()


def test_cli_waits_for_the_writer_and_reports_busy(install, capsys, monkeypatch):
    import writer_lock
    root, path = install
    fd = writer_lock.acquire_run_lock(str(path.parent / "run.lock"))
    monkeypatch.setattr(writer_lock, "wait_run_lock", lambda *a, **k: None)
    try:
        code = mcs_compliance.main(["acknowledge", "--by", "合成", "--reason", "合成", "--services", "jev",
                                    "--confirm-human"])
    finally:
        os.close(fd)
    printed = json.loads(capsys.readouterr().out.splitlines()[0])
    assert code == 3 and printed["outcome"] == "busy" and printed["command_id"]
    assert external_transfer.status(root)["state"] == "pending"


def test_cli_unknown_result_names_the_command_id_to_replay(install, capsys, monkeypatch):
    root, path = install
    original = mcs_requests.canonical

    def failing(value):
        if isinstance(value, dict) and value.get("outcome") == "applied":
            raise sqlite3.OperationalError("synthetic disk failure")
        return original(value)

    monkeypatch.setattr(mcs_requests, "canonical", failing)
    base = ["acknowledge", "--by", "合成", "--reason", "合成", "--services", "jev", "--confirm-human"]
    code, line = _cli(capsys, *base)
    printed = json.loads(line)
    assert code == 1 and printed["outcome"] == "unknown"
    assert external_transfer.status(root)["state"] == "pending"
    monkeypatch.setattr(mcs_requests, "canonical", original)
    code, line = _cli(capsys, *base, "--command-id", printed["command_id"])
    assert code == 0 and json.loads(line)["file_check"] == "matched"


def test_cli_replay_is_not_success_once_the_record_disagrees(install, capsys):
    root, path = install
    pending = open(external_transfer.path(root), "rb").read()
    first = str(uuid.uuid4())
    base = ["acknowledge", "--by", "合成", "--reason", "合成", "--services", "jev", "--confirm-human"]
    assert _cli(capsys, *base, "--command-id", first)[0] == 0
    second = str(uuid.uuid4())
    assert _cli(capsys, *base, "--command-id", second)[0] == 0
    code, line = _cli(capsys, *base, "--command-id", first)
    assert code == 1 and json.loads(line)["file_check"] == "external_transfer_superseded"
    # the newest applied receipt re-publishes a record that went back to pending
    with open(external_transfer.path(root), "wb") as stream:
        stream.write(pending)
    code, line = _cli(capsys, *base, "--command-id", second)
    assert code == 0 and json.loads(line)["file_check"] == "matched"
    # an unreadable record is reported, not overwritten and not reused as success
    with open(external_transfer.path(root), "w") as stream:
        stream.write("{")
    code, line = _cli(capsys, *base, "--command-id", second)
    assert code == 1 and json.loads(line)["file_check"] == "receipt_file_mismatch"
    assert open(external_transfer.path(root)).read() == "{"


def _stop_after_commit(monkeypatch, root):
    """Simulate a process that stops between COMMIT and the record write."""
    seen = {}
    original = external_transfer._write

    def stopped(*args, **kwargs):
        seen["receipts"] = len(_receipts(root / "data" / "ledger.db"))  # COMMIT already happened
        raise OSError("synthetic stop after commit")

    monkeypatch.setattr(external_transfer, "_write", stopped)
    return seen, original


def test_stop_after_commit_keeps_transmission_off_until_the_replay_publishes(install, monkeypatch):
    root, path = install
    before = open(external_transfer.path(root), "rb").read()
    seen, original = _stop_after_commit(monkeypatch, root)
    req = _req()
    with pytest.raises(OSError):
        _apply(path, req)
    assert seen["receipts"] == 1 and _receipts(path)[req["command_id"]]["outcome"] == "applied"
    # fail-safe: a receipt without its record never widens what is sent
    assert open(external_transfer.path(root), "rb").read() == before
    assert not any(external_transfer.auto_enable_allowed(name, root) for name in external_transfer.SERVICES)
    monkeypatch.setattr(external_transfer, "_write", original)
    stored = _apply(path, dict(req))
    assert stored == _receipts(path)[req["command_id"]]
    ack = external_transfer.status(root)["acknowledged"]
    assert (ack["command_id"], ack["at"]) == (req["command_id"], stored["acknowledged_at"])
    assert external_transfer.receipt_state(stored, root) == "matched"
    assert external_transfer.auto_enable_allowed("jev", root)
    assert not external_transfer.auto_enable_allowed("openai", root)


def test_replay_of_an_older_unpublished_receipt_never_undoes_a_later_review(install, monkeypatch):
    root, path = install
    _seen, original = _stop_after_commit(monkeypatch, root)
    older = _req(services=["anthropic", "jev", "openai"])
    with pytest.raises(OSError):
        _apply(path, older)
    monkeypatch.setattr(external_transfer, "_write", original)
    later = _req(services=["jev"])
    assert _apply(path, later)["outcome"] == "applied"
    published = _file(root)
    _apply(path, dict(older))
    assert _file(root) == published
    assert external_transfer.status(root)["acknowledged"]["services"] == ["jev"]


def test_cli_finishes_a_record_stopped_after_commit(install, capsys, monkeypatch):
    root, path = install
    _seen, original = _stop_after_commit(monkeypatch, root)
    base = ["acknowledge", "--by", "合成", "--reason", "合成", "--services", "jev", "--confirm-human"]
    code, line = _cli(capsys, *base)
    printed = json.loads(line)
    assert code == 1 and printed["outcome"] == "unknown"
    assert external_transfer.status(root)["state"] == "pending"
    monkeypatch.setattr(external_transfer, "_write", original)
    code, line = _cli(capsys, *base, "--command-id", printed["command_id"])
    assert code == 0 and json.loads(line)["file_check"] == "matched"


def test_status_checks_the_record_against_its_receipt(install, capsys):
    root, path = install
    assert "receipt" not in mcs_compliance.transfer_status()
    assert _apply(path, _req())["outcome"] == "applied"
    assert mcs_compliance.transfer_status()["receipt"] == "matched"
    # a record written without the receipted command is reported as such
    external_transfer.acknowledge(by="合成", reason="合成", services=["jev"], confirm_human=True,
                                  root=root, command_id=str(uuid.uuid4()))
    assert mcs_compliance.transfer_status()["receipt"] == "receipt_file_mismatch"
    external_transfer.acknowledge(by="合成", reason="合成", services=["jev"], confirm_human=True, root=root)
    assert mcs_compliance.transfer_status()["receipt"] == "unreceipted"
    path.unlink()
    external_transfer.acknowledge(by="合成", reason="合成", services=["jev"], confirm_human=True,
                                  root=root, command_id=str(uuid.uuid4()))
    assert mcs_compliance.transfer_status()["receipt"] == "unverified"
    assert mcs_compliance.main(["status"]) == 0
    assert json.loads(capsys.readouterr().out)["external_transfer"]["receipt"] == "unverified"


def test_chat_buttons_cannot_widen_external_ai_transmission():
    from adapters.common import human_commands
    assert external_transfer.CMD not in human_commands._PROJECTLESS_OPS
    assert external_transfer.CMD.removeprefix("ops.") not in human_commands.CONTROL_FIELDS
