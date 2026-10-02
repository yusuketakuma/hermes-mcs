"""install.sh — synthetic end-to-end under a stubbed PATH.

Every external command the installer reaches for (brew, git, hermes,
uv, curl, launchctl, llama-server, uname, id, df, ...) is a stub in
$STUB/bin; HOME and HERMES_HOME are temp dirs, so nothing real is
touched — no real checkout, no real services, no network. The stubs are
realistic where it matters: `uv venv` refuses an existing dir, `uv pip
install` is what creates bin/hermes, `git clone` refuses an existing
destination and lands on the default branch, checkout can fail.
mcs_setup's own suite (tests/ops/test_mcs_setup.py) covers stage 5
internals; here the venv `python` is a recording stub so the SHELL
contract — stage ordering, idempotent rerun, partial-failure recovery —
is what is tested.
"""
import os
import plistlib
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
INSTALL = ROOT / "install.sh"


def _pin():
    m = re.search(r'^HERMES_PIN="([0-9a-f]{40})"$',
                  INSTALL.read_text(), re.M)
    assert m, "install.sh must pin a 40-hex Hermes commit"
    return m.group(1)


CLONE_HEAD = "1" * 40          # the fork's default branch — never the pin

STUBS = {
    "brew": """#!/bin/sh
echo "brew $*" >> "$STUB_LOG"
if [ "$1" = "--prefix" ]; then echo "$STUB_ROOT/brew-prefix"; exit 0; fi
exit 0
""",
    # a clone refuses an existing destination and lands on the default
    # branch; checkout moves .git/HEAD; rev-parse reads it back
    "git": """#!/bin/sh
echo "git $*" >> "$STUB_LOG"
last=""
for a in "$@"; do last="$a"; done
if [ "$1" = "clone" ]; then
    if [ -e "$last" ]; then
        echo "fatal: destination path '$last' already exists" >&2; exit 128
    fi
    mkdir -p "$last/.git"; echo "$STUB_CLONE_HEAD" > "$last/.git/HEAD"
    [ -z "$STUB_FAIL_CLONE" ] || exit 128        # partial clone left behind
    exit 0
fi
if [ "$1" = "-C" ]; then
    case "$3" in
      rev-parse)
        [ -z "$STUB_FAIL_REVPARSE" ] || exit 128
        if [ -f "$2/.git/HEAD" ]; then cat "$2/.git/HEAD"
        else echo "$STUB_GIT_HEAD"; fi
        exit 0 ;;
      checkout)
        if [ -n "$STUB_FAIL_CHECKOUT_ONCE" ] \\
                && [ ! -f "$STUB_STATE/checkout-failed" ]; then
            touch "$STUB_STATE/checkout-failed"
            echo "error: pathspec '$last' did not match" >&2; exit 1
        fi
        echo "$last" > "$2/.git/HEAD"; exit 0 ;;
    esac
fi
exit 0
""",
    # `uv venv` refuses an existing dir (no --clear) and makes only the
    # interpreter; `uv pip install` is what creates bin/hermes
    "uv": """#!/bin/sh
echo "uv $*" >> "$STUB_LOG"
last=""
prev=""
py=""
for a in "$@"; do
    [ "$prev" = "--python" ] && py="$a"
    prev="$a"; last="$a"
done
case "$1" in
  venv)
    if [ -e "$last" ]; then
        echo "error: venv already exists at $last (use --clear)" >&2; exit 2
    fi
    mkdir -p "$last/bin"
    cp "$STUB_ROOT/venv-python" "$last/bin/python"
    cp "$STUB_ROOT/venv-python" "$last/bin/python3"; exit 0 ;;
  pip)
    [ -z "$STUB_FAIL_PIP" ] || { echo "error: build failed" >&2; exit 1; }
    case "$*" in *requirements-standalone.txt*) exit 0 ;; esac
    cp "$STUB_ROOT/venv-hermes" "$(dirname "$py")/hermes"; exit 0 ;;
esac
exit 0
""",
    "curl": """#!/bin/sh
echo "curl $*" >> "$STUB_LOG"
out=""
prev=""
for a in "$@"; do
    if [ "$prev" = "-o" ]; then out="$a"; fi
    prev="$a"
done
case "$*" in
  *v1/models*) [ -n "$STUB_LLM_SERVING" ] && exit 0; exit 7 ;;
esac
if [ -n "$STUB_CURL_FAIL" ]; then
    case "$*" in *"$STUB_CURL_FAIL"*) exit 6 ;; esac
fi
case "$*" in -sSfIL*) exit 0 ;; esac            # preflight HEAD probe
case "$*" in -fsIL*)                             # remote size probe
    [ -n "$STUB_MODEL_SIZE" ] && printf 'HTTP/2 200\r\ncontent-length: %s\r\n' "$STUB_MODEL_SIZE"
    exit 0 ;;
esac
if [ -n "$out" ]; then
    if [ -n "$STUB_FAIL_MODEL_ONCE" ] && [ ! -f "$STUB_STATE/model-cut" ]; then
        touch "$STUB_STATE/model-cut"
        printf 'stub-model-' > "$out"; exit 18   # connection dropped
    fi
    if [ -f "$out" ]; then printf 'bytes\\n' >> "$out"   # -C - resume
    else echo "stub-model-bytes" > "$out"; fi
fi
exit 0
""",
    "launchctl": """#!/bin/sh
echo "launchctl $*" >> "$STUB_LOG"
mkdir -p "$STUB_STATE/loaded"
case "$1" in
  print)
    label="${2##*/}"
    [ -f "$STUB_STATE/loaded/$label" ] && exit 0 || exit 1 ;;
  bootstrap)
    plist=""
    for a in "$@"; do plist="$a"; done
    label="$(basename "$plist" .plist)"
    if [ -n "$STUB_FAIL_WATCHDOG" ] && [ "$label" = org.mcs.recovery ]; then
        exit 1
    fi
    if [ -n "$STUB_FAIL_WATCHDOG_ONCE" ] && [ "$label" = org.mcs.recovery ] \\
            && [ ! -f "$STUB_STATE/watchdog-failed-once" ]; then
        touch "$STUB_STATE/watchdog-failed-once"
        echo "Bootstrap failed: 5: Input/output error" >&2; exit 5
    fi
    if [ -n "$STUB_WATCHDOG_NOLOAD" ] && [ "$label" = org.mcs.recovery ]; then
        exit 0                   # exit 0, yet the label never loads
    fi
    touch "$STUB_STATE/loaded/$label"; exit 0 ;;
  bootout)
    label="${2##*/}"
    rm -f "$STUB_STATE/loaded/$label"; exit 0 ;;
esac
exit 0
""",
    # ambient python3 answers every version probe as 3.11–3.13
    "python3": """#!/bin/sh
echo "python3 $*" >> "$STUB_LOG"
exit 0
""",
    "llama-server": """#!/bin/sh
echo "llama-server $*" >> "$STUB_LOG"
exit 0
""",
    "uname": """#!/bin/sh
if [ "$1" = "-m" ]; then echo arm64; else echo Darwin; fi
""",
    "id": """#!/bin/sh
if [ "$1" = "-u" ]; then echo "${STUB_UID:-501}"; exit 0; fi
if [ "$1" = "-un" ]; then echo stubuser; exit 0; fi
exit 0
""",
    "sw_vers": """#!/bin/sh
echo 15.1
""",
    "xcode-select": """#!/bin/sh
echo /Library/Developer/CommandLineTools
""",
    "df": """#!/bin/sh
echo "Filesystem 1024-blocks Used Available Capacity Mounted"
echo "/dev/stub 100000000 1 ${STUB_DF_KB:-50000000} 1% /"
""",
}
# copied into the venv by the uv stub (not on PATH)
VENV_PYTHON = """#!/bin/sh
echo "venv-python $*" >> "$STUB_LOG"
if [ "$1" = "-" ] && [ -n "$STUB_REAL_PYTHON" ]; then
    exec "$STUB_REAL_PYTHON" "$@"
fi
case "$*" in
  *"mcs_setup.py services"*)
    if [ -d "$HOME/.mcs/data/cmd" ] && [ -d "$HOME/.mcs/data/cmd_int" ]; then
        echo "datadirs-ready" >> "$STUB_LOG"
    fi
    [ -z "$STUB_FAIL_SERVICES" ] || exit 1 ;;
esac
exit 0
"""
VENV_HERMES = """#!/bin/sh
echo "venv-hermes $*" >> "$STUB_LOG"
echo "hermes-env HERMES_HOME=$HERMES_HOME" >> "$STUB_LOG"
case "$1" in
  config) exit 1 ;;
  plugins) [ -z "$STUB_FAIL_PLUGIN" ] || exit 1 ;;
esac
exit 0
"""


