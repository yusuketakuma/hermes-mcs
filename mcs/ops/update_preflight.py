"""Inspect runtime and backup readiness before approved update effects."""
from __future__ import annotations
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from contextlib import suppress
import mcs_runtime  # noqa: E402
import runtime_inspection  # noqa: E402
import service_management  # noqa: E402
import mcs_util  # noqa: E402
import mcs_config  # noqa: E402
import install_options  # noqa: E402
import update_journal as journal_store  # noqa: E402
import update_discovery  # noqa: E402
import restore_evidence  # noqa: E402
import environment_diagnostics  # noqa: E402
import notify_flush  # noqa: E402,F401 - restore text quarantine
import notify_digest  # noqa: E402,F401 - restored digest card reissue
import notify_urgent  # noqa: E402,F401 - restored alert settlement
import alert_view  # noqa: E402,F401 - restored alert card reissue
import extraction_progress  # noqa: E402,F401 - restored card presentation
import present_discord  # noqa: E402,F401 - restored card presentation
import present_slack  # noqa: E402,F401 - restored card presentation
import present_lineworks  # noqa: E402,F401 - restored card presentation
import local_llm  # noqa: E402,F401 - postcheck model/admission diagnostics
import llm_admission  # noqa: E402,F401 - postcheck admission diagnostics
import mcs_restore  # noqa: E402,F401 - existing backup-adoption marker gate
import provider_settings  # noqa: E402 - rollback settings checks after tree reset
import cloud_policy  # noqa: E402 - rollback provider model ownership
import cloud_inference  # noqa: E402,F401 - rollback cloud-contract validation
import semantic_jev  # noqa: E402 - rollback Jev model validation
from mcs_util import HOME, REPO

from update_lifecycle_context import (
    AGENTS_DIR,
    DATA,
    HERMES_PIN,
    HERMES_PREVIOUS_PINS,
    KEEP_CHECKOUT_ENV,
    LEDGER,
    PROTECTED,
    ProviderRollbackUnverified,
    RUNTIME_MIN,
    STATE_PATH,
    UpdateError,
    _PROVIDER_SETTINGS_ONLY,
    _bounded_timeout,
    _release_git_environment,
    _run_with_lifetime,
    _service_paths,
)
from update_repository import (
    _checkout_git,
    _git,
    _git_out,
    _index_has_flags,
    _ls_tree_paths,
    _norm,
    _safe_relpath,
    _split_target_source_matches,
    _tree_clean,
)

def _freeze_schema_rollback(state: dict, entry: dict) -> bool:
    """Persist a recovery hold before changing a schema rollback's tree."""
    if not (entry.get("schema_bump") and entry.get("backup_path")):
        return False
    applying = state.get("applying")
    if not isinstance(applying, dict) or not applying.get("rollback"):
        raise UpdateError("rollback_journal_unverifiable")
    applying["restore_incomplete"] = True
    journal_store.save_state(STATE_PATH, state)
    return True


def _fresh_rollback_state(expected: dict) -> tuple[dict, dict]:
    """Reload the same rollback after a restore callback changed its journal."""
    state = journal_store.load_state(STATE_PATH)
    current = state.get("applying")
    keys = ("sha", "prev_sha", "backup_path", "rollback", "schema_bump", "tag", "command_id",
            "manifest_snapshot", "runtime_migration", "target_kind", "release_ref")
    try:
        identity = lambda record: json.dumps(  # noqa: E731
            [[key in record, record.get(key)] for key in keys],
            sort_keys=True, allow_nan=False, separators=(",", ":"))
        same = isinstance(current, dict) and identity(current) == identity(expected)
    except (TypeError, ValueError, RecursionError):
        same = False
    if (state.get("_corrupt") or not same
            or current.get("restore_incomplete") is not True):
        raise UpdateError("rollback_journal_changed")
    return state, current


