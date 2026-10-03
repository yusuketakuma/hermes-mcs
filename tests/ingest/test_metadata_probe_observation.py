"""Synthetic raw unread observations before and after exact metadata GETs."""

import copy
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from mcs_adapter import MCSError, SchemaError, SessionExpired


PROJECT_ID = 700001
ROOT_ID = 900001
CHILD_ID = 900002
BEFORE_TIMESTAMP = 1234598761
AFTER_TIMESTAMP = 1234598762
BODY = "synthetic unread body canary"
NAME = "synthetic unread actor canary"
MISSING = object()


@pytest.fixture(scope="module")
def probe_module():
    spec = importlib.util.spec_from_file_location(
        "metadata_probe_observation",
        Path(__file__).resolve().parents[2]
        / "scripts/development/probe_message_metadata.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def raw_message(message_id=ROOT_ID, unread=True, *, replies=None):
    row = {"id": message_id, "project_id": PROJECT_ID,
           "comment": BODY, "user": {"id": 800001, "name": NAME}}
    if unread is not MISSING:
        row["is_unread"] = unread
    if replies is not None:
        row["thread_messages"] = replies
    return row


def unread_page(messages, *, has_next=False, page=1, total_entries=None):
    return {"messages": messages,
            "paginate": {"has_next": has_next, "current_page": page, "per_page": 50,
                         "total_entries": len(messages) if total_entries is None else total_entries}}


def project_page(*, unread=True, timestamp=BEFORE_TIMESTAMP, projects=None, has_next=False):
    return {"projects": [{"id": PROJECT_ID, "is_unread": unread}] if projects is None else projects,
            "paginate": {"timestamp": timestamp, "has_next": has_next}}


class ObservationAdapter:
    def __init__(self, responses, *, normalized_unread=True, normalized_reactions=None):
        self.responses = copy.deepcopy(responses)
        self.events = []
        self.deadlines = []
        self.message = SimpleNamespace(
            is_unread=normalized_unread,
            metadata={"reactions": [] if normalized_reactions is None else normalized_reactions},
            metadata_errors=[])

    def _get(self, path, params, *, extend_session):
        self.events.append(("get", path, dict(params), extend_session))
        assert extend_session is False
        assert "increment_count" not in params
        assert self.responses, "unexpected GET beyond synthetic observation sequence"
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def fetch_message_metadata(self, project_id, message_id, **kwargs):
        self.events.append(("metadata", project_id, message_id, kwargs))
        return self.message

    def _read_cache(self):
        return "synthetic-cache-only"

    def set_deadline(self, deadline):
        self.deadlines.append(deadline)

    def __getattr__(self, name):
        pytest.fail(f"unexpected adapter operation: {name}")


def observe(probe_module, responses, *, reply=False):
    adapter = ObservationAdapter(responses)
    kwargs = {"parent_id": ROOT_ID} if reply else {}
    value = probe_module._target_unread_state(
        adapter, PROJECT_ID, CHILD_ID if reply else ROOT_ID, BEFORE_TIMESTAMP, **kwargs)
    return adapter, value


def assert_safe_report(report):
    encoded = json.dumps(report)
    for canary in (BODY, NAME, str(PROJECT_ID), str(ROOT_ID), str(CHILD_ID),
                   str(BEFORE_TIMESTAMP), str(AFTER_TIMESTAMP), "800001"):
        assert canary not in encoded
    assert report["post_or_mark_read_called"] is False


@pytest.mark.parametrize("reply", [False, True])
@pytest.mark.parametrize("flag,expected", [(True, True), (False, False), (MISSING, None)])
def test_target_requires_its_own_explicit_boolean_unread_flag(probe_module, reply, flag, expected):
    target = raw_message(CHILD_ID if reply else ROOT_ID, flag)
    rows = [raw_message(ROOT_ID, True, replies=[target])] if reply else [target]
    adapter, value = observe(probe_module, [unread_page(rows)], reply=reply)
    assert value is expected
    assert adapter.events == [("get", f"/projects/{PROJECT_ID}/messages", {
        "unread": 1, "timestamp": BEFORE_TIMESTAMP, "keep_read_status": 1,
        "include_meta": 1, "exclude_terminated_ex_application": 1,
        "include_paginate_totals": 1, "per_page": 50, "page": 1}, False)]


@pytest.mark.parametrize("reply", [False, True])
@pytest.mark.parametrize("flag", [None, 0, 1, "true"])
def test_explicit_invalid_raw_flag_is_rejected(probe_module, reply, flag):
    target = raw_message(CHILD_ID if reply else ROOT_ID, flag)
    rows = [raw_message(ROOT_ID, True, replies=[target])] if reply else [target]
    with pytest.raises(SchemaError):
        observe(probe_module, [unread_page(rows)], reply=reply)


def test_root_and_reply_flags_do_not_substitute_for_each_other(probe_module):
    parent = raw_message(ROOT_ID, False, replies=[raw_message(CHILD_ID, True)])
    _, root = observe(probe_module, [unread_page([parent])])
    _, reply = observe(probe_module, [unread_page([parent])], reply=True)
    assert root is False and reply is True
    parent = raw_message(ROOT_ID, True, replies=[raw_message(CHILD_ID, False)])
    _, reply = observe(probe_module, [unread_page([parent])], reply=True)
    assert reply is False


@pytest.mark.parametrize("reply", [False, True])
def test_missing_target_is_unknown_even_when_another_post_is_unread(probe_module, reply):
    _, value = observe(probe_module, [unread_page([raw_message(ROOT_ID + 10, True)])], reply=reply)
    assert value is None


def test_reply_missing_under_the_selected_parent_is_unknown(probe_module):
    rows = [raw_message(ROOT_ID, True, replies=[raw_message(CHILD_ID + 10, True)])]
    _, value = observe(probe_module, [unread_page(rows)], reply=True)
    assert value is None


def test_reply_under_a_different_explicit_parent_is_rejected(probe_module):
    rows = [raw_message(ROOT_ID + 10, True, replies=[raw_message(CHILD_ID, True)])]
    with pytest.raises(SchemaError):
        observe(probe_module, [unread_page(rows)], reply=True)


@pytest.mark.parametrize("reply", [False, True])
def test_target_found_before_incomplete_walk_is_still_unknown(probe_module, reply):
    target = raw_message(ROOT_ID, True, replies=[raw_message(CHILD_ID, True)] if reply else None)
    pages = [unread_page([target], has_next=True, total_entries=3),
             unread_page([raw_message(ROOT_ID + 10)], has_next=True, page=2, total_entries=3)]
    adapter, value = observe(probe_module, pages, reply=reply)
    assert value is None and len(adapter.events) == 2
    assert [event[2]["page"] for event in adapter.events] == [1, 2]
    assert all(event[2]["timestamp"] == BEFORE_TIMESTAMP for event in adapter.events)


@pytest.mark.parametrize("count,expected", [(79, True), (80, None)])
def test_unread_screen_cap_does_not_become_complete_observation(probe_module, count, expected):
    rows = [raw_message(ROOT_ID + offset) for offset in range(count)]
    pages = [unread_page(rows[:50], has_next=True, total_entries=count),
             unread_page(rows[50:], page=2, total_entries=count)]
    _, value = observe(probe_module, pages)
    assert value is expected


@pytest.mark.parametrize("reply", [False, True])
def test_partial_total_is_unknown_despite_terminal_flag(probe_module, reply):
    target = raw_message(ROOT_ID, True, replies=[raw_message(CHILD_ID, True)] if reply else None)
    _, value = observe(probe_module, [unread_page([target], total_entries=2)], reply=reply)
    assert value is None


@pytest.mark.parametrize("error", [MCSError("http_error", BODY, status=429),
                                  SessionExpired(BODY, status=401),
                                  MCSError("http_error", BODY, status=404)])
def test_midwalk_error_stops_observation_instead_of_claiming_preservation(probe_module, error):
    adapter = ObservationAdapter([unread_page([raw_message()], has_next=True, total_entries=2), error])
    with pytest.raises(MCSError) as raised:
        probe_module._target_unread_state(adapter, PROJECT_ID, ROOT_ID, BEFORE_TIMESTAMP)
    assert raised.value.kind == error.kind and raised.value.status == error.status
    assert len(adapter.events) == 2


@pytest.mark.parametrize("reply", [False, True])
@pytest.mark.parametrize("field,value", [("id", None), ("id", True), ("id", 1.0), ("id", 0),
                                         ("id", 2**63), ("project_id", True),
                                         ("project_id", 1.0), ("project_id", PROJECT_ID + 1)])
def test_invalid_raw_identity_or_project_is_rejected(probe_module, reply, field, value):
    target = raw_message(CHILD_ID if reply else ROOT_ID)
    if field == "id" and value is None:
        del target[field]
    else:
        target[field] = value
    rows = [raw_message(ROOT_ID, True, replies=[target])] if reply else [target]
    with pytest.raises(SchemaError):
        observe(probe_module, [unread_page(rows)], reply=reply)


@pytest.mark.parametrize("defect", ["root_duplicate", "page_duplicate", "child_duplicate",
                                    "root_child_collision", "page_cycle", "page_reverse", "total_drift"])
def test_duplicate_or_cycling_unread_pages_stop_without_an_exact_get(probe_module, defect):
    target = raw_message()
    pages = [unread_page([target])]
    if defect == "root_duplicate":
        pages[0] = unread_page([target, target])
    elif defect == "child_duplicate":
        pages[0] = unread_page([raw_message(replies=[raw_message(CHILD_ID), raw_message(CHILD_ID)])])
    elif defect == "root_child_collision":
        pages[0] = unread_page([raw_message(replies=[raw_message(ROOT_ID)])])
    else:
        pages = [unread_page([target], has_next=True, total_entries=2),
                 unread_page([raw_message(ROOT_ID + 10)], page=2, total_entries=2)]
        if defect == "page_duplicate":
            pages[1]["messages"] = [target]
        elif defect == "page_cycle":
            pages[1]["paginate"]["current_page"] = 1
        elif defect == "page_reverse":
            pages[1]["paginate"]["current_page"] = 0
        else:
            pages[1]["paginate"]["total_entries"] = 3
    adapter = ObservationAdapter(pages)
    with pytest.raises(SchemaError):
        probe_module._target_unread_state(adapter, PROJECT_ID, ROOT_ID, BEFORE_TIMESTAMP)
    assert all(event[0] == "get" and event[2].get("unread") == 1 for event in adapter.events)


@pytest.mark.parametrize("field,value", [("has_next", None), ("has_next", 0),
                                         ("current_page", True), ("current_page", 1.0),
                                         ("current_page", 2), ("per_page", True),
                                         ("per_page", 50.0), ("per_page", 1),
                                         ("total_entries", True), ("total_entries", 1.0),
                                         ("total_entries", -1), ("total_pages", True),
                                         ("total_pages", 1.0), ("total_pages", -1),
                                         ("total_pages", 0), ("total_pages", 2)])
def test_invalid_observation_pagination_fails_closed(probe_module, field, value):
    page = unread_page([raw_message()])
    page["paginate"][field] = value
    with pytest.raises(SchemaError):
        observe(probe_module, [page])


@pytest.mark.parametrize("total_pages", [0, 1])
def test_first_empty_unread_page_remains_unknown(probe_module, total_pages):
    page = unread_page([])
    page["paginate"]["total_pages"] = total_pages
    adapter, value = observe(probe_module, [page])
    assert value is None and len(adapter.events) == 1


@pytest.mark.parametrize("reply", [False, True])
def test_late_matching_terminal_total_pages_preserves_complete_observation(probe_module, reply):
    target = raw_message(replies=[raw_message(CHILD_ID)]) if reply else raw_message()
    pages = [unread_page([target], has_next=True, total_entries=2),
             unread_page([raw_message(ROOT_ID + 10)], page=2, total_entries=2)]
    pages[1]["paginate"]["total_pages"] = 2
    adapter, value = observe(probe_module, pages, reply=reply)
    assert value is True and len(adapter.events) == 2


@pytest.mark.parametrize("defect", ["missing_rows", "rows_object", "row_scalar", "missing_paginate",
                                    "too_many_rows", "threads_object", "thread_scalar", "empty_nonterminal"])
def test_invalid_observation_collections_are_not_empty_success(probe_module, defect):
    page = unread_page([raw_message()])
    if defect == "missing_rows":
        del page["messages"]
    elif defect == "rows_object":
        page["messages"] = {}
    elif defect == "row_scalar":
        page["messages"] = [None]
    elif defect == "missing_paginate":
        del page["paginate"]
    elif defect == "too_many_rows":
        page["messages"] = [raw_message(ROOT_ID + offset) for offset in range(51)]
    elif defect == "threads_object":
        page["messages"][0]["thread_messages"] = {}
    elif defect == "thread_scalar":
        page["messages"][0]["thread_messages"] = [None]
    else:
        page["messages"] = []
        page["paginate"]["has_next"] = True
    with pytest.raises(SchemaError):
        observe(probe_module, [page])


def probe_sequence(*, reply=False, before_target=True, after_target=True,
                   before_room=True, after_room=True, exact_unread=True):
    mid = CHILD_ID if reply else ROOT_ID
    before = raw_message(ROOT_ID, before_target)
    after = raw_message(ROOT_ID, after_target)
    if reply:
        before = raw_message(ROOT_ID, True, replies=[raw_message(CHILD_ID, before_target)])
        after = raw_message(ROOT_ID, True, replies=[raw_message(CHILD_ID, after_target)])
    exact = raw_message(mid, exact_unread)
    exact["reactions"] = []
    return [project_page(unread=before_room), unread_page([before]), {"messages": [exact]},
            project_page(unread=after_room, timestamp=AFTER_TIMESTAMP), unread_page([after])]


@pytest.mark.parametrize("reply", [False, True])
def test_probe_proves_unread_preservation_only_from_independent_target_observations(probe_module, reply):
    adapter = ObservationAdapter(probe_sequence(reply=reply))
    kwargs = {"parent_id": ROOT_ID} if reply else {}
    report = probe_module.probe(adapter, PROJECT_ID, CHILD_ID if reply else ROOT_ID, **kwargs)
    assert report["unread_preservation_proven"] is True
    assert report["target_unread_at_start"] is True and report["target_unread_after_gets"] is True
    assert report["target_read_state_unchanged"] is True
    assert report["viewed_unchanged_between_gets"] is True
    assert report["first_get_viewed_effect"] == "unproven"
    assert [event[0] for event in adapter.events] == ["get", "get", "get", "metadata", "get", "get"]
    before_projects, before_target, exact, normalized, after_projects, after_target = adapter.events
    for event in (before_projects, after_projects):
        assert event[1] == "/projects" and event[2] == {
            "page": 1, "per_page": 100, "include_meta": 1, "include_paginate_totals": 0}
    assert before_target[2]["timestamp"] == BEFORE_TIMESTAMP
    assert after_target[2]["timestamp"] == AFTER_TIMESTAMP
    assert exact[1] == (f"/projects/{PROJECT_ID}/messages/{ROOT_ID}/messages" if reply else
                        f"/projects/{PROJECT_ID}/messages")
    assert exact[2] == {"message_id": CHILD_ID if reply else ROOT_ID, "per_page": 1, "keep_read_status": 1}
    assert normalized[3] == kwargs
    assert not adapter.responses
    assert_safe_report(report)


@pytest.mark.parametrize("reply", [False, True])
@pytest.mark.parametrize("phase", ["before", "after"])
def test_cli_late_smaller_terminal_total_cannot_prove_unread_preservation(
        probe_module, monkeypatch, capsys, reply, phase):
    sequence = probe_sequence(reply=reply)
    index = 1 if phase == "before" else 4
    sequence[index]["paginate"].update(has_next=True, total_entries=2)
    terminal = unread_page([raw_message(ROOT_ID + 10)], page=2, total_entries=2)
    terminal["paginate"]["total_pages"] = 1
    sequence.insert(index + 1, terminal)
    adapter = ObservationAdapter(sequence)
    monkeypatch.setattr(probe_module, "MCSAdapter", lambda **kwargs: adapter)
    argv = ["probe", "--read-only-target", "--project-id", str(PROJECT_ID),
            "--message-id", str(CHILD_ID if reply else ROOT_ID)]
    if reply:
        argv.extend(["--parent-id", str(ROOT_ID)])
    monkeypatch.setattr(sys, "argv", argv)
    assert probe_module.main() == 1
    encoded = capsys.readouterr().out
    assert json.loads(encoded) == {"error": "schema_error"}
    assert BODY not in encoded and NAME not in encoded
    assert str(PROJECT_ID) not in encoded and str(ROOT_ID) not in encoded
    assert len(adapter.events) == (3 if phase == "before" else 7)
    if phase == "before":
        assert all(event[0] == "get" for event in adapter.events)
        assert all("message_id" not in event[2] for event in adapter.events)


@pytest.mark.parametrize("reply", [False, True])
@pytest.mark.parametrize("before_target,after_target", [
    (True, False), (False, False), (False, True), (MISSING, True),
    (True, MISSING), (MISSING, MISSING),
])
def test_unknown_changed_or_previously_read_target_cannot_prove_unread_preservation(
        probe_module, reply, before_target, after_target):
    adapter = ObservationAdapter(probe_sequence(reply=reply, before_target=before_target,
                                                after_target=after_target), normalized_unread=False)
    kwargs = {"parent_id": ROOT_ID} if reply else {}
    report = probe_module.probe(adapter, PROJECT_ID, CHILD_ID if reply else ROOT_ID, **kwargs)
    assert report["unread_preservation_proven"] is False
    if type(before_target) is not bool or type(after_target) is not bool:
        assert report["target_read_state_unchanged"] is None
    else:
        assert report["target_read_state_unchanged"] is (before_target == after_target)
    assert_safe_report(report)


@pytest.mark.parametrize("before_room,after_room", [(False, False), (True, False), (False, True)])
def test_room_read_or_changed_state_does_not_prove_unread_preservation(probe_module, before_room, after_room):
    adapter = ObservationAdapter(probe_sequence(before_room=before_room, after_room=after_room,
                                                before_target=before_room, after_target=after_room))
    report = probe_module.probe(adapter, PROJECT_ID, ROOT_ID)
    assert report["unread_preservation_proven"] is False
    assert report["read_state_unchanged"] is (before_room == after_room)
    assert_safe_report(report)


@pytest.mark.parametrize("timestamp", [None, True, 1.0, 0, -1])
def test_invalid_initial_project_timestamp_stops_before_any_message_get(probe_module, timestamp):
    projects = project_page(timestamp=timestamp)
    if timestamp is None:
        del projects["paginate"]["timestamp"]
    adapter = ObservationAdapter([projects])
    with pytest.raises(SchemaError):
        probe_module.probe(adapter, PROJECT_ID, ROOT_ID)
    assert len(adapter.events) == 1 and adapter.events[0][1] == "/projects"


@pytest.mark.parametrize("error", [SessionExpired(BODY, status=401), MCSError("http_error", BODY, status=429)])
def test_initial_raw_observation_error_prevents_exact_subject_get(probe_module, error):
    adapter = ObservationAdapter([project_page(), error])
    with pytest.raises(MCSError):
        probe_module.probe(adapter, PROJECT_ID, ROOT_ID)
    assert len(adapter.events) == 2
    assert adapter.events[1][2]["unread"] == 1


@pytest.mark.parametrize("field,value", [("id", ROOT_ID + 10), ("id", True),
                                         ("project_id", PROJECT_ID + 10), ("project_id", True)])
def test_exact_raw_mismatch_stops_before_normalized_fetch(probe_module, field, value):
    sequence = probe_sequence()
    sequence[2]["messages"][0][field] = value
    adapter = ObservationAdapter(sequence)
    with pytest.raises(SchemaError):
        probe_module.probe(adapter, PROJECT_ID, ROOT_ID)
    assert len(adapter.events) == 3 and all(event[0] == "get" for event in adapter.events)


def test_second_snapshot_invalid_timestamp_cannot_reuse_the_first_snapshot(probe_module):
    sequence = probe_sequence()
    del sequence[3]["paginate"]["timestamp"]
    adapter = ObservationAdapter(sequence)
    with pytest.raises(SchemaError):
        probe_module.probe(adapter, PROJECT_ID, ROOT_ID)
    assert len(adapter.events) == 5
    assert sum(event[0] == "get" and event[2].get("unread") == 1 for event in adapter.events) == 1


def test_room_read_flag_without_a_target_observation_is_not_target_read_proof(probe_module):
    adapter = ObservationAdapter(probe_sequence(before_room=False, after_room=False,
                                                before_target=MISSING, after_target=MISSING,
                                                exact_unread=False), normalized_unread=False)
    report = probe_module.probe(adapter, PROJECT_ID, ROOT_ID)
    assert report["unread_at_start"] is False
    assert report["target_unread_at_start"] is None and report["target_unread_after_gets"] is None
    assert report["target_read_state_unchanged"] is None
    assert report["unread_preservation_proven"] is False
    assert_safe_report(report)


@pytest.mark.parametrize("field,value", [("current_page", 2), ("current_page", True),
                                         ("current_page", 1.0), ("per_page", 50),
                                         ("per_page", True), ("per_page", 100.0)])
def test_project_page_echo_must_match_requested_paging(probe_module, field, value):
    projects = project_page()
    projects["paginate"][field] = value
    adapter = ObservationAdapter([projects])
    with pytest.raises(SchemaError):
        probe_module.probe(adapter, PROJECT_ID, ROOT_ID)
    assert len(adapter.events) == 1 and adapter.events[0][1] == "/projects"


@pytest.mark.parametrize("defect", ["duplicate", "cycle", "missing_target", "invalid_id",
                                    "invalid_flag", "empty_nonterminal", "oversized"])
def test_project_inventory_must_be_valid_and_complete_before_subject_observation(probe_module, defect):
    first = project_page()
    responses = [first]
    if defect == "duplicate":
        first["projects"].append(copy.deepcopy(first["projects"][0]))
    elif defect == "cycle":
        first["paginate"]["has_next"] = True
        responses.append(copy.deepcopy(first))
    elif defect == "missing_target":
        first["projects"][0]["id"] += 10
    elif defect == "invalid_id":
        first["projects"][0]["id"] = True
    elif defect == "invalid_flag":
        first["projects"][0]["is_unread"] = None
    elif defect == "empty_nonterminal":
        first["projects"] = []
        first["paginate"]["has_next"] = True
    else:
        first["projects"] = [{"id": PROJECT_ID + offset, "is_unread": True}
                             for offset in range(101)]
    adapter = ObservationAdapter(responses)
    with pytest.raises(SchemaError):
        probe_module.probe(adapter, PROJECT_ID, ROOT_ID)
    assert all(event[0] == "get" and event[1] == "/projects" for event in adapter.events)


def test_project_inventory_cap_does_not_accept_a_target_found_on_its_first_page(probe_module):
    responses = [project_page(projects=[{"id": PROJECT_ID + page, "is_unread": True}], has_next=True)
                 for page in range(5)]
    adapter = ObservationAdapter(responses)
    with pytest.raises(SchemaError):
        probe_module.probe(adapter, PROJECT_ID, ROOT_ID)
    assert len(adapter.events) == 5
    assert [event[2]["page"] for event in adapter.events] == [1, 2, 3, 4, 5]
    assert all(event[1] == "/projects" for event in adapter.events)


def test_complete_project_inventories_use_their_own_minimum_server_timestamp(probe_module):
    base = probe_sequence()
    before = [project_page(timestamp=BEFORE_TIMESTAMP + 1, has_next=True),
              project_page(timestamp=BEFORE_TIMESTAMP, projects=[{"id": PROJECT_ID + 10, "is_unread": False}])]
    after = [project_page(timestamp=AFTER_TIMESTAMP + 1, has_next=True),
             project_page(timestamp=AFTER_TIMESTAMP, projects=[{"id": PROJECT_ID + 10, "is_unread": False}])]
    adapter = ObservationAdapter(before + base[1:3] + after + base[4:])
    report = probe_module.probe(adapter, PROJECT_ID, ROOT_ID)
    assert report["unread_preservation_proven"] is True
    observations = [event for event in adapter.events if event[0] == "get" and event[2].get("unread") == 1]
    assert [event[2]["timestamp"] for event in observations] == [BEFORE_TIMESTAMP, AFTER_TIMESTAMP]
    assert_safe_report(report)


@pytest.mark.parametrize("after_timestamp,valid", [(BEFORE_TIMESTAMP - 1, False), (BEFORE_TIMESTAMP, True)])
def test_fresh_project_get_can_reuse_a_clock_tick_but_not_move_backwards(probe_module, after_timestamp, valid):
    sequence = probe_sequence()
    sequence[3]["paginate"]["timestamp"] = after_timestamp
    adapter = ObservationAdapter(sequence)
    if valid:
        report = probe_module.probe(adapter, PROJECT_ID, ROOT_ID)
        assert report["unread_preservation_proven"] is True
    else:
        with pytest.raises(SchemaError):
            probe_module.probe(adapter, PROJECT_ID, ROOT_ID)
    assert sum(event[0] == "get" and event[1] == "/projects" for event in adapter.events) == 2


@pytest.mark.parametrize("before_room,after_room", [(False, True), (True, False)])
def test_explicit_target_unread_in_a_read_room_is_a_contradiction(probe_module, before_room, after_room):
    adapter = ObservationAdapter(probe_sequence(before_room=before_room, after_room=after_room))
    with pytest.raises(SchemaError):
        probe_module.probe(adapter, PROJECT_ID, ROOT_ID)


@pytest.mark.parametrize("keyword,value", [("project_id", True), ("message_id", 1.0),
                                          ("timestamp", None), ("timestamp", True),
                                          ("timestamp", 0), ("parent_id", ROOT_ID)])
def test_invalid_observation_arguments_never_issue_a_get(probe_module, keyword, value):
    kwargs = {"project_id": PROJECT_ID, "message_id": ROOT_ID,
              "timestamp": BEFORE_TIMESTAMP, keyword: value}
    adapter = ObservationAdapter([])
    with pytest.raises(SchemaError):
        probe_module._target_unread_state(adapter, **kwargs)
    assert adapter.events == []


@pytest.mark.parametrize("raw_target,normalized_unread,expected_exit", [(True, True, 0),
                                                                      (MISSING, False, 2)])
def test_cli_uses_independent_observations_and_keeps_first_viewed_effect_unproven(
        probe_module, monkeypatch, capsys, raw_target, normalized_unread, expected_exit):
    adapter = ObservationAdapter(probe_sequence(before_target=raw_target, after_target=raw_target),
                                 normalized_unread=normalized_unread)
    monkeypatch.setattr(probe_module, "MCSAdapter", lambda **kwargs: adapter)
    monkeypatch.setattr(sys, "argv", ["probe", "--read-only-target", "--project-id", str(PROJECT_ID),
                                     "--message-id", str(ROOT_ID)])
    assert probe_module.main() == expected_exit
    report = json.loads(capsys.readouterr().out)
    assert report["unread_preservation_proven"] is (raw_target is True)
    assert report["first_get_viewed_effect"] == "unproven"
    assert report["viewed_unchanged_between_gets"] is True
    assert len(adapter.deadlines) == 1
    assert_safe_report(report)


def test_cli_error_output_retains_only_safe_error_kind(probe_module, monkeypatch, capsys):
    adapter = ObservationAdapter([project_page(), SessionExpired(BODY, status=401)])
    monkeypatch.setattr(probe_module, "MCSAdapter", lambda **kwargs: adapter)
    monkeypatch.setattr(sys, "argv", ["probe", "--read-only-target", "--project-id", str(PROJECT_ID),
                                     "--message-id", str(ROOT_ID)])
    assert probe_module.main() == 1
    encoded = capsys.readouterr().out
    assert json.loads(encoded) == {"error": "session_expired"}
    assert BODY not in encoded and str(PROJECT_ID) not in encoded
    assert len(adapter.events) == 2


def test_cli_read_room_with_unknown_raw_target_defers_all_unverified_routes(probe_module, monkeypatch, capsys):
    adapter = ObservationAdapter(probe_sequence(before_room=False, after_room=False,
                                                before_target=MISSING, after_target=MISSING,
                                                exact_unread=False), normalized_unread=False)
    monkeypatch.setattr(probe_module, "MCSAdapter", lambda **kwargs: adapter)
    monkeypatch.setattr(probe_module, "probe_actor_pages", lambda *args, **kwargs:
                        pytest.fail("unknown raw target must defer actors"))
    monkeypatch.setattr(sys, "argv", ["probe", "--read-only-target", "--project-id", str(PROJECT_ID),
                                     "--message-id", str(ROOT_ID), "--actor-pages", "--extended-contracts"])
    assert probe_module.main() == 2
    report = json.loads(capsys.readouterr().out)
    deferred = {"state": "deferred", "reason": "unverified_routes_on_unread_target"}
    assert report["actor_pages"] == deferred and report["extended_contracts"] == deferred
    assert len(adapter.events) == 6
    assert_safe_report(report)


@pytest.mark.parametrize("first_extra,second_errors,expected", [
    ({"mentions": {}}, [], ["mentions_invalid"]),
    ({"is_bookmarked": "true"}, [], ["is_bookmarked_invalid"]),
    ({"mentions": {}}, ["mentions_invalid", "is_pinned_invalid"], ["is_pinned_invalid", "mentions_invalid"]),
    ({}, ["mentions_invalid"], ["mentions_invalid"]),
])
def test_schema_errors_from_either_exact_get_are_retained_without_duplicates(
        probe_module, first_extra, second_errors, expected):
    sequence = probe_sequence()
    sequence[2]["messages"][0].update(first_extra)
    adapter = ObservationAdapter(sequence)
    adapter.message.metadata_errors = second_errors
    report = probe_module.probe(adapter, PROJECT_ID, ROOT_ID)
    assert report["schema_errors"] == expected
    assert report["viewed_unchanged_between_gets"] is True
    assert_safe_report(report)


@pytest.mark.parametrize("first_extra,error", [({"mentions": {}}, "mentions_invalid"),
                                            ({"is_bookmarked": "true"}, "is_bookmarked_invalid")])
def test_cli_first_exact_schema_error_blocks_success_and_all_future_gets(
        probe_module, monkeypatch, capsys, first_extra, error):
    sequence = probe_sequence(before_room=False, after_room=False,
                              before_target=False, after_target=False, exact_unread=False)
    sequence[2]["messages"][0].update(first_extra)
    adapter = ObservationAdapter(sequence, normalized_unread=False)
    monkeypatch.setattr(probe_module, "MCSAdapter", lambda **kwargs: adapter)
    monkeypatch.setattr(probe_module, "probe_actor_pages", lambda *args, **kwargs:
                        pytest.fail("first exact schema error must defer actors"))
    monkeypatch.setattr(sys, "argv", ["probe", "--read-only-target", "--project-id", str(PROJECT_ID),
                                     "--message-id", str(ROOT_ID), "--actor-pages", "--extended-contracts"])
    assert probe_module.main() == 1
    report = json.loads(capsys.readouterr().out)
    assert report["schema_errors"] == [error]
    assert report["viewed_unchanged_between_gets"] is True
    assert report["target_read_state_unchanged"] is True
    deferred = {"state": "deferred", "reason": "unverified_routes_on_unread_target"}
    assert report["actor_pages"] == deferred and report["extended_contracts"] == deferred
    assert len(adapter.events) == 6
    assert_safe_report(report)