def _world(tmp_path, *, brew=True, git_head=None, path_hermes=False):
    home = tmp_path / "home"
    hermes_home = home / ".hermes"
    stub_root = tmp_path / "stub"
    bin_dir = stub_root / "bin"
    bin_dir.mkdir(parents=True)
    (stub_root / "state").mkdir(parents=True)
    home.mkdir(parents=True)
    stubs = dict(STUBS)
    if not brew:
        del stubs["brew"]
    if path_hermes:        # a hermes on PATH that is not our venv
        stubs["hermes"] = '#!/bin/sh\necho "path-hermes $*" >> "$STUB_LOG"\n'
    for name, body in stubs.items():
        p = bin_dir / name
        p.write_text(body)
        p.chmod(0o755)
    for name, body in (("venv-python", VENV_PYTHON),
                       ("venv-hermes", VENV_HERMES)):
        (stub_root / name).write_text(body)
        (stub_root / name).chmod(0o755)
    env = {k: v for k, v in os.environ.items()
           if k not in ("HERMES_HOME", "CDPATH")}
    env.update({
        "HOME": str(home),
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "STUB_ROOT": str(stub_root),
        "STUB_LOG": str(stub_root / "calls.log"),
        "STUB_STATE": str(stub_root / "state"),
        "STUB_GIT_HEAD": git_head or _pin(),
        "STUB_CLONE_HEAD": CLONE_HEAD,
    })
    return home, hermes_home, stub_root, env


