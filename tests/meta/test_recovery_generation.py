"""M7: recovery entrypoint+4support世代のcrash injection検証(合成tmpdirのみ)。

loader・hash manifest・runtime directory・entrypoint・.prevの各対応点で
途中停止/改竄/混成を注入し、fail-closed(非0終了・旧bytes保全・冪等再試行)
を確認する。実HOME・実MCS・launchdには触れない。
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

RECOVERY = Path(__file__).resolve().parents[2] / "deployment" / "recovery"
ENTRY = "mcs_recover.py"
SUPPORTS = ("mcs_recovery_gateway.py", "mcs_recovery_core.py",
            "mcs_recovery_services.py", "mcs_recovery_database.py")


def _copy_toolchain(dest):
    """entrypoint+4support+prepareをdestへ複製する。"""
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    for name in (ENTRY, "prepare_recovery.py", *SUPPORTS):
        shutil.copy2(RECOVERY / name, dest / name)
    return dest


def _run(entry, *args, cwd=None):
    env = {"HOME": str(Path.cwd() / "nohome"), "MCS_ROOT": "/nonexistent-mcs",
           "PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    return subprocess.run([sys.executable, str(entry), *args],
                          capture_output=True, text=True, timeout=30,
                          env=env, cwd=cwd)


def _import_prepare(directory):
    """directory内のprepare_recoveryをimportして返す(contextmanager不要版)。"""
    parent = str(directory)
    sys.path.insert(0, parent)
    try:
        import prepare_recovery
        return prepare_recovery
    finally:
        sys.path.remove(parent)


def _drop_prepare():
    sys.modules.pop("prepare_recovery", None)


def _prepare(entrypoint, destination):
    """prepare_recovery.prepareをsubprocessでなくin-processで呼ぶ。"""
    module = _import_prepare(Path(entrypoint).parent)
    try:
        return module.prepare(str(entrypoint), str(destination))
    finally:
        _drop_prepare()


def _install(entrypoint, destination):
    module = _import_prepare(Path(entrypoint).parent)
    try:
        return module.install(str(entrypoint), str(destination))
    finally:
        _drop_prepare()


def _pins_of(entrypoint):
    """entrypoint bytesからSUPPORT_FILES pinsを取り出す。"""
    raw = Path(entrypoint).read_bytes()
    sys.path.insert(0, str(RECOVERY))
    try:
        import prepare_recovery
        return prepare_recovery._manifest(raw, Path(entrypoint))
    finally:
        sys.path.remove(str(RECOVERY))
        sys.modules.pop("prepare_recovery", None)


def _generation_dir(destination, entrypoint):
    pins = _pins_of(entrypoint)
    gen = hashlib.sha256(json.dumps(pins, sort_keys=True,
                    separators=(",", ":")).encode("ascii")).hexdigest()
    return Path(destination) / ("runtime-" + gen)


def _variant_entrypoint(src, dst_name, support_edits):
    """support_edits={filename: appended_bytes}の別世代entrypointを作る。"""
    src = Path(src)
    out = src.parent / dst_name
    out.mkdir()
    for name in SUPPORTS:
        raw = (src / name).read_bytes() + support_edits.get(name, b"")
        (out / name).write_bytes(raw)
    entry = (src / ENTRY).read_text()
    for name in SUPPORTS:
        old = hashlib.sha256((src / name).read_bytes()).hexdigest()
        new = hashlib.sha256((out / name).read_bytes()).hexdigest()
        if old != new:
            assert entry.count(old) == 1
            entry = entry.replace(old, new)
    (out / ENTRY).write_text(entry)
    (out / "prepare_recovery.py").write_bytes((src / "prepare_recovery.py").read_bytes())
    return out


# --- install/prepare側: 世代公開と原子置換 -------------------------------------

def test_install_publishes_content_addressed_generation(tmp_path):
    src = _copy_toolchain(tmp_path / "src")
    dst = tmp_path / "dst"
    dst.mkdir()
    assert _install(src / ENTRY, dst) is True
    gen = _generation_dir(dst, src / ENTRY)
    assert gen.is_dir()
    for name in SUPPORTS:
        assert (gen / name).read_bytes() == (src / name).read_bytes()
    assert (dst / ENTRY).read_bytes() == (src / ENTRY).read_bytes()
    assert not (dst / (ENTRY + ".prev")).exists()


def test_install_same_bytes_is_idempotent_no_prev(tmp_path):
    src = _copy_toolchain(tmp_path / "src")
    dst = tmp_path / "dst"
    dst.mkdir()
    assert _install(src / ENTRY, dst) is True
    assert _install(src / ENTRY, dst) is False
    assert not (dst / (ENTRY + ".prev")).exists()


def test_prepare_stages_bundle_without_replacing_entrypoint(tmp_path):
    src = _copy_toolchain(tmp_path / "src")
    dst = tmp_path / "dst"
    dst.mkdir()
    gen_hex = _prepare(src / ENTRY, dst)
    assert (dst / ("runtime-" + gen_hex)).is_dir()
    assert not (dst / ENTRY).exists()  # 公開前: entrypointは未置換


def test_install_keeps_prev_bytes_for_rollback(tmp_path):
    src1 = _copy_toolchain(tmp_path / "src1")
    dst = tmp_path / "dst"
    dst.mkdir()
    _install(src1 / ENTRY, dst)
    src2 = _variant_entrypoint(src1, "v2", {"mcs_recovery_core.py": b"\n# v2\n"})
    _install(src2 / ENTRY, dst)
    assert (dst / (ENTRY + ".prev")).read_bytes() == (src1 / ENTRY).read_bytes()
    assert (dst / ENTRY).read_bytes() == (src2 / ENTRY).read_bytes()
    # 2世代とも残り、entrypoint毎に自分の世代を選ぶ
    assert _generation_dir(dst, src1 / ENTRY).is_dir()
    assert _generation_dir(dst, src2 / ENTRY).is_dir()


def test_prepare_rejects_duplicate_support_manifest(tmp_path):
    src = _copy_toolchain(tmp_path / "src")
    entry = src / ENTRY
    entry.write_text(entry.read_text() + "\nSUPPORT_FILES = {}\n")
    with pytest.raises(ValueError, match="recovery_support_manifest"):
        _prepare(entry, tmp_path / "dst")


def test_prepare_rejects_support_hash_mismatch(tmp_path):
    src = _copy_toolchain(tmp_path / "src")
    (src / "mcs_recovery_core.py").write_bytes(b"tampered")
    with pytest.raises(ValueError, match="recovery_support_generation_mismatch"):
        _prepare(src / ENTRY, tmp_path / "dst")


def test_prepare_rejects_tampered_existing_generation(tmp_path):
    src = _copy_toolchain(tmp_path / "src")
    dst = tmp_path / "dst"
    dst.mkdir()
    gen_hex = _prepare(src / ENTRY, dst)
    target = dst / ("runtime-" + gen_hex) / "mcs_recovery_core.py"
    target.write_bytes(b"forged")
    with pytest.raises(ValueError, match="recovery_support_generation_mismatch"):
        _prepare(src / ENTRY, dst)
    assert target.read_bytes() == b"forged"  # 既存世代を上書き修復しない


def test_staging_leftover_does_not_affect_generation(tmp_path):
    src = _copy_toolchain(tmp_path / "src")
    dst = tmp_path / "dst"
    dst.mkdir()
    leftover = dst / ".runtime-stage-deadbeef"
    leftover.mkdir()
    (leftover / "junk").write_bytes(b"partial")
    gen_hex = _prepare(src / ENTRY, dst)
    assert (dst / ("runtime-" + gen_hex)).is_dir()


def test_install_crash_after_prev_replace_keeps_old_entrypoint(tmp_path,
                                                              monkeypatch):
    """entry_tmp→target置換の直前でcrash: .prevは新版・targetは旧版のまま。

    旧entrypointは自分の世代で動き続け、再installで収束する。
    """
    src1 = _copy_toolchain(tmp_path / "src1")
    dst = tmp_path / "dst"
    dst.mkdir()
    _install(src1 / ENTRY, dst)
    src2 = _variant_entrypoint(src1, "v2", {"mcs_recovery_core.py": b"\n# v2\n"})

    pr = _import_prepare(src2)
    real_replace = os.replace
    calls = []

    def flaky_replace(src_p, dst_p):
        calls.append((src_p, dst_p))
        # install内の2回目のreplace(entry_tmp→target)でcrashを注入
        if len(calls) == 2 and str(dst_p).endswith(ENTRY):
            raise OSError("injected crash")
        return real_replace(src_p, dst_p)

    try:
        monkeypatch.setattr(pr.os, "replace", flaky_replace)
        with pytest.raises(OSError, match="injected crash"):
            pr.install(str(src2 / ENTRY), str(dst))
    finally:
        monkeypatch.undo()
        _drop_prepare()

    # crash直前に.prevへ退避されるのは「置換される旧entrypoint」(E1)。
    # 新版E2のentrypoint bytesはどこにも残らず、準備済み世代dirだけが残る。
    assert (dst / ENTRY).read_bytes() == (src1 / ENTRY).read_bytes()
    assert (dst / (ENTRY + ".prev")).read_bytes() == (src1 / ENTRY).read_bytes()
    assert _generation_dir(dst, src1 / ENTRY).is_dir()
    assert _generation_dir(dst, src2 / ENTRY).is_dir()
    assert _run(dst / ENTRY, "--help").returncode == 0
    # 再installで冪等に収束: target=E2、.prev=直前の正常版E1
    assert _install(src2 / ENTRY, dst) is True
    assert (dst / ENTRY).read_bytes() == (src2 / ENTRY).read_bytes()
    assert (dst / (ENTRY + ".prev")).read_bytes() == (src1 / ENTRY).read_bytes()


# --- entrypoint loader側: --helpが全検証を通るprobe --------------------------------

def test_loader_runs_flat_layout(tmp_path):
    src = _copy_toolchain(tmp_path / "run")
    result = _run(src / ENTRY, "--help")
    assert result.returncode == 0
    assert "mcs_recover" in result.stdout


def test_loader_rejects_tampered_support_flat(tmp_path):
    src = _copy_toolchain(tmp_path / "run")
    core = src / "mcs_recovery_core.py"
    core.write_bytes(core.read_bytes() + b"\n# tampered\n")
    result = _run(src / ENTRY, "--help")
    assert result.returncode != 0
    assert "recovery_support" in result.stderr


def test_loader_prefers_generation_dir_and_rejects_partial(tmp_path):
    """runtime-<gen>が在ればそちらだけを見る。3/4欠落+flat完備でも失敗。"""
    src = _copy_toolchain(tmp_path / "src")
    dst = tmp_path / "dst"
    dst.mkdir()
    _install(src / ENTRY, dst)
    # flatにも正しいsupportを置く — bundled優先ならなお失敗するはず
    for name in SUPPORTS:
        shutil.copy2(src / name, dst / name)
    gen = _generation_dir(dst, src / ENTRY)
    (gen / SUPPORTS[0]).unlink()  # generation側を1件欠落させる
    result = _run(dst / ENTRY, "--help")
    assert result.returncode != 0


def test_loader_rejects_symlinked_support(tmp_path):
    src = _copy_toolchain(tmp_path / "run")
    target = src / "mcs_recovery_core.py"
    moved = src / "core.real"
    target.rename(moved)
    target.symlink_to(moved.name)
    result = _run(src / ENTRY, "--help")
    assert result.returncode != 0


def test_loader_rejects_oversized_support(tmp_path):
    src = _copy_toolchain(tmp_path / "run")
    big = src / "mcs_recovery_database.py"
    big.write_bytes(b"x" * 262145)
    result = _run(src / ENTRY, "--help")
    assert result.returncode != 0


def test_prev_entrypoint_selects_only_its_own_generation(tmp_path):
    """.prevは旧pins→旧runtime-<gen>のみ。旧世代削除で.prevは失敗、現行は動く。"""
    src1 = _copy_toolchain(tmp_path / "src1")
    dst = tmp_path / "dst"
    dst.mkdir()
    _install(src1 / ENTRY, dst)
    src2 = _variant_entrypoint(src1, "v2", {"mcs_recovery_core.py": b"\n# v2\n"})
    _install(src2 / ENTRY, dst)

    assert _run(dst / ENTRY, "--help").returncode == 0
    assert _run(dst / (ENTRY + ".prev"), "--help").returncode == 0

    old_gen = _generation_dir(dst, src1 / ENTRY)
    shutil.rmtree(old_gen)
    # .prevは自分の世代を失いflat fallbackも無い → fail-closed
    assert _run(dst / (ENTRY + ".prev"), "--help").returncode != 0
    # 現行entrypointは新世代で継続
    assert _run(dst / ENTRY, "--help").returncode == 0
