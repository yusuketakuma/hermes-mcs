"""Synthetic install sh models contracts; shared helpers preserve isolated runtime state."""
import hashlib
import json
import re
import pytest
from install_sh_testkit import INSTALL as INSTALL, _bootstraps as _bootstraps, _calls as _calls, _run as _run, _world as _world


def test_llama_stage_when_models_endpoint_unanswered(tmp_path):
    """The whole llama branch is exercised: model 'download' via stub
    curl, plist render, bootstrap — then rerun skips cleanly."""
    home, hermes_home, stub_root, env = _world(tmp_path)
    r = _run(env, hermes_home)
    assert r.returncode == 0, r.stderr
    model = hermes_home / "models" / "Qwen3.5-9B-Q4_K_M.gguf"
    assert model.read_text() == "stub-model-bytes\n"
    # second run: model exists + launchd label loaded -> quiet
    r2 = _run(env, hermes_home)
    assert r2.returncode == 0, r2.stderr
    assert len(_bootstraps(stub_root, "ai.mcs.llamaserver")) == 1


@pytest.mark.parametrize(("mem_gib", "chip", "filename", "model_name"), [
    (16, "Apple M4", "Qwen3.5-9B-Q4_K_M.gguf", "Qwen3.5-9B"),
    (32, "Apple M4", "Qwen3.8-27B-UD-Q4_K_M.gguf", "Qwen3.8-27B"),
    (48, "Apple M4 Pro", "Qwen3.6-35B-A3B-UD-Q4_K_M.gguf", "Qwen3.6-35B-A3B"),
    (128, "Apple M4 Pro", "Qwen3.6-35B-A3B-UD-Q4_K_M.gguf", "Qwen3.6-35B-A3B"),
])
def test_mac_memory_and_chip_select_qwen_model(tmp_path, mem_gib, chip, filename, model_name):
    home, hermes_home, stub_root, env = _world(tmp_path)
    env = {**env, "STUB_MEMSIZE": str(mem_gib * 1024**3), "STUB_CPU_BRAND": chip}
    r = _run(env, hermes_home)
    assert r.returncode == 0, r.stderr
    assert (hermes_home / "models" / filename).read_text() == "stub-model-bytes\n"
    plist = home / "Library" / "LaunchAgents" / "ai.mcs.llamaserver.plist"
    assert filename in plist.read_text()
    cfg = json.loads((home / ".mcs" / "config.json").read_text())
    assert cfg["local_llm"]["model"] == model_name


def test_explicit_model_choice_overrides_hardware_detection(tmp_path):
    home, hermes_home, stub_root, env = _world(tmp_path)
    env = {**env, "STUB_MEMSIZE": str(16 * 1024**3), "STUB_CPU_BRAND": "Apple M4",
           "MCS_LOCAL_MODEL": "QWEN3.6-35B-A3B"}
    r = _run(env, hermes_home)
    assert r.returncode == 0, r.stderr
    assert (hermes_home / "models" / "Qwen3.6-35B-A3B-UD-Q4_K_M.gguf").read_text() == "stub-model-bytes\n"
    cfg = json.loads((home / ".mcs" / "config.json").read_text())
    assert cfg["local_llm"]["model"] == "Qwen3.6-35B-A3B"


def test_invalid_explicit_model_choice_is_rejected_before_changes(tmp_path):
    home, hermes_home, stub_root, env = _world(tmp_path)
    r = _run({**env, "MCS_LOCAL_MODEL": "unsupported"}, hermes_home)
    assert r.returncode == 1
    assert "MCS_LOCAL_MODEL の指定が不正です" in r.stderr
    assert not (hermes_home / "models").exists()


