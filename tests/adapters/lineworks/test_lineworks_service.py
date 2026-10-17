"""Service candidates preserve literal paths and never install shared services."""
from pathlib import Path
import plistlib
import shlex
import shutil
import subprocess

import pytest

from adapters.lineworks import service


def fixture_paths(tmp_path, name='checkout space %i $HOME "quote"'):
    root = tmp_path / name
    root.mkdir(parents=True)
    python = tmp_path / 'venv space %i $HOME "quote"' / "python"
    python.parent.mkdir()
    python.write_text("#!/bin/sh\nexit 0\n")
    python.chmod(0o700)
    return root, python


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_generate_private_literal_candidates(tmp_path, monkeypatch, platform):
    root, python = fixture_paths(tmp_path)
    monkeypatch.setattr(service.os, "system", lambda *a: pytest.fail("no installation"))
    candidate = service.generate(root, python_executable=python, platform=platform)
    entry = Path(service.__file__).resolve().parents[2] / "lineworks_adapter" / "__main__.py"
    expected = [str(python), str(entry), "run", "--root", str(root)]
    assert not (root / "lineworks_adapter").exists()
    assert candidate.parent == root / "data" / "lineworks-service"
    assert candidate.stat().st_mode & 0o777 == 0o600
    assert (root / "data" / "lineworks_state").stat().st_mode & 0o777 == 0o700
    assert candidate.parent.stat().st_mode & 0o777 == 0o700
    if platform == "darwin":
        obj = plistlib.loads(candidate.read_bytes())
        assert obj["ProgramArguments"] == expected
        assert obj["WorkingDirectory"] == str(root)
        assert obj["RunAtLoad"] and obj["KeepAlive"]
        assert obj["Umask"] == 0o077
    else:
        text = candidate.read_text()
        line = next(v.removeprefix("ExecStart=") for v in text.splitlines() if v.startswith("ExecStart="))
        # Model the documented quote unescaping followed by literal %%/$$ expansion.
        assert [v.replace("%%", "%").replace("$$", "$") for v in shlex.split(line)] == expected
        assert f"WorkingDirectory={str(root).replace('%', '%%')}/.\n" in text
        assert "StandardOutput=append:" in text and "Restart=always\n" in text
    old = candidate.read_bytes()
    assert service.generate(root, python_executable=python, platform=platform) == candidate
    assert candidate.read_bytes() == old


def test_preserves_venv_symlink_executable(tmp_path):
    root, python = fixture_paths(tmp_path, "checkout")
    symlink = python.parent / "python3"
    symlink.symlink_to(python)
    result = plistlib.loads(service.generate(root, python_executable=symlink, platform="darwin").read_bytes())
    assert result["ProgramArguments"][0] == str(symlink)


def test_generated_plist_passes_native_validation(tmp_path):
    tool = shutil.which("plutil")
    if tool is None:
        pytest.skip("plutil unavailable")
    root, python = fixture_paths(tmp_path)
    candidate = service.generate(root, python_executable=python, platform="darwin")
    result = subprocess.run([tool, "-lint", str(candidate)], capture_output=True, timeout=5)
    assert result.returncode == 0


@pytest.mark.parametrize("name", ["checkout trailing ", "checkout backslash\\"])
def test_systemd_path_directive_never_ends_with_space_or_escape(tmp_path, name):
    root, python = fixture_paths(tmp_path, name)
    candidate = service.generate(root, python_executable=python, platform="linux")
    line = next(line for line in candidate.read_text().splitlines() if line.startswith("WorkingDirectory="))
    assert line == f"WorkingDirectory={root}/."


@pytest.mark.parametrize("name", ["bad\nExecStart=/bin/evil", "bad\r", "bad\t"])
def test_rejects_unit_line_injection_before_writes(tmp_path, name):
    root, python = fixture_paths(tmp_path, name)
    with pytest.raises(ValueError, match="paths_invalid"):
        service.generate(root, python_executable=python, platform="linux")
    assert not (root / "data").exists()


def test_rejects_unsupported_platform_and_missing_interpreter(tmp_path):
    root, python = fixture_paths(tmp_path)
    with pytest.raises(ValueError, match="platform_unsupported"):
        service.generate(root, platform="win32")
    with pytest.raises(ValueError, match="paths_invalid"):
        service.generate(root, python_executable=Path("/synthetic/nonexistent/python"), platform="linux")
    assert not (root / "data").exists()


def test_does_not_follow_state_directory_symlink(tmp_path):
    root, python = fixture_paths(tmp_path)
    external = tmp_path / "external"
    external.mkdir()
    (root / "data").mkdir()
    (root / "data" / "lineworks_state").symlink_to(external, target_is_directory=True)
    with pytest.raises(ValueError, match="paths_invalid"):
        service.generate(root, python_executable=python, platform="linux")
    assert list(external.iterdir()) == []
