"""Synthetic install sh software contracts; shared helpers preserve isolated runtime state."""
import os
import shutil
import subprocess
import pytest
from install_sh_testkit import INSTALL as INSTALL, ROOT as ROOT, _calls as _calls, _pin as _pin, _run as _run, _world as _world


def test_existing_hermes_checkout_never_rewritten(tmp_path):
    """An unrelated existing checkout at a non-pinned SHA is kept and
    only warned about — never cloned over, checked out, or reported as
    pinned."""
    home, hermes_home, stub_root, env = _world(
        tmp_path, git_head="b2639641dfd2591974fa9f1442a6e1d3e808da3e")
    checkout = hermes_home / "hermes-agent"
    (checkout / ".git").mkdir(parents=True)
    marker = checkout / "LOCAL-WORK-IN-PROGRESS"
    marker.write_text("do not touch")

    r = _run(env, hermes_home)
    assert r.returncode == 0, r.stderr
    assert marker.read_text() == "do not touch"
    assert "指定固定版" in r.stderr
    assert "(pinned" not in r.stdout
    assert "検証済み固定版とは異なります" in r.stdout          # summary is honest
    calls = _calls(stub_root)
    assert not any(c.startswith("git clone") for c in calls)
    assert not any(" checkout " in c for c in calls)


def test_plugin_link_collision_is_bounded_error(tmp_path):
    home, hermes_home, stub_root, env = _world(tmp_path)
    plugins = hermes_home / "plugins"
    plugins.mkdir(parents=True)
    (plugins / "mcs-discord-commands").mkdir()          # real dir!

    r = _run(env, hermes_home)
    assert r.returncode == 1
    assert "リンクではありません" in r.stderr
    assert "内容を確認・退避" in r.stderr             # recovery step


def test_hermes_on_path_without_venv_python_is_repaired(tmp_path):
    """F1: a hermes already on PATH no longer short-circuits stage 2 —
    the venv interpreter that services bakes in is created and verified,
    and services run with exactly that interpreter."""
    home, hermes_home, stub_root, env = _world(tmp_path, path_hermes=True)
    r = _run(env, hermes_home)
    assert r.returncode == 0, r.stderr
    venv_py = hermes_home / "hermes-agent" / "venv" / "bin" / "python"
    assert os.access(venv_py, os.X_OK)
    services = [c for c in _calls(stub_root) if "mcs_setup.py services" in c]
    assert services and all(c.startswith("venv-python ") for c in services)
    # the foreign PATH hermes is left alone and never used for the plugin
    assert not any(c.startswith("path-hermes") for c in _calls(stub_root))


def test_checkout_with_venv_missing_python_is_rebuilt(tmp_path):
    """F1/F2: pinned checkout + a venv whose interpreter vanished (brew
    upgrade) — rebuilt instead of 'venv already exists' forever."""
    home, hermes_home, stub_root, env = _world(tmp_path)
    venv = hermes_home / "hermes-agent" / "venv"
    (venv / "bin").mkdir(parents=True)
    (hermes_home / "hermes-agent" / ".git").mkdir()
    shutil.copy(stub_root / "venv-hermes", venv / "bin" / "hermes")
    r = _run(env, hermes_home)
    assert r.returncode == 0, r.stderr
    assert "Python 3.14 が使用できない" in r.stderr
    assert os.access(venv / "bin" / "python", os.X_OK)
    assert any(c.startswith("uv pip install") for c in _calls(stub_root))


def test_fresh_pip_failure_keeps_bootstrap_hold_on_rerun(tmp_path):
    home, hermes_home, stub_root, env = _world(tmp_path)
    failed = _run({**env, "STUB_FAIL_PIP": "1"}, hermes_home)
    assert failed.returncode == 1 and "Hermes 導入に失敗" in failed.stderr
    marker = home / ".mcs/data/.provider-bootstrap-fresh.json"
    before, calls = marker.read_bytes(), _calls(stub_root)
    retried = _run(env, hermes_home)
    assert retried.returncode != 0 and "初回導入が中断された記録" in retried.stderr
    assert marker.read_bytes() == before
    assert not any(line.startswith(("brew install", "git clone", "uv ", "launchctl bootstrap"))
                   for line in _calls(stub_root)[len(calls):])
    assert not (home / ".mcs/config.json").exists()


def test_failed_pip_install_converges_on_rerun(tmp_path):
    """F2: `uv venv` succeeded, `uv pip install` failed — the re-run
    reuses the venv and retries only the install."""
    home, hermes_home, stub_root, env = _world(tmp_path, configured=True)
    r1 = _run({**env, "STUB_FAIL_PIP": "1"}, hermes_home)
    assert r1.returncode == 1
    assert "Hermes 導入に失敗" in r1.stderr
    r2 = _run(env, hermes_home)
    assert r2.returncode == 0, r2.stderr
    assert "already exists" not in r2.stderr
    calls = _calls(stub_root)
    assert sum(c.startswith("uv venv") for c in calls) == 1
    assert sum(c.startswith("uv pip install") for c in calls) == 2
    assert not (hermes_home / "hermes-agent" / "venv"
                / ".mcs-pip-incomplete").exists()


