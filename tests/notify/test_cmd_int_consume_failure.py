"""A cmd_int file that cannot be quarantined or removed after its result
is published stays for the next drain and is reported; it never stops
the rest of the drain. Temp DB + stubs only."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "mcs"))
import _mcs_path  # noqa: F401

import notify_cards
import notify_spool
import notify_cmds
import pytest
from notify_testkit import (CFG, CLICKER, ORIGIN, _delivered_card, _token_for, led,
                            pinned_clock)
from notify_testkit import _uuid

__all__ = ["led", "pinned_clock"]  # shared isolated fixtures


def _notification(n):
    token = f"{n:032x}"
    return {"version": 1, "op": "notification",
            "command_id": f"{token}:{'ee' * 8}", "actor": "nurse-1",
            "token": token, "origin": ORIGIN}


def _write(root, reqs):
    notify_spool.ensure_dirs(str(root))
    for i, req in enumerate(reqs):
        (root / "cmd_int" / f"{i}.json").write_text(json.dumps(req))


def _fail_once(monkeypatch, name):
    """Fail only the consume step of the first command file (0.json)."""
    real = getattr(notify_cmds.os, name)
    calls = []

    def flaky(*args, **kwargs):
        target = str(args[0])
        if target.endswith(os.sep + "0.json") and not calls:
            calls.append(target)
            raise PermissionError("synthetic consume failure")
        return real(*args, **kwargs)
    monkeypatch.setattr(notify_cmds.os, name, flaky)
    return calls


def test_quarantine_failure_keeps_the_file_and_drains_the_rest(led, tmp_path, monkeypatch):
    root = tmp_path / "data"
    _write(root, [{"version": 99}, {"version": 98}])
    _fail_once(monkeypatch, "replace")
    result = {"errors": []}
    done = notify_cmds.drain_int_commands(led, result, CFG, str(root))
    assert done == 1                                   # only the one consumed
    left = sorted(p.name for p in (root / "cmd_int").iterdir() if p.suffix in (".json", ".invalid"))
    assert left == ["0.json", "1.json.invalid"]
    assert any(e.startswith("cmd_int_consume_failed:") for e in result["errors"])
    assert all("synthetic" not in e for e in result["errors"])
    assert len(list((root / "cmd_results").iterdir())) == 2   # both answered


def test_remove_failure_keeps_the_file_and_drains_the_rest(led, tmp_path, monkeypatch):
    root = tmp_path / "data"
    _write(root, [_notification(1), _notification(2)])
    monkeypatch.setattr(notify_cards, "sweep", lambda *a, **k: None)
    _fail_once(monkeypatch, "unlink")
    result = {"errors": []}
    done = notify_cmds.drain_int_commands(led, result, CFG, str(root))
    assert done == 1
    assert sorted(p for p in os.listdir(root / "cmd_int") if p.endswith(".json")) == ["0.json"]
    assert any(e.startswith("cmd_int_consume_failed:") for e in result["errors"])


@pytest.mark.parametrize("failure", ["consume", "publish"])
def test_an_applied_click_still_refreshes_cards_when_its_file_stays(
        led, pinned_clock, tmp_path, monkeypatch, failure):
    """THIS drain re-renders the cards a click changed, even when its file
    could not be consumed or answered and stays for the next drain."""
    spec = _delivered_card(led)
    token = _token_for(spec, "ack")
    req = {"version": 1, "op": "notification", "command_id": f"{token}:{'ab' * 8}",
           "actor": CLICKER, "token": token, "origin": dict(ORIGIN, message_id="m-9")}
    root = tmp_path / "data"
    _write(root, [req])
    sweeps = []
    monkeypatch.setattr(notify_cards, "sweep", lambda *a, **k: sweeps.append(1))
    if failure == "consume":
        _fail_once(monkeypatch, "unlink")
    else:
        def no_publish(directory, name, payload):
            raise PermissionError("synthetic publish failure")
        real_publish = notify_spool.publish_file
        monkeypatch.setattr(notify_spool, "publish_file",
                            lambda d, n, p: no_publish(d, n, p) if "cmd_results" in str(d)
                            else real_publish(d, n, p))
    applied = "SELECT count(*) FROM command_receipts WHERE outcome='applied'"
    before = led.db.execute(applied).fetchone()[0]
    result = {"errors": []}
    assert notify_cmds.drain_int_commands(led, result, CFG, str(root)) == 0
    assert led.db.execute(applied).fetchone()[0] == before + 1   # really applied
    assert sweeps == [1]
    assert (root / "cmd_int" / "0.json").exists()      # kept for the next drain


# Runner-side text caps use the shared bounded-text rule (synthetic envelopes).
def _refresh(actor, **origin):
    return {"version": 1, "op": "refresh", "command_id": _uuid(7),
            "actor": actor, "origin": {**ORIGIN, **origin}}


def test_plain_identity_and_actor_still_validate():
    assert notify_cmds.validate_int(_refresh("nurse-1")) is None


@pytest.mark.parametrize("actor", [
    " " * 200 + "nurse-1",      # the 120 cap counts the padding
    "nurse\x00-1",              # NUL never reaches the ledger
    "   ",
])
def test_actor_cap_holds_against_padding_and_nul(actor):
    assert notify_cmds.validate_int(_refresh(actor)) == "bad_actor"


def test_origin_ids_cap_counts_padding():
    req = _refresh("nurse-1", message_id="m" + " " * 80)
    assert notify_cmds.validate_int(req) == "bad_origin"
