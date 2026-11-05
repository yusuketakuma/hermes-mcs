"""MCS HTTP response, pagination, identity and endpoint schema contracts."""
import mcs_contract
import base64
import time
import urllib.parse
from datetime import datetime
import pytest
import mcs_adapter
import mcs_download
from ingest_testkit import _message
import pytest as _pytest


_CUTOFF = int(datetime.fromisoformat(
    "2026-09-21T00:00:00+09:00").timestamp())



class _ProjectsAdapter(mcs_adapter.MCSAdapter):
    def __init__(self, has_next):
        self.has_next = has_next

    def _get(self, path, params=None, extend_session=True):
        return {"projects": [], "paginate": {"has_next": self.has_next}}


@pytest.mark.parametrize("body", [
    b"\xff",
    b'{"nested":' + b'[' * 10000 + b'0' + b']' * 10000 + b'}',
    b'{"integer":' + b'9' * 10000 + b'}',
], ids=["invalid_utf8", "deep_json", "oversized_integer"])
def test_bad_api_json_is_a_local_failure_and_the_next_patient_can_be_fetched(body, recursion_limited_json):
    adapter = mcs_adapter.MCSAdapter()
    replies = iter([body, b'{"messages":[],"paginate":{"has_next":false}}'])
    adapter._request = lambda *args, **kwargs: (200, next(replies), {})
    bad = adapter.fetch_unread_messages(1, 123)
    assert isinstance(bad.error, mcs_contract.SchemaError)
    assert bad.error.detail == "invalid json payload" and bad.messages == []
    good = adapter.fetch_unread_messages(2, 123)
    assert good.error is None and good.reached and good.messages == []


def test_login_html_response_keeps_the_session_recovery_contract():
    adapter = mcs_adapter.MCSAdapter()
    adapter._request = lambda *args, **kwargs: (200, b"<html>synthetic login</html>", {})
    with pytest.raises(mcs_contract.SessionExpired):
        adapter._get("/synthetic")


def test_project_inventory_fails_when_page_cap_is_incomplete():
    with pytest.raises(mcs_contract.MCSError) as error:
        _ProjectsAdapter(True).list_projects(max_pages=1)
    assert error.value.kind == "pages_exceeded"


def test_unread_reply_snippet_remains_missing():
    parent = _message(mid=10)
    parent.replies = [_message(mid=20, state="snippet", parent_id=10,
                               unread=True)]
    adapter = mcs_adapter.MCSAdapter()
    adapter.fetch_thread = lambda *_: [
        _message(mid=20, state="snippet", parent_id=10)
    ]

    result = adapter.fetch_unread_replies(parent)

    assert result.missing == [20]


def test_contradictory_mark_read_response_is_unknown(monkeypatch):
    # the mark read is a GET on the message list (keep_read_status
    # omitted); confirmation comes from the detail read afterwards —
    # oldest_unread_message still present means the mark did not hold
    adapter = mcs_adapter.MCSAdapter()
    adapter._get = lambda path, params=None, **k: {
        "project": {"is_archived": False,
                    "oldest_unread_message": {"id": 7}},
    }
    with pytest.raises(mcs_contract.MCSError) as error:
        adapter.mark_patient_read(1, 123)
    assert error.value.kind == "mark_result_unknown"


def test_mark_read_confirms_when_oldest_unread_is_gone():
    adapter = mcs_adapter.MCSAdapter()
    adapter._get = lambda path, params=None, **k: (
        {"project": {"is_archived": False}}     # detail: key absent = read
        if path == "/projects/1" else {"messages": [], "paginate": {}})
    assert adapter.mark_patient_read(1, 123)["project"]["is_archived"] is False


def test_mark_read_empty_project_is_unknown():
    adapter = mcs_adapter.MCSAdapter()
    adapter._get = lambda path, params=None, **k: (
        {"project": {}}                        # no is_archived — not real
        if path == "/projects/1" else {"messages": [], "paginate": {}})
    with pytest.raises(mcs_contract.MCSError) as error:
        adapter.mark_patient_read(1, 123)
    assert error.value.kind == "mark_result_unknown"


def _unread_project(pid: int, unread: bool = True) -> dict:
    return {"id": pid, "type": "medical", "is_unread": unread,
            "karte": {"id": pid * 10, "last_name": "T", "first_name": "P",
                      "disease": "",
                      "station": {"name": "st"}}}


class _UnreadListAdapter(mcs_adapter.MCSAdapter):
    """Serves /projects pages from a schedule of
    (timestamp, has_next, projects) tuples, one entry per request."""

    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = []

    def _get(self, path, params=None, extend_session=True):
        ts, has_next, projs = self.pages[len(self.calls)]
        self.calls.append(params["page"])
        return {"projects": projs,
                "paginate": {"timestamp": ts, "has_next": has_next}}