def _run(env, hermes_home, *flags, script=INSTALL, cwd=None):
    args = [*flags] + ([str(hermes_home)] if hermes_home is not None else [])
    return subprocess.run(
        ["sh", str(script), *args], cwd=cwd,
        env=env, capture_output=True, text=True, timeout=60)


def _calls(stub_root):
    log = stub_root / "calls.log"
    return log.read_text().splitlines() if log.exists() else []


def _bootstraps(stub_root, label):
    return [c for c in _calls(stub_root)
            if c.startswith("launchctl bootstrap") and label in c]


def test_install_run_then_rerun_converges(tmp_path):
    """Six stages on a stubbed fresh machine, twice: second run adds no
    duplicate service entries and keeps the previous recovery tool."""
    home, hermes_home, stub_root, env = _world(tmp_path)

    r1 = _run(env, hermes_home)
    assert r1.returncode == 0, r1.stderr
    recovery = home / ".mcs-recovery" / "mcs_recover.py"
    assert recovery.is_file() and os.access(recovery, os.X_OK)
    first_gen = recovery.read_bytes()
    assert first_gen == (ROOT / "deployment" / "recovery"
                         / "mcs_recover.py").read_bytes()
    # the out-of-repo tool knows which checkout it recovers
    assert (home / ".mcs-recovery" / "repo_path").read_text().strip() \
        == str(ROOT)
    # plugin symlink landed inside the isolated hermes home
    link = hermes_home / "plugins" / "mcs-discord-commands"
    assert link.is_symlink()
    assert link.resolve() == (ROOT / "hermes_plugin").resolve()
    # both service plists rendered with real substitutions
    agents = home / "Library" / "LaunchAgents"
    llama = (agents / "ai.mcs.llamaserver.plist").read_text()
    watch = (agents / "org.mcs.recovery.plist").read_text()
    assert "__LLAMA_BIN__" not in llama and "__MODEL__" not in llama
    assert "__RECOVERY__" not in watch and "__DATA__" not in watch
    assert str(home / ".mcs-recovery") in watch
    # one bootstrap per label — launchd state holds exactly one each
    loaded = {(p.name) for p in (stub_root / "state" / "loaded").iterdir()}
    assert loaded == {"ai.mcs.llamaserver", "org.mcs.recovery"}
    # the pinned clone is reported pinned; final summary lists next steps
    assert "hermes-agent cloned (pinned" in r1.stdout
    assert "Installed. Summary:" in r1.stdout
    assert "init already syncs the gateway and validates" in r1.stdout

    r2 = _run(env, hermes_home)
    assert r2.returncode == 0, r2.stderr
    # llama-server: plist exists -> single bootstrap across both runs
    assert len(_bootstraps(stub_root, "ai.mcs.llamaserver")) == 1
    # watchdog unchanged and loaded -> no bootout/bootstrap churn (F22)
    assert len(_bootstraps(stub_root, "org.mcs.recovery")) == 1
    assert sum(c.startswith("launchctl bootout") for c in _calls(stub_root)) == 1
    assert len(list((stub_root / "state" / "loaded").iterdir())) == 2
    # a same-generation rerun has no previous generation to keep — it
    # must not mint a .prev copy of the current tool
    prev = home / ".mcs-recovery" / "mcs_recover.py.prev"
    assert not prev.exists()
    assert recovery.read_bytes() == first_gen   # same repo generation
    # no second plugin dir/link objects appeared; no second clone/venv
    assert len(list((hermes_home / "plugins").iterdir())) == 1
    calls = _calls(stub_root)
    assert sum(c.startswith("git clone") for c in calls) == 1
    assert sum(c.startswith("uv venv") for c in calls) == 1
    assert sum(c.startswith("uv pip") for c in calls) == 1


