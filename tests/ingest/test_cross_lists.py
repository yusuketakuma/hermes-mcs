"""Completely synthetic cross-list replies at the real worker HTTP opening boundary."""
import io
import json
import urllib.error
import urllib.parse
from collections import deque
from email.message import Message as Headers
from types import SimpleNamespace
from typing import TypedDict

import pytest

import mcs_adapter
import mcs_util
import mcs_worker
from cross_lists import JSON, capture_rows, sync_cross_list
from ledger import Ledger

PID, MID, STAMP = 900001, 900101, 1900000000


class Response(io.BytesIO):
    status = 200
    headers: dict[str, str] = {}


class WirePage(TypedDict):
    messages: list[dict[str, JSON]]
    paginate: dict[str, JSON]


def message(mid=MID, pid=PID, **extra) -> dict[str, JSON]:
    return {"id": mid, "project": {"id": pid}, "comment": "SYNTHETIC BODY",
            "created_at": "2026-10-04T00:00:00+09:00",
            "user": {"id": 900201, "last_name": "SYNTHETIC NAME"},
            **extra}


def page(rows, *, number=1, has_next: bool | str = False, timestamp: int | None = STAMP,
         **extra) -> WirePage:
    return {"messages": rows, "paginate": {
        "current_page": number, "per_page": 20, "has_next": has_next,
        "timestamp": timestamp, **extra}}


@pytest.fixture
def replay(monkeypatch):
    """Keep the real adapter, worker execution and JSON parser; replace HTTP open."""
    def make(responses, *, on_open=None):
        pending, calls = deque(responses), []

        def open_response(request, timeout):
            assert pending, "unexpected HTTP request"
            url = urllib.parse.urlsplit(request.full_url)
            assert (url.scheme, url.netloc) == ("https", "www.medical-care.net")
            assert request.get_method() == "GET" and request.data is None
            assert request.get_header("Authorization") == "Bearer SYNTHETIC_TOKEN"
            assert timeout > 0
            calls.append((url.path, urllib.parse.parse_qs(url.query), timeout))
            response = pending.popleft()
            if on_open is not None:
                on_open()
            if type(response) is int:
                raise urllib.error.HTTPError(
                    request.full_url, response, "SYNTHETIC", Headers(), None)
            if isinstance(response, Exception):
                raise response
            return Response(response if isinstance(response, bytes)
                            else json.dumps(response).encode())

        def opener(*handlers):
            assert handlers == (mcs_util.NoRedirect,)
            return SimpleNamespace(open=open_response)

        monkeypatch.setattr(mcs_util, "no_proxy_opener", opener)

        def worker(payload, timeout, deadline):
            assert payload["operation"] == "api"
            assert deadline is not None
            try:
                return mcs_worker._execute(dict(payload, timeout=timeout))
            except urllib.error.URLError:
                raise mcs_worker.WorkerError("network_error", retryable=True) from None

        client = mcs_adapter.MCSAdapter(worker=worker)
        client._token = "SYNTHETIC_TOKEN"
        return client, pending, calls
    return make


@pytest.fixture
def store(tmp_path):
    ledger = Ledger(str(tmp_path / "synthetic.db"))
    ledger.ensure_patient(PID)
    yield ledger
    ledger.close()


@pytest.mark.parametrize("dataset", ["mentioned", "bookmarked"])
def test_real_reader_normalizes_scoped_roots_replies_and_metadata(replay, dataset):
    raw = message(is_unread=True, reactions=[], mentions=[{"type": "project"}],
                  files=[], is_bookmarked=True)
    reply = message(MID + 1, PID + 1, parent_message={"id": MID + 20},
                    comment="", is_unread=False)
    client, pending, calls = replay([
        page([raw], has_next=True, total_entries=2),
        page([reply], number=2, total_entries=2)])
    result = client.fetch_cross_list(dataset)
    assert result.complete and result.pages == 2 and result.timestamp == STAMP
    assert not pending and client._deadline is None
    root, child = result.entries
    assert root.message.body_state == "full" and root.message.files_present
    assert root.message.metadata == {
        "reactions": [], "mentions": [{"type": "project", "id": PID}],
        "is_bookmarked": True}
    assert child.message.project_id == PID + 1 and child.message.parent_id == MID + 20
    assert child.message.body_state == "full" and child.message.body_html == ""
    assert [e.is_unread for e in result.entries] == [True, False]
    first = {"page": ["1"], "per_page": ["20"], "include_paginate_totals": ["0"],
             "no_extend_session": ["1"]}
    if dataset == "mentioned":
        first.update(unread=["0"], include_meta=["1"])
    assert calls[0][0] == f"/api/v2t/messages/{dataset}" and calls[0][1] == first
    assert calls[1][1] == {
        **{k: v for k, v in first.items() if k != "include_meta"},
        "page": ["2"], "timestamp": [str(STAMP)]}
    assert all("increment_count" not in c[1] and "keep_read_status" not in c[1] for c in calls)


