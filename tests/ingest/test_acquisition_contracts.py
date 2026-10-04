"""Wholly synthetic raw acquisition replies through the real API worker.

These cases prove local request/normalization contracts, not live unread
preservation. Mentioned/bookmarked cross lists, structured values and group
consultations have their own wire contracts in test_cross_lists.py,
test_project_metadata.py and test_group_consultations.py.
test_fixture_synthetic.py lints this file and test_cross_lists.py only (reserved
id bands, SYNTHETIC strings); the other two use canary-style values instead.
Thread pagination omission records current compatibility, not proof that the
server returned every reply.
"""

import io
import json
import random
import urllib.error
import urllib.parse
from collections import Counter, deque
from datetime import datetime
from email.message import Message as Headers
from types import SimpleNamespace

import pytest

import mcs_adapter
import mcs_util
import mcs_worker


PID = 900000001
MID = 900000101
STAMP = 1900000000
DATE = "2026-10-03T00:00:00+09:00"


class Response(io.BytesIO):
    status = 200
    headers: dict[str, str] = {}


def raw_message(mid: int = MID, **extra):
    return {"id": mid, "comment": "SYNTHETIC BODY", "created_at": DATE,
            "user": {"id": 900000201, "last_name": "SYNTHETIC",
                     "first_name": "SYNTHETIC_AUTHOR"}, **extra}


def page(messages, has_next=False):
    return {"messages": messages, "paginate": {"has_next": has_next}}


def message_query(number=1, *, history=False, **extra):
    params: dict[str, int | str] = {
        "page": number, "per_page": 10, "keep_read_status": 1,
        "exclude_terminated_ex_application": 1}
    params.update({"sort": "pinned"} if history else {
        "unread": 1, "timestamp": STAMP, "include_meta": 1,
        "include_paginate_totals": 1})
    return dict(params, **extra)


@pytest.fixture
def replay(monkeypatch):
    """Replace only HTTP opening; keep adapter, worker and JSON parsing real."""
    def make(exchanges):
        pending = deque(exchanges)
        observed = []

        def open_response(request, timeout):
            assert pending, "unexpected acquisition request"
            path, query, reply = pending.popleft()
            url = urllib.parse.urlsplit(request.full_url)
            assert (url.scheme, url.netloc, url.path) == (
                "https", "www.medical-care.net", "/api/v2t" + path)
            assert urllib.parse.parse_qs(url.query) == {
                key: [str(value)] for key, value in query.items()}
            assert request.get_method() == "GET" and request.data is None
            assert request.get_header("Authorization") == "Bearer SYNTHETIC_TOKEN"
            assert request.get_header("Accept") == "application/json"
            assert timeout > 0
            observed.append(request)
            if type(reply) is int:
                raise urllib.error.HTTPError(
                    request.full_url, reply, "SYNTHETIC", Headers(), None)
            return Response(json.dumps(reply).encode())

        def opener(*handlers):
            assert handlers == (mcs_util.NoRedirect,)
            return SimpleNamespace(open=open_response)

        monkeypatch.setattr(mcs_util, "no_proxy_opener", opener)

        def worker(payload, timeout, deadline):
            assert payload["operation"] == "api"
            return mcs_worker._execute(dict(payload, timeout=timeout))

        adapter = mcs_adapter.MCSAdapter(worker=worker)
        adapter._token = "SYNTHETIC_TOKEN"
        return adapter, pending, observed

    return make


def test_snapshot_timestamp_reaches_every_unread_page_without_marking(replay):
    projects = [
        {"projects": [{"id": PID, "type": "medical", "is_unread": True}],
         "paginate": {"timestamp": STAMP, "has_next": True}},
        {"projects": [{"id": PID + 1, "type": "medical", "is_unread": False}],
         "paginate": {"timestamp": STAMP + 5, "has_next": False}},
    ]
    adapter, pending, observed = replay([
        ("/projects", {"per_page": 100, "page": i + 1, "include_meta": 1,
                       "include_paginate_totals": 0}, reply)
        for i, reply in enumerate(projects)
    ] + [
        (f"/projects/{PID}/messages", message_query(),
         page([raw_message(is_unread=True)], True)),
        (f"/projects/{PID}/messages", message_query(2),
         page([raw_message(MID + 1, is_unread=True)])),
    ])
    snapshot = adapter.list_unread()
    batch = adapter.fetch_unread_messages(
        snapshot.patients[0].project_id, snapshot.timestamp)
    assert snapshot.timestamp == STAMP
    assert [p.project_id for p in snapshot.patients] == [PID]
    assert batch.reached and batch.error is None and batch.pages == 2
    assert all(m.is_unread for m in batch.messages)
    assert not pending and len(observed) == 4


