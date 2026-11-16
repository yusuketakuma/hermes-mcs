"""Repo-root pytest guard.

`integration/test_hermes_*` requires a hermes-agent checkout on sys.path
(it imports `gateway.*`). On a plain clone of this repo there is no
hermes — skip those files instead of failing at import. Inside a hermes
checkout they are collected and run normally
(scripts/run_tests.sh <abs path>).

The skip is FILE-scoped, not directory-scoped: pure-synthetic
narratives (mcs + hermes_plugin + fake clients only — e.g.
`test_mcs_recovery_narrative.py`) run without the hermes checkout.
New gateway-bound tests MUST keep the `test_hermes_*` prefix.
"""
import errno
import faulthandler
import importlib.util
import json
import os

import pytest


@pytest.fixture
def snapshot_child_clock():
    """Pass a test's current mocked clock into each synthetic snapshot child."""
    import snapshots

    prepare = snapshots._prepare_in_child

    def with_clock(*args, **kwargs):
        code = snapshots._PREPARE_CODE
        stamp = snapshots.time.time()
        snapshots._PREPARE_CODE = f"import time\ntime.time = lambda: {stamp!r}\n" + code
        try:
            return prepare(*args, **kwargs)
        finally:
            snapshots._PREPARE_CODE = code

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(snapshots, "_prepare_in_child", with_clock)
        yield


# Per-test hang guard (stdlib only): a test whose setup, call and teardown
# together exceed the limit dumps every thread's traceback and exits the
# run, so a wedged test cannot hold a CI runner until the job timeout.
# MCS_TEST_TIMEOUT overrides the limit in seconds; 0 disables the guard.
# The dump goes to a copy of the real stderr taken before output capture
# redirects fd 2, otherwise it would vanish with the exiting process. The
# functions are captured at import so a test that monkeypatches
# faulthandler (tests/ingest/test_run_watchdog.py) cannot disarm the guard
# for later tests. A test that arms its own in-process faulthandler timer
# replaces the guard for the rest of that test only.
_dump_later = faulthandler.dump_traceback_later
_cancel_dump = faulthandler.cancel_dump_traceback_later
_hang_stderr = None


def _test_timeout():
    try:
        return max(0.0, float(os.environ.get("MCS_TEST_TIMEOUT", "300")))
    except ValueError:
        return 300.0


# CI sharding (ci/run_pytest_groups.py): --mcs-shard=I/N keeps the I-th of N
# deterministic parts of the collected tests and deselects the rest, so N
# processes together run every test exactly once. Without the option nothing
# changes. ci/pytest_shards.py is loaded by path only when the option is used,
# because nested runs copy this file alone into a temporary directory.
_shard = None


def pytest_addoption(parser):
    group = parser.getgroup("mcs-shard", "CI shards (ci/run_pytest_groups.py)")
    group.addoption("--mcs-shard", default=None, metavar="I/N",
                    help="run only the I-th of N deterministic test-file shards")
    group.addoption("--mcs-shard-durations", default=None, metavar="PATH",
                    help="per-file seconds used to balance --mcs-shard")
    group.addoption("--mcs-shard-report", default=None, metavar="PATH",
                    help="write the shard's selection and completion report here")