def test_max_chip_with_128gb_selects_split_122b_model(tmp_path):
    home, hermes_home, stub_root, env = _world(tmp_path)
    env = {**env, "STUB_MEMSIZE": str(128 * 1024**3), "STUB_CPU_BRAND": "Apple M4 Max"}
    r = _run(env, hermes_home)
    assert r.returncode == 0, r.stderr
    models = hermes_home / "models"
    for number in range(1, 4):
        assert (models / f"Qwen3.5-122B-A10B-Q4_K_M-0000{number}-of-00003.gguf").read_text() == "stub-model-bytes\n"
    plist = home / "Library" / "LaunchAgents" / "ai.mcs.llamaserver.plist"
    assert "Qwen3.5-122B-A10B-Q4_K_M-00001-of-00003.gguf" in plist.read_text()
    cfg = json.loads((home / ".mcs" / "config.json").read_text())
    assert cfg["local_llm"]["model"] == "Qwen3.5-122B-A10B"


def test_ultra_chip_with_512gb_selects_split_397b_model(tmp_path):
    home, hermes_home, stub_root, env = _world(tmp_path)
    env = {**env, "STUB_MEMSIZE": str(512 * 1024**3), "STUB_CPU_BRAND": "Apple M3 Ultra"}
    r = _run(env, hermes_home)
    assert r.returncode == 0, r.stderr
    models = hermes_home / "models"
    for number in range(1, 7):
        assert (models / f"Qwen3.5-397B-A17B-Q4_K_M-0000{number}-of-00006.gguf").read_text() == "stub-model-bytes\n"
    plist = home / "Library" / "LaunchAgents" / "ai.mcs.llamaserver.plist"
    assert "Qwen3.5-397B-A17B-Q4_K_M-00001-of-00006.gguf" in plist.read_text()
    cfg = json.loads((home / ".mcs" / "config.json").read_text())
    assert cfg["local_llm"]["model"] == "Qwen3.5-397B-A17B"


def test_existing_qwen_model_is_kept_on_high_memory_mac(tmp_path):
    home, hermes_home, stub_root, env = _world(tmp_path)
    model = hermes_home / "models" / "Qwen3.5-9B-Q4_K_M.gguf"
    model.parent.mkdir(parents=True)
    model.write_bytes(b"stub-model-bytes\n")
    env = {**env, "STUB_MEMSIZE": str(128 * 1024**3), "STUB_CPU_BRAND": "Apple M4 Max"}
    r = _run(env, hermes_home)
    assert r.returncode == 0, r.stderr
    assert model.read_bytes() == b"stub-model-bytes\n"
    assert not (hermes_home / "models" / "Qwen3.5-122B-A10B-Q4_K_M-00001-of-00003.gguf").exists()
    plist = home / "Library" / "LaunchAgents" / "ai.mcs.llamaserver.plist"
    assert "Qwen3.5-9B-Q4_K_M.gguf" in plist.read_text()


def test_active_plist_model_wins_over_larger_cached_model(tmp_path):
    """A rerun must keep the model the launch agent actually loads: a larger
    GGUF that merely sits in the models directory is not a switch."""
    home, hermes_home, stub_root, env = _world(tmp_path)
    r = _run(env, hermes_home)
    assert r.returncode == 0, r.stderr
    plist = home / "Library" / "LaunchAgents" / "ai.mcs.llamaserver.plist"
    assert "Qwen3.5-9B-Q4_K_M.gguf" in plist.read_text()
    bigger = hermes_home / "models" / "Qwen3.8-27B-UD-Q4_K_M.gguf"
    bigger.write_bytes(b"stub-model-bytes\n")
    env = {**env, "STUB_MEMSIZE": str(128 * 1024**3), "STUB_CPU_BRAND": "Apple M4 Max"}
    r2 = _run(env, hermes_home)
    assert r2.returncode == 0, r2.stderr
    text = plist.read_text()
    assert "Qwen3.5-9B-Q4_K_M.gguf" in text
    assert "Qwen3.8-27B-UD-Q4_K_M.gguf" not in text


def test_larger_cached_model_is_used_without_an_active_agent(tmp_path):
    """Without a launch agent the largest complete cached set is still kept,
    so a manually prepared model is not downloaded again."""
    home, hermes_home, stub_root, env = _world(tmp_path)
    models = hermes_home / "models"
    models.mkdir(parents=True)
    for name in ("Qwen3.5-9B-Q4_K_M.gguf", "Qwen3.8-27B-UD-Q4_K_M.gguf"):
        (models / name).write_bytes(b"stub-model-bytes\n")
    r = _run(env, hermes_home)
    assert r.returncode == 0, r.stderr
    plist = home / "Library" / "LaunchAgents" / "ai.mcs.llamaserver.plist"
    assert "Qwen3.8-27B-UD-Q4_K_M.gguf" in plist.read_text()


