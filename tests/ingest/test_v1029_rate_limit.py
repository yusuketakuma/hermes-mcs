"""Synthetic regressions: 429 honors a bounded Retry-After, stays retryable, and stops the run's API reads."""
import base64
import time

import pytest

import collection_stages
import mcs_adapter
from mcs_contract import MCSError


def _adapter(monkeypatch, statuses, headers=None):
    calls, sleeps = [], []

    def worker(payload, timeout, deadline):
        calls.append(payload["url"])
        status = next(statuses)
        return {"status": status, "headers": dict(headers or {}),
                "body": base64.b64encode(b'{"memo_summary": null}').decode("ascii")}

    adapter = mcs_adapter.MCSAdapter(worker=worker)
    adapter._token = "synthetic-token"
    monkeypatch.setattr(mcs_adapter.time, "sleep", sleeps.append)
    return adapter, calls, sleeps


@pytest.mark.parametrize(("header", "expected"), [
    ("7", 7.0), ("3600", 60.0), ("Wed, 21 Oct 2026 07:28:00 GMT", 1.5), (None, 1.5)])
def test_retry_after_is_honored_and_bounded(monkeypatch, header, expected):
    headers = {} if header is None else {"Retry-After": header}
    adapter, calls, sleeps = _adapter(monkeypatch, iter([429, 200]), headers)
    assert adapter._request("GET", "/projects")[0] == 200
    assert sleeps == [expected] and len(calls) == 2


def test_retry_after_never_sleeps_past_the_run_deadline(monkeypatch):
    adapter, _calls, sleeps = _adapter(monkeypatch, iter([429, 200]), {"retry-after": "60"})
    adapter.set_deadline(time.monotonic() + 5)
    adapter._request("GET", "/projects")
    assert 0 < sleeps[0] <= 5


def test_second_429_stops_every_later_api_request_of_the_run(monkeypatch):
    adapter, calls, sleeps = _adapter(monkeypatch, iter([429, 429]))
    with pytest.raises(MCSError) as first:
        adapter._request("GET", "/projects")
    assert (first.value.kind, first.value.status, first.value.retryable) == ("http_error", 429, True)
    assert len(calls) == 2 and len(sleeps) == 1
    with pytest.raises(MCSError) as later:
        adapter._request("GET", "/projects/1/messages")
    assert (later.value.kind, later.value.status, later.value.retryable) == ("rate_limited", 429, True)
    assert len(calls) == 2                      # the server is not contacted again
    assert mcs_adapter.MCSAdapter(worker=lambda *a, **k: None)._rate_limited == 0


def test_karte_summary_keeps_due_after_rate_limit(monkeypatch):
    adapter, calls, _sleeps = _adapter(monkeypatch, iter([429, 429]))
    marks = []

    class Ledger:
        def karte_summary_due(self):
            return [1, 2, 3]

        def karte_id(self, pid):
            return 100 + pid

        def karte_summary_mark_due(self, pid, due=True):
            marks.append((pid, due))

        def karte_summary_failed(self, pid):
            failed.append(pid)

    failed = []
    result = {}
    collection_stages.stage_karte_summary(adapter, Ledger(), result, time.monotonic() + 600)
    assert marks == [(1, True), (2, True), (3, True)]
    assert failed == [1]                        # never-sent GETs get no 6 h backoff
    assert [e["kind"] for e in result["karte_summary"]["errors"]] == [
        "http_error", "rate_limited", "rate_limited"]
    assert len(calls) == 2


def test_metadata_shadow_defers_locally_refused_targets_without_observations(monkeypatch):
    adapter, calls, _sleeps = _adapter(monkeypatch, iter([]))
    adapter._rate_limited = mcs_adapter.RATE_LIMIT_STOP
    targets = [{"project_id": 1, "message_id": m, "parent_id": None, "due_total": 2}
               for m in (11, 12)]
    monkeypatch.setattr(collection_stages.message_observations,
                        "metadata_watch_targets", lambda db: targets)
    saved = []
    ledger = type("L", (), {"db": None, "save_metadata_shadow":
                            lambda self, *a, **k: saved.append(a)})()
    result = {}
    collection_stages.stage_metadata_shadow(adapter, ledger, result, time.monotonic() + 600)
    stats = result["metadata_shadow"]
    assert (stats["deferred"], stats["deferred_reasons"]) == (2, {"rate_limited": 2})
    assert saved == [] and calls == [] and stats["errors"] == []


def test_human_approved_stamp_post_is_not_refused_locally(monkeypatch):
    adapter, calls, _sleeps = _adapter(monkeypatch, iter([200]))
    adapter._rate_limited = mcs_adapter.RATE_LIMIT_STOP
    assert adapter.write_reaction(1, next(iter(mcs_adapter.REACTION_LABELS))) == 200
    assert len(calls) == 1
