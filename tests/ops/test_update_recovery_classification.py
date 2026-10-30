"""Synthetic recovery route tables and ordered observations across live hold rereads."""
from itertools import product

import pytest

import update_policy
import service_management
from test_mcs_update import updater as updater


@pytest.mark.parametrize("applying,done,expected", [
    (False, False, "finish_pre_merge"),
    (False, True, "complete_done_bookkeeping"),
    (True, False, "observe_head"),
    (True, True, "observe_head"),
])
def test_journal_position_oracle(applying, done, expected):
    assert update_policy.classify_recovery_journal_position(
        has_applying=applying, has_done=done) == expected


@pytest.mark.parametrize("head,previous,target,incomplete,expected", [
    ("t", "p", "t", False, "target"),
    ("t", "p", "t", True, "target"),
    ("p", "p", "t", False, "previous"),
    ("p", "p", "t", True, "restore_incomplete_wrong_head"),
    ("x", "p", "t", False, "unknown"),
    ("x", "p", "t", True, "restore_incomplete_wrong_head"),
    ("t", "t", "t", False, "target"),
    ("t", "t", "t", True, "target"),
    ("x", None, "t", False, "unknown"),
    ("", "", "t", False, "unknown"),
    (None, None, None, False, "target"),
    (None, None, None, True, "target"),
])
def test_head_position_oracle(head, previous, target, incomplete, expected):
    assert update_policy.classify_recovery_head_position(
        head=head, previous_sha=previous, target_sha=target,
        restore_incomplete=incomplete) == expected


# Independently reviewed rows, ordered by (rollback, runtime, reinstall, done).
# No oracle calls the classifier or reconstructs its conditional expression.
_TARGET_ROWS = [
    "forward", "forward", "manual_reinstall_required", "forward",
    "forward_runtime", "forward_runtime", "undo_interrupted_migration", "forward_runtime",
    "rollback", "rollback", "rollback", "rollback",
    "rollback_runtime", "rollback_runtime", "rollback_runtime", "rollback_runtime",
]
_PREVIOUS_ROWS = [
    "reset_previous_tree", "finish_pre_merge", "reset_previous_tree", "finish_pre_merge",
    "reset_previous_tree", "finish_pre_merge", "undo_interrupted_migration", "undo_interrupted_migration",
    "held_lost_rollback_target", "held_lost_rollback_target",
    "held_lost_rollback_target", "held_lost_rollback_target",
    "held_lost_rollback_target", "held_lost_rollback_target",
    "held_lost_rollback_target", "held_lost_rollback_target",
]


@pytest.mark.parametrize("flags,expected", list(zip(product((False, True), repeat=4), _TARGET_ROWS)))
def test_target_continuation_oracle(flags, expected):
    rollback, runtime, reinstall, done = flags
    assert update_policy.classify_recovery_target_continuation(
        rollback=rollback, runtime_migration=runtime,
        reinstall=reinstall, reinstall_done=done) == expected


@pytest.mark.parametrize("flags,expected", list(zip(product((False, True), repeat=4), _PREVIOUS_ROWS)))
def test_previous_continuation_oracle(flags, expected):
    held, rollback, runtime, clean = flags
    assert update_policy.classify_recovery_previous_continuation(
        held=held, rollback=rollback, runtime_migration=runtime, clean=clean) == expected