@pytest.mark.parametrize("raw,unread,body_state", [
    (message(), None, "full"),
    (message(comment=None), None, "unknown"),
    (message(comment=None, comment_snippet="SYNTHETIC"), None, "snippet"),
    (message(delete_user={"id": 900201}, is_unread=True), True, "deleted"),
])
def test_absent_unread_is_not_false_and_body_state_reuses_normalizer(
        replay, raw, unread, body_state):
    client, _, _ = replay([page([raw], timestamp=None)])
    result = client.fetch_cross_list("mentioned")
    assert result.complete and result.timestamp is None
    assert result.entries[0].is_unread is unread
    assert result.entries[0].message.body_state == body_state
    assert capture_rows(result)[0]["is_unread"] is unread


@pytest.mark.parametrize("raw", [
    {}, {"messages": []}, {"messages": None, "paginate": {"has_next": False}},
    page([{"id": MID}]), page([message(project=None)]),
    page([message(project_id=PID + 1)]), page([message(id=True)]),
    page([message(is_unread="true")]),
    page([message(parent_message={"id": MID})]),
    page([message(parent_message={"id": MID + 1, "project_id": PID + 1})]),
    page([message(parent_id=MID + 1)]),
    page([message(), message()]),
    page([message()], current_page=2),
    page([message()], total_entries=0),
    page([message()], total_pages=2),
    page([message()], has_next="yes"),
    page([message(MID + i) for i in range(21)]),
    b"{not-json", b"[]",
])
def test_absent_malformed_and_conflicting_evidence_fails_closed(replay, raw):
    client, pending, calls = replay([raw])
    result = client.fetch_cross_list("bookmarked")
    assert not result.complete and result.entries == [] and result.reason == "schema_error"
    assert not pending and len(calls) == 1


@pytest.mark.parametrize("second,reason", [
    (page([message(MID + 1)], number=2, timestamp=STAMP + 1), "schema_error"),
    (page([message(MID + 1)], number=2, timestamp=None), "schema_error"),
    (page([message()], number=2), "schema_error"),
    (401, "session_expired"), (403, "http_error"), (429, "http_error"),
    (503, "http_error"), (urllib.error.URLError("SYNTHETIC"), "network_error"),
])
def test_partial_failure_discards_working_set_without_retry_or_probe(replay, second, reason):
    client, pending, calls = replay([page([message()], has_next=True), second])
    result = client.fetch_cross_list("mentioned", unread_only=True)
    assert not result.complete and result.entries == [] and result.reason == reason
    assert calls[0][1]["unread"] == ["1"] and len(calls) == 2 and not pending
    assert client._deadline is None


@pytest.mark.parametrize("response,options,reason", [
    (page([message()], has_next=True, timestamp=None), {}, "snapshot_missing"),
    (page([], has_next=True), {}, "schema_error"),
    (page([message()], has_next=True), {"max_pages": 1}, "page_limit"),
    (page([message(), message(MID + 1)]), {"max_rows": 1}, "row_limit"),
])
def test_page_row_progress_and_snapshot_bounds(replay, response, options, reason):
    client, pending, calls = replay([response])
    result = client.fetch_cross_list("mentioned", **options)
    assert not result.complete and result.reason == reason and result.entries == []
    assert not pending and len(calls) == 1


def test_total_pages_terminal_contract(replay):
    first, last = page([message()], has_next=True), page([], number=2)
    for raw in (first, last):
        del raw["paginate"]["has_next"]
        raw["paginate"]["total_pages"] = 2
    client, _, _ = replay([first, last])
    assert client.fetch_cross_list("bookmarked").complete


def test_optional_metadata_errors_do_not_destroy_body_or_invent_absence(replay):
    client, _, _ = replay([page([message(mentions="invalid", reactions=[])])])
    result = client.fetch_cross_list("mentioned")
    assert result.complete and result.entries[0].message.body_state == "full"
    row = capture_rows(result)[0]
    assert row["metadata"] == {"reactions": []} and row["metadata_errors"] == ["mentions_invalid"]


def test_deadline_is_absolute_and_restored_without_sleep(replay, monkeypatch):
    clock = {"now": 100.0}
    monkeypatch.setattr("cross_lists.time.monotonic", lambda: clock["now"])
    client, pending, calls = replay(
        [page([message()], has_next=True)], on_open=lambda: clock.update(now=103.0))
    client.set_deadline(110.0)
    result = client.fetch_cross_list("mentioned", deadline_s=2)
    assert result.reason == "deadline_exceeded" and result.entries == []
    assert len(calls) == 1 and calls[0][2] == 2 and not pending
    assert client._deadline == 110.0
    client.set_deadline(99.0)
    result = client.fetch_cross_list("mentioned")
    assert result.reason == "deadline_exceeded" and len(calls) == 1
    assert client._deadline == 99.0


@pytest.mark.parametrize("options", [
    {"max_pages": 11}, {"max_pages": True}, {"per_page": 21},
    {"max_rows": 201}, {"deadline_s": float("nan")}, {"deadline_s": 0},
])
def test_bad_bounds_do_not_open_http(replay, options):
    client, _, calls = replay([])
    with pytest.raises(ValueError):
        client.fetch_cross_list("mentioned", **options)
    assert calls == []