def precheck_local(cfg: dict, *, deadline=None) -> list[str]:
    """Local-only gates (no tag objects needed yet)."""
    errors = []
    if deadline is not None:
        _bounded_timeout(None, deadline)
    if not _tree_clean(**({} if deadline is None else {"deadline": deadline})):
        errors.append("tree_dirty")
    try:
        errs, _ = mcs_config.validate_config(cfg)
        errors.extend("config: " + e for e in errs)
    except Exception as e:
        errors.append(f"validate_config_failed: {type(e).__name__}")
    with suppress(OSError):
        import maintenance
        # Preserve the updater's reserve as well as the backup's own guard.
        free = shutil.disk_usage(DATA).free
        legacy_need = os.path.getsize(LEDGER) * 2 + 64 * 1024 * 1024
        backup_need_mb = maintenance._backup_need_mb(LEDGER)
        if free < legacy_need or free / (1024 * 1024) < backup_need_mb:
            errors.append("insufficient_disk")
    # an owned idle watchdog whose explicit selection drifted is repaired by
    # the post-merge `services`; anything else still blocks before merge
    errors.extend(service_management.runtime_gate_errors(cfg, repair_recovery=deadline is None, update_repo=REPO, allow_runtime_migration=deadline is None, paths=_service_paths(), **({} if deadline is None else {"deadline": deadline})))
    if cfg.get("runtime_mode") == "standalone":
        if runtime_inspection.python_problem(mcs_runtime.python_executable(cfg, root=HOME), **({} if deadline is None else {"deadline": deadline})):
            errors.append("standalone_interpreter_unavailable")
    else:
        hermes = mcs_util.hermes_exe(cfg)
        if not runtime_inspection.executable_available(hermes):
            errors.append("hermes_not_resolvable")
    problem = _migration_checkout_problem(cfg, **({} if deadline is None else {"deadline": deadline}))
    if problem:
        errors.append("runtime_migration: " + problem)
    return errors


def _migration_checkout_problem(cfg: dict, *, deadline=None) -> str | None:
    """Refuse before merge what install.sh would refuse mid-migration."""
    try:
        venv, checkout = _runtime_venv(cfg)
    except (ValueError, KeyError, OSError):
        return None          # the runtime gate already reports a bad selection
    if checkout is None:
        return None
    try:
        head = _checkout_git(checkout, "rev-parse", "HEAD", **({} if deadline is None else {"deadline": deadline}))
        index = _checkout_git(checkout, "ls-files", "-v", "-z", **({} if deadline is None else {"deadline": deadline}))
    except (OSError, subprocess.TimeoutExpired):
        return "hermes_checkout_unreadable"
    if head.returncode != 0 or not update_discovery.COMMIT_SHA_RE.fullmatch(head.stdout.strip()) or index.returncode != 0:
        return "hermes_checkout_unreadable"
    if _index_has_flags(index.stdout):
        return "hermes_checkout_branch_or_dirty"
    keep = os.environ.get(KEEP_CHECKOUT_ENV)
    if not keep and not _needs_migration(venv, **({} if deadline is None else {"deadline": deadline})) and not _refresh_runtime_pin(venv, head.stdout.strip(), **({} if deadline is None else {"deadline": deadline})):
        return None
    try:
        branch = _checkout_git(checkout, "symbolic-ref", "-q", "HEAD", **({} if deadline is None else {"deadline": deadline}))
        dirty = _checkout_git(checkout, "status", "--porcelain", "--untracked-files=no", **({} if deadline is None else {"deadline": deadline}))
    except (OSError, subprocess.TimeoutExpired):
        return "hermes_checkout_unreadable"
    if dirty.returncode != 0 or branch.returncode not in (0, 1):
        return "hermes_checkout_unreadable"
    if dirty.stdout.strip():
        return "hermes_checkout_branch_or_dirty"
    if keep:
        # the operator confirmed this exact checkout: kept as is, never moved
        # (decided before the pin rules, exactly as install.sh does)
        if not update_discovery.COMMIT_SHA_RE.fullmatch(keep):
            return "keep_hermes_checkout_invalid"
        if head.stdout.strip() != keep:
            return "keep_hermes_checkout_mismatch"
        if not _checkout_admits_python314(checkout, **({} if deadline is None else {"deadline": deadline})):
            return "keep_hermes_checkout_python_unsupported"
        return None
    if head.stdout.strip() in HERMES_PREVIOUS_PINS | {HERMES_PIN}:
        # a pin is moved/used detached; a branch there is someone's work
        return "hermes_checkout_branch_or_dirty" if branch.returncode == 0 else None
    return "hermes_checkout_unknown_commit"


