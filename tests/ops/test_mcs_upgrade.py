"""Upgrade path — scripts/mcs_upgrade.py launcher + mcs_update plan /
reinstall. Fully synthetic: temp git repos, stubbed launchctl/install.sh.
"""
import update_apply
import update_cli
import update_execution
import update_lifecycle_context
import update_planning
import update_preflight
import update_recovery
import update_repository
import update_restore
import update_runtime_migration
import update_services
import mcs_util
import service_contract
import service_management
import safe_io
import settings_migration_receipt
import update_discovery
import update_launch
import importlib.util
import fcntl
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
import uuid

import pytest

import mcs_update
from ops_testkit import _git, _make_repo, in_process_post_merge
import test_mcs_update
from test_mcs_update import _apply_env
from ledger import Ledger
import mcs_cli
import mcs_requests
import update_policy

updater = test_mcs_update.updater   # shared fixture
prepared_run_log = test_mcs_update.prepared_run_log

ROOT = Path(__file__).resolve().parents[2]


def _assert_migration_evidence(path, legacy):
    evidence = json.loads(path.read_bytes())
    assert evidence['v'] == 2 and evidence['kind'] == 'install-options-migration'
    assert evidence['legacy_sha256'] == hashlib.sha256(legacy.read_bytes()).hexdigest()
    assert len(evidence['base_sha256']) == 64 and len(evidence['operation']) == 32