@pytest.mark.parametrize(("extra", "state", "body", "files_present"), [
    ({}, "unknown", "", False),
    ({"comment": None}, "unknown", "", False),
    ({"comment_snippet": "SYNTHETIC SNIPPET"}, "snippet",
     "SYNTHETIC SNIPPET", False),
    ({"comment": ""}, "full", "", False),
    ({"comment": "", "files": []}, "full", "", True),
    ({"comment": "", "files": None}, "full", "", True),
    ({"comment": "SYNTHETIC HTML <b>BODY</b>"}, "full",
     "SYNTHETIC HTML <b>BODY</b>", False),
    ({"comment": "SYNTHETIC OLD", "delete_user": {"id": 900000201}},
     "deleted", "", False),
])
def test_body_states_from_raw_reply(replay, extra, state, body, files_present):
    raw = raw_message()
    del raw["comment"]
    raw.update(extra)
    adapter, pending, _ = replay([
        (f"/projects/{PID}/messages", message_query(), page([raw]))])
    batch = adapter.fetch_unread_messages(PID, STAMP)
    assert batch.error is None and batch.reached
    message = batch.messages[0]
    assert (message.body_state, message.body_html, message.files_present) == (
        state, body, files_present)
    assert not pending


def test_reply_hydration_preserves_unread_attachments_and_tombstone(replay):
    root = raw_message(
        count={"thread_messages": 3},
        thread_messages=[
            raw_message(MID + i, comment=None, comment_snippet="SYNTHETIC",
                        is_unread=i < 3) for i in (1, 2, 3)])
    attachment = {"name": "SYNTHETIC.pdf",
                  "url": "https://www.medical-care.net/files/SYNTHETIC_FILE"}
    adapter, pending, _ = replay([
        (f"/projects/{PID}/messages", message_query(), page([root])),
        (f"/projects/{PID}/messages/{MID}/messages",
         {"keep_read_status": 1, "page": 1},
         page([raw_message(MID + 1, comment="", files=[attachment]),
               raw_message(MID + 2, delete_user={"id": 900000201}),
               raw_message(MID + 3)])),
    ])
    parent = adapter.fetch_unread_messages(PID, STAMP).messages[0]
    replies = adapter.fetch_unread_replies(parent)
    assert replies.missing == []
    assert {m.message_id for m in replies.messages} == {MID + 1, MID + 2}
    hydrated = {m.message_id: m for m in parent.replies}
    assert hydrated[MID + 1].body_state == "full"
    assert hydrated[MID + 2].body_state == "deleted"
    assert hydrated[MID + 3].body_state == "snippet"
    assert all(m.parent_id == MID and m.project_id == PID
               for m in hydrated.values())
    assert all(hydrated[mid].is_unread for mid in (MID + 1, MID + 2))
    file = hydrated[MID + 1].attachments[0]
    assert (file.file_id, file.name, file.url) == (
        "SYNTHETIC_FILE", attachment["name"], attachment["url"])
    assert hydrated[MID + 1].files_present and not pending


@pytest.mark.parametrize("history", [False, True])
@pytest.mark.parametrize("broken", [
    404, 401, {"messages": []},
    page([raw_message(MID + 1), {"id": False}]),
    page([raw_message(MID + 1, files={})]),
    page([raw_message(MID + 1, thread_messages=[{"id": 0}])]),
    {"messages": [], "paginate": {"has_next": "false"}},
])
def test_mid_page_failure_keeps_only_completed_pages(replay, history, broken):
    adapter, pending, _ = replay([
        (f"/projects/{PID}/messages", message_query(history=history),
         page([raw_message()], True)),
        (f"/projects/{PID}/messages", message_query(2, history=history), broken),
    ])
    batch = (adapter.fetch_history(PID, 0) if history
             else adapter.fetch_unread_messages(PID, STAMP))
    assert [m.message_id for m in batch.messages] == [MID]
    assert batch.pages == 1 and not batch.reached and not batch.capped
    assert batch.error.kind == (
        "http_error" if broken == 404 else
        "session_expired" if broken == 401 else "schema_error")
    assert not pending


