"""Synthetic install sh models contracts; shared helpers preserve isolated runtime state."""
import hashlib
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
    assert dl and all("-C -" in c and "--retry" in c for c in dl)


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
    assert re.search(r'^MODEL_URL="https://huggingface\.co/[^"]+/resolve/[0-9a-f]{40}/[^"]+\.gguf"$',
                     text, re.M)
    assert re.search(r'^MODEL_SHA256="[0-9a-f]{64}"$', text, re.M)
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