def test_partial_states_resume_without_duplicates(tmp_path):
    """Interrupt-style pre-seeded state at three boundaries, then a
    rerun: plugin symlink survives, recovery .prev keeps the old
    generation, watchdog plist is replaced once and reloaded once."""
    home, hermes_home, stub_root, env = _world(tmp_path)
    (hermes_home / "plugins").mkdir(parents=True)
    link = hermes_home / "plugins" / "mcs-discord-commands"
    link.symlink_to(ROOT / "hermes_plugin")            # killed after 3/6
    rec = home / ".mcs-recovery"
    rec.mkdir(parents=True)
    old_gen = b"# old generation - only thing that still runs"
    (rec / "mcs_recover.py").write_bytes(old_gen)      # killed after cp
    agents = home / "Library" / "LaunchAgents"
    agents.mkdir(parents=True)
    (agents / "org.mcs.recovery.plist").write_text(
        "OLD-PLIST")                                  # manifest existed

    r = _run(env, hermes_home)
    assert r.returncode == 0, r.stderr
    assert link.is_symlink()
    assert link.resolve() == (ROOT / "hermes_plugin").resolve()
    assert len(list((hermes_home / "plugins").iterdir())) == 1
    assert (rec / "mcs_recover.py.prev").read_bytes() == old_gen
    assert (rec / "mcs_recover.py").read_bytes() == (
        ROOT / "deployment" / "recovery" / "mcs_recover.py").read_bytes()
    plist = (agents / "org.mcs.recovery.plist").read_text()
    assert plist != "OLD-PLIST" and "__RECOVERY__" not in plist
    assert len(_bootstraps(stub_root, "org.mcs.recovery")) == 1


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
    assert "not pinned" in r.stderr
    assert "(pinned" not in r.stdout
    assert "NOT the validated pin" in r.stdout          # summary is honest
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
    assert "not a symlink" in r.stderr
    assert "remove it manually" in r.stderr             # recovery step


def test_missing_brew_fails_with_recovery_hint(tmp_path):
    home, hermes_home, _, env = _world(tmp_path, brew=False)
    r = _run(env, hermes_home)
    assert r.returncode == 1
    assert "brew not found" in r.stderr
    assert "install.sh" in r.stderr                     # actionable hint


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


def test_skip_flags_bypass_stages(tmp_path):
    """--no-llm/--no-plugin/--no-recovery leave those stages untouched —
    the partially-provisioned machine (own LLM, self-managed plugins,
    no watchdog) still converges through the rest."""
    home, hermes_home, stub_root, env = _world(tmp_path)
    r = _run(env, hermes_home, "--no-llm", "--no-plugin",
             "--no-recovery")
    assert r.returncode == 0, r.stderr
    assert "stage skipped (--no-llm)" in r.stdout
    assert not (hermes_home / "plugins" / "mcs-discord-commands").exists()
    assert not (home / ".mcs-recovery").exists()
    agents = home / "Library" / "LaunchAgents"
    assert not (agents / "ai.mcs.llamaserver.plist").exists()
    assert not (agents / "org.mcs.recovery.plist").exists()
    # no model download was attempted; hermes stage still ran
    assert not any(c.startswith("curl") and "-o" in c
                   for c in _calls(stub_root))
    assert any(c.startswith("git clone") for c in _calls(stub_root))


def test_no_brew_and_no_services_flags(tmp_path):
    """--no-brew runs without a brew stub; --no-services never invokes
    mcs_setup."""
    home, hermes_home, stub_root, env = _world(tmp_path, brew=False)
    r = _run(env, hermes_home, "--no-brew", "--no-services")
    assert r.returncode == 0, r.stderr
    assert "stage skipped (--no-brew)" in r.stdout
    assert not any(c.startswith("brew install") for c in _calls(stub_root))
    assert not any("mcs_setup" in c for c in _calls(stub_root))


@pytest.mark.parametrize("flag", ["--bogus", "-x", "-no-llm"])
def test_unknown_option_is_bounded_error(tmp_path, flag):
    """F8: single-dash typos are options, never a HERMES_HOME."""
    home, hermes_home, _, env = _world(tmp_path)
    r = _run(env, hermes_home, flag)
    assert r.returncode == 2
    assert "unknown option" in r.stderr
    assert not hermes_home.exists()
    assert not (home / flag).exists()


