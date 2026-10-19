"""Runtime inspection and installer recovery gates use synthetic machine facts."""
import json
from pathlib import Path
import plistlib
import sys
from types import SimpleNamespace

import pytest

import mcs_setup
import runtime_inspection


def _facts(sqlite="3.51.3"):
    return {"python": "3.14.8", "sqlite": sqlite,
            "packages": {"discord.py": None, "slack-bolt": None, "slack-sdk": None}}


def test_probe_observes_each_call_without_retaining_runtime_state(tmp_path, monkeypatch):
    executable = tmp_path / "synthetic-python"
    executable.touch(mode=0o700)
    replies = iter((_facts(), _facts("3.51.0")))
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout=json.dumps(next(replies)))

    monkeypatch.setattr(runtime_inspection, "run_bounded", run)
    assert runtime_inspection.probe_python(str(executable)) == _facts()
    assert runtime_inspection.probe_python(str(executable)) == _facts("3.51.0")
    assert len(calls) == 2
    assert all(argv[:3] == [str(executable), "-I", "-c"]
               and kwargs == {"timeout": 20} for argv, kwargs in calls)


def _installer_recovery_script():
    source = (Path(__file__).resolve().parents[2] / "install.sh").read_text()
    function = source.split("recovery_runtime_ok() {", 1)[1].split("\nloaded()", 1)[0]
    return function.split('"$1" -I -B -c \'', 1)[1].split("\n' \"$REPO\"", 1)[0]


@pytest.mark.parametrize("case", ["safe", "unsafe", "template_drift", "deployed_drift"])
def test_installer_recovery_gate_uses_public_inspection_without_real_commands(
        tmp_path, monkeypatch, capsys, case):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    selected = str(tmp_path / "independent-python")
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"recovery_python": selected}))
    agents = tmp_path / "agents"
    agents.mkdir()
    monkeypatch.setattr(mcs_setup, "AGENTS_DIR", str(agents))
    if case == "deployed_drift":
        (agents / "org.mcs.recovery.plist").write_bytes(plistlib.dumps(
            {"ProgramArguments": [str(tmp_path / "different-python"), "recovery.py"]}))
    template = tmp_path / "watchdog.plist"
    template.write_bytes(plistlib.dumps({"ProgramArguments": [
        str(tmp_path / "different-template-python") if case == "template_drift"
        else "__RECOVERY_PYTHON__", "recovery.py"]}))
    probes = []
    monkeypatch.setattr(runtime_inspection, "probe_python", lambda exe:
                        probes.append(exe) or _facts("3.51.0" if case == "unsafe" else "3.51.3"))
    monkeypatch.setattr(runtime_inspection, "run_bounded",
                        lambda *a, **k: pytest.fail("installer attempted a real runtime command"))
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setattr(sys, "argv", ["synthetic-probe", str(checkout), str(template),
                                     str(config), "", "0"])
    script = compile(_installer_recovery_script(), "<synthetic-installer-recovery>", "exec")
    if case == "safe":
        exec(script, {})
        assert capsys.readouterr().out.strip() == selected
        assert probes == [selected]
    else:
        with pytest.raises(SystemExit) as caught:
            exec(script, {})
        assert caught.value.code != 0
        assert probes == ([selected] if case == "unsafe" else [])
