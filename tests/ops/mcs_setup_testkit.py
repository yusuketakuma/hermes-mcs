"""Synthetic mcs setup testkit contracts; shared helpers preserve isolated runtime state."""
import setup_config_snapshot
import setup_integration
import environment_diagnostics
import service_management
import runtime_inspection
import lifecycle_io
import scheduled_jobs
import json
import subprocess
import mcs_setup
import local_llm
import mcs_util


_DISCORD_CFG = {"notify": {"interactive": "discord", "discord": {
    "profile": "cco", "application_id": "app",
    "guild_id": "g", "channel_id": "ch"}}}

_SLACK_CFG = {"notify": {"interactive": "slack", "slack": {
    "profile": "ops", "application_id": "A1",
    "team_id": "T1", "channel_id": "C1"}}}


def _assert_bool_key(key, value):
    errors, warnings = mcs_setup.validate_config({
        "mcs_login_id": "synthetic", "notify_target": "slack:#synthetic",
        key: value})
    assert errors == [f"{key}: true（有効）かfalse（無効）を指定してください"]
    assert warnings == []


def _linux_probe(monkeypatch):
    """Stub the machine probes so check_environment runs outside macOS/Hermes."""
    monkeypatch.setattr(environment_diagnostics.sys, "platform", "linux")
    monkeypatch.setattr(environment_diagnostics.os.path, "exists", lambda p: True)
    monkeypatch.setattr(local_llm, "bounded_request", lambda *a, **k: (200, {}, b"{}"))
    monkeypatch.setattr(mcs_util.shutil, "which", lambda *a: "/usr/bin/hermes")
    monkeypatch.setattr(environment_diagnostics.os.path, "isfile", lambda p: True)
    monkeypatch.setattr(environment_diagnostics.os, "access", lambda p, m: True)


def _init_env(monkeypatch, tmp_path, cfg):
    """Common stubs for init tests — no real FS/keychain/prompts."""
    monkeypatch.setattr(mcs_setup, "cmd_check", lambda args: 0)
    monkeypatch.setattr(mcs_setup, "CONF_PATH", str(tmp_path / "c.json"))
    monkeypatch.setattr(setup_config_snapshot, "CONF_PATH", str(tmp_path / "c.json"))
    monkeypatch.setattr(mcs_setup, "ENV_PATH", str(tmp_path / ".env"))
    monkeypatch.setattr(setup_integration, "ENV_PATH", str(tmp_path / ".env"))
    monkeypatch.setattr(mcs_setup, "HOME", str(tmp_path))
    monkeypatch.setattr(setup_integration, "HOME", str(tmp_path))
    (tmp_path / "c.json").write_text(json.dumps(cfg))
    monkeypatch.delenv("MCS_SETUP_PASSWORD", raising=False)
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    # plugin integration is tested separately — keep init tests off the
    # real hermes CLI entirely
    monkeypatch.setattr(mcs_setup, "_apply_plugin_integration",
                        lambda c, a: True)
    # init syncs the gateway: a real `hermes gateway install` under a temp
    # HOME rewrote the live gateway LaunchAgent on 2026-10-04.
    monkeypatch.setattr(service_management, 'sync_gateway', lambda *a, **k: 0)
    monkeypatch.setattr(mcs_util, 'load_config', lambda: dict(cfg))
    # the dictionary step reads a bundle: generic init tests get none (skipped)
    import mcs_drug
    monkeypatch.setattr(mcs_drug, "BUNDLE", tmp_path / "no-bundle")


# ---- services (launchd + hermes cron automation) --------------------