def test_opt_in_and_no_implicit_login(store, replay):
    client, _, calls = replay([])
    assert sync_cross_list(store, client, "mentioned")["state"] == "disabled"
    client._token = None
    assert sync_cross_list(store, client, "mentioned", enabled=True)["state"] == "unknown"
    assert client.fetch_cross_list("bookmarked").reason == "cached_session_required"
    assert not store.artifacts("cross_list_v1") and calls == []
    with pytest.raises(ValueError):
        client.fetch_cross_list("bookmarked", unread_only=True)


def test_capture_never_changes_ordinary_collection_or_metadata_current(store, replay):
    original = mcs_adapter._norm_message(message(is_unread=True, reactions=[]), PID)
    store.save_messages([original], project_id=PID)
    tables = [r[0] for r in store.db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT IN "
        "('artifacts','sqlite_sequence')")]
    def snapshot():
        return {name: [tuple(r) for r in store.db.execute(f'SELECT * FROM "{name}"')]
                for name in tables}
    before = snapshot()
    client, _, _ = replay([page([message(comment="SYNTHETIC CHANGED BODY",
                                       is_unread=False, is_bookmarked=True)])])
    result = sync_cross_list(store, client, "mentioned", enabled=True, now=100)
    assert result["state"] == "complete" and snapshot() == before
    serialized = store.artifacts("cross_list_v1")[0]["content"]
    assert "SYNTHETIC CHANGED BODY" not in serialized and "SYNTHETIC NAME" not in serialized
    # Ordinary collection still owns body/metadata updates and keeps its request.
    client, pending, calls = replay([{"messages": [message(is_unread=True)],
                                     "paginate": {"has_next": False}}])
    client.set_deadline(10**12)
    batch = client.fetch_unread_messages(PID, STAMP)
    assert batch.reached and batch.error is None and batch.messages[0].is_unread
    assert not pending and calls[0][1]["timestamp"] == [str(STAMP)]
    assert calls[0][1]["keep_read_status"] == ["1"]


@pytest.mark.parametrize("extra", [
    {"pid": PID + 1}, {"parent_message": {"id": MID + 20}},
])
def test_stored_project_or_parent_conflict_fails_capture(store, replay, extra):
    store.save_messages([mcs_adapter._norm_message(message(), PID)], project_id=PID)
    client, _, _ = replay([page([message(**extra)])])
    result = sync_cross_list(store, client, "mentioned", enabled=True)
    assert result["state"] == "failed" and result["reason"] == "scope_mismatch"
    payload = json.loads(store.artifacts("cross_list_v1")[0]["content"])
    assert payload["rows"] == [] and not payload["complete"]


def test_cli_captures_real_synthetic_wire_without_publishing(store, replay, tmp_path, monkeypatch, capsys):
    import cross_lists

    client, pending, calls = replay([page([message(is_unread=True)])])
    monkeypatch.setattr(client, "_read_cache", lambda: "SYNTHETIC_TOKEN")
    monkeypatch.setattr(mcs_adapter, "MCSAdapter", lambda **kwargs: client)
    assert cross_lists.main([
        "--database", str(tmp_path / "synthetic.db"), "--dataset", "mentioned",
        "--read-only-get", "--token-cache", str(tmp_path / "synthetic-cache"),
    ]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["state"] == "complete" and result["artifact_id"]
    assert not pending and len(calls) == 1
    capture = store.artifacts("cross_list_v1")[0]["content"]
    assert "SYNTHETIC BODY" not in capture and "SYNTHETIC NAME" not in capture
    assert store.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0


def test_cli_busy_lock_never_opens_writer_or_adapter(store, tmp_path, monkeypatch, capsys):
    import os
    import cross_lists
    import ledger

    lock_fd = mcs_util.acquire_run_lock(str(tmp_path / "run.lock"))
    assert lock_fd is not None
    def forbidden(*args, **kwargs):
        pytest.fail("writer and adapter must not be opened while lock is busy")

    monkeypatch.setattr(ledger, "Ledger", forbidden)
    monkeypatch.setattr(mcs_adapter, "MCSAdapter", forbidden)
    try:
        assert cross_lists.main([
            "--database", str(tmp_path / "synthetic.db"), "--dataset", "mentioned",
            "--read-only-get", "--token-cache", str(tmp_path / "synthetic-cache"),
        ]) == 1
    finally:
        os.close(lock_fd)
    assert json.loads(capsys.readouterr().out) == {"state": "held", "reason": "run_lock_busy"}


@pytest.mark.parametrize("flags", [[], ["--read-only-get", "--unread-only"]])
def test_cli_rejects_unapproved_or_ungrounded_get_options(tmp_path, flags):
    import cross_lists

    with pytest.raises(SystemExit) as result:
        cross_lists.main([
            "--database", str(tmp_path / "absent.db"), "--dataset", "bookmarked",
            "--token-cache", str(tmp_path / "absent-cache"), *flags,
        ])
    assert result.value.code == 2