def test_list_unread_filters_read_projects_and_uses_earliest_ts():
    adapter = _UnreadListAdapter([
        (100, True, [_unread_project(11), _unread_project(12, unread=False)]),
        # page timestamps are a per-request server clock — they legitimately
        # differ between pages; the snapshot keeps the earliest so mark-read
        # can never cover a post made after the walk started.
        (105, False, [_unread_project(13)]),
    ])

    snap = adapter.list_unread()

    assert snap.timestamp == 100
    assert adapter.calls == [1, 2]
    assert [p.project_id for p in snap.patients] == [11, 13]
    assert [p.project_id for p in snap.listed_patients] == [11, 12, 13]


def test_list_unread_fails_when_pagination_sticks():
    adapter = _UnreadListAdapter([
        (100, True, [_unread_project(11)]),
        (100, True, [_unread_project(11)]),  # same ids again
    ])

    with pytest.raises(mcs_contract.SchemaError) as error:
        adapter.list_unread()
    assert "not advancing" in error.value.detail


def test_list_unread_rejects_missing_is_unread():
    adapter = _UnreadListAdapter([
        (100, False, [{"id": 11, "type": "medical", "karte": {}}]),
    ])

    with pytest.raises(mcs_contract.SchemaError) as error:
        adapter.list_unread()
    assert "is_unread" in error.value.detail


def test_list_unread_fails_on_schema_error():
    class Adapter(mcs_adapter.MCSAdapter):
        def __init__(self):
            self.calls = 0

        def _get(self, path, params=None, extend_session=True):
            self.calls += 1
            return {"projects": []}  # paginate missing

    adapter = Adapter()
    with pytest.raises(mcs_contract.SchemaError):
        adapter.list_unread()
    assert adapter.calls == 1


def test_history_returns_saved_pages_and_error():
    class Adapter(mcs_adapter.MCSAdapter):
        def _get(self, path, params=None, extend_session=True):
            if params["page"] == 1:
                return {"messages": [{"id": 1, "comment": "ok"}],
                        "paginate": {"has_next": True}}
            return {"messages": "broken", "paginate": {"has_next": False}}

    batch = Adapter().fetch_history(1, 0, max_pages=2)
    assert [m.message_id for m in batch.messages] == [1]
    assert batch.pages == 1
    assert batch.reached is False
    assert isinstance(batch.error, mcs_contract.SchemaError)


def test_invalid_nested_text_is_reported_as_schema_error():
    class Adapter(mcs_adapter.MCSAdapter):
        def _get(self, path, params=None, extend_session=True):
            return {
                "projects": [{
                    "id": 1,
                    "karte": {},
                    "last_message": {"created_at": 7},
                }],
                "paginate": {"has_next": False},
            }

    with pytest.raises(mcs_contract.SchemaError):
        Adapter().list_projects()


@pytest.mark.parametrize("kind", ["unread", "history"])
def test_pagination_schema_failure_retains_completed_pages(kind):
    class Adapter(mcs_adapter.MCSAdapter):
        def _get(self, path, params=None, extend_session=True):
            page = params["page"]
            return {"messages": [{"id": page, "comment": "body"}],
                    "paginate": {"has_next": True if page == 1 else "false"}}

    adapter = Adapter()
    batch = (adapter.fetch_unread_messages(1, 123, max_pages=2)
             if kind == "unread" else adapter.fetch_history(1, 0, max_pages=2))
    assert [m.message_id for m in batch.messages] == [1]
    assert batch.pages == 1 and not batch.reached
    assert isinstance(batch.error, mcs_contract.SchemaError)


def test_invalid_history_date_does_not_certify_cutoff():
    class Adapter(mcs_adapter.MCSAdapter):
        def _get(self, path, params=None, extend_session=True):
            return {"messages": [{"id": 1, "comment": "body",
                                  "created_at": "invalid"}],
                    "paginate": {"has_next": True}}

    batch = Adapter().fetch_history(1, 123, max_pages=1)
    assert not batch.reached
    assert isinstance(batch.error, mcs_contract.SchemaError)