def test_interrupted_checkout_is_resumed_and_never_called_pinned(tmp_path):
    """F3: clone ok, pinned checkout failed. The re-run redoes the clone
    (marker) instead of accepting the default-branch HEAD."""
    home, hermes_home, stub_root, env = _world(tmp_path, configured=True)
    r1 = _run({**env, "STUB_FAIL_CHECKOUT_ONCE": "1", "STUB_FAIL_FETCH": "1"}, hermes_home)
    assert r1.returncode == 1
    assert "固定版" not in r1.stdout
    assert (hermes_home / ".hermes-agent.install-in-progress").exists()
    r2 = _run(env, hermes_home)
    assert r2.returncode == 0, r2.stderr
    head = (hermes_home / "hermes-agent" / ".git" / "HEAD").read_text()
    assert head.strip() == _pin()
    assert "指定固定版" not in r2.stderr
    assert not (hermes_home / ".hermes-agent.install-in-progress").exists()
    assert sum(c.startswith("git clone") for c in _calls(stub_root)) == 2


@pytest.mark.parametrize("available", [True, False])
def test_full_clone_fetches_unadvertised_pin_or_stops_before_venv(tmp_path, available):
    """Only a local bare repository is reachable; the pin has no branch or tag."""
    real_git = shutil.which("git")
    assert real_git
    home, hermes_home, stub_root, env = _world(tmp_path)
    env.update(GIT_ALLOW_PROTOCOL="file", GIT_CONFIG_NOSYSTEM="1",
               GIT_CONFIG_GLOBAL=os.devnull, GIT_AUTHOR_NAME="Synthetic",
               GIT_AUTHOR_EMAIL="synthetic@example.invalid",
               GIT_COMMITTER_NAME="Synthetic",
               GIT_COMMITTER_EMAIL="synthetic@example.invalid", STUB_REAL_GIT=real_git)
    remote = tmp_path / "synthetic.git"

    def git(*args, input=None):
        return subprocess.run([real_git, *args], env=env, input=input, text=True,
                              capture_output=True, check=True).stdout.strip()

    git("init", "--bare", "-q", str(remote))
    tree = git("-C", str(remote), "mktree", input="")
    base = git("-C", str(remote), "commit-tree", tree, "-m", "base")
    hidden = git("-C", str(remote), "commit-tree", tree, "-p", base, "-m", "pin")
    git("-C", str(remote), "update-ref", "refs/heads/main", base)
    git("-C", str(remote), "symbolic-ref", "HEAD", "refs/heads/main")
    git("-C", str(remote), "update-ref", "refs/pull/1/head", hidden)
    git("-C", str(remote), "config", "uploadpack.allowAnySHA1InWant", "true")
    pin = hidden if available else "2" * 40
    script = tmp_path / "install.sh"
    (tmp_path / "mcs").symlink_to(ROOT / "mcs", target_is_directory=True)
    (tmp_path / "deployment").symlink_to(ROOT / "deployment", target_is_directory=True)
    script.write_text(INSTALL.read_text().replace(
        'HERMES_REPO="https://github.com/yusuketakuma/hermes-agent.git"',
        f'HERMES_REPO="{remote.as_uri()}"').replace(_pin(), pin))
    (stub_root / "bin/git").write_text('''#!/bin/sh
echo "git $*" >> "$STUB_LOG"
case "$*" in "clone --filter="*) exit 128 ;; esac
exec "$STUB_REAL_GIT" "$@"
''')
    result = _run(env, hermes_home, "--no-brew", "--no-llm", "--no-plugin",
                  "--no-services", "--no-recovery", script=script)
    checkout = hermes_home / "hermes-agent"
    calls = _calls(stub_root)
    assert sum(" fetch -q origin " in c for c in calls) == 1
    if available:
        assert result.returncode == 0, result.stdout + result.stderr
        assert git("-C", str(checkout), "rev-parse", "HEAD") == pin
        assert not (hermes_home / ".hermes-agent.install-in-progress").exists()
    else:
        assert result.returncode == 1
        assert "通信と指定版の公開状況を確認" in result.stderr
        assert git("-C", str(checkout), "rev-parse", "HEAD") == base
        assert (hermes_home / ".hermes-agent.install-in-progress").exists()
        assert not (checkout / "venv").exists()
        assert not any(c.startswith("uv pip install") for c in calls)