def _trace_world(updater, monkeypatch, *, applying=None, markers=(None,),
                 heads=("p",), clean=(True,), stages=("merge",), restore_consent=None):
    """Replace machine effects, retaining real isolated journal and lock files."""
    trace, reports = [], []
    state = updater.journal_store.default_state()
    state.update(applying=applying, stages=[{"stage": s, "at": 1} for s in stages])
    if restore_consent is not None:
        state["restore_consent"] = restore_consent
    updater.journal_store.save_state(updater.STATE_PATH, state)
    marker_values, head_values, clean_values = iter(markers), iter(heads), iter(clean)

    def marker():
        trace.append("marker")
        return next(marker_values)

    def head():
        trace.append("head")
        return next(head_values)

    def tree_clean():
        trace.append("clean")
        value = next(clean_values)
        if isinstance(value, Exception):
            raise value
        return value

    def changes(old_sha, new_sha):
        trace.append(("changes", old_sha, new_sha))
        return []

    def report(result, detail, **extra):
        trace.append(("report", result))
        reports.append((result, detail, extra))

    def forbidden(*args, **kwargs):
        pytest.fail("unexpected machine operation in synthetic recovery trace")

    monkeypatch.setattr(updater, "_check_runtime_restore", lambda entry: trace.append("runtime_gate"))
    monkeypatch.setattr(updater, "_clean_stale_git_locks", lambda: trace.append("cleanup") or [])
    monkeypatch.setattr(updater, "_journal_consent_from_marker", lambda *a: trace.append("adopt_marker"))
    monkeypatch.setattr(updater, "_last_report", lambda: {})
    monkeypatch.setattr(updater, "_awaiting_consent_marker", marker)
    monkeypatch.setattr(updater, "_head_sha", head)
    monkeypatch.setattr(updater, "_tree_clean", tree_clean)
    monkeypatch.setattr(updater, "_local_changes_outside", changes)
    monkeypatch.setattr(updater, "_git", forbidden)
    monkeypatch.setattr(updater, "_git_out", lambda args: trace.append(tuple(args)))
    monkeypatch.setattr(updater, "_report", report)
    monkeypatch.setattr(updater, "_alert_due", lambda *a: (False, None))
    monkeypatch.setattr(updater, "restart_agents", lambda **kw: trace.append("drainers") or [])
    monkeypatch.setattr(updater, "_remove_marker", lambda: trace.append("remove_marker"))
    monkeypatch.setattr(updater, "_enqueue_notice", forbidden)
    monkeypatch.setattr(updater, "_finish_recovery", lambda state, result, removed: trace.append(("finish", result)) or 0)
    monkeypatch.setattr(updater, "_complete_done_bookkeeping", lambda state: trace.append("done") or 0)
    monkeypatch.setattr(updater, "_undo_interrupted_migration", lambda *a: trace.append("undo_migration") or 0)
    monkeypatch.setattr(updater, "_restore_db", forbidden)
    monkeypatch.setattr(updater, "quiesce", lambda: trace.append("quiesce"))
    monkeypatch.setattr(updater, "_restore_runtime", lambda entry: trace.append("restore_runtime"))
    monkeypatch.setattr(service_management, "retire_to_snapshot",
                        lambda snapshot, *, cfg, paths: trace.append("membership") or [])
    monkeypatch.setattr(updater, "_use_migrated_runtime", lambda migration: trace.append("use_runtime"))
    monkeypatch.setattr(updater, "_services_reconcile", lambda: trace.append("services"))
    monkeypatch.setattr(updater, "_postcheck", lambda *a: trace.append("postcheck") or [])
    monkeypatch.setattr(updater, "_finish_resumed", lambda *a: trace.append("finish_resumed") or 0)
    return trace, reports


@pytest.mark.parametrize("stage,last", [("merge", ("finish", "interrupted_pre_merge")), ("done", "done")])
def test_journal_only_routes_do_not_observe_head(updater, monkeypatch, stage, last):
    trace, _ = _trace_world(updater, monkeypatch, stages=(stage,))
    assert updater.recover_interrupted() == 0
    assert trace == ["runtime_gate", "cleanup", "marker", last]


def test_incomplete_wrong_head_refuses_before_clean(updater, monkeypatch):
    entry = {"sha": "t", "prev_sha": "p", "rollback": True,
             "backup_path": "synthetic.db", "restore_incomplete": True}
    trace, reports = _trace_world(updater, monkeypatch, applying=entry,
                                  markers=(None, None), clean=())
    assert updater.recover_interrupted() == 1
    assert trace == ["runtime_gate", "cleanup", "adopt_marker", "marker", "head",
                     "marker", ("report", "escalate")]
    assert "writers_stopped" not in reports[0][2]


@pytest.mark.parametrize("clean,detail", [(True, "unclassifiable repo state"),
                                         ("failure", "git unverifiable: synthetic clean failure")])
def test_unknown_head_keeps_required_clean_observation(updater, monkeypatch, clean, detail):
    value = updater.UpdateError("synthetic clean failure") if clean == "failure" else clean
    trace, reports = _trace_world(updater, monkeypatch,
        applying={"sha": "t", "prev_sha": "p"}, markers=(None, None),
        heads=("x",), clean=(value,))
    assert updater.recover_interrupted() == 1
    assert trace == ["runtime_gate", "cleanup", "marker", "head", "clean", "marker",
                     ("report", "escalate"), "drainers", "remove_marker"]
    assert reports[0][1].startswith(detail)


def test_previous_route_rereads_marker_and_escalation_rereads_again(updater, monkeypatch):
    trace, reports = _trace_world(updater, monkeypatch,
        applying={"sha": "t", "prev_sha": "p", "rollback": True, "backup_path": "synthetic.db",
                  "schema_bump": False},
        markers=(None, {"phase": "awaiting_consent"}, {"phase": "awaiting_consent"}))
    assert updater.recover_interrupted() == 1
    assert trace == ["runtime_gate", "cleanup", "adopt_marker", "marker", "head", "clean",
                     "marker", "marker", ("report", "restore_consent_blocked")]
    assert "hold lost" in reports[0][1]