@pytest.mark.parametrize("seed", [0, 13, 41])
@pytest.mark.parametrize("thread", [False, True])
def test_order_and_duplicate_page_boundaries_do_not_certify_tail(replay, seed, thread):
    rows = [raw_message(MID + i, created_at=(
        "2020-01-01T00:00:00+09:00" if i % 3 == 0 else DATE))
        for i in range(12)]
    random.Random(seed).shuffle(rows)
    rows.insert(5, rows[4].copy())  # overlapping page boundary
    chunks = [rows[:3], [], rows[3:5], rows[5:]]
    path = (f"/projects/{PID}/messages/{MID + 100}/messages" if thread
            else f"/projects/{PID}/messages")
    exchanges = [
        (path, {"keep_read_status": 1, "page": i + 1} if thread
         else message_query(i + 1, history=True), page(chunk, i < 3))
        for i, chunk in enumerate(chunks)
    ]
    adapter, pending, _ = replay(exchanges)
    cutoff = int(datetime.fromisoformat(DATE).timestamp()) - 1
    first = (adapter.fetch_thread_window(PID, MID + 100, max_pages=2)
             if thread else adapter.fetch_history(PID, cutoff, max_pages=2))
    assert first.pages == 2 and not first.reached and first.error is None
    rest = (adapter.fetch_thread_window(PID, MID + 100, start_page=3)
            if thread else adapter.fetch_history(PID, cutoff, start_page=3))
    assert rest.pages == 2 and rest.reached and rest.error is None
    actual = first.messages + rest.messages
    expected = rows if thread else [
        r for r in rows if r["created_at"] == DATE]
    expected_ids = []
    for row in expected:
        identity = row["id"]
        assert isinstance(identity, int)
        expected_ids.append(identity)
    if thread:
        assert {m.message_id for m in actual} == set(expected_ids)
        assert len(actual) == len(set(expected_ids))
    else:
        assert Counter(m.message_id for m in actual) == Counter(expected_ids)
    assert not pending


@pytest.mark.parametrize("paginate", ["absent", None, {}, {"has_next": 0}])
def test_thread_missing_pagination_compatibility_is_not_malformed_empty(replay, paginate):
    raw = {"messages": [raw_message(MID + 1)]}
    if paginate != "absent":
        raw["paginate"] = paginate
    adapter, pending, _ = replay([
        (f"/projects/{PID}/messages/{MID}/messages",
         {"keep_read_status": 1, "page": 1}, raw)])
    batch = adapter.fetch_thread_window(PID, MID)
    assert batch.reached is (paginate == "absent")
    assert (batch.error is None) is (paginate == "absent")
    assert len(batch.messages) == (1 if paginate == "absent" else 0)
    assert not pending


def test_thread_window_failure_retains_progress_and_resume_contract(replay):
    path = f"/projects/{PID}/messages/{MID}/messages"
    adapter, pending, _ = replay([
        (path, {"keep_read_status": 1, "page": 4},
         page([raw_message(MID + 1)], True)),
        (path, {"keep_read_status": 1, "page": 5}, 404),
        (path, {"keep_read_status": 1, "page": 5},
         page([raw_message(MID + 2)])),
    ])
    first = adapter.fetch_thread_window(PID, MID, start_page=4)
    assert first.pages == 1 and not first.reached
    assert first.error.kind == "http_error" and first.error.status == 404
    rest = adapter.fetch_thread_window(PID, MID, start_page=5)
    assert rest.reached and rest.error is None
    assert [m.message_id for m in first.messages + rest.messages] == [
        MID + 1, MID + 2]
    assert not pending


@pytest.mark.parametrize(("extra", "metadata", "errors"), [
    ({}, {}, []),
    ({"reactions": [], "mentions": [], "is_bookmarked": False, "is_pinned": False},
     {"reactions": [], "mentions": [], "is_bookmarked": False, "is_pinned": False}, []),
    ({"reactions": [{"type": "future_kind", "count": 0, "self_reacted": False}]},
     {"reactions": [{"type": "future_kind", "count": 0, "self_reacted": False}]}, []),
    ({"reactions": None, "mentions": None, "is_bookmarked": 0, "is_pinned": "false"},
     {}, ["reactions_invalid", "mentions_invalid", "is_bookmarked_invalid", "is_pinned_invalid"]),
])
def test_optional_unknown_empty_zero_and_invalid_are_distinct(replay, extra, metadata, errors):
    adapter, pending, _ = replay([
        (f"/projects/{PID}/messages", message_query(),
         page([raw_message(**extra)]))])
    message = adapter.fetch_unread_messages(PID, STAMP).messages[0]
    assert message.body_state == "full"
    assert message.metadata == metadata and message.metadata_errors == errors
    assert not pending


