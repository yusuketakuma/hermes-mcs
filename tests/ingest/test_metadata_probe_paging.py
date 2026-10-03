"""Fully synthetic actor paging, aggregate-only output, and CLI read-state gates."""

import copy
import importlib.util
import json
from pathlib import Path
import sys

import pytest

from mcs_adapter import MCSError, SessionExpired


PROJECT_ID = 700001
MESSAGE_ID = 900001
ACTOR_ID = 10000001
SERVER_TIMESTAMP = 987654321
SYNTHETIC_NAME = "synthetic actor name canary"
SYNTHETIC_BODY = "synthetic body canary"


@pytest.fixture(scope="module")
def probe_module():
    spec = importlib.util.spec_from_file_location(
        "metadata_probe_paging",
        Path(__file__).resolve().parents[2]
        / "scripts/development/probe_message_metadata.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ScriptedAdapter:
    """Only synthetic GET responses are available; every other I/O path fails."""

    def __init__(self, responses):
        self.responses = copy.deepcopy(responses)
        self.calls = []
        self.deadlines = []

    def _get(self, path, params, *, extend_session):
        self.calls.append((path, dict(params), extend_session))
        assert extend_session is False
        assert "increment_count" not in params
        assert "keep_read_status" not in params
        assert self.responses, "unexpected GET beyond the bounded synthetic walk"
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def _read_cache(self):
        return "synthetic-cache-only"

    def set_deadline(self, deadline):
        self.deadlines.append(deadline)

    def __getattr__(self, name):
        pytest.fail(f"unexpected adapter operation: {name}")


def actor_rows(count, reaction_type=None):
    users = [{"id": ACTOR_ID + offset, "name": SYNTHETIC_NAME}
             for offset in range(count)]
    if reaction_type is not None:
        return users
    return [{"reaction_type": "viewed", "user": user} for user in users]


def page_responses(rows, reaction_type=None, *, per_page=50,
                   timestamp=SERVER_TIMESTAMP):
    counts = {}
    for row in rows:
        kind = row["reaction_type"] if reaction_type is None else reaction_type
        counts[kind] = counts.get(kind, 0) + 1
    summary = [{"type": kind, "count": count, "self_reacted": False}
               for kind, count in counts.items()]
    page_count = max(1, (len(rows) + per_page - 1) // per_page)
    key = "reactions" if reaction_type is None else "users"
    responses = []
    for page in range(1, page_count + 1):
        response = {
            key: copy.deepcopy(rows[(page - 1) * per_page:page * per_page]),
            "paginate": {
                "current_page": page, "per_page": per_page,
                "has_next": page < page_count, "total_pages": page_count,
                "total_entries": len(rows), "timestamp": timestamp,
            },
        }
        if page == 1:
            response["message"] = {
                "id": MESSAGE_ID, "project_id": PROJECT_ID,
                "reactions": summary, "comment": SYNTHETIC_BODY,
                "user": {"id": ACTOR_ID, "name": SYNTHETIC_NAME},
            }
        responses.append(response)
    return responses


def assert_aggregate_only(report):
    encoded = json.dumps(report)
    for canary in (SYNTHETIC_NAME, SYNTHETIC_BODY, str(PROJECT_ID), str(MESSAGE_ID),
                   str(ACTOR_ID), str(SERVER_TIMESTAMP)):
        assert canary not in encoded
    assert report["post_or_mark_read_called"] is False
    assert not {"actors", "actor_ids", "message", "project_id", "timestamp"} & report.keys()


def run_pages(probe_module, responses, reaction_type=None, **kwargs):
    adapter = ScriptedAdapter(responses)
    report = probe_module.probe_actor_pages(
        adapter, PROJECT_ID, MESSAGE_ID, reaction_type=reaction_type, **kwargs)
    for sample in report["samples"]:
        if not sample["complete"]:
            assert sample["multiple_kinds_per_actor_observed"] is None
    assert_aggregate_only(report)
    return adapter, report


@pytest.mark.parametrize("reaction_type", [None, "viewed"])
@pytest.mark.parametrize("count", [0, 1, 49, 50, 51, 100, 101])
def test_actor_boundaries_use_two_complete_fixed_snapshot_walks(probe_module, reaction_type, count):
    pages = page_responses(actor_rows(count, reaction_type), reaction_type)
    adapter, report = run_pages(probe_module, pages + pages, reaction_type)
    assert report["state"] == "observed"
    assert report["complete"] is True
    assert report["actor_set_unchanged"] is True
    assert len(report["samples"]) == 2
    expected_pages = max(1, (count + 49) // 50)
    for sample in report["samples"]:
        assert sample == {"complete": True, "rows": count, "pages": expected_pages,
                          "counts_match_message": True, "timestamp_stable": True,
                          "multiple_kinds_per_actor_observed": False}
    assert not adapter.responses
    suffix = "user_reactions" if reaction_type is None else "reactions"
    for offset, (path, params, extend_session) in enumerate(adapter.calls):
        page = offset % expected_pages + 1
        expected = {"page": page, "per_page": 50, "include_meta": int(page == 1),
                    "include_paginate_totals": 0}
        if reaction_type is not None:
            expected["reaction_type"] = reaction_type
        if page > 1:
            expected["timestamp"] = SERVER_TIMESTAMP
        assert path == f"/messages/{MESSAGE_ID}/{suffix}"
        assert params == expected
        assert extend_session is False


def test_same_actor_with_two_kinds_counts_as_two_distinct_reactions(probe_module):
    user = {"id": ACTOR_ID, "name": SYNTHETIC_NAME}
    rows = [{"reaction_type": kind, "user": user} for kind in ("viewed", "good")]
    pages = page_responses(rows)
    _, report = run_pages(probe_module, pages + pages)
    assert report["complete"] is True
    assert [sample["rows"] for sample in report["samples"]] == [2, 2]
    assert all(sample["multiple_kinds_per_actor_observed"] is True
               for sample in report["samples"])


@pytest.mark.parametrize("reaction_type", [None, "viewed"])
def test_actor_order_is_not_a_change_in_membership(probe_module, reaction_type):
    rows = actor_rows(51, reaction_type)
    _, report = run_pages(probe_module, page_responses(rows, reaction_type)
                         + page_responses(list(reversed(rows)), reaction_type), reaction_type)
    assert report["complete"] is True and report["actor_set_unchanged"] is True


@pytest.mark.parametrize("reaction_type", [None, "viewed"])
def test_equal_counts_do_not_hide_actor_replacement(probe_module, reaction_type):
    first = actor_rows(1, reaction_type)
    second = actor_rows(1, reaction_type)
    user = second[0]["user"] if reaction_type is None else second[0]
    user["id"] += 1000
    _, report = run_pages(probe_module, page_responses(first, reaction_type)
                         + page_responses(second, reaction_type), reaction_type)
    assert report["complete"] is False
    assert report["actor_set_unchanged"] is False
    assert all(sample["complete"] and sample["rows"] == 1 for sample in report["samples"])


@pytest.mark.parametrize("reaction_type", [None, "viewed"])
def test_each_walk_starts_a_new_snapshot_but_keeps_its_own_timestamp(probe_module, reaction_type):
    rows = actor_rows(51, reaction_type)
    pages = page_responses(rows, reaction_type)
    later = page_responses(rows, reaction_type, timestamp=SERVER_TIMESTAMP + 1)
    adapter, report = run_pages(probe_module, pages + later, reaction_type)
    assert report["complete"] is True
    assert "timestamp" not in adapter.calls[0][1]
    assert "timestamp" not in adapter.calls[2][1]
    assert adapter.calls[1][1]["timestamp"] == SERVER_TIMESTAMP
    assert adapter.calls[3][1]["timestamp"] == SERVER_TIMESTAMP + 1


@pytest.mark.parametrize("reaction_type", [None, "viewed"])
def test_current_total_pages_fallback_and_has_next_priority(probe_module, reaction_type):
    pages = page_responses(actor_rows(51, reaction_type), reaction_type)
    for page in pages:
        del page["paginate"]["has_next"]
    _, report = run_pages(probe_module, pages + pages, reaction_type)
    assert report["complete"] is True
    pages = page_responses(actor_rows(51, reaction_type), reaction_type)
    pages[0]["paginate"]["total_pages"] = 1
    pages[1]["paginate"]["total_pages"] = 100
    _, report = run_pages(probe_module, pages + pages, reaction_type)
    assert report["complete"] is True


@pytest.mark.parametrize("reaction_type", [None, "viewed"])
@pytest.mark.parametrize("defect", ["duplicate", "cycle", "backwards", "timestamp"])
def test_inconsistent_second_page_is_incomplete_and_not_retried(probe_module, reaction_type, defect):
    pages = page_responses(actor_rows(51, reaction_type), reaction_type)
    key = "reactions" if reaction_type is None else "users"
    if defect == "duplicate":
        pages[1][key][0] = copy.deepcopy(pages[0][key][0])
    elif defect == "cycle":
        pages[1]["paginate"]["current_page"] = 1
    elif defect == "backwards":
        pages[1]["paginate"]["current_page"] = 0
    else:
        pages[1]["paginate"]["timestamp"] += 1
    adapter, report = run_pages(probe_module, pages, reaction_type)
    assert report["complete"] is False
    assert report["actor_set_unchanged"] is None
    assert len(report["samples"]) == 1
    sample = report["samples"][0]
    assert sample["rows"] == 50 and sample["pages"] == 2
    assert sample["error"] == "schema_error"
    assert sample["timestamp_stable"] is (defect != "timestamp")
    assert len(adapter.calls) == 2


@pytest.mark.parametrize("reaction_type", [None, "viewed"])
def test_duplicate_within_first_page_retains_partial_count(probe_module, reaction_type):
    pages = page_responses(actor_rows(2, reaction_type), reaction_type)
    key = "reactions" if reaction_type is None else "users"
    pages[0][key][1] = copy.deepcopy(pages[0][key][0])
    _, report = run_pages(probe_module, pages, reaction_type)
    assert report["complete"] is False
    assert report["samples"][0]["rows"] == 1
    assert report["samples"][0]["error"] == "schema_error"


@pytest.mark.parametrize("reaction_type", [None, "viewed"])
@pytest.mark.parametrize("status", [429, 401])
def test_midwalk_error_retains_observed_rows_and_never_reports_zero_success(probe_module, reaction_type, status):
    pages = page_responses(actor_rows(51, reaction_type), reaction_type)
    error = (SessionExpired(SYNTHETIC_BODY, status=401) if status == 401 else
             MCSError("http_error", SYNTHETIC_BODY, status=429, retryable=True))
    adapter, report = run_pages(probe_module, [pages[0], error], reaction_type)
    assert report["complete"] is False
    assert report["actor_set_unchanged"] is None
    assert len(report["samples"]) == 1 and len(adapter.calls) == 2
    sample = report["samples"][0]
    assert sample["rows"] == 50
    assert sample["counts_match_message"] is None
    assert sample["status"] == status
    assert sample["error"] == ("session_expired" if status == 401 else "http_error")


@pytest.mark.parametrize("reaction_type", [None, "viewed"])
def test_deleted_post_is_unknown_instead_of_empty_success(probe_module, reaction_type):
    _, report = run_pages(probe_module, [MCSError("http_error", SYNTHETIC_BODY, status=404)],
                         reaction_type)
    assert report["complete"] is False
    assert report["samples"] == [{"complete": False, "rows": 0, "pages": 0,
                                  "counts_match_message": None, "timestamp_stable": None,
                                  "multiple_kinds_per_actor_observed": None,
                                  "error": "http_error", "status": 404}]
    assert report["actor_set_unchanged"] is None


@pytest.mark.parametrize("reaction_type", [None, "viewed"])
def test_second_walk_failure_does_not_claim_actor_set_stability(probe_module, reaction_type):
    pages = page_responses(actor_rows(1, reaction_type), reaction_type)
    adapter, report = run_pages(probe_module, pages + [MCSError("http_error", status=429)],
                                reaction_type)
    assert report["samples"][0]["complete"] is True
    assert report["samples"][1]["complete"] is False
    assert report["complete"] is False and report["actor_set_unchanged"] is None
    assert len(adapter.calls) == 2


@pytest.mark.parametrize("reaction_type", [None, "viewed"])
def test_page_limit_preserves_partial_count_and_stops_first_walk(probe_module, reaction_type):
    pages = page_responses(actor_rows(101, reaction_type), reaction_type)
    adapter, report = run_pages(probe_module, pages[:2], reaction_type, max_pages=2)
    assert report["complete"] is False
    assert report["samples"][0]["error"] == "page_limit"
    assert report["samples"][0]["rows"] == 100
    assert report["samples"][0]["pages"] == 2
    assert len(adapter.calls) == 2 and report["actor_set_unchanged"] is None


@pytest.mark.parametrize("reaction_type", [None, "viewed"])
def test_default_ten_page_cap_and_single_actor_page_size(probe_module, reaction_type):
    pages = page_responses(actor_rows(501, reaction_type), reaction_type)
    adapter, report = run_pages(probe_module, pages[:10], reaction_type)
    assert report["samples"][0]["error"] == "page_limit"
    assert report["samples"][0]["rows"] == 500 and len(adapter.calls) == 10
    pages = page_responses(actor_rows(2, reaction_type), reaction_type, per_page=1)
    adapter, report = run_pages(probe_module, pages + pages, reaction_type, per_page=1)
    assert report["complete"] is True
    assert [sample["pages"] for sample in report["samples"]] == [2, 2]
    assert all(params["per_page"] == 1 for _, params, _ in adapter.calls)


@pytest.mark.parametrize("reaction_type", [None, "viewed"])
@pytest.mark.parametrize("identity", [None, True, 1.0, 0, -1, 2**63, "10000001"])
def test_missing_or_invalid_actor_identity_is_incomplete(probe_module, reaction_type, identity):
    pages = page_responses(actor_rows(1, reaction_type), reaction_type)
    key = "reactions" if reaction_type is None else "users"
    user = pages[0][key][0]["user"] if reaction_type is None else pages[0][key][0]
    if identity is None:
        del user["id"]
    else:
        user["id"] = identity
    adapter, report = run_pages(probe_module, pages, reaction_type)
    assert report["complete"] is False and len(adapter.calls) == 1
    assert report["samples"][0]["error"] == "schema_error"
    assert report["samples"][0]["rows"] == 0


@pytest.mark.parametrize("kind", [None, True, "", "all", "閲覧", "a" * 65, "viewed /other"])
def test_missing_or_invalid_reaction_kind_is_incomplete(probe_module, kind):
    pages = page_responses(actor_rows(1))
    if kind is None:
        del pages[0]["reactions"][0]["reaction_type"]
    else:
        pages[0]["reactions"][0]["reaction_type"] = kind
    _, report = run_pages(probe_module, pages)
    assert report["complete"] is False
    assert report["samples"][0]["error"] == "schema_error"


@pytest.mark.parametrize("reaction_type", [None, "viewed"])
@pytest.mark.parametrize("defect", ["missing_collection", "collection_object", "row_scalar",
                                    "over_page_size", "empty_nonterminal", "missing_paginate"])
def test_malformed_collections_do_not_prove_absence(probe_module, reaction_type, defect):
    pages = page_responses(actor_rows(1, reaction_type), reaction_type)
    page = pages[0]
    key = "reactions" if reaction_type is None else "users"
    if defect == "missing_collection":
        del page[key]
    elif defect == "collection_object":
        page[key] = {}
    elif defect == "row_scalar":
        page[key] = [None]
    elif defect == "over_page_size":
        page[key] = actor_rows(51, reaction_type)
    elif defect == "empty_nonterminal":
        page[key] = []
        page["paginate"]["has_next"] = True
    else:
        del page["paginate"]
    _, report = run_pages(probe_module, pages, reaction_type)
    assert report["complete"] is False
    assert report["samples"][0]["error"] == "schema_error"


@pytest.mark.parametrize("reaction_type", [None, "viewed"])
@pytest.mark.parametrize("field,value", [
    ("timestamp", None), ("timestamp", True), ("timestamp", 1.0), ("timestamp", 0),
    ("timestamp", 2**63), ("current_page", True), ("current_page", 1.0),
    ("current_page", 2), ("per_page", True), ("per_page", 50.0), ("per_page", 1),
    ("has_next", None), ("has_next", 0), ("total_entries", True), ("total_entries", 2),
])
def test_invalid_pagination_is_incomplete(probe_module, reaction_type, field, value):
    pages = page_responses(actor_rows(1, reaction_type), reaction_type)
    if field == "timestamp" and value is None:
        del pages[0]["paginate"][field]
    else:
        pages[0]["paginate"][field] = value
    _, report = run_pages(probe_module, pages, reaction_type)
    assert report["complete"] is False
    assert report["samples"][0]["error"] == "schema_error"


@pytest.mark.parametrize("reaction_type", [None, "viewed"])
@pytest.mark.parametrize("field,value", [("total_pages", None), ("total_pages", True),
                                         ("total_pages", 1.0), ("total_pages", 0),
                                         ("current_page", None)])
def test_fallback_requires_valid_terminal_page_evidence(probe_module, reaction_type, field, value):
    pages = page_responses(actor_rows(0, reaction_type), reaction_type)
    paginate = pages[0]["paginate"]
    del paginate["has_next"]
    if value is None:
        del paginate[field]
    else:
        paginate[field] = value
    _, report = run_pages(probe_module, pages, reaction_type)
    assert report["complete"] is False
    assert report["samples"][0]["counts_match_message"] is None
    assert report["samples"][0]["error"] == "schema_error"


@pytest.mark.parametrize("reaction_type", [None, "viewed"])
@pytest.mark.parametrize("field", ["id", "project_id"])
@pytest.mark.parametrize("value", [True, 1.0, 0, 2**63, 123456789])
def test_optional_target_echo_must_match_if_present(probe_module, reaction_type, field, value):
    pages = page_responses(actor_rows(1, reaction_type), reaction_type)
    pages[0]["message"][field] = value
    _, report = run_pages(probe_module, pages, reaction_type)
    assert report["complete"] is False
    assert report["samples"][0]["error"] == "schema_error"


@pytest.mark.parametrize("reaction_type", [None, "viewed"])
@pytest.mark.parametrize("field", ["id", "project_id"])
def test_later_page_target_echo_cannot_switch_messages(probe_module, reaction_type, field):
    pages = page_responses(actor_rows(51, reaction_type), reaction_type)
    pages[1]["message"] = {field: 123456789}
    _, report = run_pages(probe_module, pages, reaction_type)
    assert report["complete"] is False
    assert report["samples"][0]["rows"] == 50
    assert report["samples"][0]["error"] == "schema_error"


@pytest.mark.parametrize("reaction_type", [None, "viewed"])
def test_summary_without_optional_echo_ids_is_allowed(probe_module, reaction_type):
    pages = page_responses(actor_rows(1, reaction_type), reaction_type)
    del pages[0]["message"]["id"]
    del pages[0]["message"]["project_id"]
    _, report = run_pages(probe_module, pages + pages, reaction_type)
    assert report["complete"] is True


@pytest.mark.parametrize("reaction_type", [None, "viewed"])
@pytest.mark.parametrize("defect", ["missing_summary", "invalid_summary", "count_mismatch", "zero_summary"])
def test_missing_invalid_or_mismatching_summary_never_proves_complete(probe_module, reaction_type, defect):
    pages = page_responses(actor_rows(1, reaction_type), reaction_type)
    message = pages[0]["message"]
    if defect == "missing_summary":
        del message["reactions"]
    elif defect == "invalid_summary":
        message["reactions"][0]["count"] = True
    elif defect == "count_mismatch":
        message["reactions"][0]["count"] = 2
    else:
        message["reactions"][0]["count"] = 0
    adapter, report = run_pages(probe_module, pages, reaction_type)
    assert report["complete"] is False and len(adapter.calls) == 1
    assert report["actor_set_unchanged"] is None
    assert report["samples"][0]["counts_match_message"] is not True


@pytest.mark.parametrize("reaction_type", [None, "viewed"])
@pytest.mark.parametrize("message_meta", [None, {}, SYNTHETIC_BODY])
def test_empty_actor_page_without_valid_summary_is_unknown(probe_module, reaction_type, message_meta):
    pages = page_responses(actor_rows(0, reaction_type), reaction_type)
    if message_meta is None:
        del pages[0]["message"]
    else:
        pages[0]["message"] = message_meta
    _, report = run_pages(probe_module, pages, reaction_type)
    assert report["complete"] is False and report["actor_set_unchanged"] is None
    sample = report["samples"][0]
    assert sample["rows"] == 0 and sample["counts_match_message"] is None


def test_viewed_walk_matches_its_kind_without_counting_other_kinds(probe_module):
    pages = page_responses(actor_rows(1, "viewed"), "viewed")
    pages[0]["message"]["reactions"].append({"type": "good", "count": 10, "self_reacted": False})
    _, report = run_pages(probe_module, pages + pages, "viewed")
    assert report["complete"] is True and report["samples"][0]["rows"] == 1


def test_viewed_walk_without_viewed_summary_remains_unknown(probe_module):
    pages = page_responses(actor_rows(0, "viewed"), "viewed")
    pages[0]["message"]["reactions"] = [{"type": "good", "count": 10, "self_reacted": False}]
    _, report = run_pages(probe_module, pages, "viewed")
    assert report["complete"] is False
    assert report["samples"][0]["counts_match_message"] is None


@pytest.mark.parametrize("keyword,value", [
    ("project_id", True), ("project_id", 1.0), ("project_id", 0),
    ("message_id", None), ("message_id", 2**63), ("message_id", "1"),
    ("max_pages", True), ("max_pages", 1.0), ("max_pages", 0), ("max_pages", 11),
    ("per_page", True), ("per_page", 1.0), ("per_page", 0), ("per_page", 51),
    ("reaction_type", True), ("reaction_type", "all"), ("reaction_type", ""),
    ("reaction_type", "閲覧"), ("reaction_type", "viewed /other"),
])
def test_invalid_probe_arguments_fail_before_any_get(probe_module, keyword, value):
    kwargs = {"project_id": PROJECT_ID, "message_id": MESSAGE_ID, keyword: value}
    adapter = ScriptedAdapter([])
    with pytest.raises(ValueError, match="invalid actor probe arguments"):
        probe_module.probe_actor_pages(adapter, **kwargs)
    assert adapter.calls == []


def cli_report(room_unread, target_unread):
    return {"unread_at_start": room_unread, "target_unread_at_start": target_unread,
            "read_state_unchanged": True, "schema_errors": [],
            "target_read_state_unchanged": True,
            "unread_preservation_proven": False, "viewed_unchanged_between_gets": True,
            "post_or_mark_read_called": False}


@pytest.mark.parametrize("room_unread,target_unread", [
    (False, True), (False, None), (True, False), (True, True), (True, None),
    (None, False), (None, True), (None, None),
])
def test_cli_defers_actor_and_extended_routes_unless_both_read_states_are_false(
        probe_module, monkeypatch, capsys, room_unread, target_unread):
    adapter = ScriptedAdapter([])
    monkeypatch.setattr(probe_module, "MCSAdapter", lambda **kwargs: adapter)
    monkeypatch.setattr(probe_module, "probe", lambda *args, **kwargs:
                        cli_report(room_unread, target_unread))
    monkeypatch.setattr(probe_module, "probe_actor_pages", lambda *args, **kwargs:
                        pytest.fail("actor probe must be deferred on unread or unknown targets"))
    monkeypatch.setattr(sys, "argv", ["probe", "--read-only-target", "--project-id", str(PROJECT_ID),
                        "--message-id", str(MESSAGE_ID), "--actor-pages", "--extended-contracts"])
    assert probe_module.main() == 2
    encoded = capsys.readouterr().out
    report = json.loads(encoded)
    assert report["actor_pages"] == {"state": "deferred", "reason": "unverified_routes_on_unread_target"}
    assert report["extended_contracts"] == report["actor_pages"]
    assert adapter.calls == [] and len(adapter.deadlines) == 1
    assert_aggregate_only(report)


def test_cli_allows_two_actor_endpoints_only_on_explicitly_read_target(probe_module, monkeypatch, capsys):
    adapter = ScriptedAdapter([])
    actor_calls, route_calls = [], []
    monkeypatch.setattr(probe_module, "MCSAdapter", lambda **kwargs: adapter)
    monkeypatch.setattr(probe_module, "probe", lambda *args, **kwargs: cli_report(False, False))

    def actors(actual_adapter, project_id, message_id, **kwargs):
        assert actual_adapter is adapter and (project_id, message_id) == (PROJECT_ID, MESSAGE_ID)
        actor_calls.append(kwargs)
        return {"complete": True, "post_or_mark_read_called": False}

    def routes(*args, **kwargs):
        route_calls.append(kwargs)
        return {"state": "observed", "post_or_mark_read_called": False}

    monkeypatch.setattr(probe_module, "probe_actor_pages", actors)
    monkeypatch.setattr(probe_module, "probe_routes", routes)
    monkeypatch.setattr(sys, "argv", ["probe", "--read-only-target", "--project-id", str(PROJECT_ID),
                        "--message-id", str(MESSAGE_ID), "--actor-pages", "--actor-per-page", "1",
                        "--extended-contracts"])
    # Read targets cannot prove unread preservation; completed actor walks still run safely.
    assert probe_module.main() == 2
    report = json.loads(capsys.readouterr().out)
    assert actor_calls == [{"reaction_type": None, "per_page": 1},
                           {"reaction_type": "viewed", "per_page": 1}]
    assert route_calls == [{"unread_at_start": False, "parent_id": None}]
    assert all(sample["complete"] for sample in report["actor_pages"].values())
    assert adapter.calls == []
    assert_aggregate_only(report)


@pytest.mark.parametrize("field,value,exit_code", [
    ("read_state_unchanged", False, 1),
    ("target_read_state_unchanged", False, 1),
    ("target_read_state_unchanged", None, 2),
    ("schema_errors", ["mentions_invalid"], 1),
])
def test_cli_defers_optional_gets_after_changed_or_unknown_observation(
        probe_module, monkeypatch, capsys, field, value, exit_code):
    adapter = ScriptedAdapter([])
    report = cli_report(False, False)
    report[field] = value
    monkeypatch.setattr(probe_module, "MCSAdapter", lambda **kwargs: adapter)
    monkeypatch.setattr(probe_module, "probe", lambda *args, **kwargs: report)
    monkeypatch.setattr(probe_module, "probe_actor_pages", lambda *args, **kwargs:
                        pytest.fail("changed or unknown observation caused optional GET"))
    monkeypatch.setattr(sys, "argv", ["probe", "--read-only-target", "--project-id", str(PROJECT_ID),
                        "--message-id", str(MESSAGE_ID), "--actor-pages", "--extended-contracts"])
    assert probe_module.main() == exit_code
    result = json.loads(capsys.readouterr().out)
    assert result["actor_pages"]["state"] == result["extended_contracts"]["state"] == "deferred"
    assert not adapter.calls


@pytest.mark.parametrize("per_page,actor_flag", [(0, True), (51, True), (1, False)])
def test_cli_invalid_page_size_fails_before_adapter_construction(probe_module, monkeypatch, per_page, actor_flag):
    monkeypatch.setattr(probe_module, "MCSAdapter", lambda **kwargs:
                        pytest.fail("invalid arguments must fail before reading a cache"))
    argv = ["probe", "--read-only-target", "--project-id", str(PROJECT_ID),
            "--message-id", str(MESSAGE_ID), "--actor-per-page", str(per_page)]
    if actor_flag:
        argv.append("--actor-pages")
    monkeypatch.setattr(sys, "argv", argv)
    with pytest.raises(SystemExit) as error:
        probe_module.main()
    assert error.value.code == 2
