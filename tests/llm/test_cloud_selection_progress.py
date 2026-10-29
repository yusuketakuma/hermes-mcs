"""Cloud selection progresses past unreceipted replies without widening history scope."""
import time

import pytest

import cloud_policy
import extract_llm
import semantic_drain
from semantic_testkit import _cfg, _ledger, _message, _patient


@pytest.fixture
def blocked_prefix(tmp_path, monkeypatch):
    led = _ledger(tmp_path)
    cfg = _cfg("assist", fact_source="shadow", summary_mode="shadow", loop_mode="shadow")
    cfg["cloud_llm"] = {"mode": "on", "project_ids": None}
    monkeypatch.setattr(extract_llm, "load_config", lambda *a: cfg)
    monkeypatch.setattr(semantic_drain, "load_config", lambda *a: cfg)
    monkeypatch.setattr(cloud_policy, "api_key", lambda *a: "synthetic-key")
    for pid in range(1, 514):
        _patient(led, pid)
        led.save_messages([_message(pid, pid=pid, body="完全合成の過去記録。")])
        led.semantic_seed(pid, [pid], {"source": "history_import"})
    with led.db:
        led.db.execute("UPDATE patients SET is_archived=1 WHERE project_id=513")
        led.db.execute("UPDATE messages SET first_seen=?,posted_at_ts=message_id", (time.time() - 1200,))
        led.db.execute("UPDATE message_revisions SET observed_at=?", (time.time() - 1200,))
        cloud_policy.capture_history_tx(led.db, "synthetic-selection-scope", time.time())
    # Roots still occur in the receipt, but their new reply does not. A root-only
    # membership test cannot authorize the expanded thread as a cloud prompt.
    for pid in range(1, 513):
        led.save_messages([_message(1000 + pid, pid=pid, parent=pid,
                                   body="完全合成の承認範囲外の新しい返信。")])
    yield led, cfg
    led.close()


def _select(led, lane):
    if lane == "semantic":
        return semantic_drain._select_cloud_due(led, "backlog", ("semantic",), False, 1)[0]
    return extract_llm._select_pending(led, 64, None, None, False,
        cloud_only=True, prioritize_patients=True)[0]


@pytest.mark.parametrize("lane", ["semantic", "extract"])
def test_receipted_work_behind_large_invalid_prefix_eventually_selected(blocked_prefix, lane):
    led, _ = blocked_prefix
    observed = []
    # Permit a finite paging implementation rather than demanding an unbounded
    # scan on the first call. Each pass must retain the exact source check.
    for _ in range(12):
        observed.extend(row for row in _select(led, lane)
                        if cloud_policy.history_thread_allowed(led.db, row["project_id"], row["message_id"]))
        if observed:
            break
    assert [row["project_id"] for row in observed] == [513]
    assert all(row["message_id"] == 513 for row in observed)


@pytest.mark.parametrize("lane", ["semantic", "extract"])
def test_revoked_history_snapshot_never_selects_previously_allowed_tail(blocked_prefix, lane):
    led, _ = blocked_prefix
    for _ in range(2):
        _select(led, lane)
    with led.db:
        led.artifact_add_tx("cloud_history_scope", '{"version":1,"sources":{}}')
    for _ in range(12):
        assert not [row for row in _select(led, lane)
                    if cloud_policy.history_thread_allowed(led.db, row["project_id"], row["message_id"])]


@pytest.mark.parametrize("raw", [
    '{"version":true,"sources":{"1:1":"' + "a" * 64 + '"}}',
    '{"version":1,"version":1,"sources":{"1:1":"' + "a" * 64 + '"}}',
    '{"version":1,"sources":{"1:1":"' + "a" * 64 + '","1:1":"' + "b" * 64 + '"}}',
])
def test_ambiguous_history_receipt_never_authorizes_source(raw):
    assert cloud_policy._history_sources(raw) == {}
