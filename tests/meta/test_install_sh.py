"""Synthetic install sh contracts; shared helpers preserve isolated runtime state."""
import os
import json
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path
import pytest
from install_sh_testkit import CLONE_HEAD as CLONE_HEAD, INSTALL as INSTALL, ROOT as ROOT, SAVED_FLAGS as SAVED_FLAGS, STOPPED as STOPPED, STUBS as STUBS, STUB_MODEL_SHA256 as STUB_MODEL_SHA256, VENV_HERMES as VENV_HERMES, VENV_PYTHON as VENV_PYTHON, _bootstraps as _bootstraps, _calls as _calls, _inject_before_config_persistence as _inject_before_config_persistence, _pin as _pin, _race_config_writer as _race_config_writer, _real_config_writer as _real_config_writer, _run as _run, _run_reinstall as _run_reinstall, _without_supported_python as _without_supported_python, _world as _world


def test_install_options_fixture_denies_unexpected_subprocesses(tmp_path):
    _, _, stub_root, env = _world(tmp_path)
    result = subprocess.run(
        [sys.executable, '-B', str(stub_root / 'install-options-runner.py'), '--fixture-self-check'],
        env=env, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert sum(line.startswith('synthetic-inventory ') for line in _calls(stub_root)) == 3


@pytest.mark.parametrize('prior_state', ['absent', 'empty_data', 'lock_only'])
def test_fresh_install_reaches_bootstrap_without_python(tmp_path, prior_state):
    home, hermes_home, stub_root, env = _world(tmp_path)
    lock = home / '.mcs/data/update.lock'
    if prior_state != 'absent':
        lock.parent.mkdir(mode=0o700, parents=True)
    if prior_state == 'lock_only':
        lock.touch(mode=0o600)
    (stub_root / 'bin/python3').write_text('#!/bin/sh\nexit 1\n')
    (stub_root / 'bin/python3.14').write_text('#!/bin/sh\nexit 1\n')
    (stub_root / 'bin/brew').write_text('#!/bin/sh\necho "brew $*" >> "$STUB_LOG"\nexit 1\n')
    result = _run(env, hermes_home, '--no-llm', '--no-recovery')
    assert result.returncode != 0
    if prior_state == 'absent':
        assert any(line.startswith('brew ') for line in _calls(stub_root))
        assert (lock.parent / '.provider-bootstrap-fresh.json').is_file()
    else:
        assert not any(line.startswith('brew ') for line in _calls(stub_root))
        assert not (lock.parent / '.provider-bootstrap-fresh.json').exists()
    assert not any(line.startswith('python3 ') for line in _calls(stub_root))
    assert lock.is_file() and lock.stat().st_mode & 0o777 == 0o600
    assert not (home / '.mcs/config.json').exists()
    assert not any(line.startswith(('git clone', 'uv ', 'launchctl bootstrap')) for line in _calls(stub_root))
    if prior_state == 'absent':
        marker = lock.parent / '.provider-bootstrap-fresh.json'
        before = marker.read_bytes()
        calls = _calls(stub_root)
        retry = _run(env, hermes_home, '--no-llm', '--no-recovery')
        assert retry.returncode != 0
        assert marker.read_bytes() == before
        assert [line for line in _calls(stub_root) if line.startswith('brew ')] == [
            line for line in calls if line.startswith('brew ')]


def _claim_marker_text(root, data):
    root_stat, data_stat = root.stat(), data.stat()
    return ('{"version":1,"root":[%d,%d,%d,%d],"data":[%d,%d,%d,%d],"config":null}\n'
            % (root_stat.st_dev, root_stat.st_ino, root_stat.st_uid, root_stat.st_mode & 0o7777,
               data_stat.st_dev, data_stat.st_ino, data_stat.st_uid, data_stat.st_mode & 0o7777))


def _claim_record_text(root):
    root_stat = root.stat()
    return ('{"version":1,"root":[%d,%d,%d,%d]}\n'
            % (root_stat.st_dev, root_stat.st_ino, root_stat.st_uid, root_stat.st_mode & 0o7777))


def _write_claim_record(root):
    import os as _os
    record = root / '.install-claim.json'
    record.write_text(_claim_record_text(root))
    _os.chmod(record, 0o600)
    return record


@pytest.mark.parametrize('claim_state', ['record_only', 'empty_data', 'lock_only', 'partial_tmp'])
def test_interrupted_claim_resumes_as_same_fresh_install(tmp_path, claim_state):
    """A crash after the evidence record but before the marker leaves any of
    these states; the next run completes the same first install instead of
    refusing, because the record proves data/ was absent at claim time."""
    home, hermes_home, stub_root, env = _world(tmp_path)
    root = home / '.mcs'
    root.mkdir(mode=0o700, parents=True)
    record = _write_claim_record(root)
    data = root / 'data'
    if claim_state != 'record_only':
        data.mkdir(mode=0o700)
    if claim_state in ('lock_only', 'partial_tmp'):
        (data / 'update.lock').touch(mode=0o600)
    if claim_state == 'partial_tmp':
        (data / '.provider-bootstrap-fresh.json.tmp').write_text('{"version":1,partial')
    result = _run(env, hermes_home)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (data / 'update.lock').is_file()
    assert (root / 'config.json').is_file()
    # a completed install consumes the bootstrap marker and the evidence
    # record by design; the record only needs to survive until the marker
    # inside data/ is confirmed
    assert not record.exists()


def test_interrupted_claim_resume_writes_the_hold(tmp_path):
    """The resumed first install writes the external-transfer hold: an
    interrupted claim never becomes a legacy install without the hold."""
    home, hermes_home, stub_root, env = _world(tmp_path)
    root = home / '.mcs'
    root.mkdir(mode=0o700, parents=True)
    _write_claim_record(root)
    result = _run(env, hermes_home)
    assert result.returncode == 0, result.stdout + result.stderr
    sys.path.insert(0, str(ROOT / "mcs"))
    import _mcs_path  # noqa: F401
    import external_transfer
    assert external_transfer.status(root)["state"] == "pending"


def test_claim_marker_survives_until_bootstrap(tmp_path):
    """Interrupt software preparation after the claim; the strict trio stays
    in place and a rerun resumes the same first install."""
    home, hermes_home, stub_root, env = _world(tmp_path)
    root = home / '.mcs'
    first = _run({**env, 'STUB_FAIL_CLONE': '1'}, hermes_home)
    assert first.returncode != 0
    data = root / 'data'
    assert (data / 'update.lock').is_file()
    marker = data / '.provider-bootstrap-fresh.json'
    assert marker.read_text() == _claim_marker_text(root, data)
    assert not (root / '.install-claim.json').exists()
    second = _run(env, hermes_home)
    assert second.returncode == 0, second.stdout + second.stderr
    assert '再開します' in second.stdout


@pytest.mark.parametrize('corrupt', ['content', 'symlink', 'not_file'])
def test_invalid_claim_record_refuses_before_writes(tmp_path, corrupt):
    """A claim record that fails validation (not a regular file, a symlink,
    or content that does not match the live root) stops the install before
    any write."""
    home, hermes_home, stub_root, env = _world(tmp_path)
    root = home / '.mcs'
    root.mkdir(mode=0o700, parents=True)
    record = root / '.install-claim.json'
    if corrupt == 'content':
        record.write_text('{"version":1,"root":[0,0,0,0]}\n')
        import os as _os
        _os.chmod(record, 0o600)
    elif corrupt == 'symlink':
        target = root / 'elsewhere.json'
        target.write_text(_claim_record_text(root))
        record.symlink_to(target)
    else:
        record.mkdir(mode=0o700)
    result = _run(env, hermes_home)
    assert result.returncode != 0
    assert '確保記録を確認できません' in result.stderr
    assert not (root / 'data').exists()
    assert not (root / 'config.json').exists()
    assert not any(c.startswith(('brew', 'git clone', 'uv ')) for c in _calls(stub_root))


def test_claim_record_and_config_coexisting_refuses(tmp_path):
    home, hermes_home, stub_root, env = _world(tmp_path)
    root = home / '.mcs'
    root.mkdir(mode=0o700, parents=True)
    _write_claim_record(root)
    (root / 'config.json').write_text('{}')
    result = _run(env, hermes_home)
    assert result.returncode != 0
    assert '両方あります' in result.stderr


def test_foreign_data_after_claim_record_is_same_fresh_install(tmp_path):
    """codex1 race: another process creates data/ after the evidence record
    was written. Because the record proves data/ was absent at claim time,
    the foreign data/ cannot be a legacy install: the run proceeds as the
    same first install and writes the hold. Pin the state right after the
    interrupt point and after the completing rerun."""
    home, hermes_home, stub_root, env = _world(tmp_path)
    root = home / '.mcs'
    root.mkdir(mode=0o700, parents=True)
    record = _write_claim_record(root)
    data = root / 'data'
    data.mkdir(mode=0o700)
    result = _run(env, hermes_home)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (root / 'config.json').is_file()
    assert not record.exists()
    sys.path.insert(0, str(ROOT / "mcs"))
    import _mcs_path  # noqa: F401
    import external_transfer
    assert external_transfer.status(root)["state"] == "pending"


def test_foreign_content_in_claimed_data_stops_without_destroying(tmp_path):
    """A foreign data/ carrying actual content is not a bare race artifact:
    the bootstrap inventory keeps protecting it. The install stops without
    touching the foreign file, and the rerun stops the same way instead of
    ever classifying it as a legacy install."""
    home, hermes_home, stub_root, env = _world(tmp_path)
    root = home / '.mcs'
    root.mkdir(mode=0o700, parents=True)
    _write_claim_record(root)
    data = root / 'data'
    data.mkdir(mode=0o700)
    (data / 'foreign.txt').write_text('foreign\n')
    first = _run(env, hermes_home)
    assert first.returncode != 0
    assert (data / 'foreign.txt').read_text() == 'foreign\n'
    second = _run(env, hermes_home)
    assert second.returncode != 0
    assert (data / 'foreign.txt').read_text() == 'foreign\n'
    # never completed as a legacy install either: no completion evidence,
    # no bootstrap of the foreign data root
    assert not (data / '.provider-bootstrap-complete.json').exists()


def test_root_stat_failure_in_claim_window_resumes_on_rerun(tmp_path):
    """codex1 reproduction: the first root identity probe fails once. In the
    pre-acquisition design the probe happens before the evidence record is
    written, so the interrupted run leaves nothing at all — no record, no
    data/, no config — and the rerun completes the same first install with
    the external-transfer hold."""
    home, hermes_home, stub_root, env = _world(tmp_path)
    stat_stub = stub_root / 'bin/stat'
    stat_stub.write_text("""#!/bin/sh
# fail the first identity-format probe of the exact root path, once
last=""
for arg in "$@"; do last="$arg"; done
if [ ! -f "$STUB_STATE/rootstat-failed" ] && [ "$last" = "$HOME/.mcs" ]; then
    case " $* " in
        *" %d %i %u "*)
            if /usr/bin/stat "$@" >/dev/null 2>&1; then
                touch "$STUB_STATE/rootstat-failed"
                exit 1
            fi ;;
    esac
fi
exec /usr/bin/stat "$@"
""")
    stat_stub.chmod(0o755)
    first = _run(env, hermes_home)
    assert first.returncode != 0
    root = home / '.mcs'
    assert not (root / 'data').exists()
    assert not (root / 'config.json').exists()
    assert not (root / '.install-claim.json').exists()
    second = _run(env, hermes_home)
    assert second.returncode == 0, second.stdout + second.stderr
    assert (root / 'config.json').is_file()
    assert not (root / '.install-claim.json').exists()
    sys.path.insert(0, str(ROOT / "mcs"))
    import _mcs_path  # noqa: F401
    import external_transfer
    assert external_transfer.status(root)["state"] == "pending"


def test_interrupted_claim_before_marker_resumes_on_rerun(tmp_path):
    """An interrupt after the evidence record but before the marker (the
    move-back-failure equivalent in the rename design) leaves record+lock
    without a marker; pin the state right after and the completing rerun."""
    home, hermes_home, stub_root, env = _world(tmp_path)
    root = home / '.mcs'
    root.mkdir(mode=0o700, parents=True)
    record = _write_claim_record(root)
    data = root / 'data'
    data.mkdir(mode=0o700)
    (data / 'update.lock').touch(mode=0o600)
    # right after the interrupt: record present, no marker anywhere
    assert record.exists()
    assert not (data / '.provider-bootstrap-fresh.json').exists()
    assert not (root / 'config.json').exists()
    result = _run(env, hermes_home)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (root / 'config.json').is_file()
    assert not record.exists()
    sys.path.insert(0, str(ROOT / "mcs"))
    import _mcs_path  # noqa: F401
    import external_transfer
    assert external_transfer.status(root)["state"] == "pending"


def _gate_lockf(stub_root):
    """Park a run marked STUB_LOCK_GATE after it opened the lock file and
    before it locks; every run then takes the real lock through the existing
    flock fallback (EX_USAGE), on macOS and Linux alike."""
    lockf = stub_root / 'bin/lockf'
    lockf.write_text('''#!/bin/sh
echo "lockf $*" >> "$STUB_LOG"
if [ -n "${STUB_LOCK_GATE:-}" ]; then
    touch "$STUB_STATE/lock-reached"
    n=0
    while [ ! -e "$STUB_STATE/lock-release" ]; do
        n=$((n + 1))
        [ "$n" -le 1800 ] || exit 1
        sleep 0.1
    done
fi
exit 64
''')
    lockf.chmod(0o755)


def _start_parked(env, hermes_home, stub_root):
    import time
    late = subprocess.Popen(
        ['sh', str(INSTALL), str(hermes_home)], env={**env, 'STUB_LOCK_GATE': '1'},
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    deadline = time.monotonic() + 60
    while not (stub_root / 'state/lock-reached').exists():
        if late.poll() is not None or time.monotonic() > deadline:
            out, err = late.communicate(timeout=60) if late.poll() is not None else ('', '')
            late.kill()
            raise AssertionError('late run never reached the lock: ' + out + err)
        time.sleep(0.05)
    return late


def _release_parked(late, stub_root):
    (stub_root / 'state/lock-release').touch()
    try:
        out, err = late.communicate(timeout=60)
    finally:
        late.kill()
    return late.returncode, out, err


def test_late_claimant_after_completed_first_install_writes_nothing(tmp_path):
    """Two first installs start together and both classify as claimants
    before the lock. The first completes (install, facility-confirmation
    hold, marker consumed); only then does the late one get the lock. The
    late run must re-read the live state, write no marker and stop, and the
    next run is an ordinary existing install."""
    home, hermes_home, stub_root, env = _world(tmp_path)
    _gate_lockf(stub_root)
    root = home / '.mcs'
    data = root / 'data'
    marker = data / '.provider-bootstrap-fresh.json'
    late = _start_parked(env, hermes_home, stub_root)
    try:
        # the late run wrote the record and parked before locking
        assert (root / '.install-claim.json').is_file()
        first = _run(env, hermes_home)
    except BaseException:
        _release_parked(late, stub_root)
        raise
    assert first.returncode == 0, first.stdout + first.stderr
    config_after_first = (root / 'config.json').read_bytes()
    assert not marker.exists()
    rc, out, err = _release_parked(late, stub_root)
    assert rc != 0, out + err
    assert '別の導入が先に進みました' in err
    # the stale pre-lock decision must not re-create the consumed marker
    assert not marker.exists()
    assert not (root / '.install-claim.json').exists()
    assert (root / 'config.json').read_bytes() == config_after_first
    sys.path.insert(0, str(ROOT / "mcs"))
    import _mcs_path  # noqa: F401
    import external_transfer
    assert external_transfer.status(root)["state"] == "pending"
    following = _run(env, hermes_home)
    assert following.returncode == 0, following.stdout + following.stderr
    assert not marker.exists()
    assert external_transfer.status(root)["state"] == "pending"


def test_record_appearing_while_waiting_for_lock_is_not_legacy(tmp_path):
    """A run that found data/ without a record before the lock (a peer wrote
    the record between its two checks) finds the record once it holds the
    lock. The pre-lock legacy decision is not trusted: the run stops without
    writing, and the rerun resumes the same first install with the hold."""
    import os as _os
    home, hermes_home, stub_root, env = _world(tmp_path)
    _gate_lockf(stub_root)
    root = home / '.mcs'
    data = root / 'data'
    data.mkdir(mode=0o700, parents=True)
    _os.chmod(root, 0o700)
    (data / 'update.lock').touch(mode=0o600)
    late = _start_parked(env, hermes_home, stub_root)
    try:
        record = _write_claim_record(root)
    except BaseException:
        _release_parked(late, stub_root)
        raise
    rc, out, err = _release_parked(late, stub_root)
    assert rc != 0, out + err
    assert '別の導入が先に進みました' in err
    assert record.is_file()
    assert not (data / '.provider-bootstrap-fresh.json').exists()
    assert not (root / 'config.json').exists()
    following = _run(env, hermes_home)
    assert following.returncode == 0, following.stdout + following.stderr
    assert not record.exists()
    sys.path.insert(0, str(ROOT / "mcs"))
    import _mcs_path  # noqa: F401
    import external_transfer
    assert external_transfer.status(root)["state"] == "pending"


def test_python_format_marker_resumes_without_byte_identity(tmp_path):
    """A valid first-install record written by the Python writer (sorted keys)
    passes the structural fresh-resume validation and is not rejected for key
    order or whitespace differences."""
    home, hermes_home, stub_root, env = _world(tmp_path)
    root = home / '.mcs'
    data = root / 'data'
    data.mkdir(mode=0o700, parents=True)
    import os as _os
    _os.chmod(root, 0o700)
    _os.chmod(data, 0o700)
    (data / 'update.lock').touch()
    _os.chmod(data / 'update.lock', 0o600)
    root_stat, data_stat = root.stat(), data.stat()
    marker = {
        'version': 1,
        'root': [root_stat.st_dev, root_stat.st_ino, root_stat.st_uid, root_stat.st_mode & 0o7777],
        'data': [data_stat.st_dev, data_stat.st_ino, data_stat.st_uid, data_stat.st_mode & 0o7777],
        'config': None,
    }
    (data / '.provider-bootstrap-fresh.json').write_text(
        json.dumps(marker, sort_keys=True) + '\n')
    _os.chmod(data / '.provider-bootstrap-fresh.json', 0o600)
    result = _run(env, hermes_home)
    assert result.returncode == 0, result.stdout + result.stderr
    assert '再開します' in result.stdout


@pytest.mark.parametrize("root_value", ["empty", "different"])
def test_install_refuses_unsupported_data_root_before_writes(tmp_path, root_value):
    home, hermes_home, stub_root, env = _world(tmp_path)
    other = tmp_path / "other-root"
    env["MCS_ROOT"] = "" if root_value == "empty" else str(other)
    result = _run(env, None, "--mode", "standalone", "--no-brew", "--no-llm")
    assert result.returncode != 0
    assert "MCS_ROOT" in result.stderr
    assert list(home.iterdir()) == []
    assert not other.exists()
    assert not hermes_home.exists()
    assert not any(call.startswith(("git clone", "uv ", "launchctl bootstrap"))
                   for call in _calls(stub_root))


@pytest.mark.parametrize("root_value", ["unset", "default", "normalized", "physical", "tilde"])
def test_install_accepts_default_data_root_spellings(tmp_path, root_value):
    home, _, _, env = _world(tmp_path)
    env.pop("MCS_ROOT", None)
    if root_value == "default":
        env["MCS_ROOT"] = str(home / ".mcs")
    elif root_value == "normalized":
        env["MCS_ROOT"] = str(home) + "/./.mcs/"
    elif root_value == "physical":
        alias = tmp_path / "home-alias"
        alias.symlink_to(home, target_is_directory=True)
        env["MCS_ROOT"] = str(alias / ".mcs")
    elif root_value == "tilde":
        env["MCS_ROOT"] = "~/.mcs"
    result = _run(env, None, "--mode", "standalone", "--no-brew", "--no-llm",
                  "--no-services", "--no-recovery")
    assert result.returncode == 0, result.stdout + result.stderr
    assert (home / ".mcs/venv").is_dir()


def test_install_run_then_rerun_converges(tmp_path):
    """Six stages on a stubbed fresh machine, twice: second run adds no
    duplicate service entries and keeps the previous recovery tool."""
    home, hermes_home, stub_root, env = _world(tmp_path)

    r1 = _run(env, hermes_home)
    assert r1.returncode == 0, r1.stderr
    assert json.loads((home / ".mcs" / "config.json").read_text())["install_args"] == []
    assert not (home / ".mcs" / "data" / "install_options.json").exists()
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
    assert "Hermes の固定版" in r1.stdout
    assert "ソフトの準備が完了しました。まだ利用開始前の初期設定が必要です。" in r1.stdout
    assert "専用 Chrome の起動・MCS へのログイン" in r1.stdout

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
    home, hermes_home, stub_root, env = _world(tmp_path, configured=True)
    (hermes_home / "plugins").mkdir(parents=True)
    link = hermes_home / "plugins" / "mcs-discord-commands"
    link.symlink_to(ROOT / "hermes_plugin")            # killed after 3/6
    rec = home / ".mcs-recovery"
    rec.mkdir(mode=0o700, parents=True)
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


def test_skip_flags_bypass_stages(tmp_path):
    """--no-llm/--no-plugin/--no-recovery leave those stages untouched —
    the partially-provisioned machine (own LLM, self-managed plugins,
    no watchdog) still converges through the rest."""
    home, hermes_home, stub_root, env = _world(tmp_path)
    r = _run(env, hermes_home, "--no-llm", "--no-plugin",
             "--no-recovery")
    assert r.returncode == 0, r.stderr
    assert "--no-llm のため省略します" in r.stdout
    assert not (hermes_home / "plugins" / "mcs-discord-commands").exists()
    assert not (home / ".mcs-recovery").exists()
    agents = home / "Library" / "LaunchAgents"
    assert not (agents / "ai.mcs.llamaserver.plist").exists()
    assert not (agents / "org.mcs.recovery.plist").exists()
    # no model download was attempted; hermes stage still ran
    assert not any(c.startswith("curl") and "-o" in c
                   for c in _calls(stub_root))
    assert any(c.startswith("git clone") for c in _calls(stub_root))
    # the updater's plan repeats these opt-outs on a reinstall
    assert json.loads((home / ".mcs" / "config.json").read_text())["install_args"] \
        == ["--no-llm", "--no-plugin", "--no-recovery"]


def test_no_brew_and_no_services_flags(tmp_path):
    """--no-brew runs without a brew stub; --no-services never invokes
    mcs_setup."""
    home, hermes_home, stub_root, env = _world(tmp_path, brew=False)
    r = _run(env, hermes_home, "--no-brew", "--no-services")
    assert r.returncode == 0, r.stderr
    assert "--no-brew のため省略します" in r.stdout
    assert not any(c.startswith("brew install") for c in _calls(stub_root))
    assert not any("mcs_setup.py" in c for c in _calls(stub_root))


@pytest.mark.parametrize("flag", ["--bogus", "-x", "-no-llm"])
def test_unknown_option_is_bounded_error(tmp_path, flag):
    """F8: single-dash typos are options, never a HERMES_HOME."""
    home, hermes_home, _, env = _world(tmp_path)
    r = _run(env, hermes_home, flag)
    assert r.returncode == 2
    assert "未対応のオプション" in r.stderr
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
    argv = watch["ProgramArguments"]
    assert argv[1] == "-I"
    assert argv[3:7] == ["exec", "--data-dir", str(home / ".mcs/data"), "--"]
    assert argv[argv.index("--") + 1:] == [
        argv[0], str(home / ".mcs-recovery" / "mcs_recover.py"), "--if-stale"]
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
    assert "後で続ける場合のコマンド:" in r.stdout
    assert f"'{home}/.local/bin/mcs' doctor" in r.stdout
    shim = (home / ".local" / "bin" / "hermes").read_text()
    assert f"exec '{venv_py.parent / 'hermes'}' \"$@\"" in shim


def test_same_release_rerun_keeps_the_real_previous_generation(tmp_path):
    home, hermes_home, stub_root, env = _world(tmp_path)
    rec = home / ".mcs-recovery"
    rec.mkdir(mode=0o700, parents=True)
    (rec / "mcs_recover.py").write_bytes(b"# old generation")
    assert _run(env, hermes_home).returncode == 0
    assert (rec / "mcs_recover.py.prev").read_bytes() == b"# old generation"
    assert _run(env, hermes_home).returncode == 0
    assert (rec / "mcs_recover.py.prev").read_bytes() == b"# old generation"


def test_custom_hermes_home_refused_with_services(tmp_path):
    home, _hermes_home, stub_root, env = _world(tmp_path, configured=True)
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
    assert "は使用しません" in r.stderr
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


def test_data_dirs_exist_before_services_bootstrap(tmp_path):
    """F12: WatchPaths/log dirs exist (0700) before agents load."""
    home, hermes_home, stub_root, env = _world(tmp_path)
    r = _run(env, hermes_home)
    assert r.returncode == 0, r.stderr
    assert "datadirs-ready" in _calls(stub_root)
    for d in ("data", "data/cmd", "data/cmd_int"):
        assert (home / ".mcs" / d).stat().st_mode & 0o777 == 0o700


def test_unloaded_hermes_llamacpp_plist_gets_a_load_hint(tmp_path):
    home, hermes_home, stub_root, env = _world(tmp_path)
    agents = home / "Library" / "LaunchAgents"
    agents.mkdir(parents=True)
    (agents / "ai.hermes.llamacpp.plist").write_text("<plist/>")
    r = _run(env, hermes_home)
    assert r.returncode == 0, r.stderr
    assert "未起動" in r.stderr and "launchctl bootstrap" in r.stderr
    assert not (agents / "ai.mcs.llamaserver.plist").exists()


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
    assert "導入を妨げる問題 0件" in r.stdout
    assert "  要対処 " not in r.stdout
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
    rec.mkdir(mode=0o700)
    (rec / "repo_path").write_text(f"{other}\n")
    r = _run({**env, "STUB_UID": "0", "STUB_DF_KB": "1000",
              "STUB_CURL_FAIL": "github.com"}, None, "--mode", "hermes", "--preflight")
    assert r.returncode == 1
    out = r.stdout
    for text in ("要対処  管理者 root で実行されています", "要対処  Homebrew が未導入です",
                 "要対処  ディスク:", "要対処  通信: github.com に接続できません",
                 "要対処  既存の導入先は別のフォルダです"):
        assert text in out, out
    assert out.count("対処: ") >= 5
    assert "導入を妨げる問題 5件" in out
    assert sorted(p.name for p in home.iterdir()) == [".mcs-recovery"]


def test_preflight_flags_worktree(tmp_path):
    """A copy of install.sh next to a `.git` file is a git worktree."""
    home, hermes_home, stub_root, env = _world(tmp_path)
    wt = tmp_path / "wt"
    wt.mkdir()
    shutil.copy(INSTALL, wt / "install.sh")
    (wt / ".git").write_text("gitdir: /elsewhere/.git/worktrees/wt\n")
    r = _run(env, None, "--preflight", script=wt / "install.sh")
    assert "Git の作業用フォルダ" in r.stdout


@pytest.mark.parametrize('with_ocr', [False, True])
def test_dry_run_prints_plan_and_writes_nothing(tmp_path, with_ocr):
    home, hermes_home, stub_root, env = _world(tmp_path)
    r = _run(env, None, "--mode", "hermes", "--dry-run", "--no-llm", *(["--with-yomitoku"] if with_ocr else []))
    assert r.returncode == 0, r.stdout
    assert "導入予定（--dry-run" in r.stdout
    assert "4/6 ローカルAI 省略" in " ".join(r.stdout.split())
    assert "mcs_setup.py services" in r.stdout
    assert "uv pip install -e hermes-agent[messaging]（Hermes ディレクトリで実行）" in r.stdout
    if with_ocr:
        from ocr_model_contract import ENGINE_VERSION
        assert f'Python 3.13・Yomitoku {ENGINE_VERSION}・CPUモデル' in r.stdout
        assert '0.16.0' not in r.stdout
    else:
        assert '追加OCR' not in r.stdout
    assert list(home.iterdir()) == []


def test_foreign_mcs_launcher_blocks_preflight_and_install_before_stage1(tmp_path):
    """A foreign ~/.local/bin/mcs is an NG in --dry-run, and install stops
    before brew/hermes run instead of partway through; it is never touched."""
    home, hermes_home, stub_root, env = _world(tmp_path)
    shim = home / ".local" / "bin" / "mcs"
    shim.parent.mkdir(parents=True)
    shim.write_text("#!/bin/sh\n# some other tool\n")
    r = _run(env, None, "--dry-run", "--no-llm")
    assert r.returncode == 1, r.stdout
    assert "要対処  " + str(shim) + " は MCS の起動コマンドではありません" in r.stdout
    r = _run(env, hermes_home)
    assert r.returncode != 0 and "MCS の起動コマンドではない" in r.stderr
    assert not any(c.startswith(("brew", "git", "uv", "venv-python")) for c in _calls(stub_root))
    assert shim.read_text() == "#!/bin/sh\n# some other tool\n"


def test_standalone_documented_service_preserves_literal_paths(tmp_path):
    from mcs_standalone.service import render
    import run_log_install
    root = tmp_path / "home & space"
    _, body = render(root, platform="darwin")
    spec = plistlib.loads(body.encode())
    argv = spec["ProgramArguments"]
    assert argv[:2] == [str(root / "venv/bin/python3"), "-I"]
    assert argv[2] == run_log_install.helper_path(root, checkout=ROOT)
    assert argv[3:7] == ["exec", "--data-dir", str(root / "data"), "--"]
    assert argv[7:] == [str(root / "venv/bin/python3"), "-m",
                        "mcs_standalone", "run", "--root", str(root)]
    assert spec["StandardOutPath"] == spec["StandardErrorPath"] == os.devnull
    guide = (ROOT / "docs/guides/INSTALLATION.md").read_text()
    assert "./install.sh --mode standalone" in guide
    assert '"$PY" mcs/ops/mcs_setup.py check' in guide


def test_standalone_installer_has_no_hermes_checkout_or_commands(tmp_path):
    import json
    import sys
    home, _, stub, env = _world(tmp_path)
    env["STUB_REAL_PYTHON"] = sys.executable
    first = _run(env, None, "--mode", "standalone", "--no-llm")
    assert first.returncode == 0, first.stdout + first.stderr
    assert json.loads((home / ".mcs/config.json").read_text())["runtime_mode"] == "standalone"
    assert f"cd '{ROOT}' && " in first.stdout.split("-m mcs_standalone status")[0].splitlines()[-1]
    # The default target does not show LINE WORKS steps.
    assert "lineworks_adapter" not in first.stdout
    calls = _calls(stub)
    text = "\n".join(calls)
    assert "requirements-standalone.lock --require-hashes" in text
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


def test_fresh_mac_without_python_explains_instead_of_silent_exit(tmp_path):
    home, hermes_home, stub_root, env = _world(tmp_path)
    _without_supported_python(stub_root)
    r = _run(env, hermes_home, "--no-brew", "--no-llm", "--no-plugin", "--no-services",
             "--recovery-python", "/synthetic/recovery/python3")
    assert r.returncode != 0
    assert "--recovery-python は導入前に Python 3.14 で検証します" in r.stderr
    assert not (home / ".mcs").exists()
    r = _run(env, hermes_home, "--no-brew", "--no-llm", "--no-plugin", "--no-services")
    assert "復旧用 Python は段階6で確認します" in r.stderr
    assert "【1/6】基本ソフトの準備（Homebrew）" in r.stdout


def test_lineworks_guide_path_b_uses_standalone_venv_python():
    import re
    guide = (ROOT / "docs/guides/LINEWORKS.md").read_text()
    assert 'PY="$HOME/.mcs/venv/bin/python3"' in guide
    assert "`PY=python3.14`" not in guide
    assert 'B-1〜B-2' in guide and 'B-1〜B-4' not in guide
    uses = list(re.finditer(r"\$PY", guide))
    assert uses
    assert all(guide[match.start() - 1] == '"' and guide[match.end()] == '"' for match in uses)


def test_hermes_install_routes_lineworks_without_plugin_or_gateway(tmp_path):
    """LINE WORKS uses its own adapter: --no-plugin must not claim every
    interactive transport needs the plugin, and the final guidance names
    the lineworks_adapter steps (no secret values are printed)."""
    home, hermes_home, stub_root, env = _world(tmp_path, configured=True)
    (home / ".mcs/config.json").write_text(
        '{"runtime_mode":"hermes","notify_target":"lineworks:synthetic-room"}')
    plan = _run(env, None, "--mode", "hermes", "--dry-run", "--no-plugin", "--no-llm")
    assert "省略（--no-plugin。LINE WORKS では不要）" in plan.stdout
    r = _run(env, hermes_home, "--no-llm", "--no-plugin", "--no-recovery")
    assert r.returncode == 0, r.stderr
    assert "LINE WORKS では不要" in r.stdout
    assert "操作カードにはこのプラグインが必要" not in r.stdout
    for sub in ("init", "check", "service"):
        assert f"cd '{ROOT}' && " in r.stdout.split(f"-m lineworks_adapter {sub}")[0].splitlines()[-1]
    assert "docs/guides/LINEWORKS.md" in r.stdout


def test_japanese_help_is_read_only(tmp_path):
    home, _, stub_root, env = _world(tmp_path)
    result = _run(env, None, '--help')
    assert result.returncode == 0
    assert '使い方:' in result.stdout
    assert '次の6段階' in result.stdout
    assert '新規導入の推奨' in result.stdout
    assert 'MCS 単独接続では使用しません' in result.stdout
    assert 'パスワード・トークンは画面に表示しません' in result.stdout
    assert list(home.iterdir()) == []
    assert _calls(stub_root) == []


def test_standalone_records_implicit_plugin_skip(tmp_path):
    home, _, _stub, env = _world(tmp_path)
    result = _run(env, None, '--mode', 'standalone', '--no-brew', '--no-llm', '--no-services', '--no-recovery')
    assert result.returncode == 0, result.stderr
    assert json.loads((home / '.mcs' / 'config.json').read_text())['install_args'] == [
        '--no-brew', '--no-llm', '--no-plugin', '--no-recovery']