def _shards():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ci", "pytest_shards.py")
    spec = importlib.util.spec_from_file_location("_mcs_pytest_shards", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_shard_report(state):
    path = state["path"]
    if path:
        temporary = path + ".tmp"
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(state["report"], handle, sort_keys=True)
        os.replace(temporary, path)


def _configure_shard(config):
    global _shard
    spec = config.getoption("--mcs-shard")
    if spec is None:
        if config.getoption("--mcs-shard-durations") or config.getoption("--mcs-shard-report"):
            raise pytest.UsageError("--mcs-shard-durations/--mcs-shard-report need --mcs-shard")
        return
    shards = _shards()
    try:
        index, count = shards.parse_spec(spec)
    except ValueError as error:
        raise pytest.UsageError(str(error)) from None
    _shard = {"module": shards, "index": index, "count": count,
              "durations": config.getoption("--mcs-shard-durations"),
              "path": config.getoption("--mcs-shard-report"),
              "collect_not_passed": 0, "ran": set(), "report": None}


def pytest_collectreport(report):
    if _shard is not None and not report.passed:
        _shard["collect_not_passed"] += 1


# trylast: -k/-m/--deselect run first, so the shards split what would run.
@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config, items):
    if _shard is None:
        return
    shards = _shard["module"]
    durations, split = shards.load_durations(_shard["durations"]) if _shard["durations"] else ({}, set())
    nodeids = [item.nodeid for item in items]
    assignment, info = shards.plan(nodeids, _shard["count"], durations, split)
    mine = _shard["index"] - 1
    keep = [item for item in items if assignment[item.nodeid] == mine]
    drop = [item for item in items if assignment[item.nodeid] != mine]
    if drop:
        config.hook.pytest_deselected(items=drop)
    items[:] = keep
    _shard["selected"] = {item.nodeid for item in keep}
    _shard["report"] = shards.report(nodeids, _shard["index"], _shard["count"], assignment, info,
                                     _shard["collect_not_passed"])
    _write_shard_report(_shard)


def pytest_runtest_logfinish(nodeid, location):
    if _shard is not None:
        _shard["ran"].add(nodeid)


def pytest_sessionfinish(session, exitstatus):
    if _shard is not None and _shard["report"] is not None:
        _shard["report"].update(finished=True, exitstatus=int(exitstatus),
                                ran=len(_shard["ran"] & _shard["selected"]),
                                collect_not_passed=_shard["collect_not_passed"])
        _write_shard_report(_shard)


def pytest_sessionstart(session):
    # Kept out of pytest_configure: that hook is the hang guard's and is tested in isolation.
    _configure_shard(session.config)


def pytest_configure(config):
    global _hang_stderr
    if _hang_stderr is None and _test_timeout():
        duplicate = os.dup(2)
        try:
            _hang_stderr = os.fdopen(duplicate, "w")
        except BaseException:
            try:
                os.close(duplicate)
            except OSError as cleanup_error:
                if cleanup_error.errno != errno.EBADF:
                    raise
            raise


def pytest_unconfigure(config):
    global _hang_stderr, _shard
    _shard = None
    _cancel_dump()
    if _hang_stderr is not None:
        _hang_stderr.close()
        _hang_stderr = None


@pytest.hookimpl(wrapper=True)
def pytest_runtest_protocol(item, nextitem):
    limit = _test_timeout()
    if limit and _hang_stderr is not None:
        _dump_later(limit, exit=True, file=_hang_stderr)
        try:
            return (yield)
        finally:
            _cancel_dump()
    return (yield)


collect_ignore_glob = []
# the hermes checkout's own venv carries every third-party import the
# hermes-bound files need at collection time — a partial environment
# (gateway importable, discord/slack_sdk/dotenv missing) must
# skip too, not fail at import
_hermes_deps = ("gateway", "hermes_cli", "discord", "dotenv", "slack_sdk")
if any(importlib.util.find_spec(mod) is None for mod in _hermes_deps):
    collect_ignore_glob.append("integration/test_hermes_*")


def _deeper_than(text, limit):
    depth, in_string, escaped = 0, False, False
    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char in "[{":
            depth += 1
            if depth > limit:
                return True
        elif char in "]}":
            depth -= 1
    return False


@pytest.fixture
def recursion_limited_json(monkeypatch):
    """Decode like the recursion-limited json of Python <= 3.12.

    Python 3.13+ decodes deep nesting iteratively, so the unreadable-record
    branches are kept under test by failing nesting deeper than the old
    limit. The returned list records each deep payload that was refused."""
    original, hits = json.JSONDecoder.raw_decode, []

    def raw_decode(self, text, idx=0):   # json.loads/load decode through here too
        if _deeper_than(text[idx:], 990):
            hits.append(len(text))
            raise RecursionError("synthetic decoder depth failure")
        return original(self, text, idx)

    monkeypatch.setattr(json.JSONDecoder, "raw_decode", raw_decode)
    return hits
