"""Desktop bootstrap places a permanent checkout over fictional git repos only."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("install_local", ROOT / "scripts/install_local.py")
install_local = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(install_local)

LOCAL = {"platform": "darwin", "home": "", "home_writable": True,
         "gui_session": True, "git": True, "sandbox_hints": []}
FAKE_INSTALL = """#!/bin/sh
here=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd -P)
printf '%s\\n' "$here" "$PWD" "$*" > "$HOME/install-ran"
"""


def _git(path, *args):
    return subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True,
                          text=True).stdout.strip()


def _repo(path, marker="mcs"):
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "fictional@example.invalid")
    _git(path, "config", "user.name", "Fictional")
    (path / "install.sh").write_text(FAKE_INSTALL)
    (path / "README").write_text(marker)
    _git(path, "add", ".")
    _git(path, "commit", "-q", "-m", f"fictional {marker}")
    return path


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "home with 'quote' $(x)"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(install_local, "environment", lambda: dict(LOCAL, home=str(home)))
    return home


@pytest.fixture
def source(tmp_path):
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    repo = _repo(tmp_path / "desktop workspace" / "hermes-mcs")
    _git(repo, "remote", "add", "origin", str(bare))
    return repo


def _ran(home):
    return (home / "install-ran").read_text().splitlines()


def test_default_clones_to_runtime_root_and_runs_its_installer(home, source):
    assert install_local.main(["--mode", "hermes"], source=str(source)) == 0
    dest = home / ".mcs"
    assert _git(dest, "rev-parse", "HEAD") == _git(source, "rev-parse", "HEAD")
    assert _git(dest, "symbolic-ref", "--short", "HEAD") == "main"
    assert _git(dest, "remote", "get-url", "origin") == _git(source, "remote", "get-url", "origin")
    here, cwd, args = _ran(home)
    assert Path(here) == Path(cwd) == dest.resolve() and args == "--mode hermes"
    assert not list(home.glob(".mcs-clone-*"))


def test_uncommitted_source_changes_are_reported_and_not_copied(home, source, capsys):
    (source / "local-edit").write_text("fictional")
    assert install_local.main([], source=str(source)) == 0
    assert "commit されていない変更" in capsys.readouterr().out
    assert not (home / ".mcs" / "local-edit").exists()


def test_existing_same_project_checkout_is_reused_untouched(home, source):
    dest = home / ".mcs"
    subprocess.run(["git", "clone", "-q", str(source), str(dest)], check=True)
    (dest / "data").mkdir()
    (dest / "data" / "ledger.db").write_bytes(b"fictional")
    before = _git(dest, "rev-parse", "HEAD")
    assert install_local.main([], source=str(source)) == 0
    assert _git(dest, "rev-parse", "HEAD") == before
    assert (dest / "data" / "ledger.db").read_bytes() == b"fictional"
    assert Path(_ran(home)[0]) == dest.resolve()


@pytest.mark.parametrize("shallow_source", [True, False])
def test_same_head_shallow_and_full_checkouts_are_reused(home, source, tmp_path, shallow_source):
    (source / "README").write_text("fictional second revision")
    _git(source, "commit", "-am", "second fictional commit")
    shallow = tmp_path / "shallow"
    subprocess.run(["git", "clone", "-q", "--depth", "1", source.as_uri(), str(shallow)],
                   check=True, capture_output=True)
    assert _git(shallow, "rev-parse", "--is-shallow-repository") == "true"
    assert _git(shallow, "rev-parse", "HEAD") == _git(source, "rev-parse", "HEAD")
    origin, dest = (shallow, source) if shallow_source else (source, shallow)
    before = _git(dest, "rev-parse", "HEAD")
    assert install_local.main(["--dest", str(dest)], source=str(origin)) == 0
    assert _git(dest, "rev-parse", "HEAD") == before
    assert Path(_ran(home)[0]) == dest.resolve()


def test_installed_checkout_at_another_version_is_never_installed_over(home, source, capsys):
    dest = home / ".mcs"
    subprocess.run(["git", "clone", "-q", str(source), str(dest)], check=True)
    before = _git(dest, "rev-parse", "HEAD")
    (source / "README").write_text("fictional newer revision")
    _git(source, "commit", "-qam", "newer fictional commit")
    assert install_local.main([], source=str(source)) == 2
    assert "UPGRADE_AGENT.md" in capsys.readouterr().err
    assert _git(dest, "rev-parse", "HEAD") == before
    assert not (home / "install-ran").exists()


def test_recorded_install_location_wins_without_dest(home, source, tmp_path):
    previous = tmp_path / "elsewhere" / "mcs"
    subprocess.run(["git", "clone", "-q", str(source), str(previous)], check=True)
    (home / ".mcs-recovery").mkdir()
    (home / ".mcs-recovery" / "repo_path").write_text(f"{previous}\n")
    assert install_local.main([], source=str(source)) == 0
    assert Path(_ran(home)[0]) == previous.resolve()
    assert not (home / ".mcs").exists()


def test_empty_explicit_dest_with_spaces_is_cloned(home, source, tmp_path):
    dest = tmp_path / "my apps" / "M C S"
    dest.mkdir(parents=True)
    assert install_local.main(["--dest", str(dest)], source=str(source)) == 0
    assert (dest / "README").read_text() == "mcs"
    assert Path(_ran(home)[0]) == dest.resolve()


@pytest.mark.parametrize("root_value", ["empty", "different"])
def test_unsupported_data_root_is_refused_before_clone(home, source, tmp_path, monkeypatch, capsys, root_value):
    dest = tmp_path / "permanent-source"
    other = tmp_path / "other-root"
    monkeypatch.setenv("MCS_ROOT", "" if root_value == "empty" else str(other))
    assert install_local.main(["--dest", str(dest)], source=str(source)) == 2
    assert "MCS_ROOT" in capsys.readouterr().err
    assert not dest.exists() and not other.exists()
    assert list(home.iterdir()) == []


@pytest.mark.parametrize("root_value", ["unset", "default", "normalized", "physical", "tilde"])
def test_default_data_root_spellings_keep_custom_source_dest(home, source, tmp_path, monkeypatch, root_value):
    monkeypatch.delenv("MCS_ROOT", raising=False)
    if root_value == "default":
        monkeypatch.setenv("MCS_ROOT", str(home / ".mcs"))
    elif root_value == "normalized":
        monkeypatch.setenv("MCS_ROOT", str(home) + "/./.mcs/")
    elif root_value == "physical":
        alias = tmp_path / "home-alias"
        alias.symlink_to(home, target_is_directory=True)
        monkeypatch.setenv("MCS_ROOT", str(alias / ".mcs"))
    elif root_value == "tilde":
        monkeypatch.setenv("MCS_ROOT", "~/.mcs")
    dest = tmp_path / "permanent-source"
    assert install_local.main(["--dest", str(dest)], source=str(source)) == 0
    assert Path(_ran(home)[0]) == dest.resolve()


@pytest.mark.parametrize("dry_run", [False, True])
def test_relative_data_root_stays_default_when_installer_changes_directory(home, source, tmp_path, monkeypatch, dry_run):
    with (source / "install.sh").open("a") as stream:
        stream.write('printf \'%s\\n\' "$MCS_ROOT" > "$HOME/installer-root"\n')
    _git(source, "commit", "-am", "record synthetic installer root")
    dest = tmp_path / "permanent-source"
    if dry_run:
        subprocess.run(["git", "clone", "-q", str(source), str(dest)],
                       check=True, capture_output=True)
    monkeypatch.chdir(home)
    monkeypatch.setenv("MCS_ROOT", ".mcs")
    args = ["--dest", str(dest), *(["--dry-run"] if dry_run else [])]
    assert install_local.main(args, source=str(source)) == 0
    assert (home / "installer-root").read_text().strip() == str(home / ".mcs")
    assert os.environ["MCS_ROOT"] == ".mcs"
    assert Path(_ran(home)[0]) == dest.resolve()


@pytest.mark.parametrize("kind", ["other_project", "runtime_data", "nonempty", "file"])
def test_unsafe_destinations_are_refused_without_changes(home, source, tmp_path, kind, capsys):
    dest = tmp_path / "dest"
    if kind == "other_project":
        _repo(dest, marker="other")
    elif kind == "file":
        dest.write_text("fictional")
    else:
        dest.mkdir()
        (dest / ("config.json" if kind == "runtime_data" else "notes.txt")).write_text("{}")
    before = sorted(p.name for p in dest.iterdir()) if dest.is_dir() else dest.read_text()
    assert install_local.main(["--dest", str(dest)], source=str(source)) == 2
    after = sorted(p.name for p in dest.iterdir()) if dest.is_dir() else dest.read_text()
    assert after == before and not (home / "install-ran").exists()
    if kind == "runtime_data":
        assert "データは移動しません" in capsys.readouterr().err


def test_source_that_is_not_a_checkout_is_refused(home, tmp_path):
    plain = tmp_path / "zip-download"
    plain.mkdir()
    assert install_local.main([], source=str(plain)) == 2


@pytest.mark.parametrize("problem", [
    {"gui_session": False}, {"platform": "linux"}, {"home_writable": False}, {"git": False}])
def test_non_local_session_is_not_reported_as_installed(home, source, monkeypatch, problem, capsys):
    monkeypatch.setattr(install_local, "environment", lambda: dict(LOCAL, home=str(home), **problem))
    assert install_local.main([], source=str(source)) == 3
    err = capsys.readouterr().err
    assert "導入しません" in err and "python3" in err
    assert not (home / ".mcs").exists() and not (home / "install-ran").exists()


@pytest.mark.parametrize(("flags", "status"), [
    (["--dry-run"], 0), (["--preflight"], 2), (["--check-only"], 2),
    (["--dry-run", "--preflight"], 2), (["--dry-run", "--check-only"], 2)])
def test_read_only_writes_nothing(home, source, flags, status, capsys):
    assert install_local.main(flags, source=str(source)) == status
    assert not (home / ".mcs").exists() and not (home / "install-ran").exists()
    if status:
        assert "導入前の確認は未実施" in capsys.readouterr().err


def test_interrupted_clone_leaves_no_partial_checkout_and_rerun_converges(home, source, monkeypatch):
    real = install_local._git

    def broken(path, *args, **kwargs):
        if args[:1] == ("checkout",):
            raise install_local.Refused("synthetic interruption")
        return real(path, *args, **kwargs)

    monkeypatch.setattr(install_local, "_git", broken)
    assert install_local.main([], source=str(source)) == 4
    assert not (home / ".mcs").exists() and not list(home.glob(".mcs-clone-*"))
    monkeypatch.setattr(install_local, "_git", real)
    assert install_local.main([], source=str(source)) == 0
    assert (home / ".mcs" / "README").exists()


def test_environment_reports_where_it_runs():
    env = install_local.environment()
    assert set(env) == set(LOCAL)
    json.dumps(env)   # plain data only, printed to the operator
    assert os.path.isabs(env["home"])


def test_data_only_runtime_root_falls_back_to_a_permanent_source_dir(home, source, capsys):
    runtime = home / ".mcs"
    (runtime / "data").mkdir(parents=True)
    (runtime / "config.json").write_text("{}")
    assert install_local.main([], source=str(source)) == 0
    dest = home / ".local" / "share" / "hermes-mcs"
    assert (dest / "README").read_text() == "mcs" and Path(_ran(home)[0]) == dest.resolve()
    assert sorted(p.name for p in runtime.iterdir()) == ["config.json", "data"]
    assert "設定・データだけ" in capsys.readouterr().out


def test_sandbox_variables_alone_do_not_block_a_reachable_session(home, source, monkeypatch, capsys):
    monkeypatch.setattr(install_local, "environment", lambda: dict(
        LOCAL, home=str(home), sandbox_hints=["CODEX_SANDBOX_NETWORK_DISABLED"]))
    assert install_local.main([], source=str(source)) == 0
    assert "参考: サンドボックス" in capsys.readouterr().out


def test_suggested_command_is_safely_quoted(home, tmp_path, monkeypatch, capsys):
    import shlex
    odd = _repo(tmp_path / "it's $(touch x) `y` dir")
    monkeypatch.setattr(install_local, "environment", lambda: dict(LOCAL, home=str(home), gui_session=False))
    assert install_local.main(["--mode", "standalone"], source=str(odd)) == 3
    line = [row for row in capsys.readouterr().err.splitlines() if "install_local.py" in row][0]
    assert shlex.split(line) == ["python3", str(odd / "scripts" / "install_local.py"),
                                 "--mode", "standalone"]


def test_missing_git_binary_is_a_controlled_refusal(home, source, monkeypatch, capsys):
    real = subprocess.run

    def no_git(argv, *args, **kwargs):
        if argv[0] == "git":
            raise FileNotFoundError("synthetic: git missing")
        return real(argv, *args, **kwargs)

    monkeypatch.setattr(install_local.subprocess, "run", no_git)
    assert install_local.main([], source=str(source)) == 2
    assert "git を実行できません" in capsys.readouterr().err
    assert not (home / ".mcs").exists()


def test_launchctl_timeout_means_no_gui_session(monkeypatch):
    monkeypatch.setattr(install_local.shutil, "which", lambda name: "/bin/" + name)

    def hang(argv, *args, **kwargs):
        raise subprocess.TimeoutExpired(argv, 10)

    monkeypatch.setattr(install_local.subprocess, "run", hang)
    env = install_local.environment()
    assert env["gui_session"] is False
    assert any("launchd" in problem for problem in install_local.local_problems(env))
