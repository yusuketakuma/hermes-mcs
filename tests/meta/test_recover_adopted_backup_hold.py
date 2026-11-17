"""IA-099: an adopted backup whose writers verifiably resumed is not a consent hold."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from mcs_recover_testkit import rec  # noqa: F401  (fixture)

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("resumed, held", [(True, False), (False, True)])
def test_watchdog_follows_the_checkout_proof_for_an_adopted_marker(rec, tmp_path, monkeypatch, resumed, held):  # noqa: F811
    data = tmp_path / "data"
    (data / "restore_pending.json").write_text(json.dumps({"by": "mcs_backup_restore", "phase": "awaiting_consent"}))
    asked = []
    monkeypatch.setattr(rec.mcs_recovery_gateway, "_adoption_resumed",
                        lambda path: asked.append(path) or resumed)
    assert (rec.mcs_recovery_core._awaiting_consent() is not None) is held
    assert asked == [str(data)]


def test_other_awaiting_markers_never_ask_the_checkout(rec, tmp_path, monkeypatch):  # noqa: F811
    (tmp_path / "data" / "restore_pending.json").write_text(json.dumps({"by": "mcs_update", "phase": "awaiting_consent"}))
    monkeypatch.setattr(rec.mcs_recovery_gateway, "_adoption_resumed", lambda path: pytest.fail("not adoption"))
    assert rec.mcs_recovery_core._awaiting_consent() is not None


def test_resumed_check_fails_closed_without_an_adoption(tmp_path):
    data = tmp_path / "data"
    data.mkdir(mode=0o700)
    result = subprocess.run([sys.executable, "-I", "-B", str(ROOT / "mcs/ops/mcs_restore.py"),
                             "resumed", "--destination", str(data)], capture_output=True, text=True, timeout=60)
    assert result.returncode == 1
    assert json.loads(result.stdout) == {"ok": True, "writers_resumed": False}


@pytest.mark.parametrize("output, code, resumed", [
    ('{"ok": true, "writers_resumed": true}', 0, True),
    ('{"ok": true, "writers_resumed": false}', 1, False),
    ('{"ok": true, "writers_resumed": true}', 1, False),
])
def test_adoption_resumed_accepts_only_the_exact_checkout_proof(rec, tmp_path, output, code, resumed):  # noqa: F811
    data = tmp_path / "root" / "data"
    data.mkdir(parents=True)
    script = tmp_path / "root" / "mcs" / "ops" / "mcs_restore.py"
    script.parent.mkdir(parents=True)
    script.write_text(f"import sys\nprint({output!r})\nsys.exit({code})\n")
    assert rec.mcs_recovery_gateway._adoption_resumed(str(data)) is resumed


def test_gateway_helper_proof_is_frozen_into_the_detached_program(rec):  # noqa: F811
    program = rec.mcs_recovery_gateway._GATEWAY_RESTART_PROGRAM
    assert "def _adoption_resumed(data):" in program and "_adoption_resumed(data)" in program
    compile(program, "gateway-restart", "exec")