def _python314(*, deadline=None) -> str | None:
    """The interpreter install.sh's find_py would pick (3.14 <= v < 3.15)."""
    for name in ("python3.14", "python3"):
        exe = shutil.which(name)
        if not exe:
            continue
        try:
            ok = _run_with_lifetime([exe, "-I", "-c", "import sys; sys.exit(not (3, 14) <= "
                                 "sys.version_info[:2] < (3, 15))"],
                                capture_output=True, timeout=30, **({} if deadline is None else {"deadline": deadline})).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            continue
        if ok:
            return exe
    return None


def _checkout_admits_python314(checkout: str, *, deadline=None) -> bool:
    """[project].requires-python checked by that interpreter, same script as install.sh."""
    python = _python314(**({} if deadline is None else {"deadline": deadline}))
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "requires_python_admits.py")
    if python is None:
        return False
    try:
        return _run_with_lifetime([python, "-I", script, os.path.join(checkout, "pyproject.toml")],
                              capture_output=True, timeout=30, **({} if deadline is None else {"deadline": deadline})).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _provider_migration_needed() -> bool:
    owners = {model: name for name, model in cloud_policy.PROVIDERS.items()}
    first = provider_settings.read(HOME, model_owners=owners, jev_model=semantic_jev.JEV_MODEL)
    second = provider_settings.read(HOME, model_owners=owners, jev_model=semantic_jev.JEV_MODEL)
    if (first["mode"] not in ("legacy_unverified", "split")
            or second["mode"] != first["mode"] or second["snapshot"] != first["snapshot"]):
        raise ValueError("provider_settings_unverified")
    return first["mode"] == "legacy_unverified"


def _provider_rollback_preflight(target: str | object) -> bool | None:
    """Block unproven split-setting rollback before changing code or workers."""
    try:
        state = provider_settings.probe(HOME)
        if state["state"] in ("root_missing", "uninitialized_or_legacy_unverified", "legacy_unverified"):
            return
        if state["state"] != "requires_checked_read":
            raise ProviderRollbackUnverified("provider_rollback_settings_unverified")
        owners = {model: name for name, model in cloud_policy.PROVIDERS.items()}
        first = provider_settings.read(HOME, model_owners=owners, jev_model=semantic_jev.JEV_MODEL)
        if (first["mode"] != "split" or target is not _PROVIDER_SETTINGS_ONLY
                and not _split_target_source_matches(target, allow_reviewed_rollback=True)):
            raise ProviderRollbackUnverified("provider_rollback_target_unverified")
        second = provider_settings.read(HOME, model_owners=owners, jev_model=semantic_jev.JEV_MODEL)
        if second["mode"] != "split" or second["snapshot"] != first["snapshot"]:
            raise ProviderRollbackUnverified("provider_rollback_settings_changed")
        return True
    except ProviderRollbackUnverified:
        raise
    except UpdateError as error:
        raise ProviderRollbackUnverified("provider_rollback_target_unverified") from error
    except (OSError, ValueError, TypeError, KeyError, RecursionError, ImportError) as error:
        raise ProviderRollbackUnverified("provider_rollback_settings_unverified") from error