def test_install_paths_remain_literal_shell_and_xml_data(tmp_path):
    unusual = tmp_path / "space & <tag> 'quote' $(printf SHOULD_NOT_RUN)"
    home, hermes_home, stub_root, env = _world(unusual)
    result = _run(env, hermes_home)
    assert result.returncode == 0, result.stderr
    # the path may be printed literally, but never expanded
    for out in (result.stdout, result.stderr):
        assert "SHOULD_NOT_RUN" not in out.replace(
            "$(printf SHOULD_NOT_RUN)", "")
    agents = home / "Library" / "LaunchAgents"
    llama = plistlib.loads((agents / "ai.mcs.llamaserver.plist").read_bytes())
    watch = plistlib.loads((agents / "org.mcs.recovery.plist").read_bytes())
    assert llama["WorkingDirectory"] == str(hermes_home)
    assert watch["ProgramArguments"][1] == str(home / ".mcs-recovery" / "mcs_recover.py")
    assert any(c.startswith("venv-hermes ") for c in _calls(stub_root))


def test_backslash_home_and_final_instructions_survive(tmp_path):
    """F18/F19: /bin/sh echo would interpret backslashes; the unquoted
    heredoc ate the `\\` line continuations. Paths print literally and
    the printed commands are copy-pasteable (shell-quoted)."""
    home, hermes_home, stub_root, env = _world(tmp_path / "back\\slash\\n dir")
    r = _run(env, hermes_home)
    assert r.returncode == 0, r.stderr
    assert (home / ".mcs-recovery" / "repo_path").read_text() \
        == f"{ROOT}\n"
    venv_py = hermes_home / "hermes-agent" / "venv" / "bin" / "python"
    assert f"'{venv_py}' '{ROOT}/mcs/ops/mcs_setup.py' init\n" in r.stdout
    assert "init --yes \\\n        --login-id <ID>" in r.stdout
    assert f"'{venv_py}' '{ROOT}/mcs/ops/mcs_setup.py' check" in r.stdout
    shim = (home / ".local" / "bin" / "hermes").read_text()
    assert f"exec '{venv_py.parent / 'hermes'}' \"$@\"" in shim


def test_same_release_rerun_keeps_the_real_previous_generation(tmp_path):
    home, hermes_home, stub_root, env = _world(tmp_path)
    rec = home / ".mcs-recovery"
    rec.mkdir(parents=True)
    (rec / "mcs_recover.py").write_bytes(b"# old generation")
    assert _run(env, hermes_home).returncode == 0
    assert (rec / "mcs_recover.py.prev").read_bytes() == b"# old generation"
    assert _run(env, hermes_home).returncode == 0
    assert (rec / "mcs_recover.py.prev").read_bytes() == b"# old generation"


def test_custom_hermes_home_refused_with_services(tmp_path):
    home, _hermes_home, stub_root, env = _world(tmp_path)
    custom = tmp_path / "elsewhere"
    r = _run(env, custom)
    assert r.returncode == 2 and "HERMES_HOME" in r.stderr
    r2 = _run(env, custom, "--no-services")
    assert r2.returncode == 0, r2.stderr
    # F7: child hermes commands act on the home being installed
    assert f"hermes-env HERMES_HOME={custom}" in _calls(stub_root)


def test_hermes_home_is_normalised_before_comparing(tmp_path):
    """F8: a trailing slash or a relative spelling of ~/.hermes is the
    default home, not a 'custom' one."""
    home, hermes_home, stub_root, env = _world(tmp_path)
    r = _run(env, f"{hermes_home}/")
    assert r.returncode == 0, r.stderr
    r2 = _run(env, ".hermes", cwd=home)
    assert r2.returncode == 0, r2.stderr
    assert (home / ".mcs-recovery" / "repo_path").exists()


def test_env_hermes_home_is_not_silently_overridden(tmp_path):
    """F7: a caller's differing HERMES_HOME is warned about, and every
    child hermes sees the home actually being installed."""
    home, hermes_home, stub_root, env = _world(tmp_path)
    r = _run({**env, "HERMES_HOME": str(tmp_path / "profile-x")},
             hermes_home)
    assert r.returncode == 0, r.stderr
    assert "is NOT used" in r.stderr
    envs = {c for c in _calls(stub_root) if c.startswith("hermes-env")}
    assert envs == {f"hermes-env HERMES_HOME={hermes_home}"}


