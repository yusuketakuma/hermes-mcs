"""Synthetic mcs setup services contracts; shared helpers preserve isolated runtime state."""
import setup_integration
import service_contract
import service_management
import lifecycle_io
import scheduled_jobs
import tempfile
from pathlib import Path
import pytest
import mcs_setup
import mcs_util
from mcs_setup_testkit import _cron_creates as _cron_creates, _desired_cron_entries as _desired_cron_entries, _seed_manifest as _seed_manifest, _services_env as _services_env, _stale_owned_cron as _stale_owned_cron


def test_services_renders_bootstraps_and_registers(monkeypatch, tmp_path):
    # dedupe is by SCRIPT filename — deployed job names drifted
    # ("MCS job drain" vs canonical "MCS durable drain")
    calls, args = _services_env(
        monkeypatch, tmp_path, cron_names={"mcs_check.sh"})
    assert mcs_setup.cmd_services(args) == 0
    # wrapper scripts rendered with placeholders substituted
    s = (tmp_path / "scripts" / "mcs_check.sh").read_text()
    assert "__PYTHON__" not in s and "/h/venv/bin/python" in s
    # every launchd plist rendered and bootstrapped
    for label in service_contract.AGENT_LABELS:
        body = (tmp_path / "agents" / f"{label}.plist").read_text()
        assert "__REPO__" not in body
    boots = [a for a in calls if a[:2] == ["launchctl", "bootstrap"]]
    assert len(boots) == len(service_contract.AGENT_LABELS) == 5
    # cron: the job whose script is already registered is skipped;
    # the other 6 (incl. OCR and the update check) are created
    creates = [" ".join(a) for a in calls
               if a[:3] == ["/x/hermes", "cron", "create"]]
    assert len(creates) == 6
    assert not any("mcs_check.sh" in c for c in creates)
    assert any("mcs_deep.sh" in c for c in creates)
    assert any("mcs_health.sh" in c for c in creates)
    assert any("mcs_update.sh" in c for c in creates)
    # the service manifest was written with all rendered identities
    import json as _json
    manifest = _json.loads(
        (tmp_path / "data" / "service_manifest.json").read_text())
    assert len(manifest["scripts"]) == 8
    offsite = tmp_path / "scripts" / "mcs_offsite.sh"
    assert offsite.is_file() and "ENABLED=0" in offsite.read_text()
    assert not any("mcs_offsite.sh" in command for command in creates)
    assert len(manifest["agents"]) == len(service_contract.AGENT_LABELS)
    assert len(manifest["cron"]) == 7


def test_services_skips_loaded_agents_and_existing_cron(
        monkeypatch, tmp_path):
    loaded = set(service_contract.AGENT_LABELS)
    calls, args = _services_env(
        monkeypatch, tmp_path, loaded=loaded,
        cron_entries=[{"id": f"{i:06d}", "name": n, "schedule": s,
                       "script": sc}
                      for i, (n, s, sc) in enumerate(scheduled_jobs.CRON_JOBS)])
    # pre-render identical plists so 'loaded + unchanged' is exercised
    subs = service_management.service_substitutions(
        mcs_util.load_config(), paths=mcs_setup._service_paths())
    (tmp_path / "agents").mkdir(parents=True)
    for label in service_contract.AGENT_LABELS:
        body = scheduled_jobs.render_template(
            (Path(mcs_setup.REPO_ROOT) / "deployment/launchagents"
             / f"{label}.plist").read_text(), subs)
        (tmp_path / "agents" / f"{label}.plist").write_text(body)
    assert mcs_setup.cmd_services(args) == 0
    assert not any(a[:2] == ["launchctl", "bootstrap"] for a in calls)
    assert not any(a[:3] == ["/x/hermes", "cron", "create"]
                   for a in calls)


def test_services_dry_run_writes_nothing(monkeypatch, tmp_path):
    calls, args = _services_env(monkeypatch, tmp_path)
    args.dry_run = True
    assert mcs_setup.cmd_services(args) == 0
    assert not (tmp_path / "scripts").exists()
    assert not (tmp_path / "agents").exists()
    assert calls == []