def test_history_ordered_cutoff_still_walks_to_natural_end():
    """FIX-AD1: under sort=pinned a below-cutoff tail never certifies
    'reached' — the walk continues until has_next is false, because a
    page-boundary pinned straggler could resume above the cutoff."""
    class Adapter(mcs_adapter.MCSAdapter):
        def _get(self, path, params=None, extend_session=True):
            if params["page"] == 1:
                return {"messages": [
                    {"id": 3, "comment": "n2",
                     "created_at": "2026-09-22T00:00:00+09:00"},
                    {"id": 2, "comment": "n1",
                     "created_at": "2026-09-21T12:00:00+09:00"},
                    {"id": 1, "comment": "old",
                     "created_at": "2020-01-01T00:00:00+09:00"}],
                    "paginate": {"has_next": True}}
            return {"messages": [
                {"id": 4, "comment": "older",
                     "created_at": "2019-01-01T00:00:00+09:00"}],
                    "paginate": {"has_next": False}}

    batch = Adapter().fetch_history(1, _CUTOFF, max_pages=5)
    assert [m.message_id for m in batch.messages] == [3, 2]
    assert batch.reached and batch.pages == 2 and batch.error is None


def test_history_pinned_straggler_at_page_boundary_keeps_walking():
    """FIX-AD1 regression: an old item ending a page must not terminate
    the walk — the next page can resume above the cutoff."""
    class Adapter(mcs_adapter.MCSAdapter):
        def _get(self, path, params=None, extend_session=True):
            if params["page"] == 1:
                return {"messages": [
                    {"id": 3, "comment": "new",
                     "created_at": "2026-09-22T00:00:00+09:00"},
                    {"id": 9, "comment": "pinned-old-at-boundary",
                     "created_at": "2020-01-01T00:00:00+09:00"}],
                    "paginate": {"has_next": True}}
            return {"messages": [
                {"id": 2, "comment": "new-on-page-2",
                     "created_at": "2026-09-21T12:00:00+09:00"}],
                    "paginate": {"has_next": False}}

    batch = Adapter().fetch_history(1, _CUTOFF, max_pages=5)
    assert [m.message_id for m in batch.messages] == [3, 2]
    assert batch.reached and batch.pages == 2


def test_history_pinned_order_violation_walks_to_natural_end():
    class Adapter(mcs_adapter.MCSAdapter):
        def _get(self, path, params=None, extend_session=True):
            if params["page"] == 1:
                return {"messages": [
                    {"id": 9, "comment": "pinned-old",
                     "created_at": "2020-01-01T00:00:00+09:00"},
                    {"id": 3, "comment": "new",
                     "created_at": "2026-09-22T00:00:00+09:00"}],
                    "paginate": {"has_next": True}}
            return {"messages": [
                {"id": 2, "comment": "new2",
                 "created_at": "2026-09-21T12:00:00+09:00"}],
                "paginate": {"has_next": False}}

    batch = Adapter().fetch_history(1, _CUTOFF, max_pages=5)
    assert [m.message_id for m in batch.messages] == [3, 2]
    assert batch.reached and batch.pages == 2 and batch.error is None


def test_history_order_violation_without_end_is_not_certified():
    class Adapter(mcs_adapter.MCSAdapter):
        def _get(self, path, params=None, extend_session=True):
            return {"messages": [
                {"id": 9 - params["page"], "comment": "pinned-old",
                 "created_at": "2020-01-01T00:00:00+09:00"},
                {"id": 100 + params["page"], "comment": "new",
                 "created_at": "2026-09-22T00:00:00+09:00"}],
                "paginate": {"has_next": True}}

    batch = Adapter().fetch_history(1, _CUTOFF, max_pages=2)
    assert [m.message_id for m in batch.messages] == [101, 102]
    assert not batch.reached and batch.pages == 2


def test_paginated_thread_is_not_reported_complete():
    adapter = mcs_adapter.MCSAdapter()
    adapter._get = lambda *a, **k: {
        "messages": [{"id": 2, "comment": "body"}],
        "paginate": {"has_next": True}}
    with pytest.raises(mcs_contract.MCSError) as error:
        adapter.fetch_thread(1, 1)
    assert error.value.kind == "thread_incomplete"


def test_malformed_mark_response_stays_unknown():
    adapter = mcs_adapter.MCSAdapter()
    adapter._get = lambda path, params=None, **k: (
        {"data": [1]}                        # no project object at all
        if path == "/projects/1" else {"messages": [], "paginate": {}})
    with pytest.raises(mcs_contract.MCSError) as error:
        adapter.mark_patient_read(1, 123)
    assert error.value.kind == "mark_result_unknown"