def _services_env(monkeypatch, tmp_path, cron_names=frozenset(),
                  loaded=frozenset(), cron_entries=None,
                  cron_applies=True):
    """Isolate cmd_services: temp dirs, no real launchctl/hermes.
    `cron_names` is legacy sugar: it fabricates verified `cron list`
    entries whose schedules match CRON_JOBS (=> 'exists', no edit).
    The fake hermes cron is stateful — create/edit/remove change what
    the next `cron list` returns — unless `cron_applies` is False
    (a zero-exit call that changed nothing)."""
    from types import SimpleNamespace
    calls = []
    loaded = set(loaded)
    entries = list(cron_entries or [])
    sched_by_script = {s: sched for _, sched, s in scheduled_jobs.CRON_JOBS}
    for i, name in enumerate(cron_names):
        entries.append({"id": f"9{i:05d}", "name": name,
                        "schedule": sched_by_script.get(name,
                                                        "0 0 * * *"),
                        "script": name})
    monkeypatch.setattr(mcs_setup, "SCRIPTS_DIR", str(tmp_path / "scripts"))
    monkeypatch.setattr(mcs_setup, "AGENTS_DIR", str(tmp_path / "agents"))
    monkeypatch.setattr(mcs_setup, "MANIFEST_PATH",
                        str(tmp_path / "data" / "service_manifest.json"))
    monkeypatch.setattr(mcs_setup, "HERMES_PY", "/h/venv/bin/python")
    from test_runtime_compatibility import _facts
    monkeypatch.setattr(runtime_inspection, "probe_python", lambda exe: _facts())
    # interpreter probe has its own tests — keep it out of `calls`
    monkeypatch.setattr(service_management, 'hermes_py_problem', lambda**_service_kwargs: None)
    monkeypatch.setattr(mcs_setup, "HOME", str(tmp_path))
    monkeypatch.setattr(setup_integration, "HOME", str(tmp_path))
    monkeypatch.setattr(mcs_setup, "RECOVERY_DIR", str(tmp_path.resolve() / "log-helper-recovery"))
    # Live service rendering requires a genuine private helper and root-bound
    # pointer. Prepare the canonical source in this test's synthetic root.
    import run_log_install
    (tmp_path / "data").mkdir(mode=0o700, exist_ok=True)
    run_log_install.prepare(tmp_path, mcs_setup.REPO_ROOT, mcs_setup.RECOVERY_DIR,
                            promote=True)
    monkeypatch.setattr(mcs_setup.sys, "platform", "darwin")
    monkeypatch.setattr(service_management, 'agent_loaded',
                        lambda label, **_service_kwargs: label in loaded)
    monkeypatch.setattr(mcs_util, 'hermes_exe', lambda cfg: "/x/hermes")
    _previous_service_load_config_1007 = mcs_util.load_config
    monkeypatch.setattr(mcs_util, 'load_config',
                        lambda path=None: _previous_service_load_config_1007(path) if path else {})
    monkeypatch.setattr(service_management, 'inspect_cron', lambda h: entries)
    monkeypatch.setattr(mcs_setup.os.path, "isfile", lambda p: True)
    monkeypatch.setattr(mcs_setup.os, "access", lambda p, m: True)

    def fake_run(argv, **kw):
        calls.append(list(argv))
        # simulate launchd: bootstrap registers the label, bootout drops it
        if argv[:2] == ["launchctl", "bootstrap"]:
            loaded.add(argv[3].rsplit("/", 1)[-1][:-6])
        elif argv[:2] == ["launchctl", "bootout"]:
            loaded.discard(argv[2].rsplit("/", 1)[-1])
        elif argv[:2] == ["launchctl", "print"]:
            # launchd answers 113 for a label it does not know
            rc = 0 if argv[2].rsplit("/", 1)[-1] in loaded else 113
            return SimpleNamespace(returncode=rc, stdout="", stderr="")
        elif argv[1:3] == ["cron", "create"] and cron_applies:
            entries.append({"id": f"c{len(calls):05d}",
                            "name": argv[argv.index("--name") + 1],
                            "schedule": argv[3],
                            "script": argv[argv.index("--script") + 1]})
        elif argv[1:3] == ["cron", "edit"] and cron_applies:
            next(e for e in entries if e["id"] == argv[3])["schedule"] = \
                argv[argv.index("--schedule") + 1]
        elif argv[1:3] == ["cron", "remove"] and cron_applies:
            entries[:] = [e for e in entries if e["id"] != argv[3]]
        # the service SQLite probe reads the version the child prints
        return SimpleNamespace(returncode=0, stdout="3 51 3\n" if "sqlite3" in argv[-1] else "", stderr="")
    monkeypatch.setattr(mcs_setup.subprocess, "run", fake_run)
    return calls, SimpleNamespace(dry_run=False)


def _cron_creates(calls):
    return [a for a in calls if a[1:3] == ["cron", "create"]]


def _seed_manifest(tmp_path):
    import json
    path = tmp_path / "data" / "service_manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    previous = {"v": 1, "cron": [{"name": "MCS unread check",
                                  "script": "mcs_check.sh",
                                  "id": "000001"}],
                "agents": [], "scripts": []}
    path.write_text(json.dumps(previous))
    return path, previous


def _desired_cron_entries():
    return [{"id": f"{i:06d}", "name": n, "schedule": s, "script": sc}
            for i, (n, s, sc) in enumerate(scheduled_jobs.CRON_JOBS)]