def test_llama_plist_change_reloads_only_when_not_serving(tmp_path):
    """F9/F13: a changed plist is applied when the agent is loaded but
    not serving; a serving one is kept with an exact reload hint."""
    home, hermes_home, stub_root, env = _world(tmp_path)
    assert _run(env, hermes_home).returncode == 0
    plist = home / "Library" / "LaunchAgents" / "ai.mcs.llamaserver.plist"
    plist.write_text(plist.read_text().replace("49152", "4096"))
    r = _run(env, hermes_home)
    assert r.returncode == 0, r.stderr
    assert len(_bootstraps(stub_root, "ai.mcs.llamaserver")) == 2
    assert "49152" in plist.read_text()
    plist.write_text(plist.read_text().replace("49152", "4096"))
    r = _run({**env, "STUB_LLM_SERVING": "1"}, hermes_home)
    assert r.returncode == 0, r.stderr
    assert "起動設定を更新しました" in r.stderr and "launchctl bootout" in r.stderr
    assert len(_bootstraps(stub_root, "ai.mcs.llamaserver")) == 2
    # The deferred reload is not forgotten once the plist on disk is current.
    r = _run({**env, "STUB_LLM_SERVING": "1"}, hermes_home)
    assert r.returncode == 0, r.stderr
    assert "まだ反映されていません" in r.stderr and "起動設定にも変更はありません" not in r.stdout
    assert len(_bootstraps(stub_root, "ai.mcs.llamaserver")) == 2
    r = _run(env, hermes_home)
    assert r.returncode == 0, r.stderr
    assert len(_bootstraps(stub_root, "ai.mcs.llamaserver")) == 3
    r = _run(env, hermes_home)
    assert r.returncode == 0, r.stderr
    assert "起動設定にも変更はありません" in r.stdout
    assert len(_bootstraps(stub_root, "ai.mcs.llamaserver")) == 3


def test_model_download_resumes_from_part(tmp_path):
    """F15: a dropped download keeps .part; the re-run resumes it."""
    home, hermes_home, stub_root, env = _world(tmp_path)
    r1 = _run({**env, "STUB_FAIL_MODEL_ONCE": "1"}, hermes_home)
    assert r1.returncode == 1 and "再開します" in r1.stderr
    part = hermes_home / "models" / "Qwen3.5-9B-Q4_K_M.gguf.part"
    assert part.read_text() == "stub-model-"
    r2 = _run(env, hermes_home)
    assert r2.returncode == 0, r2.stderr
    model = hermes_home / "models" / "Qwen3.5-9B-Q4_K_M.gguf"
    assert model.read_text() == "stub-model-bytes\n"
    dl = [c for c in _calls(stub_root) if c.startswith("curl") and ".part" in c]
    assert dl and all("-C -" in c and "--retry" in c and "--speed-time" in c for c in dl)
    heads = [c for c in _calls(stub_root) if c.startswith("curl -fsIL")]
    assert heads and all(" -m " in c for c in heads)


def test_model_download_stops_before_filling_the_volume(tmp_path):
    home, hermes_home, stub_root, env = _world(tmp_path)
    r = _run({**env, "STUB_DF_KB": "1000"}, hermes_home)
    assert r.returncode == 1
    assert "空き容量が不足" in r.stderr
    assert not [c for c in _calls(stub_root) if c.startswith("curl") and " -o " in c]
    assert not (hermes_home / "models" / "Qwen3.5-9B-Q4_K_M.gguf.part").exists()