@pytest.mark.parametrize("schema_bump", [None, True])
def test_schema_rollback_freezes_before_classifying_previous_head(updater, monkeypatch, schema_bump):
    from pathlib import Path

    applying = {"sha": "t", "prev_sha": "p", "rollback": True, "backup_path": "synthetic.db"}
    if schema_bump is not None:
        applying["schema_bump"] = schema_bump
    trace, reports = _trace_world(updater, monkeypatch, applying=applying,
                                  markers=(None, {"phase": "awaiting_consent"}), clean=())
    ensure = updater._ensure_recovery_marker

    def freeze():
        trace.append("ensure")
        ensure()

    monkeypatch.setattr(updater, "_ensure_recovery_marker", freeze)
    assert updater.recover_interrupted() == 1
    assert trace == ["ensure", "runtime_gate", "cleanup", "adopt_marker", "marker", "head",
                     "marker", ("report", "restore_consent_blocked")]
    state = updater.journal_store.load_state(updater.STATE_PATH)
    assert state["applying"]["restore_incomplete"] is True
    assert Path(updater.MARKER_PATH).exists()
    assert "writers_stopped" not in reports[0][2]


@pytest.mark.parametrize("after_head,tail", [
    ("p", ["head", "clean", ("finish", "mixed_tree_reset")]),
    ("x", ["head", "marker", ("report", "escalate"), "drainers", "remove_marker"]),
])
def test_previous_reset_has_inverse_diff_and_short_circuit_verification(updater, monkeypatch, after_head, tail):
    trace, _ = _trace_world(updater, monkeypatch,
        applying={"sha": "t", "prev_sha": "p"}, markers=(None, None, None),
        heads=("p", after_head), clean=(False, True))
    assert updater.recover_interrupted() == (0 if after_head == "p" else 1)
    assert trace == ["runtime_gate", "cleanup", "marker", "head", "clean", "marker",
                     ("changes", "t", "p"), ("reset", "--hard", "p"), *tail]


def test_target_reset_rechecks_clean_without_reobserving_head(updater, monkeypatch):
    trace, _ = _trace_world(updater, monkeypatch,
        applying={"sha": "t", "prev_sha": "p"}, heads=("t",), clean=(False, True))
    assert updater.recover_interrupted() == 0
    assert trace == ["runtime_gate", "cleanup", "marker", "head", "clean",
                     ("changes", "p", "t"), ("reset", "--hard", "t"), "clean",
                     "services", "drainers", "postcheck", "finish_resumed"]


@pytest.mark.parametrize("flags,effects", [
    ({"rollback": True}, ["restore_runtime", "membership", "services", "drainers", "postcheck", "finish_resumed"]),
    ({"rollback": True, "runtime_migration": {"synthetic": True}, "reinstall": True},
     ["quiesce", "restore_runtime", "membership", "services", "drainers", "postcheck", "finish_resumed"]),
    ({"runtime_migration": {"synthetic": True}}, ["use_runtime", "services", "drainers", "postcheck", "finish_resumed"]),
    ({"reinstall": True, "runtime_migration": {"synthetic": True}}, ["undo_migration"]),
])
def test_target_continuation_preserves_effect_order(updater, monkeypatch, flags, effects):
    trace, _ = _trace_world(updater, monkeypatch,
        applying={"sha": "t", "prev_sha": "p", **flags}, heads=("t",))
    assert updater.recover_interrupted() == 0
    assert trace == ["runtime_gate", "cleanup", "marker", "head", "clean", *effects]


def test_previous_migration_undo_precedes_dirty_tree_reset(updater, monkeypatch):
    trace, _ = _trace_world(updater, monkeypatch,
        applying={"sha": "t", "prev_sha": "p", "rollback": True,
                  "runtime_migration": {"synthetic": True}},
        markers=(None, None), clean=(False,))
    assert updater.recover_interrupted() == 0
    assert trace == ["runtime_gate", "cleanup", "marker", "head", "clean", "marker", "undo_migration"]


@pytest.mark.parametrize("pending", [False, True])
def test_target_database_hold_only_clears_after_successful_restore(updater, monkeypatch, pending):
    entry = {"sha": "t", "prev_sha": "p", "rollback": True,
             "backup_path": "synthetic.db", "restore_incomplete": True}
    trace, _ = _trace_world(updater, monkeypatch, applying=entry,
        heads=("t",), markers=(), restore_consent={"report_id": "synthetic"})

    def restore(path, on_hold):
        trace.append("restore_db")
        assert path == "synthetic.db"
        if pending:
            raise updater.RestoreConsentPending({"report_id": "synthetic"})

    def runtime(record):
        saved = updater.journal_store.load_state(updater.STATE_PATH)
        assert "restore_consent" not in saved
        assert "restore_incomplete" not in saved["applying"]
        trace.append("restore_runtime")

    monkeypatch.setattr(updater, "_restore_db", restore)
    monkeypatch.setattr(updater, "_restore_runtime", runtime)
    monkeypatch.setattr(updater, "_consent_hold", lambda *a: trace.append("hold"))
    assert updater.recover_interrupted() == 0
    prefix = ["runtime_gate", "cleanup", "head", "clean", "restore_db"]
    if pending:
        assert trace == [*prefix, "hold"]
        saved = updater.journal_store.load_state(updater.STATE_PATH)
        assert saved["restore_consent"] == {"report_id": "synthetic"}
        assert saved["applying"]["restore_incomplete"] is True
    else:
        assert trace == [*prefix, "restore_runtime", "membership", "services",
                         "drainers", "postcheck", "finish_resumed"]