def test_assert_allowed_url_bad_port_is_mcserror():
    """A malformed port (':bad', out-of-range, broken bracket) makes
    urlparse's .port raise ValueError — it must surface as MCSError
    'url_not_allowed', not leak as a bare ValueError that escapes
    stage_attachments' except MCSError and poisons the whole tick."""
    for bad in ("https://www.medical-care.net:bad/x",
                "https://www.medical-care.net:99999/x",
                "https://synthetic@www.medical-care.net/f",
                "https://www.medical-care.net/f#fragment",
                "https://[::1/x"):
        with pytest.raises(mcs_contract.MCSError) as e:
            mcs_download.validate_download_url(bad)
        assert e.value.kind == "url_not_allowed"
    # allowed still passes
    mcs_download.validate_download_url("https://www.medical-care.net/f")
    mcs_download.validate_download_url("https://www.medical-care.net:443/f")


def test_projects_malformed_last_message_timestamp_is_schema_error():
    """A malformed created_at must not silently become epoch 0 — that
    would make a live project look inactive to init_data (FIX-ID2)."""
    class Adapter(mcs_adapter.MCSAdapter):
        def _get(self, path, params=None, extend_session=True):
            return {
                "projects": [{
                    "id": 1, "type": "medical",
                    "karte": {"last_name": "S", "first_name": "T",
                              "station": {"name": "st"}},
                    "last_message": {"created_at": "not-a-date"},
                }],
                "paginate": {"has_next": False},
            }

    with pytest.raises(mcs_contract.SchemaError,
                       match="last_message.created_at"):
        Adapter().list_projects()


def test_projects_without_last_message_get_zero_activity():
    """Absent last_message is legitimate — last_activity=0 simply skips
    the project in init_data's active filter."""
    class Adapter(mcs_adapter.MCSAdapter):
        def _get(self, path, params=None, extend_session=True):
            return {
                "projects": [{
                    "id": 1, "type": "medical",
                    "karte": {"last_name": "S", "first_name": "T",
                              "station": {"name": "st"}},
                }],
                "paginate": {"has_next": False},
            }

    ps = Adapter().list_projects()
    assert len(ps) == 1 and ps[0].last_activity == 0


def test_adapter_request_raises_deadline_exceeded():
    """F13: a propagated deadline cuts HTTP work before the wire —
    retries never extend past it."""
    a = mcs_adapter.MCSAdapter()
    a._token = "t"
    a.set_deadline(time.monotonic() - 1)
    with pytest.raises(mcs_contract.MCSError) as e:
        a._request("GET", "/projects")
    assert e.value.kind == "deadline_exceeded"


def test_unread_thread_larger_than_default_window_can_complete():
    pages = []

    class Adapter(mcs_adapter.MCSAdapter):
        def _get(self, path, params=None, **kwargs):
            page = params["page"]
            pages.append(page)
            start = (page - 1) * 10 + 1
            return {"messages": [{"id": mid, "comment": "synthetic"}
                                 for mid in range(start, min(start + 10, 102))],
                    "paginate": {"has_next": page < 11}}

    parent = _message(mid=1000)
    parent.reply_count = 101
    parent.replies = [_message(mid=101, parent_id=1000, state="snippet", unread=True)]
    batch = Adapter().fetch_unread_replies(parent)
    assert batch.missing == [] and batch.messages[0].body_state == "full"
    assert pages == list(range(1, 12))


# ---------- self_profile (MCS-derived self identity) ----------

def test_self_profile_normalizes_user_envelope():
    """GET /users/self -> sender id + display name + professions +
    stations — the signal engine's default self identity."""
    class Adapter(mcs_adapter.MCSAdapter):
        def _get(self, path, params=None, extend_session=True):
            assert path == "/users/self"
            return {"user": {
                "id": 42, "last_name": "山田", "first_name": "薬剤",
                "specialist_categories": [{"name": "薬剤師"},
                                          {"name": "管理薬剤師"}],
                "stations": [{"id": 7, "name": "みどり薬局"}]}}

    p = Adapter().self_profile()
    assert p == {"sender_id": 42, "name": "山田 薬剤",
                 "professions": ["薬剤師", "管理薬剤師"],
                 "organizations": ["みどり薬局"],
                 "stations": [{"id": 7, "name": "みどり薬局"}]}


def test_self_profile_accepts_bare_user_object():
    class Adapter(mcs_adapter.MCSAdapter):
        def _get(self, path, params=None, extend_session=True):
            return {"id": 7, "last_name": "佐藤", "first_name": "花子",
                    "stations": [{"name": "みどり薬局"}]}

    p = Adapter().self_profile()
    assert p["sender_id"] == 7 and p["name"] == "佐藤 花子"
    assert p["professions"] == [] and p["organizations"] == ["みどり薬局"]


def test_self_profile_endpoint_unavailable_maps_kind():
    """An endpoint failure is re-raised with a stable kind so the
    caller can log-and-continue instead of failing the run."""
    class Adapter(mcs_adapter.MCSAdapter):
        def _get(self, path, params=None, extend_session=True):
            raise mcs_contract.MCSError("http_error", "404", status=404)

    import pytest
    with pytest.raises(mcs_contract.MCSError) as e:
        Adapter().self_profile()
    assert e.value.kind == "self_profile_unavailable"


