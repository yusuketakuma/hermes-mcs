"""Installer companions are exact and rejected before changing synthetic install state."""
import hashlib
import re
import shutil

import pytest

from install_sh_testkit import INSTALL, ROOT, _run, _world


def _snapshot(root):
    if not root.exists():
        return None
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.rglob("*") if path.is_file() and not path.is_symlink()}


def test_runtime_stage_is_pinned_and_companions_precede_entrypoint_publication():
    source = INSTALL.read_text()
    pin = re.search(r'^INSTALL_RUNTIME_SHA256="([0-9a-f]{64})"$', source, re.M).group(1)
    helper = ROOT / "deployment/install/runtime.sh"
    assert hashlib.sha256(helper.read_bytes()).hexdigest() == pin
    stages = helper.read_text()
    assert 'cp "$RECOVERY_SRC"' not in stages
    assert '_rchanged="$("$MCS_PY"' in stages
    assert '. "$INSTALL_RUNTIME_FIXED"' in source
    assert '256 "$INSTALL_RUNTIME_FIXED"' in source
    assert 'if [ "$MODE" = install ]; then\n    [ -f "$INSTALL_RUNTIME_STAGE" ]' in source
    assert source.index('INSTALL_RUNTIME_SHA256=') < source.index('_fresh_marker=')


@pytest.mark.parametrize("fault", ["missing", "modified", "symlink"])
def test_runtime_stage_mismatch_keeps_existing_install_state(tmp_path, fault):
    home, hermes_home, _stub_root, env = _world(tmp_path)
    checkout = tmp_path / "partial-checkout"
    checkout.mkdir()
    script = checkout / "install.sh"
    shutil.copyfile(INSTALL, script)
    (checkout / "mcs").symlink_to(ROOT / "mcs", target_is_directory=True)
    directory = checkout / "deployment/install"
    directory.mkdir(parents=True)
    stage = directory / "runtime.sh"
    if fault == "modified":
        stage.write_bytes((ROOT / "deployment/install/runtime.sh").read_bytes() + b"\n# synthetic mismatch\n")
    elif fault == "symlink":
        stage.symlink_to(ROOT / "deployment/install/runtime.sh")
    target = home / ".mcs"
    before = _snapshot(target)
    result = _run(env, hermes_home, script=script)
    assert result.returncode == 1
    assert "補助ファイル" in result.stderr or "世代" in result.stderr
    assert _snapshot(target) == before
    assert not (target / "data/.provider-bootstrap-fresh.json").exists()


def test_stage_replacement_after_hash_never_changes_executed_bytes(tmp_path):
    home, hermes_home, stub, env = _world(tmp_path)
    checkout = tmp_path / "race-checkout"
    checkout.mkdir()
    script = checkout / "install.sh"
    shutil.copyfile(INSTALL, script)
    (checkout / "mcs").symlink_to(ROOT / "mcs", target_is_directory=True)
    deployment = checkout / "deployment"
    shutil.copytree(ROOT / "deployment", deployment)
    stage = deployment / "install/runtime.sh"
    marker = tmp_path / "unverified-execution"
    private = tmp_path / "private-tmp"
    private.mkdir(mode=0o700)
    real_sum = shutil.which("shasum")
    assert real_sum
    wrapper = stub / "bin/shasum"
    wrapper.write_text("""#!/bin/sh
"$STUB_REAL_SUM" "$@" || exit $?
printf 'printf "unverified" > "%s"\\nexit 42\\n' "$STUB_UNVERIFIED_MARKER" > "$STUB_SWAP_STAGE"
""")
    wrapper.chmod(0o700)
    env.update(TMPDIR=str(private), STUB_REAL_SUM=real_sum, STUB_SWAP_STAGE=str(stage),
               STUB_UNVERIFIED_MARKER=str(marker))
    result = _run(env, hermes_home, "--no-brew", "--no-llm", "--no-plugin",
                  "--no-services", "--no-recovery", script=script)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not marker.exists()
    assert stage.read_text().endswith("exit 42\n")
    assert not list(private.glob("mcs-install-runtime.*"))
    assert (home / ".mcs/config.json").is_file()
