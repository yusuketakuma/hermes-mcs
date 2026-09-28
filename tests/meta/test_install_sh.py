"""install.sh — synthetic end-to-end under a stubbed PATH.

Every external command the installer reaches for (brew, git, hermes,
uv, curl, launchctl, llama-server, uname) is a stub in $STUB/bin; HOME
and HERMES_HOME are temp dirs, so nothing real is touched — no real
checkout, no real services, no network. mcs_setup's own suite
(tests/ops/test_mcs_setup.py) covers stage 5 internals; here `python3`
is a recording stub so the SHELL contract — stage ordering, idempotent
rerun, partial-failure recovery — is what is tested.
"""
import os
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
INSTALL = ROOT / "install.sh"


def _pin():
    m = re.search(r'^HERMES_PIN="([0-9a-f]{40})"$',
                  INSTALL.read_text(), re.M)
    assert m, "install.sh must pin a 40-hex Hermes commit"
    return m.group(1)


STUBS = {
    "brew": """#!/bin/sh
echo "brew $*" >> "$STUB_LOG"
if [ "$1" = "--prefix" ]; then echo "$STUB_ROOT/brew-prefix"; exit 0; fi
exit 0
""",
    "git": """#!/bin/sh
echo "git $*" >> "$STUB_LOG"
last=""
for a in "$@"; do last="$a"; done
if [ "$1" = "clone" ]; then mkdir -p "$last/.git"; exit 0; fi
if [ "$1" = "-C" ]; then
    if [ "$3" = "rev-parse" ]; then echo "$STUB_GIT_HEAD"; exit 0; fi
    exit 0
fi
exit 0
""",
    "uv": """#!/bin/sh
echo "uv $*" >> "$STUB_LOG"
last=""
for a in "$@"; do last="$a"; done
if [ "$1" = "venv" ]; then
    mkdir -p "$last/bin"
    cat > "$last/bin/hermes" <<'INNER'
#!/bin/sh
echo "venv-hermes $*" >> "$STUB_LOG"
case "$1" in
  config) exit 1 ;;
esac
exit 0
INNER
    chmod +x "$last/bin/hermes"
fi
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
  *v1/models*) exit 1 ;;        # llama-server is never answering
esac
if [ -n "$out" ]; then echo "stub-model-bytes" > "$out"; fi
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
    touch "$STUB_STATE/loaded/$label"; exit 0 ;;
  bootout)
    label="${2##*/}"
    rm -f "$STUB_STATE/loaded/$label"; exit 0 ;;
esac
exit 0
""",
    "python3": """#!/bin/sh
echo "python3 $*" >> "$STUB_LOG"
exit 0
""",
    "llama-server": """#!/bin/sh
echo "llama-server $*" >> "$STUB_LOG"
exit 0
""",
    "uname": """#!/bin/sh
echo Darwin
""",
    "id": """#!/bin/sh
if [ "$1" = "-u" ]; then echo 501; exit 0; fi
exit 0
""",
}


def _world(tmp_path, *, brew=True, git_head=None):
    home = tmp_path / "home"
    hermes_home = home / ".hermes"
    stub_root = tmp_path / "stub"
    bin_dir = stub_root / "bin"
    bin_dir.mkdir(parents=True)
    (stub_root / "state").mkdir(parents=True)
    home.mkdir(parents=True)
    for name, body in STUBS.items():
        if name == "brew" and not brew:
            continue
        p = bin_dir / name
        p.write_text(body)
        p.chmod(0o755)
    env = dict(os.environ)
    env.update({
        "HOME": str(home),
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "STUB_ROOT": str(stub_root),
        "STUB_LOG": str(stub_root / "calls.log"),
        "STUB_STATE": str(stub_root / "state"),
        "STUB_GIT_HEAD": git_head or _pin(),
    })
    return home, hermes_home, stub_root, env


def _run(env, hermes_home, *flags):
    return subprocess.run(
        ["sh", str(INSTALL), *flags, str(hermes_home)],
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

    r2 = _run(env, hermes_home)
    assert r2.returncode == 0, r2.stderr
    # llama-server: plist exists -> single bootstrap across both runs
    assert len(_bootstraps(stub_root, "ai.mcs.llamaserver")) == 1
    # watchdog: bootout+bootstrap is a reload — still exactly one label
    assert len(list((stub_root / "state" / "loaded").iterdir())) == 2
    # previous recovery generation retained, current refreshed
    prev = home / ".mcs-recovery" / "mcs_recover.py.prev"
    assert prev.read_bytes() == first_gen
    assert recovery.read_bytes() == first_gen   # same repo generation
    # no second plugin dir/link objects appeared
    assert len(list((hermes_home / "plugins").iterdir())) == 1


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
    only warned about — never cloned over or checked out."""
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
    assert not any(c.startswith("git clone") for c in _calls(stub_root))


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


def test_unknown_option_is_bounded_error(tmp_path):
    home, hermes_home, _, env = _world(tmp_path)
    r = _run(env, hermes_home, "--bogus")
    assert r.returncode == 2
    assert "unknown option" in r.stderr


def test_install_paths_remain_literal_shell_and_xml_data(tmp_path):
    import plistlib
    unusual = tmp_path / "space & <tag> 'quote' $(printf SHOULD_NOT_RUN)"
    home, hermes_home, stub_root, env = _world(unusual)
    result = _run(env, hermes_home)
    assert result.returncode == 0, result.stderr
    assert "SHOULD_NOT_RUN" not in result.stderr
    agents = home / "Library" / "LaunchAgents"
    llama = plistlib.loads((agents / "ai.mcs.llamaserver.plist").read_bytes())
    watch = plistlib.loads((agents / "org.mcs.recovery.plist").read_bytes())
    assert llama["WorkingDirectory"] == str(hermes_home)
    assert watch["ProgramArguments"][1] == str(home / ".mcs-recovery" / "mcs_recover.py")
    assert any(c.startswith("venv-hermes ") for c in _calls(stub_root))