def test_service_templates_preserve_literal_paths(monkeypatch, tmp_path):
    import plistlib
    import subprocess
    calls, args = _services_env(monkeypatch, tmp_path)
    unusual = str(tmp_path / "space & <tag> $(touch SHOULD_NOT_EXIST) 'quote'")
    monkeypatch.setattr(mcs_setup, "HERMES_PY", unusual + "/python")
    monkeypatch.setattr(mcs_setup, "HOME", unusual)
    monkeypatch.setattr(setup_integration, "HOME", unusual)
    import run_log_install
    Path(unusual, "data").mkdir(mode=0o700, parents=True)
    run_log_install.prepare(unusual, mcs_setup.REPO_ROOT, mcs_setup.RECOVERY_DIR,
                            promote=True)
    assert mcs_setup.cmd_services(args) == 0
    script = (tmp_path / "scripts" / "mcs_health.sh").read_text()
    # Replace just the final invocation with a literal-value print; no service runs.
    script = script[:script.index('"$PY"')] + 'printf "%s" "$PY"\n'
    # _services_env stubs subprocess.run, so use Popen for this isolated shell.
    with subprocess.Popen(["/bin/sh"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True, cwd=tmp_path) as proc:
        out, err = proc.communicate(script, timeout=5)
        assert proc.returncode == 0, err
    assert out == unusual + "/python"
    assert not (tmp_path / "SHOULD_NOT_EXIST").exists()
    plist = plistlib.loads((tmp_path / "agents" / "local.mcs-cmd.plist").read_bytes())
    assert plist["ProgramArguments"][0] == unusual + "/python"
    assert plist["WatchPaths"] == [unusual + "/data/cmd"]


@pytest.mark.parametrize("output,expected", [
    ("unexpected output", None), ("", None),
    ("No scheduled jobs.\nCreate one with hermes cron create", []),
])
def test_unverifiable_cron_list_is_not_an_empty_schedule(monkeypatch, output, expected):
    from types import SimpleNamespace
    monkeypatch.setattr(mcs_setup.subprocess, "run", lambda *a, **k:
                        SimpleNamespace(returncode=0, stdout=output))
    assert service_management.inspect_cron('/synthetic/hermes') == expected


def test_decorated_schedule_compares_actual_five_fields():
    assert service_contract.normalize_schedule('cron: */5 * * * * (UTC)') == "*/5 * * * *"


def test_services_retires_previous_manifest_agent(monkeypatch, tmp_path):
    import json
    obsolete = "ai.mcs.extract-obsolete"
    calls, args = _services_env(monkeypatch, tmp_path, loaded={obsolete})
    agents = tmp_path / "agents"
    agents.mkdir()
    (agents / (obsolete + ".plist")).write_text("synthetic")
    (tmp_path / "data").mkdir(exist_ok=True)
    Path(mcs_setup.MANIFEST_PATH).write_text(json.dumps({"agents": [{"label": obsolete}]}))
    assert mcs_setup.cmd_services(args) == 0
    assert not (agents / (obsolete + ".plist")).exists()
    assert ["launchctl", "bootout", f"gui/{mcs_setup.os.getuid()}/" + obsolete] in calls


def test_services_hermes_missing_reports_problem(monkeypatch, tmp_path):
    calls, args = _services_env(monkeypatch, tmp_path)
    monkeypatch.setattr(mcs_setup.os.path, "isfile", lambda p: False)
    assert mcs_setup.cmd_services(args) == 1


def test_services_rerun_keeps_one_cron_per_script(monkeypatch, tmp_path):
    calls, args = _services_env(monkeypatch, tmp_path)
    assert mcs_setup.cmd_services(args) == 0
    assert mcs_setup.cmd_services(args) == 0
    entries = service_management.inspect_cron('/x/hermes')
    scripts = [e["script"] for e in entries]
    assert sorted(scripts) == sorted(s for _, _, s in scheduled_jobs.CRON_JOBS)
    # the second run found every job and created nothing
    assert len(_cron_creates(calls)) == len(scheduled_jobs.CRON_JOBS)


def test_services_failure_preserves_last_recoverable_manifest(
        monkeypatch, tmp_path):
    import json
    calls, args = _services_env(monkeypatch, tmp_path)
    path, previous = _seed_manifest(tmp_path)
    monkeypatch.setattr(service_management, 'inspect_cron', lambda h: None)
    assert mcs_setup.cmd_services(args) == 1
    assert json.loads(path.read_text()) == previous
    assert not _cron_creates(calls)


def test_services_duplicate_owned_script_blocks_cron_mutation(
        monkeypatch, tmp_path):
    import json
    dup = [{"id": f"00000{i}", "name": "MCS unread check",
            "schedule": "*/5 * * * *", "script": "mcs_check.sh"}
           for i in (1, 2)]
    # an obsolete owned job would normally be removed — not while blocked
    stale = {"id": "000009", "name": "old", "schedule": "0 0 * * *",
             "script": "mcs_old.sh"}
    calls, args = _services_env(monkeypatch, tmp_path,
                                cron_entries=dup + [stale])
    path, previous = _seed_manifest(tmp_path)
    previous["cron"].append({"script": "mcs_old.sh"})
    path.write_text(json.dumps(previous))
    assert mcs_setup.cmd_services(args) == 1
    assert not any(a[1:2] == ["cron"] and a[2] != "list" for a in calls)
    assert json.loads(path.read_text()) == previous


def test_services_noop_create_is_a_problem_not_a_manifest_entry(
        monkeypatch, tmp_path):
    """`cron create` exiting 0 without a job appearing must not be
    recorded as installed."""
    calls, args = _services_env(monkeypatch, tmp_path, cron_applies=False)
    assert mcs_setup.cmd_services(args) == 1
    assert _cron_creates(calls)
    assert not (tmp_path / "data" / "service_manifest.json").exists()


def test_services_noop_remove_keeps_undesired_job(
        monkeypatch, tmp_path):
    """`cron remove` exiting 0 while the job remains is not converged.
    The previous manifest must still name the script, or a later run
    no longer knows to remove it."""
    import json
    stale = _stale_owned_cron()
    calls, args = _services_env(
        monkeypatch, tmp_path,
        cron_entries=_desired_cron_entries() + [stale],
        cron_applies=False)
    path, previous = _seed_manifest(tmp_path)
    previous["cron"].append(dict(stale))
    path.write_text(json.dumps(previous))
    assert mcs_setup.cmd_services(args) == 1
    assert ["/x/hermes", "cron", "remove", stale["id"]] in calls
    assert json.loads(path.read_text()) == previous
    # stale manifest from this run: a later pass still tries to remove
    calls.clear()
    assert mcs_setup.cmd_services(args) == 1
    assert ["/x/hermes", "cron", "remove", stale["id"]] in calls
    assert json.loads(path.read_text()) == previous


def test_services_removed_undesired_cron_leaves_manifest(
        monkeypatch, tmp_path):
    """A remove that actually drops the job converges: problems 0 and
    the rewritten manifest no longer lists it."""
    import json
    stale = _stale_owned_cron()
    calls, args = _services_env(
        monkeypatch, tmp_path,
        cron_entries=_desired_cron_entries() + [dict(stale)])
    path, previous = _seed_manifest(tmp_path)
    previous["cron"].append({"script": stale["script"]})
    path.write_text(json.dumps(previous))
    assert mcs_setup.cmd_services(args) == 0
    assert ["/x/hermes", "cron", "remove", stale["id"]] in calls
    kept = json.loads(path.read_text())
    scripts = [c.get("script") for c in kept["cron"]]
    assert stale["script"] not in scripts
    assert scripts == [s for _, _, s in scheduled_jobs.CRON_JOBS]


def test_services_noop_remove_keeps_created_job_owned(
        monkeypatch, tmp_path):
    """A create that lands and a remove that does not must both stay
    owned: problems > 0, and a later run that drops the new script from
    the desired set still removes it."""
    import json
    from types import SimpleNamespace
    stale = _stale_owned_cron()
    present = _desired_cron_entries()
    created = present.pop()
    calls, args = _services_env(
        monkeypatch, tmp_path, cron_entries=present + [stale])
    path, previous = _seed_manifest(tmp_path)
    previous["cron"].append(dict(stale))
    path.write_text(json.dumps(previous))
    inner = mcs_setup.subprocess.run
    hold = {"remove": True}
    removed: set[str] = set()
    entries = service_management.inspect_cron('/x/hermes')

    def listing(_hermes):
        # copy: the remove loop must see every pre-change job. Mutating
        # the iterated list (the helper's slice-assign) skips the next one.
        return [dict(e) for e in entries if e["id"] not in removed]

    def fake_run(argv, **kw):
        if argv[1:3] == ["cron", "remove"]:
            calls.append(list(argv))
            if not hold["remove"]:
                removed.add(argv[3])
            return SimpleNamespace(returncode=0,
                                   stdout="Success: job removed.",
                                   stderr="")
        return inner(argv, **kw)
    monkeypatch.setattr(service_management, 'inspect_cron', listing)
    monkeypatch.setattr(mcs_setup.subprocess, "run", fake_run)
    assert mcs_setup.cmd_services(args) == 1
    scripts = [c.get("script") for c in
               json.loads(path.read_text())["cron"]]
    assert created["script"] in scripts
    assert stale["script"] in scripts
    new_id = next(e["id"] for e in service_management.inspect_cron('/x/hermes')
                  if e.get("script") == created["script"])
    hold["remove"] = False
    calls.clear()
    monkeypatch.setattr(
        scheduled_jobs, "CRON_JOBS",
        [job for job in scheduled_jobs.CRON_JOBS
         if job[2] != created["script"]])
    assert mcs_setup.cmd_services(args) == 0
    assert ["/x/hermes", "cron", "remove", new_id] in calls
    assert all(e.get("script") != created["script"]
               for e in service_management.inspect_cron('/x/hermes'))
    retired = [c.get("script") for c in
               json.loads(path.read_text())["cron"]]
    assert created["script"] not in retired


@pytest.mark.parametrize("failure", ["edit", "agent"])
def test_services_failed_run_keeps_created_job_owned(
        monkeypatch, tmp_path, failure):
    """A create confirmed in `after` is owned even when another step
    (desired-job edit, or a later stage) fails: problems > 0, a second
    failing run keeps ownership, and retiring the job removes it."""
    import json
    from types import SimpleNamespace
    entries = _desired_cron_entries()
    created = entries.pop()
    if failure == "edit":
        entries[0]["schedule"] = "1 1 * * *"
    calls, args = _services_env(monkeypatch, tmp_path, cron_entries=entries)
    path, _ = _seed_manifest(tmp_path)
    fail = {"on": True}
    inner = mcs_setup.subprocess.run

    def fake_run(argv, **kw):
        if fail["on"] and argv[1:3] == ["cron", "edit"]:
            calls.append(list(argv))
            return SimpleNamespace(returncode=1, stdout="",
                                   stderr="synthetic edit failed")
        return inner(argv, **kw)
    monkeypatch.setattr(mcs_setup.subprocess, "run", fake_run)
    if failure == "agent":
        monkeypatch.setattr(service_management, 'agent_reconcile',
                            lambda *a, **_service_kwargs: not fail["on"])
    assert mcs_setup.cmd_services(args) == 1
    assert len(_cron_creates(calls)) == 1
    first = json.loads(path.read_text())
    assert created["script"] in [c.get("script") for c in first["cron"]]
    # a second failing run creates nothing and keeps ownership stable
    calls.clear()
    assert mcs_setup.cmd_services(args) == 1
    assert not _cron_creates(calls)
    assert json.loads(path.read_text()) == first
    new_id = next(e["id"] for e in service_management.inspect_cron('/x/hermes')
                  if e["script"] == created["script"])
    fail["on"] = False
    calls.clear()
    monkeypatch.setattr(
        scheduled_jobs, "CRON_JOBS",
        [job for job in scheduled_jobs.CRON_JOBS if job[2] != created["script"]])
    assert mcs_setup.cmd_services(args) == 0
    assert ["/x/hermes", "cron", "remove", new_id] in calls
    assert all(e["script"] != created["script"]
               for e in service_management.inspect_cron('/x/hermes'))
    assert created["script"] not in [
        c.get("script") for c in json.loads(path.read_text())["cron"]]


def test_services_unparseable_script_identity_is_a_problem(
        monkeypatch, tmp_path):
    """A job whose name matches but whose Script field is missing is
    unverifiable: no duplicate create, no 'exists' manifest record."""
    entries = [{"id": f"{i:06d}", "name": n, "schedule": s, "script": sc}
               for i, (n, s, sc) in enumerate(scheduled_jobs.CRON_JOBS)]
    del entries[0]["script"]
    calls, args = _services_env(monkeypatch, tmp_path,
                                cron_entries=entries)
    assert mcs_setup.cmd_services(args) == 1
    assert not _cron_creates(calls)
    assert not (tmp_path / "data" / "service_manifest.json").exists()


def test_services_post_verify_tolerates_undecodable_schedule(
        monkeypatch, tmp_path):
    """Post-change verification uses the pre-check's tolerance: a job
    whose displayed schedule does not parse is not drift, so a create
    elsewhere still converges and the manifest is written."""
    entries = [{"id": f"{i:06d}", "name": n, "schedule": s, "script": sc}
               for i, (n, s, sc) in enumerate(scheduled_jobs.CRON_JOBS)]
    entries[0]["schedule"] = "every 5m"
    del entries[-1]                                  # forces one create
    calls, args = _services_env(monkeypatch, tmp_path,
                                cron_entries=entries)
    assert mcs_setup.cmd_services(args) == 0
    assert len(_cron_creates(calls)) == 1
    assert (tmp_path / "data" / "service_manifest.json").exists()


def test_services_installs_gateway_when_interactive(
        monkeypatch, tmp_path):
    """interactive=discord + unsupervised gateway -> install + start
    through the public `hermes gateway` CLI."""
    calls, args = _services_env(
        monkeypatch, tmp_path,
        loaded=set(service_contract.AGENT_LABELS),
        cron_names={s for _, _, s in scheduled_jobs.CRON_JOBS})
    _previous_service_load_config_1434 = mcs_util.load_config
    monkeypatch.setattr(
        mcs_util, 'load_config',
        lambda path=None: _previous_service_load_config_1434(path) if path else
        {"notify": {"interactive": "discord"}})
    calls.clear()
    # fake_run returns empty stdout -> "supervised" absent -> install
    assert mcs_setup.cmd_services(args) == 0
    gw = [a for a in calls if "gateway" in a]
    assert ["/x/hermes", "gateway", "install"] in gw
    assert ["/x/hermes", "gateway", "start"] in gw


def test_services_gateway_supervised_is_noop(monkeypatch, tmp_path):
    from types import SimpleNamespace
    calls, args = _services_env(
        monkeypatch, tmp_path,
        loaded=set(service_contract.AGENT_LABELS),
        cron_names={s for _, _, s in scheduled_jobs.CRON_JOBS})
    _previous_service_load_config_1452 = mcs_util.load_config
    monkeypatch.setattr(
        mcs_util, 'load_config',
        lambda path=None: _previous_service_load_config_1452(path) if path else
        {"notify": {"interactive": "discord"}})

    def fake_run(argv, **kw):
        calls.append(list(argv))
        return SimpleNamespace(
            returncode=0,
            stdout="✓ Gateway is supervised by launchd (PID 1)",
            stderr="")
    monkeypatch.setattr(mcs_setup.subprocess, "run", fake_run)
    calls.clear()
    assert mcs_setup.cmd_services(args) == 0
    gw = [a for a in calls if "gateway" in a]
    assert gw == [["/x/hermes", "gateway", "status"]]


def test_services_gateway_skipped_when_off(monkeypatch, tmp_path):
    calls, args = _services_env(
        monkeypatch, tmp_path,
        loaded=set(service_contract.AGENT_LABELS),
        cron_names={s for _, _, s in scheduled_jobs.CRON_JOBS})
    calls.clear()
    assert mcs_setup.cmd_services(args) == 0
    assert not any("gateway" in a for a in calls)


# ---- plugin integration (hermes config CLI, never hermes internals) --

def test_standalone_services_owns_one_host_and_no_hermes_jobs(monkeypatch, tmp_path):
    from mcs_standalone import service

    calls, args = _services_env(monkeypatch, tmp_path)
    requests = []
    monkeypatch.setattr(service, "request_restart", lambda root: requests.append(root))
    _previous_service_load_config_1488 = mcs_util.load_config
    monkeypatch.setattr(mcs_util, 'load_config', lambda path=None: _previous_service_load_config_1488(path) if path else {"runtime_mode": "standalone"})
    monkeypatch.setattr(service_management, 'standalone_py_problem', lambda cfg, **_service_kwargs: None)
    monkeypatch.setattr(mcs_util, 'hermes_exe', lambda cfg: pytest.fail("Hermes must not be resolved"))
    assert mcs_setup.cmd_services(args) == 0
    manifest = mcs_util.load_config(mcs_setup.MANIFEST_PATH)
    assert manifest["runtime_mode"] == "standalone"
    assert manifest["python"] == str(tmp_path / "venv/bin/python3")
    assert manifest["cron"] == [] and manifest["scheduler"] == "standalone"
    assert [row["label"] for row in manifest["agents"]] == ["ai.mcs.standalone", "local.mcs-health"]
    assert not any("cron" in call or "gateway" in call for call in calls)
    calls.clear()
    assert mcs_setup.cmd_services(args) == 0
    assert not any("bootstrap" in call for call in calls)
    assert requests == [str(tmp_path)]


def test_standalone_loaded_host_without_heartbeat_starts_without_kill(monkeypatch, tmp_path):
    from mcs_standalone import service

    calls, args = _services_env(monkeypatch, tmp_path, loaded={service_contract.STANDALONE_LABEL})
    _previous_service_load_config_1508 = mcs_util.load_config
    monkeypatch.setattr(mcs_util, 'load_config', lambda path=None: _previous_service_load_config_1508(path) if path else {"runtime_mode": "standalone"})
    monkeypatch.setattr(service_management, 'standalone_py_problem', lambda cfg, **_service_kwargs: None)
    attempts = iter([False, True])

    def request(root):
        if not next(attempts):
            raise ValueError("standalone_status_unavailable")

    monkeypatch.setattr(service, "request_restart", request)
    assert mcs_setup.cmd_services(args) == 0
    assert ["launchctl", "kickstart", f"gui/{mcs_setup.os.getuid()}/ai.mcs.standalone"] in calls
    assert not any("-k" in call or "bootout" in call for call in calls)


def test_standalone_mode_switch_stops_only_previous_owned_jobs(monkeypatch, tmp_path):
    calls, args = _services_env(monkeypatch, tmp_path, loaded={"local.mcs-int"},
                               cron_entries=[{"id": "aaaaaa", "name": "old", "schedule": "*/5 * * * *", "script": "mcs_check.sh"},
                                             {"id": "bbbbbb", "name": "other", "schedule": "0 0 * * *", "script": "other.sh"}])
    service_management.save_manifest({'agents': [{'label': 'local.mcs-int'}], 'cron': [{'script': 'mcs_check.sh'}]}, paths=mcs_setup._service_paths())
    _previous_service_load_config_1527 = mcs_util.load_config
    monkeypatch.setattr(mcs_util, 'load_config', lambda path=None: _previous_service_load_config_1527(path) if path else {"runtime_mode": "standalone"})
    monkeypatch.setattr(service_management, 'standalone_py_problem', lambda cfg, **_service_kwargs: None)
    assert mcs_setup.cmd_services(args) == 0
    assert ["/x/hermes", "cron", "remove", "aaaaaa"] in calls
    assert not any("bbbbbb" in call or "ai.hermes.gateway" in call for call in calls)
    assert ["launchctl", "bootout", f"gui/{mcs_setup.os.getuid()}/local.mcs-int"] in calls


@pytest.mark.parametrize("outcomes,late,ok,boots,loads", [
    ([5, 0], False, True, 2, True),
    ([5, 5, 5], True, True, 3, True),
    ([5, 5, 5], False, False, 3, True),
    # exit 0 is not proof: the label must answer `print` afterwards
    ([0], False, False, 1, False),
])
def test_agent_reconcile_retries_transient_bootstrap(
        monkeypatch, outcomes, late, ok, boots, loads):
    import subprocess
    from types import SimpleNamespace
    state = {"loaded": True, "calls": [], "outcomes": list(outcomes)}

    def fake_run(argv, **kwargs):
        verb = argv[1]
        state["calls"].append(verb)
        rc = 0
        if verb == "bootout":
            state["loaded"] = False
        elif verb == "bootstrap":
            rc = state["outcomes"].pop(0)
            if (rc == 0 and loads) or (late and not state["outcomes"]):
                state["loaded"] = True
        elif verb == "print":
            rc = 0 if state["loaded"] else 113
        return subprocess.CompletedProcess(
            argv, rc, "success", "" if rc == 0 else "5: Input/output error")

    sleeps, notes = [], []
    monkeypatch.setattr(lifecycle_io, 'run_bounded', fake_run)
    monkeypatch.setattr(service_management, "time",
                        SimpleNamespace(time=service_management.time.time,
                                        sleep=sleeps.append))
    assert service_management.agent_reconcile('ai.mcs.x', '/p.plist', notes.append, False, paths=mcs_setup._service_paths()) is ok
    assert state["calls"][:2] == ["print", "bootout"]
    assert state["calls"].count("bootstrap") == boots
    assert state["calls"][-1] == "print"
    assert sleeps == [1] * sum(rc != 0 for rc in outcomes)
    assert any("起動に失敗しました" in n for n in notes) is (not ok)


def test_services_refuses_missing_interpreter(monkeypatch, tmp_path,
                                              capsys):
    """Rendering wrappers/plists around a missing interpreter makes every
    job fail at every run — services must stop before writing anything."""
    probe = service_management.hermes_py_problem
    calls, args = _services_env(monkeypatch, tmp_path)
    monkeypatch.setattr(service_management, 'hermes_py_problem', probe)
    monkeypatch.setattr(mcs_setup.os.path, "isfile",
                        lambda p: p != mcs_setup.HERMES_PY)
    assert mcs_setup.cmd_services(args) == 1
    assert calls == []
    assert not (tmp_path / "scripts").exists()
    assert "設定ファイルは作成していません" in capsys.readouterr().out


@pytest.mark.parametrize("verb", ["install", "uninstall", "restart", "start", "stop"])
def test_gateway_lifecycle_refused_in_test_sandbox(monkeypatch, tmp_path, verb):
    """Under MCS_TEST_SANDBOX a gateway lifecycle call never runs a real hermes executable: it
    writes the LaunchAgent under the real account home whatever HOME/HERMES_HOME a test set."""
    monkeypatch.setenv("MCS_TEST_SANDBOX", "1")
    with tempfile.TemporaryDirectory(prefix='mcs-synthetic-gateway-') as directory:
        marker = Path(directory) / "spawned"
        exe = Path(directory) / "hermes"
        exe.write_text(f"#!/bin/sh\ntouch {marker}\n")
        exe.chmod(0o755)
        r = service_management.hermes_cli(str(exe), '', 'gateway', verb)
        assert r is not None and r.returncode == 126 and "MCS_TEST_SANDBOX" in r.stderr
        assert not marker.exists()
        # read-only verbs still run
        assert service_management.hermes_cli(str(exe), '', 'gateway', 'status').returncode == 0
        assert marker.exists()


def test_changed_standalone_plist_is_reloaded_into_launchd(monkeypatch, tmp_path):
    from mcs_standalone import service

    monkeypatch.delenv("XPC_SERVICE_NAME", raising=False)
    calls, args = _services_env(monkeypatch, tmp_path, loaded={service_contract.STANDALONE_LABEL})
    _previous_service_load_config_2348 = mcs_util.load_config
    monkeypatch.setattr(mcs_util, 'load_config', lambda path=None: _previous_service_load_config_2348(path) if path else {"runtime_mode": "standalone"})
    monkeypatch.setattr(service_management, 'standalone_py_problem', lambda cfg, **_service_kwargs: None)
    requests = []
    monkeypatch.setattr(service, "request_restart", lambda root: requests.append(root))
    stale = service_management.standalone_service_path(paths=mcs_setup._service_paths())
    (tmp_path / "agents").mkdir(exist_ok=True)
    with open(stale, "w") as f:
        f.write("<plist>old checkout</plist>")
    assert mcs_setup.cmd_services(args) == 0
    assert ["launchctl", "bootout", f"gui/{mcs_setup.os.getuid()}/ai.mcs.standalone"] in calls
    assert any(c[:2] == ["launchctl", "bootstrap"] for c in calls)
    assert requests == []