def test_self_profile_empty_is_schema_error():
    class Adapter(mcs_adapter.MCSAdapter):
        def _get(self, path, params=None, extend_session=True):
            return {"user": {"id": 9}}

    import pytest
    with pytest.raises(mcs_contract.SchemaError):
        Adapter().self_profile()


def test_self_profile_session_expired_passthrough():
    """An expired session stays SessionExpired — it must never be
    relabeled as a missing/unsupported endpoint."""
    class Adapter(mcs_adapter.MCSAdapter):
        def _get(self, path, params=None, extend_session=True):
            raise mcs_contract.SessionExpired("token rejected")

    import pytest
    with pytest.raises(mcs_contract.SessionExpired):
        Adapter().self_profile()


def test_self_profile_normalizes_sender_id():
    """A non-scalar user id (unexpected shape) degrades to None —
    never poisons the artifact with a dict."""
    class Adapter(mcs_adapter.MCSAdapter):
        def _get(self, path, params=None, extend_session=True):
            return {"user": {"id": {"unexpected": 1},
                             "last_name": "山田", "first_name": "太郎",
                             "specialist_categories":
                                 [{"name": "薬剤師"}],
                             "stations": [{"name": "みどり薬局"}]}}

    p = Adapter().self_profile()
    assert p["sender_id"] is None
    assert p["name"] == "山田 太郎"
    assert p["professions"] == ["薬剤師"]
    assert p["organizations"] == ["みどり薬局"]


# ---------- fetch_latest (newest-message probe endpoint) ----------

class _LatestAdapter(mcs_adapter.MCSAdapter):
    """Serves /projects/{pid}/messages/latest responses verbatim."""
    def __init__(self, response):
        self.response = response
        self.calls = []

    def _get(self, path, params=None, extend_session=True):
        self.calls.append((path, params))
        return self.response


def test_fetch_latest_parses_newest_message():
    adapter = _LatestAdapter({
        "is_self_only": True,
        "message": {"id": 42}})
    out = adapter.fetch_latest(7)
    assert out == {"message_id": 42, "is_self_only": True}
    path, params = adapter.calls[0]
    assert path == "/projects/7/messages/latest"
    assert params is None


def test_fetch_latest_no_newer_message():
    for response in ({"is_self_only": False, "message": None},
                     {"is_self_only": False, "message": {}},
                     {"is_self_only": False}):
        out = _LatestAdapter(response).fetch_latest(7)
        assert out == {"message_id": None, "is_self_only": False}


@pytest.mark.parametrize("response", [
    {"message": {"id": "abc"}},
    {"message": {"id": 0}},
    {"message": {"id": 2**63}},
    {"message": {"id": None}},
    {"message": "not-a-dict"},
    {"message": []},
    {"is_self_only": "yes", "message": None},
])
def test_fetch_latest_rejects_malformed(response):
    adapter = _LatestAdapter(response)
    with pytest.raises(mcs_contract.SchemaError):
        adapter.fetch_latest(7)


def _status_adapter(monkeypatch, statuses):
    adapter = mcs_adapter.MCSAdapter(worker=lambda *a, **k: None)
    adapter._token = "synthetic-bearer"
    calls = []

    def worker(payload, **kwargs):
        path = urllib.parse.urlparse(payload["url"]).path
        calls.append(path)
        status = next(s for p, s in statuses.items() if path.endswith(p))
        return {"status": status, "headers": {},
                "body": base64.b64encode(b"{}").decode("ascii")}

    adapter._worker = worker
    monkeypatch.setattr(adapter, "_sleep_bounded", lambda seconds: None)
    return adapter, calls


def test_route_level_403_under_valid_session_is_not_expiry(monkeypatch):
    """A 403 while the session probe still succeeds (retired route,
    revoked project) is a per-request error — never a re-login trigger
    that aborts the whole run."""
    adapter, calls = _status_adapter(
        monkeypatch, {"/users/self/count": 200, "/projects/2/messages": 403})
    with pytest.raises(mcs_contract.MCSError) as error:
        adapter._request("GET", "/projects/2/messages", retries=0)
    assert not isinstance(error.value, mcs_contract.SessionExpired)
    assert error.value.kind == "forbidden" and error.value.status == 403
    assert calls[-1].endswith("/users/self/count")


