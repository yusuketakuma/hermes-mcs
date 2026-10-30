"""Inspect and launch deployed update wrappers after the caller commits approval."""
import os
import subprocess
import sys

import mcs_runtime

HERMES_WRAPPER = os.path.expanduser("~/.hermes/scripts/mcs_update.sh")
STRIPPED_ENV = (
    "_HERMES_GATEWAY", "_HERMES_GATEWAY_BREAKAWAY",
    "HERMES_SUPERVISED_CHILD", "HERMES_S6_SUPERVISED_CHILD",
    "HERMES_GATEWAY", "HERMES_GATEWAY_MODE", "HERMES_GATEWAY_DETACHED",
    "HERMES_CRON_JOB", "HERMES_JOB_ID", "HERMES_QUIET", "TERMINAL_CWD")


def launch_environment(environment, *, detached: bool) -> dict:
    """Copy a borrowed environment, dropping only the update launch identities."""
    cleaned = {key: value for key, value in environment.items() if key not in STRIPPED_ENV}
    return without_job_identity(cleaned) if detached else cleaned



def without_job_identity(environment) -> dict:
    """Copy a borrowed environment without its current launchd job identity."""
    return {key: value for key, value in environment.items() if key not in ("XPC_SERVICE_NAME", "MCS_JOB_PID")}


def wrapper_path(cfg: dict, *, root, hermes_wrapper: str) -> str:
    """Select the existing deployed wrapper for the configured runtime."""
    return os.path.join(mcs_runtime.scripts_dir(cfg, root=root), "mcs_update.sh") \
        if mcs_runtime.standalone(cfg) else hermes_wrapper


def inspect_launch(*, root, hermes_wrapper: str) -> tuple[bool, dict]:
    """Read short local readiness facts at the caller's receipt boundary.

    Keep the wrapper and mode observations separate: wrapper selection errors
    retain the configured Hermes fallback, while an unreadable mode is off.
    No Git, process launch, receipt or journal operation occurs here.
    """
    from mcs_util import load_config
    try:
        wrapper = wrapper_path(load_config(), root=root, hermes_wrapper=hermes_wrapper)
    except Exception:
        wrapper = hermes_wrapper
    if not os.path.isfile(wrapper):
        return False, {}
    update = {}
    try:
        cfg = load_config()
        if isinstance(cfg.get("update"), dict):
            update = cfg["update"]
    except Exception:
        pass
    return True, update


def spawn_detached(*, root, data_dir, hermes_wrapper: str, environment, recovery_dir=None) -> None:
    """Launch only the wrapper, after the caller commits the approval receipt.

    No approval is carried in argv: the updater re-scans committed receipts.
    A verified independent helper drains output after this parent returns.
    """
    from mcs_util import load_config
    env = launch_environment(environment, detached=True)
    os.makedirs(data_dir, exist_ok=True)
    wrapper = wrapper_path(load_config(), root=root, hermes_wrapper=hermes_wrapper)
    from run_log_install import helper_path
    helper = helper_path(root, recovery_dir, require_installed=True)
    subprocess.Popen([sys.executable, "-I", helper, "exec", "--data-dir", os.fspath(data_dir),
                      "--", wrapper], stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env,
                     close_fds=True, start_new_session=True)