def test_root_is_refused(tmp_path):
    """F14: nothing is written when run as root."""
    home, hermes_home, stub_root, env = _world(tmp_path)
    r = _run({**env, "STUB_UID": "0"}, hermes_home)
    assert r.returncode == 1
    assert "root" in r.stderr
    assert list(home.iterdir()) == []
    assert _calls(stub_root) == []


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
    assert "no usable Python" in r.stderr
    assert os.access(venv / "bin" / "python", os.X_OK)
    assert any(c.startswith("uv pip install") for c in _calls(stub_root))


def test_failed_pip_install_converges_on_rerun(tmp_path):
    """F2: `uv venv` succeeded, `uv pip install` failed — the re-run
    reuses the venv and retries only the install."""
    home, hermes_home, stub_root, env = _world(tmp_path)
    r1 = _run({**env, "STUB_FAIL_PIP": "1"}, hermes_home)
    assert r1.returncode == 1
    assert "installing hermes-agent" in r1.stderr
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
    home, hermes_home, stub_root, env = _world(tmp_path)
    r1 = _run({**env, "STUB_FAIL_CHECKOUT_ONCE": "1"}, hermes_home)
    assert r1.returncode == 1
    assert "pinned" not in r1.stdout
    assert (hermes_home / ".hermes-agent.install-in-progress").exists()
    r2 = _run(env, hermes_home)
    assert r2.returncode == 0, r2.stderr
    head = (hermes_home / "hermes-agent" / ".git" / "HEAD").read_text()
    assert head.strip() == _pin()
    assert "not pinned" not in r2.stderr
    assert not (hermes_home / ".hermes-agent.install-in-progress").exists()
    assert sum(c.startswith("git clone") for c in _calls(stub_root)) == 2


def test_interrupted_clone_is_redone(tmp_path):
    home, hermes_home, stub_root, env = _world(tmp_path)
    r1 = _run({**env, "STUB_FAIL_CLONE": "1"}, hermes_home)
    assert r1.returncode == 1 and "git clone" in r1.stderr
    r2 = _run(env, hermes_home)
    assert r2.returncode == 0, r2.stderr
    head = (hermes_home / "hermes-agent" / ".git" / "HEAD").read_text()
    assert head.strip() == _pin()


def test_unreadable_head_is_fatal(tmp_path):
    home, hermes_home, stub_root, env = _world(tmp_path)
    (hermes_home / "hermes-agent" / ".git").mkdir(parents=True)
    r = _run({**env, "STUB_FAIL_REVPARSE": "1"}, hermes_home)
    assert r.returncode == 1
    assert "cannot read HEAD" in r.stderr


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
    home, hermes_home, stub_root, env = _world(tmp_path)
    shim = home / ".local" / "bin" / "hermes"
    shim.parent.mkdir(parents=True)
    shim.write_text("#!/bin/sh\n# my own hermes launcher\n")
    r = _run(env, hermes_home)
    assert r.returncode == 0, r.stderr
    assert shim.read_text() == "#!/bin/sh\n# my own hermes launcher\n"
    assert "not the MCS shim" in r.stderr


def test_other_checkout_requires_force_repo(tmp_path):
    """F6: an install recorded for another checkout is not silently
    repointed; --force-repo switches it."""
    home, hermes_home, stub_root, env = _world(tmp_path)
    other = tmp_path / "other-checkout"
    other.mkdir()
    rec = home / ".mcs-recovery"
    rec.mkdir(parents=True)
    (rec / "repo_path").write_text(f"{other}\n")
    r = _run(env, hermes_home)
    assert r.returncode == 1
    assert "--force-repo" in r.stderr and str(other) in r.stderr
    assert (rec / "repo_path").read_text() == f"{other}\n"
    assert _calls(stub_root) == []                     # nothing ran
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


def test_data_dirs_exist_before_services_bootstrap(tmp_path):
    """F12: WatchPaths/log dirs exist (0700) before agents load."""
    home, hermes_home, stub_root, env = _world(tmp_path)
    r = _run(env, hermes_home)
    assert r.returncode == 0, r.stderr
    assert "datadirs-ready" in _calls(stub_root)
    for d in ("data", "data/cmd", "data/cmd_int"):
        assert (home / ".mcs" / d).stat().st_mode & 0o777 == 0o700


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
    assert "plist changed" in r.stderr and "launchctl bootout" in r.stderr
    assert len(_bootstraps(stub_root, "ai.mcs.llamaserver")) == 2