def test_403_with_failing_session_probe_stays_expiry(monkeypatch):
    adapter, calls = _status_adapter(
        monkeypatch, {"/users/self/count": 403, "/projects/2/messages": 403})
    with pytest.raises(mcs_contract.SessionExpired) as error:
        adapter._request("GET", "/projects/2/messages", retries=0)
    assert error.value.status == 403
    # the probe itself never recurses into another probe
    assert len(calls) == 2


def test_403_with_unreachable_session_probe_stays_expiry(monkeypatch):
    """A probe that itself fails must not replace the 403's expiry
    classification with its own network error."""
    adapter, calls = _status_adapter(
        monkeypatch, {"/users/self/count": 503, "/projects/2/messages": 403})
    with pytest.raises(mcs_contract.SessionExpired):
        adapter._request("GET", "/projects/2/messages", retries=0)


# ---------- station_staffs (own pharmacy roster) ----------

def _staff_page(users, has_next):
    return {"paginate": {"current_page": 1, "per_page": 100,
                         "timestamp": 0, "total_entries": len(users),
                         "total_pages": 1, "has_next": has_next},
            "users": users}


def test_station_staffs_paginates_and_skips_station_accounts():
    calls = []
    pages = {
        (7, 1): _staff_page([
            {"id": 1, "last_name": "山田", "first_name": "花子",
             "specialist_categories": [{"name": "薬剤師"}],
             "is_self": True, "is_station_account": False},
            {"id": 2, "last_name": "みどり", "first_name": "薬局",
             "specialist_categories": [], "is_station_account": True}],
            True),
        (7, 2): _staff_page([
            {"id": 3, "last_name": "佐藤", "first_name": "一郎",
             "specialist_categories": [{"name": "事務"}]}], False),
    }

    class Adapter(mcs_adapter.MCSAdapter):
        def _get(self, path, params=None, extend_session=True):
            calls.append((path, params, extend_session))
            sid = int(path.split("/")[2])
            return pages[(sid, params["page"])]

    staff = Adapter().station_staffs([{"id": 7, "name": "みどり薬局"},
                                      {"id": "bad", "name": "x"}])
    assert staff == [
        {"staff_id": 1, "name": "山田 花子", "professions": ["薬剤師"],
         "station": "みどり薬局", "is_self": True},
        {"staff_id": 3, "name": "佐藤 一郎", "professions": ["事務"],
         "station": "みどり薬局", "is_self": False}]
    assert [c[0] for c in calls] == ["/stations/7/staffs"] * 2
    assert all(c[1]["per_page"] == 100 and c[2] is False for c in calls)


@_pytest.mark.parametrize("dropped", [
    {"id": 88, "is_self": True},                                   # no name
    {"id": "x", "last_name": "合成", "is_self": True},              # invalid id
    {"id": 88, "last_name": "合成", "is_self": True, "is_station_account": True},
])
def test_strict_self_refuses_a_self_row_the_roster_would_drop(dropped):
    """A write's identity proof: a skipped second self must not leave one that looks unique."""
    page = _staff_page([{"id": 77, "last_name": "合成", "first_name": "一", "is_self": True},
                        dropped], False)

    class Adapter(mcs_adapter.MCSAdapter):
        def _get(self, path, params=None, extend_session=True):
            return page

    st = [{"id": 7, "name": "合成薬局"}]
    assert [r["staff_id"] for r in Adapter().station_staffs(st)] == [77]   # read contract unchanged
    with _pytest.raises(mcs_contract.SchemaError):
        Adapter().station_staffs(st, strict_self=True)


def test_station_staffs_failure_kinds():
    import pytest

    class Down(mcs_adapter.MCSAdapter):
        def _get(self, path, params=None, extend_session=True):
            raise mcs_contract.MCSError("http_error", "500", status=500)

    class Expired(mcs_adapter.MCSAdapter):
        def _get(self, path, params=None, extend_session=True):
            raise mcs_contract.SessionExpired("token rejected")

    class Bad(mcs_adapter.MCSAdapter):
        def _get(self, path, params=None, extend_session=True):
            return {"users": "nope"}

    st = [{"id": 7, "name": "みどり薬局"}]
    with pytest.raises(mcs_contract.MCSError) as e:
        Down().station_staffs(st)
    assert e.value.kind == "station_staffs_unavailable"
    with pytest.raises(mcs_contract.SessionExpired):
        Expired().station_staffs(st)
    with pytest.raises(mcs_contract.SchemaError):
        Bad().station_staffs(st)


# ---- F-5: unread screen cap (80 rows) ----

def _raw_msg(mid):
    return {"id": mid, "comment": "synthetic", "user": {"id": 1},
            "created_at": "2026-09-19T00:00:00+09:00", "is_unread": True}


