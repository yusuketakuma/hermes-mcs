"""install.sh guards: IA-022 recovery directory mode and IA-012 runnable fallback Python."""
import os
import re
import subprocess

import pytest

from install_sh_testkit import INSTALL


def _loose(directory):
    source = INSTALL.read_text()
    function = re.search(r"^recovery_dir_loose\(\) \{.*?^\}\n", source, re.M | re.S).group(0)
    result = subprocess.run(["/bin/sh", "-c", function + 'recovery_dir_loose'],
                            env={**os.environ, "RECOVERY_DIR": str(directory)}, timeout=10)
    return result.returncode == 0


@pytest.mark.parametrize("mode, loose", [(0o755, True), (0o700, False), (0o775, False), (0o757, False)])
def test_only_an_own_directory_without_group_or_other_write_is_tightened(tmp_path, mode, loose):
    directory = tmp_path / "recovery"
    directory.mkdir()
    directory.chmod(mode)
    assert _loose(directory) is loose


def test_links_and_missing_directories_are_left_alone(tmp_path):
    target = tmp_path / "real"
    target.mkdir()
    target.chmod(0o755)
    link = tmp_path / "link"
    link.symlink_to(target)
    assert not _loose(link)
    assert not _loose(tmp_path / "missing")


def test_install_tightens_before_the_first_recovery_check_and_preflight_does_not_change():
    source = INSTALL.read_text()
    tighten = source.index('chmod 700 "$RECOVERY_DIR"')
    check = source.index('RECOVERY_PYTHON="$(recovery_runtime_ok "$_probe_host")"')
    assert source.index('if [ "$MODE" != install ]; then\n    preflight') < tighten < check
    preflight = source[source.index("preflight() {"):source.index('if [ "$MODE" != install ]; then\n    preflight')]
    assert "chmod" not in preflight


def _runnable(tmp_path, preferred, system_python):
    source = INSTALL.read_text()
    function = re.search(r"^runnable_py\(\) \{.*?^\}\n", source, re.M | re.S).group(0)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    stub = bin_dir / "python3"
    stub.write_text(f"#!/bin/sh\nexit {0 if system_python else 1}\n")
    stub.chmod(0o755)
    result = subprocess.run(["/bin/sh", "-c", function + 'runnable_py "$1"', "sh", str(preferred)],
                            env={"PATH": f"{bin_dir}:/bin"}, capture_output=True, text=True, timeout=10)
    return result.returncode, result.stdout.strip()


def test_fallback_python_must_start_before_it_is_used(tmp_path):
    """IA-012/GA-bootstrap-2: a python3 that cannot start is not blamed on config.json."""
    broken = tmp_path / "venv-python"
    broken.write_text("#!/bin/sh\nexit 1\n")
    broken.chmod(0o755)
    assert _runnable(tmp_path, broken, True) == (0, str(tmp_path / "bin" / "python3"))
    assert _runnable(tmp_path, broken, False)[0] == 1
    assert _runnable(tmp_path, tmp_path / "missing", False)[0] == 1