@pytest.mark.parametrize("parent", [None, MID])
def test_exact_metadata_raw_self_counts_mentions_and_flags(replay, parent):
    reactions = [
        {"type": kind, "count": i + 1, "self_reacted": i % 2 == 0}
        for i, kind in enumerate(("viewed", "accepted", "thanked", "good", "completed"))]
    mentions = [{"type": "user", "user": {"id": 900000201}},
                {"type": "station", "station": {"id": 900000301}},
                {"type": "project"}]
    path = (f"/projects/{PID}/messages" if parent is None
            else f"/projects/{PID}/messages/{parent}/messages")
    adapter, pending, _ = replay([
        (path, {"message_id": MID + 1, "per_page": 1, "keep_read_status": 1,
                "no_extend_session": 1},
         {"messages": [raw_message(MID + 1, project_id=PID, is_unread=True,
                                  reactions=reactions, mentions=mentions,
                                  is_bookmarked=True, is_pinned=True)]})])
    message = adapter.fetch_message_metadata(PID, MID + 1, parent_id=parent)
    assert message.parent_id == parent and message.is_unread
    assert message.metadata == {
        "reactions": reactions,
        "mentions": [{"type": "user", "id": 900000201},
                     {"type": "station", "id": 900000301},
                     {"type": "project", "id": PID}],
        "is_bookmarked": True, "is_pinned": True}
    assert message.metadata_errors == [] and not pending


@pytest.mark.parametrize("kind", [None, "viewed"])
@pytest.mark.parametrize("failure", [False, True])
def test_actor_raw_routes_keep_snapshot_and_partial_failure(replay, kind, failure):
    suffix = "user_reactions" if kind is None else "reactions"
    path = f"/messages/{MID}/{suffix}"
    key = "reactions" if kind is None else "users"
    queries = [{"page": i, "per_page": 1, "include_meta": int(i == 1),
                "include_paginate_totals": 0, "no_extend_session": 1}
               for i in (1, 2)]
    queries[1]["timestamp"] = STAMP
    if kind is not None:
        for query in queries:
            query["reaction_type"] = kind
    replies = []
    for i in (1, 2):
        user = {"id": 900000201 + i, "last_name": "SYNTHETIC",
                "specialist_categories": [{"name": "SYNTHETIC_PROFESSION"}],
                "stations": [{"name": "SYNTHETIC_ORGANIZATION"}],
                "icon_url": "https://invalid.example/synthetic-icon",
                "email": "synthetic@invalid.example"}
        replies.append({
            key: [{"reaction_type": "viewed", "user": user}] if kind is None else [user],
            "paginate": {"has_next": i == 1, "timestamp": STAMP}})
    replies[0]["message"] = {"id": MID, "project_id": PID, "reactions": [
        {"type": "viewed", "count": 2, "self_reacted": False}]}
    adapter, pending, _ = replay([
        (path, queries[0], replies[0]),
        (path, queries[1], 404 if failure else replies[1])])
    result = (adapter.fetch_reaction_actors(PID, MID, per_page=1) if kind is None
              else mcs_adapter.walk_reaction_actors(
                  adapter._get, PID, MID, reaction_type=kind, per_page=1))
    assert result["complete"] is (not failure)
    assert len(result["actors"]) == (1 if failure else 2)
    assert all(set(actor) == {"actor_id", "reaction_type", "profession",
                              "name", "organization"}
               for actor in result["actors"])
    assert all(actor["name"] == "SYNTHETIC"
               and actor["organization"] == "SYNTHETIC_ORGANIZATION"
               and actor["profession"] == "SYNTHETIC_PROFESSION"
               for actor in result["actors"])
    if failure:
        assert result["error"] == "http_error" and result["status"] == 404
    else:
        assert result["counts_match_message"] and result["timestamp_stable"]
    assert not pending


@pytest.mark.parametrize("archived", [False, True])
def test_available_horizontal_inventory_raw_requests(replay, archived):
    path, key = ("/kartes", "kartes") if archived else ("/projects", "projects")
    row = ({"id": 900000401, "medical_project": {"id": PID}}
           if archived else {"id": PID, "type": "medical"})
    queries = [{"page": i, "per_page": 7, "include_paginate_totals": 0}
               for i in (1, 2)]
    if archived:
        for query in queries:
            query["is_archived"] = 1
    adapter, pending, _ = replay([
        (path, queries[0], {key: [row], "paginate": {"has_next": True}}),
        (path, queries[1], {key: [], "paginate": {"has_next": False}})])
    inventory = (adapter.list_archived_kartes(per_page=7) if archived
                 else adapter.list_projects(per_page=7))
    assert [patient.project_id for patient in inventory] == [PID]
    assert not pending