def _launcher():
    spec = importlib.util.spec_from_file_location(
        "mcs_upgrade_launcher", ROOT / "scripts" / "mcs_upgrade.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize('confirmed', [False, True])
def test_reconciliation_launcher_runs_verified_target_helper_without_fetch(
        tmp_path, monkeypatch, capfd, confirmed):
    repo, bare = _make_repo(tmp_path)
    stub = "import json,sys\nprint(json.dumps({'argv':sys.argv[1:]}))\n"
    _release(tmp_path / 'remote-work', bare, 'v1.2.0',
             {'mcs/ops/settings_migration_reconcile.py': stub, 'mcs/_mcs_path.py': ''})
    _git(repo, 'fetch', '-q', '--tags')
    _git(repo, 'remote', 'set-url', 'origin', str(tmp_path / 'missing-remote'))
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('MCS_ROOT', str(tmp_path / 'synthetic-mcs'))
    args = ['--repo', str(repo), '--no-fetch', 'reconcile-settings', '--to', 'v1.2.0']
    if confirmed:
        args += ['--confirm-human', '--reason', 'synthetic review']
    assert _launcher().main(args) == 0
    result = json.loads(capfd.readouterr().out)
    assert result['argv'] == (['--confirm-human', '--reason=synthetic review'] if confirmed else [])
    if not confirmed:
        assert not (tmp_path / '.mcs-release-control').exists()


def test_reconciliation_launcher_extracts_the_real_helper_and_dependencies(tmp_path, monkeypatch, capfd):
    repo, bare = _make_repo(tmp_path)
    files = ('mcs/_mcs_path.py', 'mcs/core/safe_io.py', 'mcs/core/mcs_util.py',
             'mcs/core/provider_settings.py',
             'mcs/core/install_options_contract.py', 'mcs/ops/setup/config_edit.py',
             'mcs/ops/install_options.py', 'mcs/ops/update/settings_migration_receipt.py',
             'mcs/ops/settings_migration_reconcile.py', 'mcs/ops/update/update_journal.py',
             'mcs/ops/release_apply_owner.py', 'mcs/ops/release_apply_checks.py')
    _release(tmp_path / 'remote-work', bare, 'v1.2.0', {name: (ROOT / name).read_text() for name in files})
    _git(repo, 'fetch', '-q', '--tags')
    _git(repo, 'remote', 'set-url', 'origin', str(tmp_path / 'missing-remote'))
    root = tmp_path / 'synthetic-mcs'
    data = root / 'data'
    data.mkdir(parents=True, mode=0o700)
    config, legacy = root / 'config.json', data / 'install_options.json'
    config.write_text('{"unknown": {"keep": true}}')
    config.chmod(0o600)
    legacy.write_text('{"v": 1, "install_args": ["--no-llm"]}')
    import install_options
    base = install_options.inspect(config, legacy)
    _, digest = install_options._legacy(legacy)
    evidence = settings_migration_receipt.Receipt(data, base, digest)
    evidence.begin()
    evidence.close()
    before = config.read_bytes(), legacy.read_bytes()
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('MCS_ROOT', str(root))
    args = ['--repo', str(repo), '--no-fetch', 'reconcile-settings', '--to', 'v1.2.0']
    assert _launcher().main(args) == 0
    assert '設定へのコピー前' in capfd.readouterr().out
    assert _launcher().main(args + ['--confirm-human', '--reason', 'synthetic review']) == 0
    assert '保留を解除しました' in capfd.readouterr().out
    assert (config.read_bytes(), legacy.read_bytes()) == before
    assert not (root / settings_migration_receipt.NAME).exists()
    assert len(list(root.glob('.install-options-migration.reconcile.*'))) == 1


def test_release_fence_stops_old_target_before_git_extraction_or_python(tmp_path, monkeypatch):
    import release_apply_owner
    runtime = str(tmp_path / 'runtime')
    monkeypatch.setenv('MCS_ROOT', runtime)
    with release_apply_owner._control_store(runtime) as store:
        store.write('release-owner-activation.json', {'synthetic': True})
    launcher = _launcher()
    monkeypatch.setattr(launcher, '_git', lambda *_a, **_k: pytest.fail('Git before release hold'))
    monkeypatch.setattr(launcher, 'extract', lambda *_a, **_k: pytest.fail('old target extraction'))
    assert launcher.main(['--repo', str(tmp_path / 'repo'), 'apply', '--to', 'v1.0.0']) == 2
    assert launcher._RELEASE_GATE_FD is None


def test_bootstrap_git_child_inherits_gate_and_scope_restores_on_error(tmp_path, monkeypatch):
    launcher = _launcher()
    calls = []

    def run(argv, **kwargs):
        calls.append(tuple(os.fstat(fd).st_ino for fd in kwargs.get('pass_fds', ())))
        return subprocess.CompletedProcess(argv, 0, '', '')

    monkeypatch.setattr(launcher.subprocess, 'run', run)
    with pytest.raises(RuntimeError), launcher._release_scope(str(tmp_path / 'runtime')) as fd:
        identity = os.fstat(fd).st_ino
        launcher._git(str(tmp_path), 'status')
        raise RuntimeError('synthetic scope exit')
    launcher._git(str(tmp_path), 'status')
    assert calls == [(identity,), ()] and launcher._RELEASE_GATE_FD is None


def _release(work, bare, tag, files):
    for path, text in files.items():
        (work / path).parent.mkdir(parents=True, exist_ok=True)
        (work / path).write_text(text)
        _git(work, "add", path)
    _git(work, "commit", "-qm", tag)
    _git(work, "tag", tag)
    _git(work, "push", "-q", str(bare), "main", tag)
    return _git(work, "rev-parse", "HEAD").stdout.strip()


# Marker-only CLI/parser declarations: _installed_setup_command verifies these
# literals before routing through the installed launcher; they are never run.
_SLACK_SETUP_SOURCES = {
    "mcs/ops/mcs_cli.py":
        'if rest[0] in ("api-keys", "slack-channels"):\n    pass\n',
    "mcs/ops/mcs_setup.py":
        'sub.add_parser("api-keys")\nsub.add_parser("slack-channels")\n',
}


@pytest.mark.parametrize('declarations,expected', [
    ('code', True), ('old', False), ('comments', False), ('strings', False),
    ('route_comment', 'unverified'), ('parser_comment', 'unverified'),
    ('invalid', 'unverified'), ('empty', 'unverified'),
    ('old_strings', 'unverified'),
])
def test_setup_capability_requires_syntax_not_comments_or_strings(tmp_path, declarations, expected):
    ops = tmp_path / 'mcs' / 'ops'
    ops.mkdir(parents=True)
    cli = 'if rest[0] in ("api-keys",):\n    pass\n'
    parser = 'sub.add_parser("api-keys")\n'
    route = 'if rest[0] in ("slack-channels",):\n    pass\n'
    action = 'sub.add_parser("slack-channels")\n'
    if declarations == 'code':
        cli += route
        parser += action
    elif declarations in ('comments', 'strings', 'route_comment', 'parser_comment'):
        cli += route if declarations == 'parser_comment' else (
            '"""' + route + '"""\n' if declarations == 'strings'
            else '# if rest[0] in ("slack-channels",):\n')
        parser += action if declarations == 'route_comment' else (
            '"""' + action + '"""\n' if declarations == 'strings'
            else '# sub.add_parser("slack-channels")\n')
    elif declarations == 'invalid':
        cli += route + 'invalid (\n'
        parser += action
    elif declarations == 'empty':
        cli = parser = ''
    elif declarations == 'old_strings':
        cli, parser = '"""' + cli + '"""\n', '"""' + parser + '"""\n'
    (ops / 'mcs_cli.py').write_text(cli)
    (ops / 'mcs_setup.py').write_text(parser)
    if expected == 'unverified':
        with pytest.raises(ValueError, match='setup_unverified'):
            _launcher()._setup_action_supported(tmp_path, 'slack-channels')
    else:
        assert _launcher()._setup_action_supported(tmp_path, 'slack-channels') is expected


@pytest.mark.parametrize('action', ['api-keys', 'slack-channels'])
def test_setup_capability_matches_current_cli_and_parser(action):
    assert _launcher()._setup_action_supported(ROOT, action) is True


def _slack_setup_shim(monkeypatch, tmp_path, repo, rc=0):
    """Installed-launcher shim whose pinned runtime records the setup call."""
    home = tmp_path / "home"
    (home / ".local" / "bin").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    record = tmp_path / "setup_calls"
    stub = tmp_path / "stub_runtime"
    stub.write_text(f"#!/bin/sh\necho \"$@\" >> {record}\nexit {rc}\n")
    stub.chmod(0o755)
    cli = Path(os.path.realpath(repo)) / "mcs" / "ops" / "mcs_cli.py"
    shim = home / ".local" / "bin" / "mcs"
    shim.write_text("#!/bin/sh\n# MCS lifecycle launcher\n"
                     "unset PYTHONPATH PYTHONHOME\n"
                     "export MCS_LIFECYCLE_PINNED=1\n"
                     f"exec {stub} {cli} \"$@\"\n")
    shim.chmod(0o755)
    return record


def test_repo_root_follows_mcs_update_repo(tmp_path):
    env = {**os.environ, "MCS_UPDATE_REPO": str(tmp_path)}
    out = subprocess.run(
        [sys.executable, "-c", "import sys; sys.path.insert(0, sys.argv[1]);"
         "import _mcs_path, mcs_util, mcs_setup;"
         "print(mcs_util.REPO == mcs_setup.REPO_ROOT == sys.argv[2])",
         str(ROOT / "mcs"), str(tmp_path)],
        env=env, capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "True"


def test_launcher_runs_target_updater_against_live_repo(tmp_path, capfd):
    repo, bare = _make_repo(tmp_path)
    stub = ("import json, os, sys\n"
            "print(json.dumps({'argv': sys.argv[1:], 'file': __file__,"
            " 'repo': os.environ['MCS_UPDATE_REPO'], 'cwd': os.getcwd()}))\n")
    _release(tmp_path / "remote-work", bare, "v1.2.0",
             {"mcs/ops/mcs_update.py": stub, "mcs/_mcs_path.py": ""})
    _git(repo, "fetch", "-q", "--tags")
    launcher = _launcher()
    # the stub exits 0 without applying: the launcher must not report success
    assert launcher.main(["--repo", str(repo), "--no-fetch", "apply",
                          "--reinstall", "--install-arg=--no-llm"]) == 2
    captured = capfd.readouterr()
    assert "v1.2.0 は適用されていません" in captured.err
    seen = json.loads(captured.out)
    assert seen["argv"] == ["apply", "--tag", "v1.2.0", "--reinstall",
                            "--install-arg=--no-llm"]
    assert seen["repo"] == os.path.realpath(repo)
    assert not seen["file"].startswith(os.path.realpath(repo))
    assert not Path(seen["file"]).exists()          # temp copy removed
    # the live checkout itself was not touched
    assert _git(repo, "status", "--porcelain").stdout == ""


def _plan_stub():
    return ("import json, os, subprocess, sys\n"
            "if sys.argv[1:2] == ['plan']:\n"
            "    print(json.dumps({'route': 'noop'}))\n"
            "else:\n"
            "    subprocess.run(['git', '-C', os.environ['MCS_UPDATE_REPO'], 'checkout', '-q',"
            " 'refs/tags/v1.2.0'], check=True)\n")


def test_launcher_apply_succeeds_only_when_the_tag_is_checked_out(tmp_path, capfd, monkeypatch):
    repo, bare = _make_repo(tmp_path)
    _release(tmp_path / "remote-work", bare, "v1.2.0",
             {"mcs/ops/mcs_update.py": _plan_stub(), **_SLACK_SETUP_SOURCES})
    _git(repo, "fetch", "-q", "--tags")
    record = _slack_setup_shim(monkeypatch, tmp_path, repo)
    assert _launcher().main(["--repo", str(repo), "--no-fetch", "apply"]) == 0
    assert "適用されていません" not in capfd.readouterr().err
    assert record.read_text().split()[-2:] == ["setup", "slack-channels"]


def test_launcher_apply_reports_unverified_slack_setup(tmp_path, capfd, monkeypatch):
    repo, bare = _make_repo(tmp_path)
    _release(tmp_path / "remote-work", bare, "v1.2.0",
             {"mcs/ops/mcs_update.py": _plan_stub(), **_SLACK_SETUP_SOURCES})
    _git(repo, "fetch", "-q", "--tags")
    _slack_setup_shim(monkeypatch, tmp_path, repo, rc=3)
    assert _launcher().main(["--repo", str(repo), "--no-fetch", "apply"]) == 1
    assert "Slack運用ログ先の準備は未完了" in capfd.readouterr().out


def _lifecycle_plan_stub(report, trace):
    return ("import json, os, pathlib, subprocess, sys\n"
            "repo = os.environ['MCS_UPDATE_REPO']\n"
            "def git(*args):\n"
            "    return subprocess.check_output(['git', '-C', repo, *args], text=True).strip()\n"
            "target = git('rev-parse', 'refs/tags/v1.2.0^{commit}')\n"
            "if sys.argv[1] == 'plan':\n"
            f"    report = {report!r}\n"
            "    report['route'] = 'noop' if git('rev-parse', 'HEAD') == target else 'apply'\n"
            "    print(json.dumps(report))\n"
            "else:\n"
            f"    pathlib.Path({str(trace)!r}).write_text('apply')\n"
            "    subprocess.run(['git', '-C', repo, 'checkout', '-q', target], check=True)\n")


@pytest.mark.parametrize('entry,already_current,required,sources,expected', [
    ('apply', False, False, 'new', 0),
    ('guide', True, False, 'new', 0),
    ('guide', False, False, 'new', 0),
    ('apply', False, True, 'new', 1),
    ('apply', False, None, 'new', 1),
    ('apply', False, 'false', 'new', 1),
    ('apply', False, 'missing', 'new', 1),
    ('apply', False, 'missing', 'old', 0),
    ('apply', False, 'missing', 'empty', 1),
    ('apply', False, 'missing', 'partial', 1),
])
def test_launcher_slack_completion_uses_verified_plan_and_target_capability(
        tmp_path, monkeypatch, capfd, entry, already_current, required, sources, expected):
    repo, bare = _make_repo(tmp_path)
    report = {} if required == 'missing' else {'slack_channels_required': required}
    declarations = dict(_SLACK_SETUP_SOURCES)
    if sources in ('old', 'partial'):
        declarations['mcs/ops/mcs_setup.py'] = 'sub.add_parser("api-keys")\n'
    if sources == 'old':
        declarations['mcs/ops/mcs_cli.py'] = 'if rest[0] in ("api-keys",):\n    pass\n'
    if sources == 'empty':
        declarations = {name: '' for name in declarations}
    trace = tmp_path / 'apply_trace'
    _release(tmp_path / 'remote-work', bare, 'v1.2.0',
             {'mcs/ops/mcs_update.py': _lifecycle_plan_stub(report, trace), **declarations})
    _git(repo, 'fetch', '-q', '--tags')
    if already_current:
        _git(repo, 'checkout', '-q', 'v1.2.0')
    monkeypatch.setenv('HOME', str(tmp_path))
    answers = iter(('1', '2'))
    monkeypatch.setattr('builtins.input', lambda *_: next(answers))
    launcher = _launcher()
    monkeypatch.setattr(launcher.sys.stdin, 'isatty', lambda: True)
    monkeypatch.setattr(launcher, '_offer_api_keys', lambda *_: None)
    assert launcher.main(['--repo', str(repo), '--no-fetch', entry, '--to', 'v1.2.0']) == expected
    assert trace.exists() is not already_current
    output = capfd.readouterr().out
    if sources == 'old':
        assert '準備機能がありません' in output and '完了は確認していません' in output
    elif expected:
        assert 'Slack運用ログ先の準備は未完了' in output


@pytest.mark.parametrize('entry', ['apply', 'guide'])
@pytest.mark.parametrize('failure', ['timeout', 'oserror', 'json'])
def test_post_apply_confirmation_failure_reports_hold_without_reapplying(
        tmp_path, monkeypatch, capfd, entry, failure):
    repo, bare = _make_repo(tmp_path)
    trace = tmp_path / 'apply_trace'
    _release(tmp_path / 'remote-work', bare, 'v1.2.0',
             {'mcs/ops/mcs_update.py': _lifecycle_plan_stub({'slack_channels_required': False}, trace),
              **_SLACK_SETUP_SOURCES})
    _git(repo, 'fetch', '-q', '--tags')
    monkeypatch.setenv('HOME', str(tmp_path))
    answers = iter(('1', '2'))
    monkeypatch.setattr('builtins.input', lambda *_: next(answers))
    launcher = _launcher()
    monkeypatch.setattr(launcher.sys.stdin, 'isatty', lambda: True)
    monkeypatch.setattr(launcher, '_offer_api_keys', lambda *_: None)
    original = launcher.subprocess.run
    applied = []
    def run(command, **kwargs):
        if len(command) > 2 and str(command[1]).endswith('/mcs_update.py'):
            if command[2] == 'apply':
                applied.append(command)
            elif command[2] == 'plan' and applied:
                if failure == 'timeout':
                    raise subprocess.TimeoutExpired(command, 600)
                if failure == 'oserror':
                    raise OSError('synthetic private exception must not print')
                return subprocess.CompletedProcess(command, 0, '{invalid', '')
        return original(command, **kwargs)
    monkeypatch.setattr(launcher.subprocess, 'run', run)
    assert launcher.main(['--repo', str(repo), '--no-fetch', entry, '--to', 'v1.2.0']) == 1
    assert trace.read_text() == 'apply' and len(applied) == 1
    output = capfd.readouterr().out
    assert '再度更新する前に mcs recover と mcs doctor' in output
    assert 'private exception' not in output


def test_candidate_apply_preserves_approved_base_and_verifies_against_applied_head(
        tmp_path, monkeypatch):
    repo, _ = _make_repo(tmp_path)
    base = _git(repo, 'rev-parse', 'HEAD').stdout.strip()
    trace = tmp_path / 'candidate_trace'
    entry = repo / 'mcs/ops/mcs_update.py'
    entry.parent.mkdir(parents=True)
    entry.write_text(
        "import json, os, pathlib, subprocess, sys\n"
        f"trace = pathlib.Path({str(trace)!r})\n"
        "with trace.open('a') as stream: stream.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "repo = os.environ['MCS_UPDATE_REPO']\n"
        "base = sys.argv[sys.argv.index('--base-sha') + 1]\n"
        "target = sys.argv[sys.argv.index('--candidate-sha') + 1]\n"
        "if sys.argv[1] == 'apply':\n"
        f"    assert base == {base!r}\n"
        "    subprocess.run(['git', '-C', repo, 'checkout', '-q', target], check=True)\n"
        "else:\n"
        "    head = subprocess.check_output(['git', '-C', repo, 'rev-parse', 'HEAD'], text=True).strip()\n"
        "    print(json.dumps({'route': 'noop' if head == base == target else 'blocked',\n"
        "                      'slack_channels_required': False}))\n")
    _git(repo, 'add', '.')
    _git(repo, 'commit', '-qm', 'synthetic local candidate')
    target = _git(repo, 'rev-parse', 'HEAD').stdout.strip()
    _git(repo, 'reset', '--hard', base)
    monkeypatch.setenv('HOME', str(tmp_path))
    assert _launcher().main(['--repo', str(repo), 'apply', '--candidate-sha', target,
                             '--base-sha', base, '--confirm-human', '--reason', 'synthetic']) == 0
    apply, verify = [json.loads(line) for line in trace.read_text().splitlines()]
    assert apply[apply.index('--base-sha') + 1] == base
    assert '--confirm-human' in apply and '--reason=synthetic' in apply
    assert verify == ['plan', '--candidate-sha', target, '--base-sha', target]


def test_launcher_extracts_the_exact_local_candidate_without_fetch(tmp_path, monkeypatch, capfd):
    repo, _ = _make_repo(tmp_path)
    base = _git(repo, "rev-parse", "HEAD").stdout.strip()
    files = {
        "mcs/ops/mcs_update.py": (
            "import json, os, pathlib, sys\n"
            "root = pathlib.Path(__file__).resolve().parents[2]\n"
            "print(json.dumps({'argv': sys.argv[1:], 'repo': os.environ['MCS_UPDATE_REPO'],"
            " 'files': sorted(str(p.relative_to(root)) for p in root.rglob('*') if p.is_file())}))\n"),
        "mcs/core/synthetic.py": "# synthetic candidate contract\n",
        "deployment/recovery/mcs_recover.py": "# synthetic independent recovery\n",
        "deployment/recovery/mcs_recovery_core.py": "# synthetic companion\n",
        "adapters/synthetic.py": "# must not be staged\n",
    }
    for name, content in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "synthetic local candidate")
    target = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _git(repo, "reset", "--hard", base)
    launcher = _launcher()
    original = launcher._git

    def local_git(repository, *args, **kwargs):
        assert args[0] not in ("fetch", "ls-remote", "tag")
        return original(repository, *args, **kwargs)

    monkeypatch.setattr(launcher, "_git", local_git)
    assert launcher.main(["--repo", str(repo), "plan", "--candidate-sha", target,
                          "--base-sha", base]) == 0
    seen = json.loads(capfd.readouterr().out)
    assert seen["argv"] == ["plan", "--candidate-sha", target, "--base-sha", base]
    assert seen["repo"] == os.path.realpath(repo)
    assert seen["files"] == sorted(name for name in files if not name.startswith("adapters/"))
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == base


@pytest.mark.parametrize("suffix", [
    ["--candidate-sha", "a" * 39, "--base-sha", "b" * 40],
    ["--candidate-sha", "a" * 40],
    ["--candidate-sha", "a" * 40, "--base-sha", "b" * 40],
    ["--candidate-sha", "a" * 40, "--base-sha", "b" * 40, "--confirm-human"],
    ["--candidate-sha", "a" * 40, "--base-sha", "b" * 40, "--reason", "synthetic"],
    ["--to", "v1.0.28", "--base-sha", "b" * 40],
    ["--to", "v1.0.28", "--confirm-human", "--reason", "synthetic"],
    ["--to", "v1.0.28", "--candidate-sha", "a" * 40],
])
def test_launcher_rejects_candidate_without_a_frozen_human_decision(monkeypatch, suffix):
    launcher = _launcher()
    monkeypatch.setattr(launcher, "_git", lambda *a, **k: pytest.fail("invalid input must not invoke Git"))
    with pytest.raises(SystemExit) as caught:
        launcher.main(["apply", *suffix])
    assert caught.value.code == 2


def test_launcher_never_treats_deferred_candidate_apply_as_success(tmp_path, capfd):
    repo, _ = _make_repo(tmp_path)
    base = _git(repo, "rev-parse", "HEAD").stdout.strip()
    entry = repo / "mcs/ops/mcs_update.py"
    entry.parent.mkdir(parents=True)
    entry.write_text("raise SystemExit(0)\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "synthetic untagged candidate")
    target = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _git(repo, "reset", "--hard", base)
    assert _launcher().main(["--repo", str(repo), "apply", "--candidate-sha", target,
                             "--base-sha", base, "--confirm-human", "--reason",
                             "synthetic local validation"]) == 2
    assert "適用されていません" in capfd.readouterr().err
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == base


def test_launcher_missing_candidate_reports_local_guidance_before_staging(tmp_path, monkeypatch):
    repo, _ = _make_repo(tmp_path)
    base = _git(repo, "rev-parse", "HEAD").stdout.strip()
    launcher = _launcher()
    monkeypatch.setattr(launcher.tempfile, "mkdtemp",
                        lambda **kw: pytest.fail("missing candidate must not allocate staging"))
    monkeypatch.setattr(launcher, "extract",
                        lambda *a, **kw: pytest.fail("missing candidate must not extract"))
    with pytest.raises(SystemExit) as caught:
        launcher.main(["--repo", str(repo), "plan", "--candidate-sha", "0" * 40,
                       "--base-sha", base])
    message = str(caught.value.code)
    assert "未公開候補" in message and "ローカル" in message
    assert "--candidate-sha" in message and "--repo" in message
    assert "接続" not in message and "アクセス権" not in message
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == base


def test_launcher_preserves_a_dash_leading_reason_at_the_child_parser(tmp_path, capfd):
    repo, _ = _make_repo(tmp_path)
    base = _git(repo, "rev-parse", "HEAD").stdout.strip()
    entry = repo / "mcs/ops/mcs_update.py"
    entry.parent.mkdir(parents=True)
    entry.write_text(
        "import argparse, json\n"
        "parser = argparse.ArgumentParser()\n"
        "parser.add_argument('operation')\n"
        "parser.add_argument('--candidate-sha')\n"
        "parser.add_argument('--base-sha')\n"
        "parser.add_argument('--confirm-human', action='store_true')\n"
        "parser.add_argument('--reason', required=True)\n"
        "args = parser.parse_args()\n"
        "print(json.dumps({'reason': args.reason, 'confirmed': args.confirm_human}))\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "synthetic candidate parser")
    target = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _git(repo, "reset", "--hard", base)
    assert _launcher().main(["--repo", str(repo), "apply", "--candidate-sha", target,
                             "--base-sha", base, "--confirm-human", "--reason=-synthetic"]) == 2
    captured = capfd.readouterr()
    assert json.loads(captured.out) == {"reason": "-synthetic", "confirmed": True}
    assert "適用されていません" in captured.err
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == base


def test_launcher_refuses_target_without_updater(tmp_path):
    repo, _ = _make_repo(tmp_path)
    with pytest.raises(SystemExit, match="v1.1.0 に必要な更新プログラムがありません"):
        _launcher().main(["--repo", str(repo), "--no-fetch", "plan",
                          "--to", "v1.1.0"])


def test_launcher_requires_a_fetched_release_tag_not_a_same_named_branch(tmp_path):
    repo, _ = _make_repo(tmp_path)
    _git(repo, "checkout", "-qb", "v1.2.0")
    target = repo / "mcs/ops/mcs_update.py"
    target.parent.mkdir(parents=True)
    target.write_text("raise SystemExit(0)\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "synthetic non-release branch")
    before = _git(repo, "rev-parse", "HEAD").stdout
    with pytest.raises(SystemExit, match="git ls-tree に失敗しました"):
        _launcher().main(["--repo", str(repo), "--no-fetch", "plan", "--to", "v1.2.0"])
    assert _git(repo, "rev-parse", "HEAD").stdout == before
    assert _git(repo, "status", "--porcelain").stdout == ""


def test_launcher_uses_the_release_tag_despite_an_ambiguous_ref(tmp_path, capfd):
    repo, bare = _make_repo(tmp_path)
    stub = "print('synthetic release updater')\n"
    _release(tmp_path / "remote-work", bare, "v1.2.0", {"mcs/ops/mcs_update.py": stub})
    _git(repo, "fetch", "-q", "--tags")
    _git(repo, "update-ref", "refs/v1.2.0", "HEAD")
    assert _launcher().main(["--repo", str(repo), "--no-fetch", "plan", "--to", "v1.2.0"]) == 0
    assert capfd.readouterr().out.strip() == "synthetic release updater"


@pytest.mark.parametrize("mode,kind,path,error", [
    ("120000", "blob", "mcs/link.py", "扱えない種類のファイル"),
    ("160000", "commit", "mcs/submodule", "扱えない種類のファイル"),
    ("100644", "blob", "mcs/../escape.py", "安全でないファイルパス"),
    ("100644", "blob", "/escape.py", "安全でないファイルパス"),
])
def test_extract_rejects_nonfiles_and_unsafe_paths(tmp_path, monkeypatch, mode, kind, path, error):
    launcher = _launcher()
    monkeypatch.setattr(launcher, "_git", lambda *_args, **_kw:
                        f"{mode} {kind} {'a' * 40}\t{path}\0")
    with pytest.raises(SystemExit, match=error):
        launcher.extract("synthetic", "v1.2.0", str(tmp_path))
    assert list(tmp_path.iterdir()) == []


def test_extract_preserves_binary_blobs_and_executable_bit(tmp_path, monkeypatch):
    launcher = _launcher()
    blob = b"#!/bin/sh\n# synthetic binary \xff\x00\n"

    def git(_repo, *args, binary=False):
        if args[0] == "ls-tree":
            assert args[2] == "refs/tags/v1.2.0"
            return f"100755 blob {'a' * 40}\tmcs/synthetic.sh\0"
        assert args == ("cat-file", "blob", "a" * 40) and binary
        return blob

    monkeypatch.setattr(launcher, "_git", git)
    launcher.extract("synthetic", "v1.2.0", str(tmp_path))
    target = tmp_path / "mcs/synthetic.sh"
    assert target.read_bytes() == blob
    assert target.stat().st_mode & 0o777 == 0o755


@pytest.mark.parametrize("failure", [OSError(2, "synthetic missing executable"), KeyboardInterrupt()])
def test_launcher_cleans_staging_on_launch_error_or_cancellation(tmp_path, monkeypatch, failure):
    launcher = _launcher()
    staged = tmp_path / "staging"
    staged.mkdir(mode=0o700)
    monkeypatch.setattr(launcher.tempfile, "mkdtemp", lambda **_kw: str(staged))
    monkeypatch.setattr(launcher, "_git", lambda *_args, **_kw: str(tmp_path))

    def extract(*_args, local_candidate=False):
        target = staged / "mcs/ops/mcs_update.py"
        target.parent.mkdir(parents=True)
        target.write_text("# synthetic updater\n")

    def run(*_args, **_kw):
        raise failure

    monkeypatch.setattr(launcher, "extract", extract)
    monkeypatch.setattr(launcher.subprocess, "run", run)
    expected = SystemExit if isinstance(failure, OSError) else KeyboardInterrupt
    with pytest.raises(expected):
        launcher.main(["--repo", str(tmp_path), "--no-fetch", "plan", "--to", "v1.2.0"])
    assert not staged.exists()


def test_git_unavailable_reports_launch_failure(tmp_path, monkeypatch):
    launcher = _launcher()

    def run(*_args, **_kw):
        raise FileNotFoundError(2, "synthetic missing git")

    monkeypatch.setattr(launcher.subprocess, "run", run)
    with pytest.raises(SystemExit, match="git tag を起動できませんでした"):
        launcher.latest_tag(str(tmp_path))


CHANGELOG = """# 変更履歴

## [Unreleased]

## [1.2.0] — 2026-10-03

### 新機能

- x

### 更新時の注意

- install.shを再実行してください。

## [1.1.0] — 2026-10-02

### 更新時の注意

- gatewayを再起動してください。

### 技術詳細

- t

## [1.0.0] — 2026-09-01

### 更新時の注意

- 対象外の古い注意。
"""


def test_upgrade_notes_cover_only_skipped_versions(updater, tmp_path):
    repo, bare = _make_repo(tmp_path)
    updater.REPO = str(repo)
    _release(tmp_path / "remote-work", bare, "v1.2.0",
             {"CHANGELOG.md": CHANGELOG})
    _git(repo, "fetch", "-q", "--tags")
    notes = updater.upgrade_notes("v1.2.0", "v1.0.0")
    assert [n["version"] for n in notes] == ["1.1.0", "1.2.0"]
    assert notes[0]["notes"] == "- gatewayを再起動してください。"
    assert updater.upgrade_notes("v1.2.0", "v1.1.0")[0]["version"] == "1.2.0"


def _plan_env(updater, monkeypatch, tmp_path, tag_errors, *, legacy=False,
              cfg=None):
    repo, bare = _make_repo(tmp_path)
    updater.REPO = str(repo)
    if not legacy:
        _git(repo, "checkout", "-q", "main")
        (repo / "mcs/ops").mkdir(parents=True)
        (repo / "mcs/ops/mcs_update.py").write_text("")
        _git(repo, "add", ".")
        _git(repo, "commit", "-qm", "updater")
    _release(tmp_path / "remote-work", bare, "v1.2.0",
             {"CHANGELOG.md": CHANGELOG})
    _git(repo, "fetch", "-q", "--tags")
    monkeypatch.setattr(mcs_util, 'load_config', lambda: cfg or {})
    monkeypatch.setattr(updater, "precheck_local", lambda c: [])
    monkeypatch.setattr(update_apply, 'precheck_local', updater.precheck_local)
    monkeypatch.setattr(update_planning, 'precheck_local', updater.precheck_local)
    monkeypatch.setattr(updater, "precheck_tag", lambda t: list(tag_errors))
    monkeypatch.setattr(update_execution, 'precheck_tag', updater.precheck_tag)
    monkeypatch.setattr(update_planning, 'precheck_tag', updater.precheck_tag)
    return repo


def test_plan_routes_install_change_to_reinstall(updater, monkeypatch, tmp_path):
    _plan_env(updater, monkeypatch, tmp_path, ["install_sh_changed"])
    p = updater.plan("v1.2.0")
    assert p["route"] == "reinstall" and p["blockers"] == []
    assert p["reinstall"] == ["install_sh_changed"]
    assert [n["version"] for n in p["notes"]] == ["1.2.0"]


@pytest.mark.parametrize('cfg,required', [
    ({'notify_target': 'local'}, False),
    ({'notify': {'interactive': 'slack'}}, True),
    ({'notify_target': 'slack:C000000001', 'notify_system_target': 'discord:1',
      'notify': {'interactive': 'slack'}}, False),
])
def test_plan_reports_only_exact_slack_eligibility_without_external_settings(
        updater, monkeypatch, tmp_path, cfg, required):
    _plan_env(updater, monkeypatch, tmp_path, [], cfg=cfg)
    monkeypatch.setattr(service_management, 'hermes_cli', lambda *_: pytest.fail('read-only plan contacted Hermes'))
    sha = 'a' * 40
    report = updater._plan('v1.2.0', cfg, updater.journal_store.default_state(),
                           {'tag': 'v1.2.0', 'sha': sha}, sha)
    assert report['slack_channels_required'] is required


@pytest.mark.parametrize(('config', 'legacy', 'status', 'route'), [
    ('{}', '{"v": 1, "install_args": []}', 'required', 'apply'),
    ('{"install_args": []}', '{"v": 1, "install_args": []}', 'none', 'noop'),
    ('{}', '{broken', 'unverified', 'blocked'),
])
def test_noop_plan_detects_missing_or_unverified_migration_without_writing(
        updater, monkeypatch, tmp_path, config, legacy, status, route):
    _plan_env(updater, monkeypatch, tmp_path, [])
    config_path = Path(mcs_util.CONF_PATH)
    config_path.write_text(config)
    record = Path(updater.DATA) / 'install_options.json'
    record.write_text(legacy)
    before = config_path.read_bytes(), record.read_bytes()
    sha = 'a' * 40
    result = updater._plan('v1.2.0', {}, updater.journal_store.default_state(),
                           {'tag': 'v1.2.0', 'sha': sha}, sha)
    assert result['settings_migration'] == status and result['route'] == route
    assert ('settings_migration_unverified' in result['blockers']) == (status == 'unverified')
    assert (config_path.read_bytes(), record.read_bytes()) == before


def test_noop_plan_reports_pending_as_blocked(updater, monkeypatch, tmp_path):
    _plan_env(updater, monkeypatch, tmp_path, [])
    marker = Path(updater.DATA).parent / settings_migration_receipt.NAME
    marker.write_bytes(settings_migration_receipt.BODY)
    sha = 'a' * 40
    result = updater._plan('v1.2.0', {}, updater.journal_store.default_state(),
                           {'tag': 'v1.2.0', 'sha': sha}, sha)
    assert result['settings_migration'] == 'pending'
    assert result['route'] == 'blocked'
    assert 'settings_migration_pending' in result['blockers']
    assert marker.read_bytes() == settings_migration_receipt.BODY


def test_noop_migration_requiring_reinstall_does_not_claim_repair(updater, monkeypatch, tmp_path):
    _plan_env(updater, monkeypatch, tmp_path, ['install_sh_changed'])
    Path(mcs_util.CONF_PATH).write_text('{}')
    (Path(updater.DATA) / 'install_options.json').write_text('{"v": 1, "install_args": []}')
    sha = 'a' * 40
    result = updater._plan('v1.2.0', {}, updater.journal_store.default_state(),
                           {'tag': 'v1.2.0', 'sha': sha}, sha)
    assert result['route'] == 'blocked'
    assert 'settings_migration_requires_reinstall' in result['blockers']


@pytest.mark.parametrize("kw,errors,blocker", [
    ({}, ["tree_dirty"], "tree_dirty"),
    ({"legacy": True}, [], "legacy_source_manual"),
    ({"cfg": {"runtime_mode": "standalone"}}, [], "standalone_external_apply"),
])
def test_plan_blocks(updater, monkeypatch, tmp_path, kw, errors, blocker):
    _plan_env(updater, monkeypatch, tmp_path, errors, **kw)
    p = updater.plan("v1.2.0")
    assert p["route"] == "blocked" and blocker in p["blockers"]


def test_plan_reports_interrupted_update(updater, monkeypatch, tmp_path):
    _plan_env(updater, monkeypatch, tmp_path, [])
    state = updater.journal_store.default_state()
    state["stages"].append({"stage": "merge", "at": 1.0})
    updater.journal_store.save_state(updater.STATE_PATH, state)
    assert "update_in_progress_or_interrupted" in updater.plan("v1.2.0")["blockers"]


def _install_tag(updater, monkeypatch, tmp_path):
    repo = _apply_env(updater, monkeypatch,
                      precheck_errors=["install_sh_changed"])
    sha = _release(tmp_path / "remote-work", tmp_path / "remote.git",
                   "v1.2.0", {"install.sh": "#!/bin/sh\n"})
    monkeypatch.setattr(update_discovery, 'remote_tag_sha', lambda t, **_lif05_paths: sha)
    monkeypatch.setattr(updater, "_run_post_merge",
                        in_process_post_merge(updater, monkeypatch))
    monkeypatch.setattr(update_apply, '_run_post_merge', updater._run_post_merge)
    return repo, sha


def test_install_change_still_blocks_without_reinstall(updater, monkeypatch,
                                                       tmp_path):
    _install_tag(updater, monkeypatch, tmp_path)
    assert updater.apply("v1.2.0", None, None) == 1
    assert "install_sh_changed" in updater.journal_store.load_state(updater.STATE_PATH)["attempts"]["v1.2.0"]["detail"]


class _Install:
    """subprocess.Popen stand-in for install.sh."""
    calls: list = []
    rc = 0

    def __init__(self, argv, **kw):
        assert kw["start_new_session"] and kw["stdin"] == subprocess.DEVNULL
        self.calls.append(argv)
        self.pid, self.returncode = 0, self.rc

    def communicate(self, timeout=None):
        return ("NG brew" if self.rc else "ok"), None


def _fake_install(monkeypatch, rc=0):
    """Replace only the install.sh spawn — subprocess.run (git) keeps
    the real Popen."""
    calls = []
    fake = type("Install", (_Install,), {"calls": calls, "rc": rc})
    real = subprocess.Popen

    def popen(argv, *a, **kw):
        if argv and argv[0] == "/bin/sh":
            return fake(argv, **kw)
        return real(argv, *a, **kw)
    monkeypatch.setattr(mcs_update.subprocess, "Popen", popen)
    return calls


def test_reinstall_runs_install_before_services(updater, prepared_run_log, monkeypatch, tmp_path):
    repo, sha = _install_tag(updater, monkeypatch, tmp_path)
    Path(mcs_util.CONF_PATH).write_text('{"install_args": ["--no-brew"]}')
    assert updater._install_options_snapshot()[1] == ["--no-brew"]
    calls = _fake_install(monkeypatch)
    monkeypatch.setattr(updater, "_services_reconcile",
                        lambda: calls.append("services"))
    monkeypatch.setattr(update_recovery, '_services_reconcile', updater._services_reconcile)
    monkeypatch.setattr(update_restore, '_services_reconcile', updater._services_reconcile)
    monkeypatch.setattr(update_runtime_migration, '_services_reconcile', updater._services_reconcile)
    rc = updater.apply("v1.2.0", None, None, reinstall=True,
                       install_args=("--no-llm",))
    assert rc == 0, updater.journal_store.load_state(updater.STATE_PATH)["attempts"]
    assert calls == [["/bin/sh", str(repo / "install.sh"), "--no-llm",
                      "--no-services"], "services"]
    applied = updater.journal_store.load_state(updater.STATE_PATH)["applied"][-1]
    assert applied["reinstall_done"] is True and applied["sha"] == sha


@pytest.mark.parametrize('reinstall', [False, True])
def test_update_runs_same_root_settings_migration_before_merge_once(updater, prepared_run_log, monkeypatch, tmp_path, reinstall):
    repo = _apply_env(updater, monkeypatch, precheck_errors=['install_sh_changed'] if reinstall else [])
    sha = _release(tmp_path / 'remote-work', tmp_path / 'remote.git', 'v1.2.0', {'f.txt': 'new synthetic release'})
    monkeypatch.setattr(update_discovery, 'remote_tag_sha', lambda *_a, **_k: sha)
    monkeypatch.setattr(updater, '_run_post_merge',
                        in_process_post_merge(updater, monkeypatch))
    monkeypatch.setattr(update_apply, '_run_post_merge', updater._run_post_merge)
    _fake_install(monkeypatch)
    config = Path(mcs_util.CONF_PATH)
    config.write_text('{"unknown": {"keep": true}}')
    legacy = Path(updater.DATA) / 'install_options.json'
    legacy.write_text('{"v": 1, "install_args": ["--no-llm"]}')
    before = legacy.read_bytes()
    stages = []
    journal = updater.journal
    def track(state, stage):
        if stage == 'settings_migration':
            assert state['applying'] is None
            assert json.loads(config.read_text()) == {'unknown': {'keep': True}}
            fd = os.open(updater.RUN_LOCK, os.O_RDWR)
            try:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(fd)
            stages.append(stage)
        return journal(state, stage)
    monkeypatch.setattr(updater, 'journal', track)
    monkeypatch.setattr(update_apply, 'journal', updater.journal)
    monkeypatch.setattr(update_recovery, 'journal', updater.journal)
    monkeypatch.setattr(update_runtime_migration, 'journal', updater.journal)
    assert updater.apply('v1.2.0', sha, None, reinstall=reinstall,
                         install_args=('--no-llm',) if reinstall else ()) == 0
    assert stages == ['settings_migration']
    assert json.loads(config.read_text()) == {'unknown': {'keep': True}, 'install_args': ['--no-llm']}
    assert legacy.read_bytes() == before
    assert _git(repo, 'rev-parse', 'HEAD').stdout.strip() == sha


def test_apply_same_head_recopies_legacy_settings_after_prior_loss(updater, monkeypatch):
    repo = _apply_env(updater, monkeypatch)
    sha = _git(repo, 'rev-parse', 'HEAD').stdout.strip()
    monkeypatch.setattr(update_discovery, 'remote_tag_sha', lambda *_a, **_k: sha)
    config = Path(mcs_util.CONF_PATH)
    config.write_text('{"unknown": {"keep": true}}')
    legacy = Path(updater.DATA) / 'install_options.json'
    legacy.write_text('{"v": 1, "install_args": ["--no-llm"]}')
    before = legacy.read_bytes()
    monkeypatch.setattr(updater, '_run_post_merge', lambda *_a, **_k: pytest.fail('same head cannot merge'))
    monkeypatch.setattr(update_apply, '_run_post_merge', updater._run_post_merge)
    assert updater.apply('v1.1.0', sha, None) == 0
    assert json.loads(config.read_text()) == {'unknown': {'keep': True}, 'install_args': ['--no-llm']}
    assert legacy.read_bytes() == before


def test_postmerge_lost_settings_do_not_record_update_success(updater, monkeypatch, tmp_path):
    _apply_env(updater, monkeypatch)
    sha = _release(tmp_path / 'remote-work', tmp_path / 'remote.git', 'v1.2.0', {'f.txt': 'new'})
    monkeypatch.setattr(update_discovery, 'remote_tag_sha', lambda *_a, **_k: sha)
    config = Path(mcs_util.CONF_PATH)
    config.write_text('{"unknown": {"keep": true}}')
    legacy = Path(updater.DATA) / 'install_options.json'
    legacy.write_text('{"v": 1, "install_args": ["--no-llm"]}')
    original = legacy.read_bytes()

    def old_writer_after_migration(_sha, *, run_lock_fd):
        config.write_text('{"unknown": {"keep": true}}')

    monkeypatch.setattr(updater, '_run_post_merge', old_writer_after_migration)
    monkeypatch.setattr(update_apply, '_run_post_merge', updater._run_post_merge)
    monkeypatch.setattr(updater, '_rollback_tree', lambda *_a, **_k: None)
    monkeypatch.setattr(update_apply, '_rollback_tree', updater._rollback_tree)
    monkeypatch.setattr(update_recovery, '_rollback_tree', updater._rollback_tree)
    assert updater.apply('v1.2.0', sha, None) == 1
    state = updater.journal_store.load_state(updater.STATE_PATH)
    assert not state.get('applied')
    assert 'install_options_unverified' in state['attempts']['v1.2.0']['detail']
    assert legacy.read_bytes() == original


def test_config_sync_failure_keeps_receipt_and_blocks_future_mutations(updater, monkeypatch, tmp_path):
    _apply_env(updater, monkeypatch)
    sha = _release(tmp_path / 'remote-work', tmp_path / 'remote.git', 'v1.2.0', {'f.txt': 'new'})
    monkeypatch.setattr(update_discovery, 'remote_tag_sha', lambda *_a, **_k: sha)
    config = Path(mcs_util.CONF_PATH)
    config.write_text('{"unknown": {"keep": true}}')
    legacy = Path(updater.DATA) / 'install_options.json'
    legacy.write_text('{"v": 1, "install_args": ["--no-llm"]}')
    original_fsync = safe_io.fsync_dir

    def fail_config_directory(path, **kwargs):
        if Path(path) == config.parent:
            _assert_migration_evidence(Path(updater.DATA).parent / settings_migration_receipt.NAME, legacy)
            raise OSError('synthetic config directory fsync failure')
        return original_fsync(path, **kwargs)

    monkeypatch.setattr(safe_io, 'fsync_dir', fail_config_directory)
    assert updater.apply('v1.2.0', sha, None) == 1
    marker = Path(updater.DATA).parent / settings_migration_receipt.NAME
    _assert_migration_evidence(marker, legacy)
    assert json.loads(config.read_text())['install_args'] == ['--no-llm']
    original = (config.read_bytes(), legacy.read_bytes(), marker.read_bytes(),
                Path(updater.STATE_PATH).read_bytes())
    assert updater.apply('v1.2.0', sha, None) == 2
    assert updater.rollback() == 2
    assert updater.recover_interrupted() == 2
    assert updater.cmd_check() == 2
    assert updater._release_migration_probe(time.monotonic() + 10) == 'unverified'
    assert (config.read_bytes(), legacy.read_bytes(), marker.read_bytes(),
            Path(updater.STATE_PATH).read_bytes()) == original


def test_receipt_sync_failure_stops_before_config_publication(updater, monkeypatch, tmp_path):
    _apply_env(updater, monkeypatch)
    sha = _release(tmp_path / 'remote-work', tmp_path / 'remote.git', 'v1.2.0', {'f.txt': 'new'})
    monkeypatch.setattr(update_discovery, 'remote_tag_sha', lambda *_a, **_k: sha)
    config = Path(mcs_util.CONF_PATH)
    config.write_text('{"unknown": {"keep": true}}')
    legacy = Path(updater.DATA) / 'install_options.json'
    legacy.write_text('{"v": 1, "install_args": ["--no-llm"]}')
    original = config.read_bytes(), legacy.read_bytes()
    begin = settings_migration_receipt.Receipt.begin

    def uncertain_begin(item):
        begin(item)
        raise OSError('synthetic receipt directory fsync result unknown')

    monkeypatch.setattr(settings_migration_receipt.Receipt, 'begin', uncertain_begin)
    assert updater.apply('v1.2.0', sha, None) == 1
    assert (config.read_bytes(), legacy.read_bytes()) == original
    _assert_migration_evidence(Path(updater.DATA).parent / settings_migration_receipt.NAME, legacy)


def test_data_replacement_during_publication_keeps_root_receipt(updater, monkeypatch, tmp_path):
    _apply_env(updater, monkeypatch)
    sha = _release(tmp_path / 'remote-work', tmp_path / 'remote.git', 'v1.2.0', {'f.txt': 'new'})
    monkeypatch.setattr(update_discovery, 'remote_tag_sha', lambda *_a, **_k: sha)
    config = Path(mcs_util.CONF_PATH)
    config.write_text('{"unknown": {"keep": true}}')
    data = Path(updater.DATA)
    legacy = data / 'install_options.json'
    legacy.write_text('{"v": 1, "install_args": ["--no-llm"]}')
    begin = settings_migration_receipt.Receipt.begin

    def replace_data_after_begin(item):
        begin(item)
        raw = legacy.read_bytes()
        data.rename(data.with_name('old-synthetic-data'))
        data.mkdir(mode=0o700)
        legacy.write_bytes(raw)

    monkeypatch.setattr(settings_migration_receipt.Receipt, 'begin', replace_data_after_begin)
    assert updater.apply('v1.2.0', sha, None) == 2
    marker = data.parent / settings_migration_receipt.NAME
    _assert_migration_evidence(marker, legacy)
    assert settings_migration_receipt.pending(data)
    assert config.read_text() == '{"unknown": {"keep": true}}'
    assert not (data / 'update_state.json').exists()
    assert updater.apply('v1.2.0', sha, None) == 2


def test_data_replacement_after_config_publication_keeps_root_receipt(updater, monkeypatch, tmp_path):
    _apply_env(updater, monkeypatch)
    sha = _release(tmp_path / 'remote-work', tmp_path / 'remote.git', 'v1.2.0', {'f.txt': 'new'})
    monkeypatch.setattr(update_discovery, 'remote_tag_sha', lambda *_a, **_k: sha)
    config = Path(mcs_util.CONF_PATH)
    config.write_text('{"unknown": {"keep": true}}')
    data = Path(updater.DATA)
    legacy = data / 'install_options.json'
    legacy.write_text('{"v": 1, "install_args": ["--no-llm"]}')
    original_fsync = safe_io.fsync_dir
    changed = []

    def replace_data_after_rename(path, **kwargs):
        if Path(path) == config.parent and not changed:
            changed.append(True)
            raw = legacy.read_bytes()
            data.rename(data.with_name('old-synthetic-data'))
            data.mkdir(mode=0o700)
            legacy.write_bytes(raw)
        return original_fsync(path, **kwargs)

    monkeypatch.setattr(safe_io, 'fsync_dir', replace_data_after_rename)
    assert updater.apply('v1.2.0', sha, None) == 2
    assert changed and json.loads(config.read_text())['install_args'] == ['--no-llm']
    marker = data.parent / settings_migration_receipt.NAME
    _assert_migration_evidence(marker, legacy)
    assert not (data / 'update_state.json').exists()
    assert updater.apply('v1.2.0', sha, None) == 2


def test_update_rollback_keeps_legacy_record_and_migrated_settings(updater, prepared_run_log, monkeypatch, tmp_path):
    repo = _apply_env(updater, monkeypatch)
    old_head = _git(repo, 'rev-parse', 'HEAD').stdout.strip()
    sha = _release(tmp_path / 'remote-work', tmp_path / 'remote.git', 'v1.2.0', {'f.txt': 'new synthetic release'})
    monkeypatch.setattr(update_discovery, 'remote_tag_sha', lambda *_a, **_k: sha)
    monkeypatch.setattr(updater, '_run_post_merge',
                        in_process_post_merge(updater, monkeypatch))
    monkeypatch.setattr(update_apply, '_run_post_merge', updater._run_post_merge)
    monkeypatch.setattr(service_management, 'retire_to_snapshot', lambda *_a, **_k: [])
    config = Path(mcs_util.CONF_PATH)
    config.write_text('{"unknown": {"keep": true}}')
    legacy = Path(updater.DATA) / 'install_options.json'
    legacy.write_text('{"v": 1, "install_args": ["--no-brew"]}')
    before = legacy.read_bytes()
    assert updater.apply('v1.2.0', sha, None) == 0
    assert updater.rollback() == 0
    assert _git(repo, 'rev-parse', 'HEAD').stdout.strip() == old_head
    assert legacy.read_bytes() == before
    assert json.loads(legacy.read_text())['install_args'] == ['--no-brew']
    assert json.loads(config.read_text()) == {'unknown': {'keep': True}, 'install_args': ['--no-brew']}


def test_failed_reinstall_rolls_the_tree_back(updater, monkeypatch, tmp_path):
    repo, _ = _install_tag(updater, monkeypatch, tmp_path)
    before = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _fake_install(monkeypatch, rc=1)
    rolled = []
    monkeypatch.setattr(updater, "_rollback_tree", lambda e, on_hold=None: rolled.append(e))
    monkeypatch.setattr(update_apply, '_rollback_tree', updater._rollback_tree)
    monkeypatch.setattr(update_recovery, '_rollback_tree', updater._rollback_tree)
    assert updater.apply("v1.2.0", None, None, reinstall=True) == 1
    assert rolled and rolled[0]["prev_sha"] == before
    assert "install_failed" in updater.journal_store.load_state(updater.STATE_PATH)["attempts"]["v1.2.0"]["detail"]


@pytest.mark.parametrize("cid,kw", [
    ("cid-1", {"reinstall": True}),                       # receipt path
    (None, {"install_args": ("--no-llm",)}),              # args w/o reinstall
    (None, {"reinstall": True, "install_args": ("--mode",)}),
])
def test_reinstall_is_operator_only(updater, monkeypatch, tmp_path, cid, kw):
    _install_tag(updater, monkeypatch, tmp_path)
    assert updater.apply("v1.2.0", None, cid, **kw) == 2


def test_quiesce_stops_legacy_drainer_only_when_installed(updater, monkeypatch,
                                                          tmp_path):
    agents = tmp_path / "agents"
    agents.mkdir()
    monkeypatch.setattr(mcs_update, "AGENTS_DIR", str(agents))
    monkeypatch.setattr(update_lifecycle_context, 'AGENTS_DIR', mcs_update.AGENTS_DIR)
    monkeypatch.setattr(update_preflight, 'AGENTS_DIR', mcs_update.AGENTS_DIR)
    monkeypatch.setattr(update_services, 'AGENTS_DIR', mcs_update.AGENTS_DIR)
    monkeypatch.setattr(service_contract, 'RESIDENT_LABELS', ())
    monkeypatch.setattr(mcs_update, "_stray_drainer_pids", lambda: [])
    monkeypatch.setattr(update_services, '_stray_drainer_pids', mcs_update._stray_drainer_pids)
    ran = []
    monkeypatch.setattr(mcs_update, "_run", lambda argv, **k: ran.append(argv))
    monkeypatch.setattr(update_services, '_run', mcs_update._run)
    mcs_update.quiesce()
    assert ran == []
    (agents / "ai.mcs.extract-drainer-rt.plist").write_text("")
    assert mcs_update.quiesce() == []               # never restarted
    assert ran[0][:2] == ["launchctl", "bootout"]
    assert ran[0][2].endswith("/ai.mcs.extract-drainer-rt")


def test_reinstall_marks_install_sh_as_an_updater_run(updater, monkeypatch):
    seen = []

    class Install(_Install):
        calls: list = []

        def __init__(self, argv, **kw):
            seen.append(kw["env"].get(mcs_update.REINSTALL_ENV))
            super().__init__(argv, **kw)
    monkeypatch.setattr(mcs_update.subprocess, "Popen", lambda argv, *a, **kw: Install(argv, **kw))
    update_fd = updater.acquire_update_lock()
    assert update_fd is not None
    try:
        monkeypatch.setenv(updater.UPDATE_LOCK_FD_ENV, str(update_fd))
        updater._reinstall({"reinstall": True, "install_args": ["--no-llm"],
                            "install_options_base": updater._install_options_snapshot()[0]})
    finally:
        os.close(update_fd)
    assert seen == ["1"]


def test_interrupted_reinstall_is_never_rerun_unattended(updater, monkeypatch):
    calls = _fake_install(monkeypatch)
    updater._reinstall({"reinstall": True, "reinstall_done": True})
    assert calls == []


def test_plan_flags_an_unfinished_reinstall(updater, monkeypatch, tmp_path):
    _plan_env(updater, monkeypatch, tmp_path, [])
    state = updater.journal_store.default_state()
    state["applied"] = [{"tag": "v1.1.0", "reinstall": True}]
    updater.journal_store.save_state(updater.STATE_PATH, state)
    assert "reinstall_incomplete" in updater.plan("v1.2.0")["blockers"]


def test_reinstall_done_clears_a_promoted_unfinished_reinstall(
        updater, monkeypatch, tmp_path):
    """Regression: nothing could clear reinstall_incomplete after the
    operator finished install.sh by hand."""
    _plan_env(updater, monkeypatch, tmp_path, [])
    monkeypatch.setattr(updater, "_head_sha", lambda: "a" * 40)
    monkeypatch.setattr(update_apply, '_head_sha', updater._head_sha)
    monkeypatch.setattr(update_execution, '_head_sha', updater._head_sha)
    monkeypatch.setattr(update_recovery, '_head_sha', updater._head_sha)
    monkeypatch.setattr(update_repository, '_head_sha', updater._head_sha)
    monkeypatch.setattr(update_restore, '_head_sha', updater._head_sha)
    monkeypatch.setattr(update_runtime_migration, '_head_sha', updater._head_sha)
    state = updater.journal_store.default_state()
    state["applied"] = [{"tag": "v1.1.0", "sha": "b" * 40, "reinstall": True}]
    updater.journal_store.save_state(updater.STATE_PATH, state)
    assert updater.reinstall_done() == 2            # HEAD is elsewhere
    state["applied"][0]["sha"] = "a" * 40
    updater.journal_store.save_state(updater.STATE_PATH, state)
    assert updater.reinstall_done() == 0
    assert "reinstall_incomplete" not in updater.plan("v1.2.0")["blockers"]
    assert updater.reinstall_done() == 2            # nothing left to clear


def test_reinstall_done_lets_recover_finish_the_journal(updater, monkeypatch):
    monkeypatch.setattr(updater, "_head_sha", lambda: "a" * 40)
    monkeypatch.setattr(update_apply, '_head_sha', updater._head_sha)
    monkeypatch.setattr(update_execution, '_head_sha', updater._head_sha)
    monkeypatch.setattr(update_recovery, '_head_sha', updater._head_sha)
    monkeypatch.setattr(update_repository, '_head_sha', updater._head_sha)
    monkeypatch.setattr(update_restore, '_head_sha', updater._head_sha)
    monkeypatch.setattr(update_runtime_migration, '_head_sha', updater._head_sha)
    calls = []

    def recover(*, _resume_provider_migration):
        assert _resume_provider_migration is True
        calls.append(updater.journal_store.load_state(updater.STATE_PATH))
        return 0

    monkeypatch.setattr(updater, "recover_interrupted", recover)
    monkeypatch.setattr(update_apply, 'recover_interrupted', updater.recover_interrupted)
    monkeypatch.setattr(update_cli, 'recover_interrupted', updater.recover_interrupted)
    monkeypatch.setattr(update_recovery, 'recover_interrupted', updater.recover_interrupted)
    state = updater.journal_store.default_state()
    state["applying"] = {"tag": "v1.1.0", "sha": "a" * 40, "reinstall": True}
    updater.journal_store.save_state(updater.STATE_PATH, state)
    assert updater.reinstall_done() == 0
    assert calls[0]["applying"]["reinstall_done"] is True


def test_plan_reports_gate_crash_as_blocker(updater, monkeypatch, tmp_path):
    _plan_env(updater, monkeypatch, tmp_path, [])

    def boom(tag):
        raise updater.UpdateError("cat-file failed")
    monkeypatch.setattr(updater, "precheck_tag", boom)
    monkeypatch.setattr(update_execution, 'precheck_tag', updater.precheck_tag)
    monkeypatch.setattr(update_planning, 'precheck_tag', updater.precheck_tag)
    p = updater.plan("v1.2.0")
    assert p["route"] == "blocked" and p["blockers"][0].startswith("plan_failed")


def test_launcher_apply_refuses_standalone(updater, monkeypatch):
    monkeypatch.setenv("MCS_UPDATE_REPO", "/nonexistent")
    monkeypatch.setattr(mcs_util, 'load_config',
                        lambda: {"runtime_mode": "standalone"})
    assert updater.apply("v1.2.0", None, None) == 2


def test_launcher_copy_notice_never_migrates_the_live_db(updater, tmp_path):
    """Regression: a bail/escalate notice from the launcher's temp copy
    opened the live DB with the target version's Ledger and bumped its
    schema while the live tree still ran the old version."""
    import sqlite3
    live = Path(updater.REPO) / "mcs"
    live.mkdir(parents=True, exist_ok=True)
    (live / "_mcs_path.py").write_text("")
    (live / "ledger.py").write_text(
        "import json, sqlite3\n"
        "class Ledger:\n"
        "    def __init__(self, path):\n"
        "        self.db = sqlite3.connect(path)\n"
        "    def outbox_add(self, kind, pid, payload):\n"
        "        self.db.execute('CREATE TABLE IF NOT EXISTS seen(v)')\n"
        "        self.db.execute('INSERT INTO seen VALUES(?)',"
        " (json.dumps([kind, payload]),))\n"
        "        self.db.commit()\n")
    con = sqlite3.connect(updater.LEDGER)
    con.execute("PRAGMA user_version=7")
    con.close()
    assert updater._enqueue_notice("[MCS] 更新 v9 を中止しました: x")
    con = sqlite3.connect(updater.LEDGER)
    assert con.execute("PRAGMA user_version").fetchone()[0] == 7
    kind, payload = json.loads(con.execute("SELECT v FROM seen").fetchone()[0])
    con.close()
    assert kind == "update_notice" and "中止" in payload["text"]


def test_extracted_current_updater_imports_with_recovery_helper(tmp_path, monkeypatch):
    launcher = _launcher()
    files = {str(p.relative_to(ROOT)): p.read_bytes()
             for p in (ROOT / "mcs").rglob("*.py")}
    helper = "deployment/recovery/mcs_recover.py"
    files.update({str(path.relative_to(ROOT)): path.read_bytes()
                  for path in (ROOT / 'deployment/recovery').glob('*.py')})

    def git(repo, *args, binary=False):
        if args[0] == "ls-tree":
            assert args[4:] == ("mcs", "deployment/recovery")
            return "".join(f"100644 blob {name}\t{name}\0" for name in files)
        assert args[:2] == ("cat-file", "blob") and binary
        return files[args[2]]

    monkeypatch.setattr(launcher, "_git", git)
    launcher.extract("unused", "v1.0.16", str(tmp_path))
    out = subprocess.run(
        [sys.executable, str(tmp_path / "mcs/ops/mcs_update.py"), "--help"],
        capture_output=True, text=True, check=True)
    assert "rollback" in out.stdout
    assert (tmp_path / helper).read_bytes() == files[helper]


def test_candidate_helper_survives_reinstall_sync_and_requests_native_restart(tmp_path, monkeypatch):
    import plistlib
    import test_install_sh

    launcher = _launcher()
    staged = tmp_path / 'candidate'
    files = {str(p.relative_to(ROOT)): p.read_bytes()
             for p in (ROOT / 'mcs').rglob('*.py')}
    helper = 'deployment/recovery/mcs_recover.py'
    files.update({str(path.relative_to(ROOT)): path.read_bytes()
                  for path in (ROOT / 'deployment/recovery').glob('*.py')})

    def git(_repo, *args, binary=False):
        if args[0] == 'ls-tree':
            assert args[4:] == ('mcs', 'deployment/recovery')
            return ''.join(f'100644 blob {name}\t{name}\0' for name in files)
        assert args[:2] == ('cat-file', 'blob') and binary
        return files[args[2]]

    monkeypatch.setattr(launcher, '_git', git)
    launcher.extract('synthetic', 'v1.2.0', str(staged))
    # The launcher starts fresh Python; mirror that ownership for the two
    # modules whose import freezes the candidate's restart implementation.
    for name in ('update_lifecycle_context', 'update_services'):
        owner_spec = importlib.util.spec_from_file_location(name, staged / f'mcs/ops/{name}.py')
        owner = importlib.util.module_from_spec(owner_spec)
        monkeypatch.setitem(sys.modules, name, owner)
        owner_spec.loader.exec_module(owner)
    services = sys.modules['update_services']
    spec = importlib.util.spec_from_file_location('synthetic_candidate_update', staged / 'mcs/ops/mcs_update.py')
    candidate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(candidate)
    assert candidate._request_gateway_restart is not None
    frozen = candidate._request_gateway_restart.__globals__['_GATEWAY_RESTART_PROGRAM']
    captured = candidate._request_gateway_restart.__globals__
    source_path = Path(captured['__file__'])
    assert source_path.name == 'mcs_recovery_gateway.py'
    assert source_path.parent.parent == staged / 'deployment/recovery'
    assert source_path.parent.name.startswith('runtime-')
    assert captured['__loader__'].raw == files['deployment/recovery/mcs_recovery_gateway.py']
    child = subprocess.run([sys.executable, '-c',
        "import pathlib,sys; sys.path.insert(0, sys.argv[1]); import _mcs_path, mcs_update; "
        "source = pathlib.Path(mcs_update._request_gateway_restart.__globals__['__file__']); "
        "assert source.parent.parent == pathlib.Path(sys.argv[2]) / 'deployment/recovery'",
        str(staged / 'mcs'), str(staged)], capture_output=True, text=True)
    assert child.returncode == 0, child.stderr

    (staged / 'install.sh').write_bytes((ROOT / 'install.sh').read_bytes())
    install_dir = staged / 'deployment/install'
    install_dir.mkdir(parents=True)
    (install_dir / 'runtime.sh').write_bytes((ROOT / 'deployment/install/runtime.sh').read_bytes())
    template = staged / 'deployment/launchagents/org.mcs.recovery.plist'
    template.parent.mkdir(parents=True)
    template.write_bytes((ROOT / 'deployment/launchagents/org.mcs.recovery.plist').read_bytes())
    home, hermes_home, _, env = test_install_sh._world(tmp_path / 'installer')
    env.pop('MCS_ROOT', None)
    env.pop('MCS_UPDATE_OPTIONS_BASE', None)
    old = b'# synthetic old recovery generation\n'
    recovery = home / '.mcs-recovery'
    recovery.mkdir(mode=0o700)
    (recovery / 'mcs_recover.py').write_bytes(old)
    data = home / '.mcs/data'
    data.mkdir(mode=0o700, parents=True)
    fd = os.open(data / 'update.lock', os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = test_install_sh._run(
            {**env, 'MCS_UPDATE_REINSTALL': '1', 'MCS_UPDATE_LOCK_FD': str(fd)},
            hermes_home, '--no-brew', '--no-llm', '--no-plugin', '--no-services',
            script=staged / 'install.sh', pass_fds=(fd,))
    finally:
        os.close(fd)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (recovery / 'mcs_recover.py.prev').read_bytes() == old
    assert (recovery / 'mcs_recover.py').read_bytes() == files[helper]

    host = tmp_path / 'restart-home'
    agents = host / 'Library/LaunchAgents'
    agents.mkdir(parents=True)
    restart_data = host / '.mcs/data'
    restart_data.mkdir(parents=True)
    command = host / '.local/bin/hermes'
    command.parent.mkdir(parents=True)
    command.write_text('#!/bin/sh\nexit 0\n')
    command.chmod(0o700)
    plist = agents / 'ai.hermes.gateway.plist'
    plist.write_bytes(plistlib.dumps({'Label': 'ai.hermes.gateway',
                                    'ProgramArguments': [str(command), 'gateway', 'run']}))
    monkeypatch.setattr(services, 'DATA', str(restart_data))
    monkeypatch.setattr(services, 'AGENTS_DIR', str(agents))
    monkeypatch.setattr(services, '_uid', lambda: 501)
    children = []
    monkeypatch.setattr(subprocess, 'Popen', lambda argv, **kw: children.append((argv, kw)))
    candidate.restart_gateway({})
    assert len(children) == 1
    argv, kwargs = children[0]
    assert argv[1:4] == ['-I', '-c', frozen] and kwargs['start_new_session']
    assert json.loads((restart_data / 'gateway_restart.json').read_text())['status'] == 'requested'

    calls = []
    running = [101]
    def run(argv, **kwargs):
        calls.append(argv)
        if argv == [str(command), '-p', 'default', 'gateway', 'restart']:
            assert kwargs['stdin'] == subprocess.DEVNULL
            assert kwargs['stdout'] is kwargs['stderr'] and kwargs['stdout'].fileno() >= 0
            assert kwargs['env']['HERMES_HOME'] == str(host / '.hermes')
            running[0] = 202
            return subprocess.CompletedProcess(argv, 0)
        assert argv[:2] == ['launchctl', 'print']
        if argv[-1].startswith('user/'):
            return subprocess.CompletedProcess(argv, 0, f'path = {plist}\npid = {running[0]}\n')
        return subprocess.CompletedProcess(argv, 113, '')

    monkeypatch.setattr(subprocess, 'run', run)
    namespace = {'__name__': 'synthetic_frozen'}
    exec(frozen.rsplit('sys.exit(', 1)[0], namespace)
    assert namespace['_gateway_restart_run'](str(restart_data), str(agents), 501) == 0
    assert calls.count([str(command), '-p', 'default', 'gateway', 'restart']) == 1
    assert not any('kickstart' in call for call in calls)
    report = json.loads((restart_data / 'gateway_restart.json').read_text())
    assert report['status'] == 'supervisor_restart_verified' and report['pid'] == 202


@pytest.mark.parametrize("binary", [False, True])
def test_launcher_git_failure_never_reports_remote_credential_text(tmp_path, monkeypatch, binary):
    launcher = _launcher()
    secret = "SYNTHETIC_CREDENTIAL_CANARY"
    text = "fatal: unable to access https://fictional:" + secret + "@example.invalid/repo"
    monkeypatch.setattr(launcher.subprocess, "run", lambda *a, **kw:
                        subprocess.CompletedProcess(a[0], 128, b"" if binary else "",
                                                    text.encode() if binary else text))
    with pytest.raises(SystemExit) as failure:
        launcher._git(str(tmp_path), "fetch", binary=binary)
    assert "git fetch に失敗しました" in str(failure.value)
    assert secret not in str(failure.value)
    assert "example.invalid" not in str(failure.value)


def test_launcher_passes_only_a_full_confirmed_hermes_commit(tmp_path, capfd, monkeypatch):
    repo, bare = _make_repo(tmp_path)
    stub = ("import json, os, sys\n"
            "print(json.dumps({'keep': os.environ.get('MCS_UPDATE_KEEP_HERMES_CHECKOUT')}))\n")
    _release(tmp_path / "remote-work", bare, "v1.2.0",
             {"mcs/ops/mcs_update.py": stub, "mcs/_mcs_path.py": ""})
    _git(repo, "fetch", "-q", "--tags")
    monkeypatch.setenv("MCS_UPDATE_KEEP_HERMES_CHECKOUT", "f" * 40)   # stale ambient value
    launcher = _launcher()
    assert launcher.main(["--repo", str(repo), "--no-fetch", "plan",
                          "--keep-hermes-checkout", "a" * 40]) == 0
    assert json.loads(capfd.readouterr().out)["keep"] == "a" * 40
    assert launcher.main(["--repo", str(repo), "--no-fetch", "plan"]) == 0
    assert json.loads(capfd.readouterr().out)["keep"] is None
    with pytest.raises(SystemExit, match="40桁"):
        launcher.main(["--repo", str(repo), "--no-fetch", "apply",
                       "--keep-hermes-checkout", "abc123"])


# One release-tag grammar for detection, notice, approval and the CLI (F22).
#
# Fully synthetic: temp ledger, stubbed updater pins/config, temp git repos.
PRERELEASE = "v1.3.0-rc.1"


def _grammar_launcher():
    spec = importlib.util.spec_from_file_location(
        "mcs_upgrade_grammar", ROOT / "scripts" / "mcs_upgrade.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _apply_req(**fields):
    req = {"version": 1, "cmd": "ops.update_apply",
           "command_id": str(uuid.uuid4()), "actor": "synthetic-cco",
           "human_confirmed": True, "project_id": None, "reason": "go"}
    req.update(fields)
    return req


def _approve(tmp_path, monkeypatch, update_cfg, tag):
    import mcs_util
    wrapper = tmp_path / "mcs_update.sh"
    wrapper.write_text("#!/bin/sh\n")
    monkeypatch.setattr(update_launch, 'HERMES_WRAPPER', str(wrapper))
    monkeypatch.setattr(mcs_util, "load_config", lambda: {"update": update_cfg})
    monkeypatch.setattr(update_discovery, 'remote_tag_sha', lambda t, **_lif05_paths: "b" * 40)
    monkeypatch.setattr(update_discovery, 'current_version',
                        lambda**_lif05_paths: ("v1.2.0", "c" * 40))
    db = Ledger(str(tmp_path / "ledger.db"))
    try:
        return mcs_requests.apply_command(db, _apply_req(tag=tag))
    finally:
        db.close()


def test_notified_prerelease_preview_validates():
    # The notify-mode notice tells the operator to send exactly this tag.
    assert mcs_requests.validate(_apply_req(tag=PRERELEASE)) is None
    rollback = _apply_req(cmd="ops.update_rollback", tag=PRERELEASE)
    assert mcs_requests.validate(rollback) is None


def test_opted_in_prerelease_approval_is_scheduled(tmp_path, monkeypatch):
    receipt = _approve(tmp_path, monkeypatch,
                       {"mode": "notify", "include_prerelease": True}, PRERELEASE)
    assert receipt["outcome"] == "applied"
    assert receipt["tag"] == PRERELEASE and receipt["target_sha"] == "b" * 40
    # the scheduled updater picks the same receipt up
    assert mcs_update.update_policy.version_key(receipt["tag"]) is not None


def test_prerelease_approval_requires_the_opt_in(tmp_path, monkeypatch):
    receipt = _approve(tmp_path, monkeypatch, {"mode": "notify"}, PRERELEASE)
    assert receipt["outcome"] == "rejected"
    assert receipt["error"] == "prerelease_not_enabled"


@pytest.mark.parametrize("tag", [
    "v1.2.3", "1.2.3", PRERELEASE, "v1.3.0-rc1", "v1.3.0-0.alpha-x",
    "v1.0.0-rc..1", "v1.0.0-", "v1.0.0-.rc", "v1.0.0-rc.", "v1.2", "v1.2.3.4",
    "v١.٠.٢", "v1.2.3\n", "V1.2.3", "v1.0.0-rc+1",
    "v1.0.0-" + "a" * 250, None, 123,
])
def test_approval_grammar_equals_the_updater_ordering(tag):
    # anything approvable must be orderable, or the scan drops the receipt
    approvable = mcs_requests.validate(_apply_req(tag=tag)) is None
    assert approvable == update_policy.release_tag(tag) \
        == (mcs_update.update_policy.version_key(tag) is not None)


def test_apply_requires_a_single_well_formed_tag():
    req = _apply_req()
    assert mcs_requests.validate(req) == "bad_tag"
    assert mcs_requests.validate(_apply_req(tag="v1.0.0-rc..1")) == "bad_tag"


def test_launcher_copy_matches_the_shared_grammar():
    assert _grammar_launcher().SEMVER_RE.pattern == update_policy.RELEASE_TAG_RE.pattern


def test_launcher_default_target_is_the_newest_release(monkeypatch):
    launcher = _grammar_launcher()
    monkeypatch.setattr(launcher, "_git",
                        lambda *a, **k: "v1.2.0\nv1.10.0\nv1.11.0-rc.1\nnotes\n")
    assert launcher.latest_tag("unused") == "v1.10.0"


def test_launcher_accepts_an_explicit_prerelease(tmp_path, capfd):
    repo, bare = _make_repo(tmp_path)
    stub = "import json, sys\nprint(json.dumps(sys.argv[1:]))\n"
    _release(tmp_path / "remote-work", bare, PRERELEASE,
             {"mcs/ops/mcs_update.py": stub, "mcs/_mcs_path.py": ""})
    _git(repo, "fetch", "-q", "--tags")
    assert _grammar_launcher().main(["--repo", str(repo), "--no-fetch", "plan",
                             "--to", PRERELEASE]) == 0
    assert json.loads(capfd.readouterr().out) == ["plan", "--tag", PRERELEASE]


def test_cli_accepts_a_prerelease_and_rejects_non_ascii_digits(monkeypatch):
    monkeypatch.setenv("MCS_LIFECYCLE_PINNED", "1")
    calls = []
    monkeypatch.setattr(mcs_cli.os, "execv",
                        lambda exe, argv: calls.append(argv))
    mcs_cli.main(["update", "apply", "--to", PRERELEASE])
    assert calls[0][1:] == [str(ROOT / "scripts/mcs_upgrade.py"), "--repo",
                            str(ROOT), "apply", "--to", PRERELEASE]
    with pytest.raises(SystemExit) as result:
        mcs_cli.main(["update", "plan", "--to", "v١.٠.٢"])
    assert result.value.code == 2


def test_updater_help_explains_actions_in_japanese(capsys):
    with pytest.raises(SystemExit) as stopped:
        mcs_update._build_parser().parse_args(["--help"])
    assert stopped.value.code == 0
    output = capsys.readouterr().out
    assert "直近の更新前の版に戻す" in output
    assert "使い方:" in output
    assert "この説明を表示して終了する" in output
    assert "変更せずに更新内容" in output
    assert "設定・承認に従って更新" in output
    with pytest.raises(SystemExit) as stopped:
        _launcher().main(["apply", "--help"])
    assert stopped.value.code == 0
    output = capsys.readouterr().out
    assert "再導入" in output
    assert "40桁" in output


def test_updater_bad_arguments_explain_help_in_japanese(capsys):
    with pytest.raises(SystemExit) as stopped:
        mcs_update._build_parser().parse_args(["--unknown-synthetic-flag"])
    assert stopped.value.code == 2
    assert "入力内容を確認してください" in capsys.readouterr().err


@pytest.mark.parametrize("repair", [False, True])
def test_plan_exposes_reinstall_recovery_repair(updater, monkeypatch, tmp_path, repair):
    _plan_env(updater, monkeypatch, tmp_path, ["install_sh_changed"])
    monkeypatch.setattr(service_management, 'recovery_repair_pending', lambda cfg, **_service_kwargs: repair)
    report = updater.plan("v1.2.0")
    assert report["route"] == "reinstall"
    assert report["reinstall_requires_recovery_repair"] is repair
