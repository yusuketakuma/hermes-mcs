"""Writer refusal diagnostics observe synthetic gates without retrying them."""
import json
import os
from types import SimpleNamespace

import pytest

import extract_llm_source_contract as contract
import extract_llm_errors as errors
import extract_llm_resident as resident
import notify_spool
import writer_lock


@pytest.mark.parametrize("failure,reason", [
    ("update", "update_marker_present"),
    ("restore", "restore_hold_present"),
    ("unknown", "restore_state_unverifiable"),
    ("unreadable", "restore_state_unverifiable"),
    ("clear", None),
])
def test_writer_hold_is_classified_in_the_original_check(tmp_path, monkeypatch, failure, reason):
    monkeypatch.setattr(writer_lock, "HOME", str(tmp_path))
    data = tmp_path / "data"
    data.mkdir()
    calls = []

    def restore(_):
        calls.append(True)
        if failure == "unknown":
            raise OSError("synthetic secret must not be recorded")
        if failure == "unreadable":
            return {"unreadable": True}
        return object() if failure == "restore" else None

    monkeypatch.setattr(notify_spool, "restore_writer_hold", restore)
    if failure == "update":
        (data / writer_lock.UPDATE_MARKER_NAME).touch()
    with writer_lock.capture_writer_refusal() as diagnostic:
        assert writer_lock.writer_blocked() is (reason is not None)
    assert calls == ([] if failure == "update" else [True])
    assert diagnostic == {
        "writer_reason": reason or "writer_refusal_unclassified",
        "writer_stage": "update_restore_fence" if reason else "writer_guard",
    }


@pytest.fixture
def current_guard(tmp_path, monkeypatch):
    source = tmp_path / "synthetic_inference.py"
    source.write_text("# fully synthetic source\n")
    monkeypatch.setattr(contract, "inference", SimpleNamespace(
        __file__=str(source), CURRENT_VERSION="synthetic"))
    monkeypatch.setattr(contract, "_INFERENCE_VERSION", "synthetic")
    monkeypatch.setattr(contract, "_INFERENCE_SOURCE_STAT", source.stat().st_mtime_ns)
    monkeypatch.setattr(contract, "_WRITER_STATE", (False, 123))
    monkeypatch.setattr(writer_lock, "writer_blocked", lambda: False)
    monkeypatch.setattr(writer_lock, "updater_state", lambda: (False, 123))
    return source


@pytest.mark.parametrize("failure,reason", [
    ("initial", "initial_writer_blocked"),
    ("source", "inference_source_changed"),
    ("missing", "inference_source_unavailable"),
    ("version", "inference_version_changed"),
    ("generation", "source_generation_changed"),
])
def test_fence_reason_comes_from_the_failing_check(current_guard, monkeypatch, failure, reason):
    if failure == "initial":
        monkeypatch.setattr(contract, "_WRITER_STATE", (True, 123))
        monkeypatch.setattr(writer_lock, "writer_blocked", lambda: pytest.fail("short circuit lost"))
    elif failure == "source":
        monkeypatch.setattr(contract, "_INFERENCE_SOURCE_STAT", -1)
    elif failure == "missing":
        current_guard.unlink()
    elif failure == "version":
        monkeypatch.setattr(contract.inference, "CURRENT_VERSION", "changed")
    else:
        monkeypatch.setattr(writer_lock, "updater_state", lambda: (False, 124))
    with writer_lock.capture_writer_refusal() as diagnostic:
        assert contract._writer_current_diagnostic(full=True) is False
    assert diagnostic["writer_reason"] == reason


def test_full_fence_retains_hold_seen_in_updater_check(current_guard, monkeypatch):
    def changed_state():
        writer_lock.record_writer_refusal("update_marker_present", "update_restore_fence")
        return True, 123
    monkeypatch.setattr(writer_lock, "updater_state", changed_state)
    with writer_lock.capture_writer_refusal() as diagnostic:
        assert contract._writer_current_diagnostic(full=True) is False
    assert diagnostic == {"writer_reason": "update_marker_present", "writer_stage": "update_restore_fence"}