def test_interrupted_clone_is_redone(tmp_path):
    home, hermes_home, stub_root, env = _world(tmp_path, configured=True)
    r1 = _run({**env, "STUB_FAIL_CLONE": "1"}, hermes_home)
    assert r1.returncode == 1 and "取得に失敗しました" in r1.stderr
    r2 = _run(env, hermes_home)
    assert r2.returncode == 0, r2.stderr
    head = (hermes_home / "hermes-agent" / ".git" / "HEAD").read_text()
    assert head.strip() == _pin()


def test_unreadable_head_is_fatal(tmp_path):
    home, hermes_home, stub_root, env = _world(tmp_path)
    (hermes_home / "hermes-agent" / ".git").mkdir(parents=True)
    r = _run({**env, "STUB_FAIL_REVPARSE": "1"}, hermes_home)
    assert r.returncode == 1
    assert "現在の版（HEAD）を読み取れません" in r.stderr


def test_shim_symlink_is_replaced_not_written_through(tmp_path):
    """F4: ~/.local/bin/hermes -> venv/bin/hermes used to be written
    through, turning the venv entry point into a self-exec loop."""
    home, hermes_home, stub_root, env = _world(tmp_path)
    assert _run(env, hermes_home).returncode == 0
    venv_hermes = hermes_home / "hermes-agent" / "venv" / "bin" / "hermes"
    original = venv_hermes.read_bytes()
    shim = home / ".local" / "bin" / "hermes"
    shim.unlink()
    shim.symlink_to(venv_hermes)
    r = _run(env, hermes_home)
    assert r.returncode == 0, r.stderr
    assert venv_hermes.read_bytes() == original
    assert not shim.is_symlink()
    assert "exec " in shim.read_text()


def test_foreign_shim_file_is_left_alone(tmp_path):
    home, hermes_home, stub_root, env = _world(tmp_path, configured=True)
    shim = home / ".local" / "bin" / "hermes"
    shim.parent.mkdir(parents=True)
    shim.write_text("#!/bin/sh\n# my own hermes launcher\n")
    r = _run(env, hermes_home)
    assert r.returncode == 0, r.stderr
    assert shim.read_text() == "#!/bin/sh\n# my own hermes launcher\n"
    assert "MCS の起動コマンドではない" in r.stderr


def test_other_checkout_requires_force_repo(tmp_path):
    """F6: an install recorded for another checkout is not silently
    repointed; --force-repo switches it."""
    home, hermes_home, stub_root, env = _world(tmp_path)
    other = tmp_path / "other-checkout"
    other.mkdir()
    rec = home / ".mcs-recovery"
    rec.mkdir(mode=0o700, parents=True)
    (rec / "repo_path").write_text(f"{other}\n")
    r = _run(env, hermes_home)
    assert r.returncode == 1
    assert "--force-repo" in r.stderr and str(other) in r.stderr
    assert (rec / "repo_path").read_text() == f"{other}\n"
    assert _calls(stub_root) == []                     # nothing ran
    assert not (home / '.mcs').exists()
    r2 = _run(env, hermes_home, "--force-repo")
    assert r2.returncode == 0, r2.stderr
    assert (rec / "repo_path").read_text() == f"{ROOT}\n"


def test_plugin_link_to_other_checkout_requires_force_repo(tmp_path):
    home, hermes_home, stub_root, env = _world(tmp_path)
    other = tmp_path / "other" / "hermes_plugin"
    other.mkdir(parents=True)
    (hermes_home / "plugins").mkdir(parents=True)
    link = hermes_home / "plugins" / "mcs-discord-commands"
    link.symlink_to(other)
    r = _run(env, hermes_home)
    assert r.returncode == 1 and "--force-repo" in r.stderr
    assert link.resolve() == other.resolve()


def test_hermes_install_uses_its_own_uv_security_overrides(tmp_path):
    home, hermes_home, stub, env = _world(tmp_path)
    result = _run(env, hermes_home, "--no-brew", "--no-llm")
    assert result.returncode == 0, result.stdout + result.stderr
    assert (stub / "uv-pip-cwd").read_text().strip() == str(hermes_home / "hermes-agent")


@pytest.mark.parametrize("mode", [[], ["--preflight"], ["--dry-run"]])
def test_missing_uv_stops_before_hermes_checkout_or_venv_changes(tmp_path, mode):
    home, hermes_home, stub, env = _world(tmp_path)
    (stub / "bin/uv").unlink()
    result = _run(env, hermes_home, "--no-brew", "--no-llm", *mode)
    assert result.returncode != 0
    assert "uv が必要" in result.stdout + result.stderr
    assert not (hermes_home / "hermes-agent").exists()
    assert not any(call.startswith("git clone") for call in _calls(stub))