def test_complete_part_is_not_requested_again(tmp_path):
    """A .part that already holds every byte (cut between transfer and
    rename) is renamed, not re-requested — a range past the end answers
    416 and would fail every re-run."""
    home, hermes_home, stub_root, env = _world(tmp_path)
    models = hermes_home / "models"
    models.mkdir(parents=True)
    part = models / "Qwen3.5-9B-Q4_K_M.gguf.part"
    part.write_text("complete-model")
    r = _run({**env, "STUB_MODEL_SIZE": str(part.stat().st_size),
              "MCS_MODEL_SHA256": hashlib.sha256(b"complete-model").hexdigest()},
             hermes_home)
    assert r.returncode == 0, r.stderr
    assert (models / "Qwen3.5-9B-Q4_K_M.gguf").read_text() == "complete-model"
    assert not [c for c in _calls(stub_root)
                if c.startswith("curl") and "-C -" in c]


def test_dry_run_plans_no_second_llm_when_hermes_manages_it(tmp_path):
    """The plan mirrors stage 4: with a hermes-managed agent present it
    must not announce a model download or a new ai.mcs.llamaserver."""
    home, hermes_home, stub_root, env = _world(tmp_path)
    agents = home / "Library" / "LaunchAgents"
    agents.mkdir(parents=True, exist_ok=True)
    (agents / "ai.hermes.llamacpp.plist").write_text("<plist/>")
    r = _run(env, hermes_home, "--dry-run")
    plan = r.stdout[r.stdout.index("=== 導入予定"):]
    assert "Hermes 管理の" in plan and "を保持" in plan
    assert "ai.mcs.llamaserver" not in plan


def test_model_checksum_mismatch_is_fatal(tmp_path):
    home, hermes_home, stub_root, env = _world(tmp_path)
    r = _run({**env, "MCS_MODEL_SHA256": "0" * 64}, hermes_home)
    assert r.returncode == 1 and "モデルの整合性が確認できません" in r.stderr
    assert not (hermes_home / "models" / "Qwen3.5-9B-Q4_K_M.gguf").exists()


@pytest.mark.parametrize("model_bytes", [b"", b"corrupt-existing-model"])
def test_existing_invalid_model_is_preserved_and_stops_before_llm_publication(tmp_path, model_bytes):
    home, hermes_home, stub_root, env = _world(tmp_path, configured=True)
    models = hermes_home / "models"
    models.mkdir(parents=True)
    model = models / "Qwen3.5-9B-Q4_K_M.gguf"
    model.write_bytes(model_bytes)
    part = model.with_suffix(".gguf.part")
    part.write_bytes(b"prior-part-must-remain")
    agents = home / "Library" / "LaunchAgents"
    agents.mkdir(parents=True)
    plist = agents / "ai.mcs.llamaserver.plist"
    plist.write_bytes(b"prior-plist-must-remain")
    r = _run(env, hermes_home)
    assert r.returncode == 1 and "既存モデルの整合性が確認できません" in r.stderr
    assert "ファイルは保持しました" in r.stderr
    assert model.read_bytes() == model_bytes
    assert part.read_bytes() == b"prior-part-must-remain"
    assert plist.read_bytes() == b"prior-plist-must-remain"
    assert not plist.with_suffix(".plist.tmp").exists()
    assert not _bootstraps(stub_root, "ai.mcs.llamaserver")
    assert not (home / ".mcs-recovery").exists()
    calls = _calls(stub_root)
    assert not any(c.startswith("curl") and ("huggingface.co" in c or ".part" in c) for c in calls)
    assert not any("mcs_setup.py services" in c for c in calls)


def test_existing_valid_model_is_verified_without_retrieval(tmp_path):
    home, hermes_home, stub_root, env = _world(tmp_path)
    model = hermes_home / "models" / "Qwen3.5-9B-Q4_K_M.gguf"
    model.parent.mkdir(parents=True)
    model.write_bytes(b"stub-model-bytes\n")
    r = _run(env, hermes_home)
    assert r.returncode == 0, r.stderr
    assert "AI モデルは取得済みで、整合性を確認しました" in r.stdout
    assert model.read_bytes() == b"stub-model-bytes\n"
    assert len(_bootstraps(stub_root, "ai.mcs.llamaserver")) == 1
    assert not any(c.startswith("curl") and "huggingface.co" in c for c in _calls(stub_root))