@pytest.mark.parametrize("history", [False, True])
@pytest.mark.parametrize("reply", [
    page([]), {}, {"messages": None, "paginate": {"has_next": False}},
    {"messages": [], "paginate": {}},
])
def test_empty_message_collection_requires_terminal_page_evidence(replay, history, reply):
    adapter, pending, _ = replay([
        (f"/projects/{PID}/messages", message_query(history=history), reply)])
    batch = (adapter.fetch_history(PID, 0) if history
             else adapter.fetch_unread_messages(PID, STAMP))
    valid = reply == page([])
    assert batch.messages == [] and batch.reached is valid
    assert batch.pages == int(valid)
    assert (batch.error is None) is valid
    if not valid:
        assert batch.error.kind == "schema_error"
    assert not pending


@pytest.mark.parametrize("timestamp", [None, 0, True, "1900000000"])
def test_unknown_snapshot_timestamp_never_becomes_zero_success(replay, timestamp):
    adapter, pending, _ = replay([
        ("/projects", {"per_page": 100, "page": 1, "include_meta": 1,
                       "include_paginate_totals": 0},
         {"projects": [], "paginate": {
             "timestamp": timestamp, "has_next": False}})])
    with pytest.raises(mcs_adapter.SchemaError):
        adapter.list_unread()
    assert not pending


def test_recent_embedded_reply_keeps_old_history_parent(replay):
    root = raw_message(
        created_at="2020-01-01T00:00:00+09:00",
        thread_messages=[raw_message(MID + 1, comment=None,
                                     comment_snippet="SYNTHETIC RECENT")])
    adapter, pending, _ = replay([
        (f"/projects/{PID}/messages", message_query(history=True), page([root]))])
    cutoff = int(datetime.fromisoformat(DATE).timestamp()) - 1
    batch = adapter.fetch_history(PID, cutoff)
    assert batch.reached and batch.error is None
    assert [m.message_id for m in batch.messages] == [MID]
    assert batch.messages[0].replies[0].parent_id == MID
    assert not pending


def test_latest_raw_probe_has_no_mark_request_but_no_unread_proof(replay):
    adapter, pending, observed = replay([
        (f"/projects/{PID}/messages/latest", {},
         {"is_self_only": True, "message": {"id": MID}})])
    assert adapter.fetch_latest(PID) == {"message_id": MID, "is_self_only": True}
    assert len(observed) == 1 and not pending
    # This reader sends no keep_read_status. No synthetic server state
    # can establish the real endpoint's absence of read/viewed side effects.


@pytest.mark.parametrize("reader", ["unread", "history", "thread",
                                    "embedded_unread", "embedded_history"])
@pytest.mark.parametrize("project_id", [PID + 1, False, str(PID), None])
def test_conflicting_message_project_does_not_relabel_a_completed_page(replay, reader, project_id):
    # The endpoint's project cannot replace contradictory response scope.
    history = reader in ("history", "embedded_history")
    thread = reader == "thread"
    path = (f"/projects/{PID}/messages/{MID + 100}/messages" if thread
            else f"/projects/{PID}/messages")
    first = raw_message(project_id=PID)
    bad = raw_message(MID + 1, project_id=project_id)
    if reader.startswith("embedded_"):
        bad = raw_message(MID + 2, project_id=PID, thread_messages=[bad])
    adapter, pending, _ = replay([
        (path, {"keep_read_status": 1, "page": 1} if thread
         else message_query(history=history), page([first], True)),
        (path, {"keep_read_status": 1, "page": 2} if thread
         else message_query(2, history=history), page([bad])),
    ])
    batch = (adapter.fetch_thread_window(PID, MID + 100) if thread else
             adapter.fetch_history(PID, 0) if history else
             adapter.fetch_unread_messages(PID, STAMP))
    assert batch.error is not None and batch.error.kind == "schema_error"
    assert not batch.reached and batch.pages == 1
    assert [m.message_id for m in batch.messages] == [MID]
    assert not pending


@pytest.mark.parametrize("delay", ["nan", "inf"])
def test_initial_import_nonfinite_delay_rejected_before_writer_lock(monkeypatch, delay):
    import sys
    import init_data

    monkeypatch.setattr(sys, "argv", ["init_data", "--delay", delay])
    monkeypatch.setattr(init_data, "acquire_run_lock", lambda *args:
                        pytest.fail("invalid delay reached writer lock"))
    monkeypatch.setattr(init_data, "MCSAdapter", lambda *args, **kwargs:
                        pytest.fail("invalid delay reached authentication"))
    with pytest.raises(SystemExit) as error:
        init_data.main()
    assert error.value.code == 2
