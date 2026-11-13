"""IA-022: install.sh tightens an own 0755 ~/.mcs-recovery but never a shared or linked one."""
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