def test_existing_model_hash_override_still_requires_explicit_opt_in(tmp_path):
    home, hermes_home, stub_root, env = _world(tmp_path)
    model = hermes_home / "models" / "Qwen3.5-9B-Q4_K_M.gguf"
    model.parent.mkdir(parents=True)
    model.write_bytes(b"stub-model-bytes\n")
    del env["MCS_ALLOW_MODEL_SHA_OVERRIDE"]
    r = _run(env, hermes_home)
    assert r.returncode == 1 and "既存モデルの整合性が確認できません" in r.stderr
    assert "MCS_ALLOW_MODEL_SHA_OVERRIDE=1 がないため無視" in r.stderr
    assert model.read_bytes() == b"stub-model-bytes\n"
    assert not _bootstraps(stub_root, "ai.mcs.llamaserver")


@pytest.mark.parametrize("owner", ["opt-out", "hermes", "external"])
def test_non_mcs_model_ownership_does_not_inspect_or_replace_existing_model(tmp_path, owner):
    home, hermes_home, stub_root, env = _world(tmp_path)
    model = hermes_home / "models" / "Qwen3.5-9B-Q4_K_M.gguf"
    model.parent.mkdir(parents=True)
    model.write_bytes(b"unmanaged-model")
    flags = ()
    if owner == "opt-out":
        flags = ("--no-llm",)
    elif owner == "hermes":
        agents = home / "Library" / "LaunchAgents"
        agents.mkdir(parents=True)
        (agents / "ai.hermes.llamacpp.plist").write_text("<plist/>")
    else:
        env["STUB_LLM_SERVING"] = "1"
    r = _run(env, hermes_home, *flags)
    assert r.returncode == 0, r.stderr
    assert model.read_bytes() == b"unmanaged-model"
    assert not _bootstraps(stub_root, "ai.mcs.llamaserver")
    assert not (home / "Library" / "LaunchAgents" / "ai.mcs.llamaserver.plist").exists()
    assert not any(c.startswith("curl") and "huggingface.co" in c for c in _calls(stub_root))


def test_model_download_is_pinned_and_verified_by_default(tmp_path):
    """The GGUF comes from a fixed revision and is checked against its
    published sha256 even when MCS_MODEL_SHA256 is not set."""
    text = INSTALL.read_text()
    pinned = re.findall(
        r"(?:^|\n|')([^\s'\n]+\.gguf)\|(https://huggingface\.co/[^|\n]+/resolve/[0-9a-f]{40}/[^|\n]+\.gguf)\|([0-9a-f]{64})\|([0-9]+)",
        text)
    assert len(pinned) == 12
    assert all(len(entry[2]) == 64 and int(entry[3]) > 0 for entry in pinned)
    home, hermes_home, stub_root, env = _world(tmp_path)
    del env["MCS_MODEL_SHA256"]
    r = _run(env, hermes_home)
    assert r.returncode == 1 and "モデルの整合性が確認できません" in r.stderr
    assert not (hermes_home / "models" / "Qwen3.5-9B-Q4_K_M.gguf").exists()


def test_model_checksum_env_needs_the_explicit_override_flag(tmp_path):
    """MCS_MODEL_SHA256 alone must not replace the pin: without
    MCS_ALLOW_MODEL_SHA_OVERRIDE=1 the stub bytes fail the pinned check."""
    home, hermes_home, stub_root, env = _world(tmp_path)
    del env["MCS_ALLOW_MODEL_SHA_OVERRIDE"]
    r = _run(env, hermes_home)
    assert r.returncode == 1 and "モデルの整合性が確認できません" in r.stderr
    assert "MCS_ALLOW_MODEL_SHA_OVERRIDE=1 がないため無視" in r.stderr
    assert not (hermes_home / "models" / "Qwen3.5-9B-Q4_K_M.gguf").exists()


