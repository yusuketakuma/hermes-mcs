"""Synthetic install sh testkit contracts; shared helpers preserve isolated runtime state."""
import hashlib
import os
import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]

INSTALL = ROOT / "install.sh"

SAVED_FLAGS = ['--no-brew', '--no-llm', '--no-plugin', '--no-recovery']

CLONE_HEAD = "1" * 40

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
      fetch)
        [ -z "$STUB_FAIL_FETCH" ] || exit 128
        exit 0 ;;
      symbolic-ref)
        [ -n "$STUB_GIT_BRANCH" ] || exit 1
        echo "refs/heads/$STUB_GIT_BRANCH"; exit 0 ;;
      status)
        [ -z "$STUB_GIT_DIRTY" ] || echo " M synthetic"
        exit 0 ;;
      ls-files)
        [ -z "$STUB_FAIL_INDEX" ] || exit 128
        printf '%s synthetic\\n' "${STUB_GIT_INDEX_FLAG:-H}"
        exit 0 ;;
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
    pwd > "$STUB_ROOT/uv-pip-cwd"
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
  *v1/models*)
    if [ -n "$STUB_LLM_SERVING" ]; then
        if [ -n "$STUB_MODEL_RESPONSE" ]; then printf '%s' "$STUB_MODEL_RESPONSE"
        else printf '%s' '{"data":[{"id":"synthetic-model"}]}' ; fi
        exit 0
    fi
    exit 7 ;;
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
    # ambient python3 answers version probes as the supported 3.14 runtime
    "python3": """#!/bin/sh
echo "python3 $*" >> "$STUB_LOG"
case "$*" in
  *prepare_recovery.py*) exec "$STUB_PARSE_PYTHON" "$@" ;;
  *install_options.py*) exec "$STUB_PARSE_PYTHON" -B "$STUB_ROOT/install-options-runner.py" "$@" ;;
  *mcs-model-list-probe*|*mcs-runtime-migration-lock*|*mcs-installer-inherited-lock*|*requires_python_admits.py*) exec "$STUB_PARSE_PYTHON" "$@" ;;
esac
if [ "$1" = "-I" ] && [ "$2" = "-B" ] && [ "$3" = "-c" ]; then
    exec "$STUB_PARSE_PYTHON" -I -B "$STUB_ROOT/runtime-check.py" "$@"
fi
if [ "$1" = "-" ] && [ "$2" = "$HOME/.mcs/config.json" ]; then
    exec "$STUB_PARSE_PYTHON" "$@"
fi
exit 0
""",
    "ps": """#!/bin/sh
[ -z "$STUB_PS_FAIL" ] || exit 1
[ -z "$STUB_VENV_PROCESS" ] || printf '%s\\n' "$STUB_VENV_PROCESS"
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

VENV_PYTHON = """#!/bin/sh
echo "venv-python $*" >> "$STUB_LOG"
case "$*" in
  *install_options.py*) exec "$STUB_PARSE_PYTHON" -B "$STUB_ROOT/install-options-runner.py" "$@" ;;
  *mcs-model-list-probe*|*mcs-runtime-migration-lock*|*mcs-installer-inherited-lock*|*requires_python_admits.py*) exec "$STUB_PARSE_PYTHON" "$@" ;;
esac
if [ "$1" = "-I" ] && [ "$2" = "-B" ] && [ "$3" = "-c" ]; then
    exec "$STUB_PARSE_PYTHON" -I -B "$STUB_ROOT/runtime-check.py" "$@"
fi
if [ "$1" = "-" ] && [ "$2" = "$HOME/.mcs/config.json" ]; then
    exec "$STUB_PARSE_PYTHON" "$@"
fi
if [ "$1" = "-" ] && [ -n "$STUB_REAL_PYTHON" ]; then
    exec "$STUB_REAL_PYTHON" -B "$STUB_ROOT/runtime-check.py" --persist "$@"