def test_unloaded_hermes_llamacpp_plist_gets_a_load_hint(tmp_path):
    home, hermes_home, stub_root, env = _world(tmp_path)
    agents = home / "Library" / "LaunchAgents"
    agents.mkdir(parents=True)
    (agents / "ai.hermes.llamacpp.plist").write_text("<plist/>")
    r = _run(env, hermes_home)
    assert r.returncode == 0, r.stderr
    assert "NOT loaded" in r.stderr and "launchctl bootstrap" in r.stderr
    assert not (agents / "ai.mcs.llamaserver.plist").exists()


def test_model_download_resumes_from_part(tmp_path):
    """F15: a dropped download keeps .part; the re-run resumes it."""
    home, hermes_home, stub_root, env = _world(tmp_path)
    r1 = _run({**env, "STUB_FAIL_MODEL_ONCE": "1"}, hermes_home)
    assert r1.returncode == 1 and "resume" in r1.stderr
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
    r = _run({**env, "STUB_MODEL_SIZE": str(part.stat().st_size)},
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
    plan = r.stdout[r.stdout.index("=== plan"):]
    assert "hermes-managed" in plan and "is kept" in plan
    assert "ai.mcs.llamaserver" not in plan


def test_model_checksum_mismatch_is_fatal(tmp_path):
    home, hermes_home, stub_root, env = _world(tmp_path)
    r = _run({**env, "MCS_MODEL_SHA256": "0" * 64}, hermes_home)
    assert r.returncode == 1 and "checksum mismatch" in r.stderr
    assert not (hermes_home / "models" / "Qwen3.5-9B-Q4_K_M.gguf").exists()


def test_cdpath_does_not_corrupt_repo(tmp_path):
    """F17: with CDPATH set, `cd rel` prints and may pick a decoy dir."""
    home, hermes_home, stub_root, env = _world(tmp_path)
    decoy = tmp_path / "cdpath" / ROOT.name
    decoy.mkdir(parents=True)
    r = _run({**env, "CDPATH": str(decoy.parent)}, hermes_home, "--dry-run",
             script=Path(ROOT.name) / "install.sh", cwd=ROOT.parent)
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"repo_path = {ROOT}\n" in r.stdout
    assert str(decoy) not in r.stdout


def test_preflight_all_ok_writes_nothing(tmp_path):
    home, hermes_home, stub_root, env = _world(tmp_path)
    r = _run(env, None, "--preflight")
    assert r.returncode == 0, r.stdout
    assert "0 blocker(s)" in r.stdout
    assert "  NG " not in r.stdout
    assert list(home.iterdir()) == []
    assert not any(c.split()[0] in ("brew", "uv", "launchctl")
                   and "print" not in c for c in _calls(stub_root))
    # the alias is the same mode
    assert _run(env, None, "--check-only").returncode == 0


def test_preflight_reports_blockers_with_fixes(tmp_path):
    home, hermes_home, stub_root, env = _world(tmp_path, brew=False)
    other = tmp_path / "other-checkout"
    other.mkdir()
    rec = home / ".mcs-recovery"
    rec.mkdir()
    (rec / "repo_path").write_text(f"{other}\n")
    r = _run({**env, "STUB_UID": "0", "STUB_DF_KB": "1000",
              "STUB_CURL_FAIL": "github.com"}, None, "--preflight")
    assert r.returncode == 1
    out = r.stdout
    for text in ("NG    running as root", "NG    Homebrew not installed",
                 "NG    disk: only", "NG    network: cannot reach github.com",
                 "NG    existing install points at another checkout"):
        assert text in out, out
    assert out.count("fix: ") >= 5
    assert "5 blocker(s)" in out
    assert sorted(p.name for p in home.iterdir()) == [".mcs-recovery"]


def test_preflight_flags_worktree(tmp_path):
    """A copy of install.sh next to a `.git` file is a git worktree."""
    home, hermes_home, stub_root, env = _world(tmp_path)
    wt = tmp_path / "wt"
    wt.mkdir()
    shutil.copy(INSTALL, wt / "install.sh")
    (wt / ".git").write_text("gitdir: /elsewhere/.git/worktrees/wt\n")
    r = _run(env, None, "--preflight", script=wt / "install.sh")
    assert "git worktree" in r.stdout


def test_dry_run_prints_plan_and_writes_nothing(tmp_path):
    home, hermes_home, stub_root, env = _world(tmp_path)
    r = _run(env, None, "--dry-run", "--no-llm")
    assert r.returncode == 0, r.stdout
    assert "plan (--dry-run" in r.stdout
    assert "4/6 llm        skipped" in r.stdout
    assert "mcs_setup.py services" in r.stdout
    assert list(home.iterdir()) == []


STOPPED = "installation stopped; repair the failed stage and re-run install.sh"


@pytest.mark.parametrize("switch,message", [
    ("STUB_FAIL_PLUGIN", "plugins enable failed"),
    ("STUB_FAIL_SERVICES", "services reported problems"),
    ("STUB_FAIL_WATCHDOG", "watchdog bootstrap failed"),
    # exit 0 is not proof: the label must answer `print` afterwards
    ("STUB_WATCHDOG_NOLOAD", "watchdog bootstrap failed"),
])
def test_failed_stage_stops_install_then_rerun_recovers(
        tmp_path, switch, message):
    """A failed stage is a non-zero stop with a repair hint — never a
    warn-and-continue 'Installed.'. Once repaired, a plain re-run
    converges and the previous recovery generation survives both runs."""
    home, hermes_home, stub_root, env = _world(tmp_path)
    rec = home / ".mcs-recovery"
    rec.mkdir(parents=True)
    old_gen = b"# old generation - only thing that still runs"
    (rec / "mcs_recover.py").write_bytes(old_gen)

    r = _run({**env, switch: "1"}, hermes_home)
    assert r.returncode != 0
    assert message in r.stderr
    assert STOPPED in r.stderr
    assert "Installed." not in r.stdout
    if switch not in ("STUB_FAIL_WATCHDOG", "STUB_WATCHDOG_NOLOAD"):
        # stopped before stage 6 — the old tool is untouched
        assert (rec / "mcs_recover.py").read_bytes() == old_gen
        assert not (rec / "mcs_recover.py.prev").exists()

    r2 = _run(env, hermes_home)
    assert r2.returncode == 0, r2.stderr
    assert STOPPED not in r2.stderr
    assert (rec / "mcs_recover.py.prev").read_bytes() == old_gen
    assert (rec / "mcs_recover.py").read_bytes() == (
        ROOT / "deployment" / "recovery" / "mcs_recover.py").read_bytes()
    assert not list(rec.glob("*.tmp"))


def test_transient_watchdog_bootstrap_error_is_retried(tmp_path):
    """launchd's transient EIO right after bootout is retried, not a
    stopped install."""
    home, hermes_home, stub_root, env = _world(tmp_path)
    r = _run({**env, "STUB_FAIL_WATCHDOG_ONCE": "1"}, hermes_home)
    assert r.returncode == 0, r.stderr
    assert STOPPED not in r.stderr
    assert "recovery watchdog loaded" in r.stdout


def test_standalone_documented_service_preserves_literal_paths(tmp_path):
    from mcs_standalone.service import render
    root = tmp_path / "home & space"
    _, body = render(root, platform="darwin")
    spec = plistlib.loads(body.encode())
    assert spec["ProgramArguments"] == [str(root / "venv/bin/python3"), "-m",
                                         "mcs_standalone", "run", "--root", str(root)]
    assert spec["StandardOutPath"] == str(root / "data/standalone.log")
    guide = (ROOT / "docs/guides/INSTALLATION.md").read_text()
    assert "./install.sh --mode standalone" in guide
    assert "$PY mcs/ops/mcs_setup.py check" in guide


def test_standalone_installer_has_no_hermes_checkout_or_commands(tmp_path):
    import json
    import sys
    home, _, stub, env = _world(tmp_path)
    env["STUB_REAL_PYTHON"] = sys.executable
    first = _run(env, None, "--mode", "standalone", "--no-llm")
    assert first.returncode == 0, first.stdout + first.stderr
    assert json.loads((home / ".mcs/config.json").read_text())["runtime_mode"] == "standalone"
    calls = _calls(stub)
    text = "\n".join(calls)
    assert "requirements-standalone.txt" in text
    assert "hermes-agent" not in text
    assert "venv-hermes" not in text
    assert not (home / ".hermes").exists()
    assert (home / ".mcs/venv/bin/python3").exists()
    before = sum(call.startswith("uv pip install") for call in calls)
    assert before == 1
    second = _run(env, None, "--no-llm")
    assert second.returncode == 0, second.stdout + second.stderr
    assert sum(call.startswith("uv pip install") for call in _calls(stub)) == before
    assert "hermes-agent" not in "\n".join(_calls(stub))


def test_standalone_preflight_does_not_write_runtime(tmp_path):
    home, _, stub, env = _world(tmp_path)
    result = _run(env, None, "--mode", "standalone", "--preflight", "--no-llm")
    assert result.returncode == 0, result.stdout + result.stderr
    assert not (home / ".mcs").exists() and not (home / ".hermes").exists()
    assert not any(call.startswith(("git clone", "uv pip")) for call in _calls(stub))