def _stale_owned_cron():
    return {"id": "000009", "name": "old", "schedule": "0 0 * * *",
            "script": "mcs_old.sh"}


def _plugin_args(**kw):
    from types import SimpleNamespace
    base = {"yes": True, "plugin_profile": "", "plugin_user_ids": None,
                "plugin_chat_ids": None, "plugin_project_ids": None}
    base.update(kw)
    return SimpleNamespace(**base)


def _plugin_env(monkeypatch, existing=None):
    """Stub the hermes CLI surface; `existing` maps config keys that
    `config get` already resolves."""
    sets = []
    monkeypatch.setattr(mcs_util, 'hermes_exe', lambda c: "/x/hermes")
    monkeypatch.setattr(runtime_inspection, "executable_available", lambda e: True)
    monkeypatch.setattr(
        setup_integration, "_hermes_config_get",
        lambda e, p, k: (existing or {}).get(k))
    monkeypatch.setattr(
        setup_integration, "_hermes_config_set",
        lambda e, p, k, v: sets.append((p, k, str(v))) or True)
    # secrets go through the trusted Hermes command; here it forwards to the stubbed writer
    def secret_cli(e, p, *argv, input_text=None):
        if argv[:2] == ("config", "set"):
            ok = setup_integration._hermes_config_set(e, p, argv[2], input_text)
            return subprocess.CompletedProcess(argv, 0 if ok else 1, "", "")
        return subprocess.CompletedProcess(argv, 1, "", "")
    monkeypatch.setattr(setup_integration, "_secret_hermes", lambda e: (secret_cli, lambda: True))
    # the token state comes from the profile .env line; a saved value reads back
    monkeypatch.setattr(
        setup_integration, "_hermes_token_state",
        lambda e, p, k, expect=None: "registered" if expect is not None or (existing or {}).get(k)
        else "missing")
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    return sets


def _check_only(monkeypatch, runtime=([], []), config=([], [])):
    _previous_service_load_config_2029 = mcs_util.load_config
    monkeypatch.setattr(mcs_util, 'load_config',
                        lambda path=None: _previous_service_load_config_2029(path) if path else {})
    monkeypatch.setattr(service_management, 'check_runtime', lambda cfg, **_service_kwargs: runtime)
    monkeypatch.setattr(mcs_setup, "validate_config", lambda cfg: config)
    monkeypatch.setattr(mcs_setup, "check_environment",
                        lambda cfg: ([], []))
    monkeypatch.setattr(service_management, 'script_drift', lambda**_service_kwargs: ([], []))
    monkeypatch.setattr(mcs_setup, "_queue_warnings", lambda cfg: [])


# ---- runtime: services interpreter, launchd hermes, recovery, llama ----

def _stub_py(path, rc):
    # the service SQLite probe reads the version the child prints
    path.write_text(f'#!/bin/sh\ncase "$2" in *sqlite3*) echo 3 51 3;; esac\nexit {rc}\n')
    path.chmod(0o755)
    return str(path)


def _runtime_darwin(monkeypatch, tmp_path, loaded=()):
    from types import SimpleNamespace
    repo = tmp_path / "repo"
    (repo / "deployment" / "recovery").mkdir(parents=True)
    (repo / "deployment" / "recovery" / "mcs_recover.py").write_text("v1")
    for d in ("recovery", "agents"):
        (tmp_path / d).mkdir()
    monkeypatch.setattr(mcs_setup.sys, "platform", "darwin")
    monkeypatch.setattr(mcs_setup, "REPO_ROOT", str(repo))
    monkeypatch.setattr(setup_integration, "REPO_ROOT", str(repo))
    monkeypatch.setattr(mcs_setup, "RECOVERY_DIR", str(tmp_path / "recovery"))
    monkeypatch.setattr(mcs_setup, "AGENTS_DIR", str(tmp_path / "agents"))
    monkeypatch.setattr(service_management, 'hermes_py_problem', lambda**_service_kwargs: None)
    monkeypatch.setattr(runtime_inspection, "executable_available", lambda exe: True)
    monkeypatch.setattr(runtime_inspection, "probe_python", lambda exe: {
        "python": "3.14.8", "sqlite": "3.51.3",
        "packages": {"discord.py": None, "slack-bolt": None, "slack-sdk": None}})
    monkeypatch.setattr(lifecycle_io, 'run_bounded',
                        lambda *a, **k: SimpleNamespace(returncode=0))
    loaded = set(loaded)
    monkeypatch.setattr(service_management, 'agent_loaded',
                        lambda label, **_service_kwargs: label in loaded)
    return repo, loaded