fi
case "$*" in
  *"mcs_setup.py init"*) exit "${STUB_SETUP_EXIT:-0}" ;;
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
  cron) [ "$2" = list ] && [ "$3" = --all ] || exit 1
        echo "No scheduled jobs in profile 'default'." ;;
  config) exit 1 ;;
  plugins) [ -z "$STUB_FAIL_PLUGIN" ] || exit 1 ;;
esac
exit 0
"""

STUB_MODEL_SHA256 = hashlib.sha256(b"stub-model-bytes\n").hexdigest()

STOPPED = "導入を中断しました。失敗した段階の原因を確認してください。"


def _pin():
    m = re.search(r'^HERMES_PIN="([0-9a-f]{40})"$',
                  INSTALL.read_text(), re.M)
    assert m, "install.sh must pin a 40-hex Hermes commit"
    return m.group(1)


def _world(tmp_path, *, brew=True, git_head=None, path_hermes=False, configured=False):
    home = tmp_path / "home"
    hermes_home = home / ".hermes"
    stub_root = tmp_path / "stub"
    bin_dir = stub_root / "bin"
    bin_dir.mkdir(parents=True)
    (stub_root / "state").mkdir(parents=True)
    home.mkdir(parents=True)
    if configured:
        data = home / '.mcs/data'
        data.mkdir(mode=0o700, parents=True)
        config = data.parent / 'config.json'
        config.write_text('{"runtime_mode":"hermes"}')
        config.chmod(0o600)
    stubs = dict(STUBS)
    if not brew:
        del stubs["brew"]
    if path_hermes:        # a Hermes コマンド that is not our venv
        stubs["hermes"] = '#!/bin/sh\necho "path-hermes $*" >> "$STUB_LOG"\n'
    # find_py prefers python3.14 before python3; shadow them so a
    # host Python (e.g. /usr/bin/python3.12 on Linux CI) never replaces the stub.
    for version in ("python3.14",):
        stubs.setdefault(version, "#!/bin/sh\nexit 1\n")
    for name, body in stubs.items():
        p = bin_dir / name
        p.write_text(body)
        p.chmod(0o755)
    for name, body in (("venv-python", VENV_PYTHON),
                       ("venv-hermes", VENV_HERMES)):
        (stub_root / name).write_text(body)
        (stub_root / name).chmod(0o755)
    # Every install_options child uses this isolated runner. No command is
    # spawned: worker and cron listings are synthetic, while the real cron
    # parser and fresh-install checks still consume their output.
    (stub_root / "install-options-runner.py").write_text(f"""\
import os, socket, subprocess, sys
from pathlib import Path
root = Path({str(ROOT)!r})
sys.path.insert(0, str(root / 'mcs'))
def blocked(*args, **kwargs):
    raise AssertionError('non-fixture operation denied')
socket.create_connection = blocked
socket.getaddrinfo = blocked
socket.socket.connect = blocked
socket.socket.connect_ex = blocked
socket.socket.sendto = blocked
subprocess.Popen = blocked