def _precheck_target_tree(tag: str, standalone: bool, *, deadline=None, installed_schema=None, expected_target_schema=None) -> tuple[list[str], list]:
    """Inspect fixed target objects without importing any candidate code."""
    errors: list[str] = []
    if standalone and not _standalone_target_supported(tag, **({} if deadline is None else {"deadline": deadline})):
        errors.append("standalone_runtime_missing_in_target")
    paths = []
    entries = _ls_tree_paths(tag, **({} if deadline is None else {"deadline": deadline}))
    for mode, otype, _sha, path in entries:
        if deadline is not None:
            _bounded_timeout(None, deadline)
        if mode not in ("100644", "100755", "040000") or \
                otype not in ("blob", "tree"):
            errors.append(f"bad_entry_type: {mode} {path}")
        if not _safe_relpath(path):
            errors.append(f"unsafe_path: {path!r}")
        paths.append(path)
    norm_protected = tuple(_norm(p) for p in PROTECTED)
    for path in paths:
        if deadline is not None:
            _bounded_timeout(None, deadline)
        np_ = _norm(path)
        if any(np_ == p or np_.startswith(p.rstrip("/") + "/")
               for p in norm_protected):
            errors.append(f"protected_path: {path}")
    # Rule-based ignored-path check (S16): the tag must not track
    # anything the repo's own ignore rules would cover.
    if paths:
        try:
            r = _run_with_lifetime(
                ["git", "-C", REPO, "check-ignore", "-z", "--stdin",
                 "--no-index"],
                input="\0".join(paths), capture_output=True, text=True,
                timeout=update_discovery.LOCAL_TIMEOUT_S, **({} if deadline is None else {"deadline": deadline, "env": _release_git_environment()}))
        except (OSError, subprocess.TimeoutExpired):
            errors.append("check_ignore_failed")
        else:
            if r.returncode == 0:
                errors.extend(f"ignored_path_tracked: {name}"
                              for name in r.stdout.split("\0") if name)
            elif r.returncode > 1:
                errors.append("check_ignore_failed")
    # untracked collision: merge would refuse, but name the files first
    untracked = set(_git_out(
        ["ls-files", "--others", "--exclude-standard", "-z"], **({} if deadline is None else {"deadline": deadline})).split("\0"))
    diff = set(_git_out(
        ["diff", "--name-only", "-z", "HEAD", tag], **({} if deadline is None else {"deadline": deadline})).split("\0"))
    clash = (untracked & diff) - {""}
    if clash:
        errors.append("untracked_collision: " + ",".join(sorted(clash)[:10]))
    # fast-forward feasibility + ancestry must be provable BEFORE the
    # quiesce window, not discovered inside it (L3)
    if _git(["merge-base", "--is-ancestor", "HEAD", tag]
            , **({} if deadline is None else {"deadline": deadline})).returncode != 0:
        errors.append("not_fast_forward: HEAD is not an ancestor of tag")
    # schema compat: candidate SCHEMA_VERSION vs live user_version
    try:
        src = _git_out(["show", f"{tag}:mcs/core/ledger.py"], **({} if deadline is None else {"deadline": deadline}))
        m = re.search(r"SCHEMA_VERSION\s*=\s*([0-9]+)", src)
        if not m:
            errors.append("schema_version_unparseable")
        else:
            new_ver = int(m.group(1))
            if expected_target_schema is not None and (type(expected_target_schema) is not int or new_ver != expected_target_schema):
                errors.append('release_schema_target_mismatch')
            cur_ver = restore_evidence.database_version(LEDGER) if deadline is None else (
                installed_schema if type(installed_schema) is int and installed_schema > 0 else None)
            if cur_ver is None:
                # unknown is not zero: never report a bump from 0
                errors.append("schema_version_unknown")
            elif new_ver < cur_ver:
                errors.append(f"schema_downgrade:{cur_ver}->{new_ver}")
            elif new_ver > cur_ver:
                errors.append(f"schema_bump:{cur_ver}->{new_ver}")
    except UpdateError:
        errors.append("schema_version_unreadable")
    return errors, entries