def test_maintenance_lock_refusal_preserves_wait_and_short_circuit(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(errors, "acquire_run_lock", lambda: calls.append("acquire") or None)
    monkeypatch.setattr(resident.time, "sleep", lambda seconds: calls.append(("sleep", seconds)))
    monkeypatch.setattr(contract, "_writer_current", lambda **_: pytest.fail("guard probed after no lock"))
    monkeypatch.setattr(resident, "Ledger", lambda *_: pytest.fail("ledger opened"))
    monkeypatch.setattr(resident.sys, "argv", ["extract_llm", "--revive-failed"])
    assert resident.main() == 1
    assert calls == ["acquire", ("sleep", 1)]
    assert json.loads(capsys.readouterr().out) == {
        "ok": False, "error": "writer_unavailable",
        "writer_reason": "run_lock_not_acquired", "writer_stage": "write_lock",
    }


def test_final_maintenance_guard_is_reported_without_reprobe(tmp_path, monkeypatch, capsys):
    descriptor = os.open(tmp_path / "synthetic.lock", os.O_CREAT | os.O_RDWR, 0o600)
    checks = []

    def current(**_):
        checks.append(True)
        if len(checks) == 2:
            writer_lock.record_writer_refusal("inference_version_changed", "writer_fence")
            return False
        return True

    monkeypatch.setattr(errors, "acquire_run_lock", lambda: descriptor)
    monkeypatch.setattr(contract, "_writer_current", current)
    monkeypatch.setattr(resident, "Ledger", lambda *_: pytest.fail("ledger opened"))
    monkeypatch.setattr(resident.sys, "argv", ["extract_llm", "--revive-failed"])
    assert resident.main() == 1
    assert len(checks) == 2
    assert json.loads(capsys.readouterr().out)["writer_reason"] == "inference_version_changed"
    with pytest.raises(OSError):
        os.fstat(descriptor)


def test_unknown_guard_cannot_reuse_an_earlier_lock_cause(monkeypatch):
    monkeypatch.setattr(contract, "_writer_current", lambda **_: False)
    with writer_lock.capture_writer_refusal() as diagnostic:
        writer_lock.record_writer_refusal("run_lock_not_acquired", "ledger_lock")
        assert contract._writer_current_diagnostic(full=True) is False
    assert diagnostic == {"writer_reason": "writer_refusal_unclassified", "writer_stage": "writer_fence"}


def test_capture_restores_outer_context_even_after_exception():
    with writer_lock.capture_writer_refusal() as outer:
        with pytest.raises(RuntimeError), writer_lock.capture_writer_refusal() as inner:
            writer_lock.record_writer_refusal("restore_hold_present", "update_restore_fence")
            raise RuntimeError("synthetic")
        writer_lock.record_writer_refusal("run_lock_not_acquired", "ledger_lock")
    assert inner["writer_reason"] == "restore_hold_present"
    assert outer["writer_reason"] == "run_lock_not_acquired"
    writer_lock.record_writer_refusal("update_marker_present", "update_restore_fence")
    assert outer["writer_reason"] == "run_lock_not_acquired"


@pytest.mark.parametrize("reason,stage", [
    ("synthetic secret", "writer_fence"),
    ("restore_hold_present", "synthetic secret"),
    ("restore_hold_present", "write_lock"),
    ({"synthetic": "secret"}, []),
])
def test_only_fixed_reason_stage_pairs_are_recorded(reason, stage):
    with writer_lock.capture_writer_refusal() as diagnostic:
        writer_lock.record_writer_refusal(reason, stage)
    assert diagnostic == {"writer_reason": "writer_refusal_unclassified", "writer_stage": "writer_guard"}


def test_candidate_loaded_source_contract_ignores_active_refusal_capture(monkeypatch):
    import semantic_candidate

    monkeypatch.setattr(semantic_candidate, "_LOADED_SOURCES", None)
    before = semantic_candidate.loaded_source_contract()
    assert "writer_lock" in before
    with writer_lock.capture_writer_refusal():
        writer_lock.record_writer_refusal("restore_hold_present", "update_restore_fence")
        # Force fresh collection: a cached witness would hide runtime-state regressions.
        monkeypatch.setattr(semantic_candidate, "_LOADED_SOURCES", None)
        during = semantic_candidate.loaded_source_contract()
        assert json.loads(json.dumps(during)) == before
    monkeypatch.setattr(semantic_candidate, "_LOADED_SOURCES", None)
    assert semantic_candidate.loaded_source_contract() == before