def synthetic_run(command, *args, **kwargs):
    if args or not isinstance(command, list) or kwargs.get('shell'):
        return blocked()
    responses = {{
        ('/bin/ps', '-axo', 'pid=,args='): '',
        ('/bin/launchctl', 'list'): 'PID Status Label\\n',
        ('/usr/bin/systemctl', '--user', 'list-units', '--all', '--plain', '--no-legend'): '',
    }}
    key = tuple(command)
    if key in responses:
        with open(os.environ['STUB_LOG'], 'a') as log:
            log.write('synthetic-inventory ' + command[0] + '\\n')
        return subprocess.CompletedProcess(command, 0, responses[key], '')
    if len(command) == 4 and command[1:] == ['cron', 'list', '--all']:
        home = Path(os.environ['HOME']).resolve()
        executable = Path(command[0])
        try:
            resolved = executable.resolve(strict=True)
            resolved.relative_to(home)
            contents = resolved.read_text()
        except (OSError, ValueError, UnicodeError):
            return blocked()
        fixture = (Path(os.environ['STUB_ROOT']) / 'venv-hermes').read_text()
        unknown = '#!/bin/sh\\nprintf "synthetic-hermes %s\\\\n" "$*" >> "$STUB_LOG"\\nexit 7\\n'
        # The installer publishes a shell shim at ~/.local/bin/hermes. Verify
        # its exact body and its fixture target, but never execute either.
        if resolved == home / '.local' / 'bin' / 'hermes':
            target = Path(os.environ.get('MCS_FRESH_HERMES_EXE', ''))
            try:
                resolved_target = target.resolve(strict=True)
                resolved_target.relative_to(home)
                quoted = chr(39) + str(target).replace(chr(39), chr(39) + chr(92) + chr(39) + chr(39)) + chr(39)
                shim = '#!/usr/bin/env bash\\nunset PYTHONPATH\\nunset PYTHONHOME\\nexec ' + quoted + ' "$@"\\n'
                if contents == shim and resolved_target.read_text() == fixture:
                    contents = fixture
            except (OSError, ValueError, UnicodeError):
                return blocked()
        if contents == fixture:
            with open(os.environ['STUB_LOG'], 'a') as log:
                log.write('venv-hermes cron list --all\\n')
            return subprocess.CompletedProcess(command, 0, "No scheduled jobs in profile 'default'.\\n", '')
        if contents == unknown:
            with open(os.environ['STUB_LOG'], 'a') as log:
                log.write('synthetic-hermes cron list --all\\n')
            return subprocess.CompletedProcess(command, 7, '', '')
    return blocked()
subprocess.run = synthetic_run
if sys.argv[1:] == ['--fixture-self-check']:
    for command in (['/bin/ps', '-axo', 'pid=,args='], ['/bin/launchctl', 'list'],
                    ['/usr/bin/systemctl', '--user', 'list-units', '--all', '--plain', '--no-legend']):
        assert subprocess.run(command, capture_output=True, text=True).returncode == 0
    for call in (lambda: subprocess.run(['/bin/echo', 'unsafe']),
                 lambda: subprocess.Popen(['/bin/echo', 'unsafe'])):
        try:
            call()
        except AssertionError:
            pass
        else:
            raise AssertionError('unexpected process allowed')
    raise SystemExit(0)
import _mcs_path
import install_options
index = next(i for i, arg in enumerate(sys.argv) if Path(arg).name == 'install_options.py')
with open(os.environ['STUB_LOG'], 'a') as log:
    log.write('fresh-runner ' + sys.argv[index + 1] + '\\n')
raise SystemExit(install_options.main(sys.argv[index + 1:]))
""")
    # Run the production template parser and metadata validator, replacing
    # only the target interpreter's process result with synthetic facts.
    (stub_root / "runtime-check.py").write_text(f"""
import json, os, runpy, subprocess, sys
fixture_home = os.environ['HOME']
fixture_root = os.environ.get('MCS_ROOT', os.path.join(fixture_home, '.mcs'))
runpy.run_path({str(ROOT / 'tests/conftest.py')!r})
os.environ['HOME'] = fixture_home
os.environ['MCS_ROOT'] = fixture_root
import mcs_util
mcs_util.HOME = os.environ['MCS_ROOT']
mcs_util.CONF_PATH = os.path.join(mcs_util.HOME, 'config.json')
sys.path.insert(0, {str(ROOT / 'mcs')!r})
import _mcs_path
import runtime_inspection
def metadata(argv, **kwargs):
    with open(os.environ['STUB_LOG'], 'a') as stream:
        stream.write('runtime-metadata ' + argv[0] + '\\n')
    case = os.environ.get('STUB_RECOVERY_RUNTIME', 'safe')
    facts = {{'python': '3.14.8', 'sqlite': '3.51.3',
              'packages': {{'discord.py': None, 'slack-bolt': None, 'slack-sdk': None}}}}
    if case == 'unsafe':
        facts['sqlite'] = '3.51.0'
    if case == 'unsupported':
        facts['python'] = '3.8.10'
    return subprocess.CompletedProcess(
        argv, 127 if case == 'failed' else 0,
        'not-json' if case == 'unknown' else json.dumps(facts), '')