def _precheck_target_runtime_files(tag: str, standalone: bool, *, deadline=None) -> list[str]:
    """Retain the existing installer, lock and SDK change refusals."""
    errors = []
    if _git(["diff", "--quiet", "HEAD", tag, "--", "install.sh"]
            , **({} if deadline is None else {"deadline": deadline})).returncode != 0:
        errors.append("install_sh_changed")
    if _git(["diff", "--quiet", "HEAD", tag, "--",
             "deployment/sdc-release-lock.json", "deployment/sdc-wheels/"]
            , **({} if deadline is None else {"deadline": deadline})).returncode != 0:
        errors.append("release_dependency_lock_changed")
    # standalone SDK pins live in ~/.mcs/venv, which only install.sh updates
    if standalone and _git(
            ["diff", "--quiet", "HEAD", tag, "--",
             "deployment/requirements-standalone.txt"], **({} if deadline is None else {"deadline": deadline})).returncode != 0:
        errors.append("standalone_requirements_changed")
    if "--with-yomitoku" in (recorded_install_args() or []) and _git(
            ["diff", "--quiet", "HEAD", tag, "--",
             "deployment/requirements-ocr.txt", "scripts/setup_yomitoku.py",
             "mcs/extract/ocr_model_contract.py"],
            **({} if deadline is None else {"deadline": deadline})).returncode != 0:
        errors.append("ocr_requirements_changed")
    return errors