class _UnreadPagesAdapter(mcs_adapter.MCSAdapter):
    def __init__(self, pages, total=None):
        self.pages = pages          # list of message-count per page
        self.total = total

    def _get(self, path, params=None, extend_session=True):
        page = params["page"]
        n = self.pages[page - 1] if page <= len(self.pages) else 0
        base = (page - 1) * 100
        pag = {"has_next": page < len(self.pages)}
        if self.total is not None:
            pag["total_entries"] = self.total
        return {"messages": [_raw_msg(base + i + 1) for i in range(n)],
                "paginate": pag}


@pytest.mark.parametrize(("pages", "total", "capped"), [
    ([10] * 8, None, True),        # exactly the 80-row screen cap
    ([10] * 8, 80, True),          # cap with an honest total
    ([10, 2], 12, False),          # complete short walk
    ([10, 2], 30, True),           # server reports more than it listed
    ([3], None, False),
])
def test_unread_walk_flags_screen_cap(pages, total, capped):
    batch = _UnreadPagesAdapter(pages, total).fetch_unread_messages(1, 123)
    assert batch.reached and batch.error is None
    assert batch.capped is capped
    assert len(batch.messages) == sum(pages)


def test_unread_walk_page_cap_error_is_not_capped_flag():
    batch = _UnreadPagesAdapter([10] * 30).fetch_unread_messages(1, 123, max_pages=3)
    assert batch.error is not None and batch.error.kind == "pages_exceeded"
    assert batch.capped is False


@pytest.mark.parametrize(("detail", "expect"), [
    ({"project": {"is_archived": False, "oldest_unread_message": {"id": 7}}}, 7),
    ({"project": {"is_archived": False, "oldest_unread_message": None}}, None),
    ({"data": {"project": {"is_archived": True, "oldest_unread_message": {"id": 9}}}}, 9),
])
def test_oldest_unread_id_reads_project_detail(detail, expect):
    adapter = mcs_adapter.MCSAdapter()
    adapter._get = lambda path, params=None, **k: detail
    assert adapter.oldest_unread_id(1) == expect


@pytest.mark.parametrize("detail", [
    {}, {"project": {}}, {"project": {"is_archived": False,
                                       "oldest_unread_message": {"id": "x"}}},
])
def test_oldest_unread_id_rejects_malformed_detail(detail):
    adapter = mcs_adapter.MCSAdapter()
    adapter._get = lambda path, params=None, **k: detail
    with pytest.raises(mcs_contract.SchemaError):
        adapter.oldest_unread_id(1)


# ---- F-5: fallback acknowledgement when the unread route rejects a capped project ----

def _mark_adapter(unread_kind, calls):
    adapter = mcs_adapter.MCSAdapter()

    def get(path, params=None, **k):
        calls.append((path, dict(params or {})))
        if path.endswith("/messages") and params and params.get("unread") == 1:
            raise mcs_contract.MCSError(unread_kind, "GET messages", status=400)
        if path.endswith("/messages"):
            return {"messages": [], "paginate": {"has_next": False}}
        return {"project": {"is_archived": False, "oldest_unread_message": None}}
    adapter._get = get
    return adapter


def test_mark_read_fallback_uses_plain_list_only_when_allowed():
    calls = []
    adapter = _mark_adapter("http_error", calls)
    assert adapter.mark_patient_read(1, 123, fallback_plain=True)["project"]["is_archived"] is False
    plain = [p for path, p in calls if path.endswith("/messages") and "unread" not in p]
    assert plain == [{"timestamp": 123, "per_page": 1, "page": 1,
                      "include_paginate_totals": 0}]
    assert "keep_read_status" not in plain[0]        # the read must clear the flag


def test_mark_read_without_fallback_or_other_kinds_raises():
    calls = []
    with pytest.raises(mcs_contract.MCSError) as e:
        _mark_adapter("http_error", calls).mark_patient_read(1, 123)
    assert e.value.kind == "http_error" and len(calls) == 1
    calls.clear()
    with pytest.raises(mcs_contract.MCSError) as e:
        _mark_adapter("network_error", calls).mark_patient_read(1, 123, fallback_plain=True)
    assert e.value.kind == "network_error" and len(calls) == 1


# ---------- 連携サマリー: GET /kartes/{id}/memo_summary ----------

def _memo_adapter(memo_summary, calls=None):
    adapter = mcs_adapter.MCSAdapter()

    def _get(path, params=None, **k):
        if calls is not None:
            calls.append(path)
        return {"memo_summary": memo_summary}
    adapter._get = _get
    return adapter