runtime_inspection.run_bounded = metadata
runtime_inspection.recovery_python = lambda cfg: cfg.get("recovery_python", os.path.join(os.environ["STUB_ROOT"], "bin/python3.14"))
if sys.argv[1] == '--persist':
    code = sys.stdin.read()
    sys.argv = sys.argv[2:]
else:
    index = sys.argv.index('-c')
    code = sys.argv[index + 1]
    sys.argv = ['-c', *sys.argv[index + 2:]]
exec(compile(code, '<installer-runtime-check>', 'exec'))
""")
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
        "STUB_PARSE_PYTHON": sys.executable,
        "STUB_UID": str(os.getuid()),
        "STUB_REAL_PYTHON": sys.executable,
        # sha256 of the stub curl's model bytes ("stub-model-bytes\n")
        "MCS_MODEL_SHA256": STUB_MODEL_SHA256,
        "MCS_ALLOW_MODEL_SHA_OVERRIDE": "1",
    })
    return home, hermes_home, stub_root, env


def _run(env, hermes_home, *flags, script=INSTALL, cwd=None, pass_fds=()):
    args = [*flags] + ([str(hermes_home)] if hermes_home is not None else [])
    return subprocess.run(
        ["sh", str(script), *args], cwd=cwd,
        env=env, capture_output=True, text=True, timeout=60, pass_fds=pass_fds)


def _run_reinstall(env, hermes_home, *flags):
    import fcntl
    data = Path(env['HOME']) / '.mcs/data'
    data.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(data / 'update.lock', os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return _run({**env, 'MCS_UPDATE_REINSTALL': '1', 'MCS_UPDATE_LOCK_FD': str(fd)},
                    hermes_home, *flags, pass_fds=(fd,))
    finally:
        os.close(fd)


def _calls(stub_root):
    log = stub_root / "calls.log"
    return [line for line in log.read_text().splitlines() if line.strip()] if log.exists() else []


def _bootstraps(stub_root, label):
    return [c for c in _calls(stub_root)
            if c.startswith("launchctl bootstrap") and label in c]


def _real_config_writer(env):
    env["STUB_REAL_PYTHON"] = sys.executable
    return env


def _inject_before_config_persistence(stub_root, code):
    script = stub_root / "runtime-check.py"
    text = script.read_text()
    marker = "exec(compile(code, '<installer-runtime-check>', 'exec'))"
    assert text.count(marker) == 1
    script.write_text(text.replace(marker, "if sys.argv[0] == '-':\n" +
                                   "\n".join("    " + line for line in code.splitlines()) +
                                   "\n" + marker))


def _race_config_writer(stub_root, raw, *, symlink=False, mode=0o600, swap_parent=False, hardlink=False):
    script = stub_root / "runtime-check.py"
    text = script.read_text()
    marker = "exec(compile(code, '<installer-runtime-check>', 'exec'))"
    assert text.count(marker) == 1
    inject = f"""
if sys.argv[0] == '-':
    from pathlib import Path
    original_link = os.link
    def race_link(src, dst, **kwargs):
        target = Path(os.environ['HOME']) / '.mcs' / 'config.json'
        if {swap_parent!r}:
            parent = target.parent
            alternate = parent.parent / 'synthetic-alternate'
            alternate.mkdir(mode=0o700)
            parent.rename(parent.parent / 'synthetic-original')
            parent.symlink_to(alternate, target_is_directory=True)
            return original_link(src, dst, **kwargs)
        if {symlink!r}:
            other = target.parent.parent / 'synthetic-target.json'
            other.write_text({raw!r})
            target.symlink_to(other)
        else:
            target.write_text({raw!r})
            target.chmod({mode!r})
            if {hardlink!r}:
                original_link(target, target.parent.parent / 'synthetic-linked-config')
        return original_link(src, dst, **kwargs)
    os.link = race_link
"""
    script.write_text(text.replace(marker, inject + marker))


def _without_supported_python(stub_root):
    stub = stub_root / "bin" / "python3"
    stub.write_text('#!/bin/sh\n[ "$1" = "-c" ] && exit 1\nexit 0\n')  # e.g. a 3.9 system Python