def test_reload_pending_never_truncates_existing_or_linked_files(tmp_path):
    import os

    home, hermes_home, stub_root, env = _world(tmp_path)
    assert _run(env, hermes_home).returncode == 0
    plist = home / "Library/LaunchAgents/ai.mcs.llamaserver.plist"
    pending = home / ".mcs/data/.llama-reload-pending"

    # An existing owned single-link marker is preserved without truncating its bytes.
    pending.write_bytes(b"keep-existing-sentinel")
    pending.chmod(0o600)
    r = _run({**env, "STUB_LLM_SERVING": "1"}, hermes_home)
    assert r.returncode == 0, r.stderr
    assert pending.read_bytes() == b"keep-existing-sentinel"
    assert "まだ反映されていません" in r.stderr

    # A symlink or multi-link file is never truncated or overwritten; warning is emitted.
    pending.unlink()
    outside = tmp_path / "outside-target"
    outside.write_bytes(b"outside-sentinel")
    outside.chmod(0o600)
    pending.symlink_to(outside)
    plist.write_text(plist.read_text().replace("49152", "4096"))
    r_sym = _run({**env, "STUB_LLM_SERVING": "1"}, hermes_home)
    assert r_sym.returncode == 0, r_sym.stderr
    assert outside.read_bytes() == b"outside-sentinel"
    assert pending.is_symlink()
    assert "再起動待ちの記録を保存できません" in r_sym.stderr

    pending.unlink()
    hard_src = tmp_path / "hard-target"
    hard_src.write_bytes(b"hardlink-sentinel")
    hard_src.chmod(0o600)
    os.link(hard_src, pending)
    plist.write_text(plist.read_text().replace("49152", "4096"))
    r_hard = _run({**env, "STUB_LLM_SERVING": "1"}, hermes_home)
    assert r_hard.returncode == 0, r_hard.stderr
    assert hard_src.read_bytes() == b"hardlink-sentinel"
    assert "再起動待ちの記録を保存できません" in r_hard.stderr


def test_model_resume_uses_only_final_http_content_length_and_never_overestimates_space(tmp_path):
    home, hermes_home, stub_root, env = _world(tmp_path)
    models = hermes_home / "models"
    models.mkdir(parents=True)
    part = models / "Qwen3.5-9B-Q4_K_M.gguf.part"
    part.write_bytes(b"p" * 2048)

    # Redirect has Content-Length: 120, final 200 has none -> falls back to 6 GiB + 1 GiB,
    # so 2 GB free (~2097152 KB) must stop before starting curl -o.
    redirect_missing_final = (
        "HTTP/1.1 302 Found\\r\\nContent-Length: 120\\r\\n\\r\\n"
        "HTTP/2 200\\r\\nServer: synthetic\\r\\n\\r\\n"
    )
    r_missing = _run({**env, "STUB_DF_KB": "2097152",
                      "STUB_MODEL_HEAD_RAW": redirect_missing_final},
                     hermes_home, "--no-brew", "--no-plugin", "--no-services", "--no-recovery")
    assert r_missing.returncode == 1
    assert "空き容量が不足" in r_missing.stderr
    assert part.read_bytes() == b"p" * 2048

    # Final 200 Content-Length smaller than .part (512 < 2048) -> must not go negative.
    smaller_final = "HTTP/2 200\\r\\nContent-Length: 512\\r\\n\\r\\n"
    r_smaller = _run({**env, "STUB_DF_KB": "2097152",
                      "STUB_MODEL_HEAD_RAW": smaller_final},
                     hermes_home, "--no-brew", "--no-plugin", "--no-services", "--no-recovery")
    assert r_smaller.returncode == 1
    assert "空き容量が不足" in r_smaller.stderr
    assert part.read_bytes() == b"p" * 2048

    # Redirect (Content-Length: 120) followed by final 200 with exact complete length -> adopts final.
    exact_final = (
        "HTTP/1.1 302 Found\\r\\nContent-Length: 120\\r\\n\\r\\n"
        f"HTTP/2 200\\r\\nContent-Length: {part.stat().st_size}\\r\\n\\r\\n"
    )
    r_ok = _run({**env, "STUB_DF_KB": "2097152",
                 "STUB_MODEL_HEAD_RAW": exact_final,
                 "MCS_MODEL_SHA256": hashlib.sha256(b"p" * 2048).hexdigest()},
                hermes_home, "--no-brew", "--no-plugin", "--no-services", "--no-recovery")
    assert r_ok.returncode == 0, r_ok.stderr
    assert (models / "Qwen3.5-9B-Q4_K_M.gguf").read_bytes() == b"p" * 2048