def precheck_tag(tag: str) -> list[str]:
    """Candidate-tree gates; legacy apply retains its existing candidate probe."""
    standalone = mcs_runtime.standalone(mcs_util.load_config())
    errors, entries = _precheck_target_tree(tag, standalone)
    # candidate preflight: exact blobs, not git archive (S3)
    tmp = tempfile.mkdtemp(prefix="mcs_preflight_")
    try:
        for mode, otype, bsha, path in entries:
            if otype != "blob" or not (path == "mcs" or path.startswith("mcs/")):
                continue
            if not _safe_relpath(path):
                raise UpdateError("unsafe_path: " + path)
            dst = os.path.join(tmp, path)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            blob = _run_with_lifetime(
                ["git", "-C", REPO, "cat-file", "blob", bsha],
                capture_output=True, timeout=update_discovery.LOCAL_TIMEOUT_S)
            if blob.returncode != 0:
                raise UpdateError("cat-file failed: " + path)
            with open(dst, "wb") as f:
                f.write(blob.stdout)
            if mode == "100755":
                os.chmod(dst, 0o755)
        probe = _run_with_lifetime(
            [sys.executable, "-c",
             "import sys, json, os\n"
             "sys.path.insert(0, sys.argv[1])\n"
             "import _mcs_path  # noqa\n"
             "import mcs_setup\n"
             "errs, _ = mcs_setup.validate_config(\n"
             "    json.load(open(os.environ['MCS_CONF'])))\n"
             "print(json.dumps(errs))",
             os.path.join(tmp, "mcs")],
            capture_output=True, text=True, timeout=60,
            env={**os.environ, "MCS_CONF": os.path.join(HOME,
                                                       "config.json")})
        if probe.returncode != 0:
            errors.append("preflight_failed: "
                          + (probe.stderr or "").strip()[:200])
        else:
            try:
                errors.extend("new_config: " + e
                              for e in json.loads(probe.stdout.strip() or "[]"))
            except json.JSONDecodeError:
                errors.append("preflight_unparseable")
    except (UpdateError, subprocess.TimeoutExpired, OSError) as e:
        errors.append(f"preflight_failed: {e}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    errors.extend(_precheck_target_runtime_files(tag, standalone))
    return errors


def _candidate_args_valid(candidate_sha, base_sha) -> bool:
    """Only complete commit identities may select an unpublished local target."""
    return all(isinstance(value, str) and update_discovery.COMMIT_SHA_RE.fullmatch(value)
               for value in (candidate_sha, base_sha))


def recorded_install_args() -> list[str] | None:
    """Return verified install opt-outs or None when they cannot be trusted."""
    return install_options.recorded(mcs_util.CONF_PATH, os.path.join(DATA, "install_options.json"))


def _install_options_snapshot():
    legacy = os.path.join(DATA, "install_options.json")
    base = install_options.inspect(mcs_util.CONF_PATH, legacy)
    current = install_options.selection(mcs_util.CONF_PATH, legacy)
    if install_options.inspect(mcs_util.CONF_PATH, legacy) != base:
        raise ValueError("install_options_changed")
    return base, current


def _standalone_target_supported(ref, *, deadline=None):
    return all(_git(["cat-file", "-e", f"{ref}:{name}"], **({} if deadline is None else {"deadline": deadline})).returncode == 0
               for name in ("mcs_standalone/__main__.py", "mcs/core/mcs_runtime.py"))


def _runtime_version(venv: str, *, deadline=None) -> tuple[str, tuple[int, int]] | None:
    """(interpreter, (major, minor)) reported by the venv, or None if unknown."""
    for name in ("python3", "python"):
        exe = os.path.join(venv, "bin", name)
        if os.access(exe, os.X_OK):
            try:
                out = _run_with_lifetime(
                    [exe, "-I", "-c", "import sys; print(*sys.version_info[:2])"],
                    capture_output=True, text=True, timeout=30, **({} if deadline is None else {"deadline": deadline}))
                major, minor = map(int, out.stdout.split())
            except (OSError, subprocess.TimeoutExpired, ValueError):
                return None
            return (exe, (major, minor)) if out.returncode == 0 else None
    return None


def _needs_migration(venv: str, *, deadline=None) -> bool:
    """Only a runtime that runs and reports an older Python is migrated."""
    found = _runtime_version(venv, **({} if deadline is None else {"deadline": deadline}))
    return found is not None and found[1] < RUNTIME_MIN


def _refresh_runtime_pin(venv: str, head: str | None, *, deadline=None) -> bool:
    """Whether a supported runtime needs dependencies from the new Hermes pin."""
    if head not in HERMES_PREVIOUS_PINS or head == HERMES_PIN or os.environ.get(KEEP_CHECKOUT_ENV):
        return False
    found = _runtime_version(venv, **({} if deadline is None else {"deadline": deadline}))
    return found is not None and RUNTIME_MIN <= found[1] < (3, 15)


def _runtime_python(venv: str) -> str | None:
    """The rebuilt venv's interpreter when it reports >= RUNTIME_MIN."""
    found = _runtime_version(venv)
    return found[0] if found and found[1] >= RUNTIME_MIN else None


def _runtime_venv(cfg: dict) -> tuple[str, str | None]:
    """(venv, Hermes checkout or None) of the interpreter services run."""
    venv = os.path.dirname(os.path.dirname(os.path.abspath(
        service_management.selected_python(cfg, paths=_service_paths()))))
    if cfg.get("runtime_mode") == "standalone":
        return venv, None
    checkout = os.path.dirname(venv)     # <hermes-agent>/venv
    # .git is a directory in a clone and a file in a linked worktree
    return venv, checkout if os.path.lexists(os.path.join(checkout, ".git")) else None


def _check_environment(cfg: dict, *, deadline=None):
    """environment_diagnostics.check_environment with the updater's paths."""
    return environment_diagnostics.check_environment(
        cfg, paths=_service_paths(), chrome_bin=mcs_util.CHROME_BIN,
        credential_paths=(os.path.join(mcs_util.HOME, ".env"),
                          os.path.expanduser("~/.hermes/.env")),
        agents_dir=AGENTS_DIR, **({} if deadline is None else {"deadline": deadline}))


def _baseline_check(cfg: dict, *, deadline=None) -> list[str]:
    if deadline is not None:
        _bounded_timeout(None, deadline)
    try:
        errs, _ = _check_environment(cfg, **({} if deadline is None else {"deadline": deadline}))
        if deadline is not None:
            _bounded_timeout(None, deadline)
        return errs
    except Exception:
        if deadline is not None:
            _bounded_timeout(None, deadline)
        return []