def test_fetch_memo_summary_keeps_only_the_contract_fields():
    calls = []
    out = _memo_adapter({
        "is_editable": True, "is_read": False, "read_style": "multi_line",
        "comment": "合成サマリー", "updated_at": "2026-09-30T10:00:00+09:00",
        "user": {"profession": "看護師", "name": "合成 花子", "id": 9,
                 "email": "never@example.invalid"},
        "extra": {"dropped": True},
    }, calls).fetch_memo_summary(77)

    assert calls == ["/kartes/77/memo_summary"]
    assert out == {"comment": "合成サマリー",
                   "updated_at": "2026-09-30T10:00:00+09:00",
                   "is_editable": True,
                   "user": {"profession": "看護師", "name": "合成 花子"}}


def test_fetch_memo_summary_unregistered_form_is_none():
    assert _memo_adapter({"is_editable": True, "is_read": True,
                          "read_style": "single_line"}).fetch_memo_summary(1) is None


def test_fetch_memo_summary_truncates_oversize_comment():
    out = _memo_adapter({"is_editable": False, "is_read": True,
                         "read_style": "single_line",
                         "comment": "あ" * 1000}).fetch_memo_summary(1)
    assert len(out["comment"]) == mcs_adapter.KARTE_SUMMARY_MAX_CHARS
    assert out["updated_at"] == "" and out["user"] == {"profession": "",
                                                        "name": ""}


@pytest.mark.parametrize("memo", [
    None,                                                   # object missing
    {"is_read": True, "read_style": "single_line"},         # is_editable
    {"is_editable": "yes", "is_read": True, "read_style": "single_line"},
    {"is_editable": True, "is_read": 1, "read_style": "single_line"},
    {"is_editable": True, "is_read": True},                 # read_style
    {"is_editable": True, "is_read": True, "read_style": "single_line",
     "comment": 5},
    {"is_editable": True, "is_read": True, "read_style": "single_line",
     "comment": "x", "user": "nurse"},
    {"is_editable": True, "is_read": True, "read_style": "single_line",
     "comment": "x", "updated_at": 7},
])
def test_fetch_memo_summary_rejects_bad_shapes(memo):
    with pytest.raises(mcs_contract.SchemaError):
        _memo_adapter(memo).fetch_memo_summary(1)


@pytest.mark.parametrize("karte_id", ["absent", None, "11", 0])
def test_project_row_without_usable_karte_id_is_tolerated(karte_id):
    # one legacy/unlinked row must not fail the whole /projects snapshot
    # (every tick would go 'failed'); it just gets no 連携サマリー
    row = _unread_project(11)
    if karte_id == "absent":
        del row["karte"]["id"]
    else:
        row["karte"]["id"] = karte_id
    snap = _UnreadListAdapter([(100, False, [row])]).list_unread()
    assert snap.patients[0].karte_id is None
    row["karte"] = None
    snap = _UnreadListAdapter([(100, False, [row])]).list_unread()
    assert snap.patients[0].karte_id is None
    row["karte"] = "x"
    with pytest.raises(mcs_contract.SchemaError, match="karte invalid"):
        _UnreadListAdapter([(100, False, [row])]).list_unread()


def test_project_row_karte_id_lands_on_unread_patient():
    snap = _UnreadListAdapter([(100, False, [_unread_project(11)])]).list_unread()
    assert snap.patients[0].karte_id == 110


def test_history_page_observations_precede_filter_and_preserve_empty_page_and_order():
    import history_walk
    class Adapter(mcs_adapter.MCSAdapter):
        def _get(self,path,params=None,extend_session=True):
            return {'messages': [{'id':9,'comment':'old','created_at':'2020-01-01T00:00:00+09:00'},
                                  {'id':2,'comment':'old','created_at':'2020-01-01T00:00:00+09:00'}]
                    if params['page'] == 1 else [],
                    'paginate':{'has_next':params['page'] == 1}}
    batch = Adapter().fetch_history(1,_CUTOFF,max_pages=3)
    assert batch.messages == [] and batch.pages == 2 and batch.reached
    assert batch.page_observations == [history_walk.observation(1,_CUTOFF,1,10,True,[9,2]),
                                       history_walk.observation(1,_CUTOFF,2,10,False,[])]


def test_history_failed_half_page_never_publishes_partial_observation():
    class Adapter(mcs_adapter.MCSAdapter):
        def _get(self,path,params=None,extend_session=True):
            return {'messages':[{'id':1,'comment':'synthetic'}, {'id':None}],
                    'paginate':{'has_next':False}}
    batch = Adapter().fetch_history(1,0,max_pages=3)
    assert batch.messages == [] and batch.pages == 0 and batch.page_observations == []
    assert batch.error.kind == 'schema_error' and not batch.reached
