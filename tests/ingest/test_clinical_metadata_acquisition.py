"""Clinical rotation at the real adapter worker boundary, entirely synthetic."""
import base64
import json
import time
from urllib.parse import parse_qs, urlsplit

import pytest

import ledger
from mcs_adapter import MCSAdapter
import project_metadata as metadata
from project_metadata_view import get_project_metadata


class Wire:
    def __init__(self, items=250):
        self.items = items
        self.value_rows = 1
        self.value_rows_by_item = {}
        self.calls = []
        self.fail = None
        self.on_call = None
        self.generation = "first"
        self.timestamp = 12345
        self.values_definition = False

    def __call__(self, request, **kwargs):
        url = urlsplit(request["url"])
        params = parse_qs(url.query)
        self.calls.append(request)
        assert request["operation"] == "api" and request["method"] == "GET"
        assert params["no_extend_session"] == ["1"] and request["data"] is None
        assert url.hostname == "www.medical-care.net"
        if self.on_call:
            self.on_call(url.path)
        if self.fail and self.fail in url.path:
            return {"status": 500, "body": "", "headers": {}}
        if url.path.endswith("medication_periods"):
            body = {"medication_periods": [{"begin_date": "2026-10-01", "end_date": None,
                    "medicine_informations": [{"id": 1, "name": "完全合成薬",
                                                "secret": "DROP_CANARY"}]}]}
        else:
            page = int(params["page"][0])
            if url.path.endswith("observation_items"):
                key = "observation_items"
                start = (page - 1) * 50
                rows = [{"lab_test_item": {"id": item, "name": self.generation + str(item),
                                           "input_type": "scalar", "unit": "fictional-unit"}}
                        for item in range(start + 1, min(start + 50, self.items) + 1)]
                total, pages = self.items, max(1, (self.items + 49) // 50)
            else:
                assert url.path.endswith("/values")
                key, total = "observation_values", self.value_rows_by_item.get(
                    int(url.path.split("/")[-2]), self.value_rows)
                pages = max(1, (total + 49) // 50)
                rows = [{"observation_issued_at": "2026-10-07T10:00:00+09:00",
                         "scalar": 123, "contact": "DROP_CANARY"}
                        for _ in range((page - 1) * 50, min(page * 50, total))]
            body = {key: rows, "paginate": {"current_page": page, "per_page": 50,
                    "has_next": page < pages, "timestamp": self.timestamp,
                    "total_entries": total, "total_pages": pages}}
            if key == "observation_values" and page == 1 and self.values_definition:
                item = int(url.path.split("/")[-2])
                body["observation_item"] = {"lab_test_item": {
                    "id": item, "name": self.generation + str(item),
                    "input_type": "scalar", "unit": "fictional-unit"}}
        return {"status": 200, "body": base64.b64encode(json.dumps(body).encode()).decode(),
                "headers": {}}


@pytest.fixture
def world(tmp_path, monkeypatch):
    clock = [1790000000.0]
    monkeypatch.setattr(metadata.time, "time", lambda: clock[0])
    monkeypatch.setattr(MCSAdapter, "_sleep_bounded", lambda *_args: None)
    db = ledger.Ledger(str(tmp_path / "synthetic.db"))
    for pid in (1, 2, 3, 4, 5):
        db.ensure_patient(pid)
    with db.db:
        db.db.execute("UPDATE patients SET project_type='medical',karte_id=project_id*10")
        db.db.execute("UPDATE patients SET is_archived=1 WHERE project_id=3")
        db.db.execute("UPDATE patients SET project_type='group' WHERE project_id=4")
        db.db.execute("UPDATE patients SET karte_id=NULL WHERE project_id=5")
    wire = Wire()
    client = MCSAdapter(worker=wire, token_cache=str(tmp_path / "synthetic-cache.json"))
    client._write_cache("synthetic-session")
    yield db, client, wire, clock
    db.close()


def run(db, client, **options):
    return metadata.sync_clinical_metadata(
        db, client, enabled=options.pop("enabled", True),
        deadline=options.pop("deadline", time.monotonic() + 300), **options)


def progress(db, pid=1):
    return json.loads(db.artifacts(metadata.CLINICAL_PROGRESS_KIND, project_id=pid)[0]["content"])


def test_all_250_items_for_each_live_patient_rotate_with_http_budget_and_pins(world):
    db, client, wire, clock = world
    for _ in range(50):
        before = len(wire.calls)
        stats = run(db, client)
        assert stats["requests"] == len(wire.calls) - before <= metadata.CLINICAL_GET_CAP
        assert stats["failed"] == 0
        if all(progress(db, pid).get("next_due_at", 0) > clock[0] for pid in (1, 2)):
            break
    else:
        pytest.fail("the registered-item cursor did not converge")
    for pid in (1, 2):
        seen = {int(urlsplit(request["url"]).path.split("/")[-2])
                for request in wire.calls if f"/kartes/{pid * 10}/" in request["url"]
                and urlsplit(request["url"]).path.endswith("/values")}
        assert seen == set(range(1, 251))
        values = get_project_metadata(db.db, pid, "observation_values", item_id=250, now=clock[0])
        assert values["state"] == "complete" and values["rows"][0]["scalar"] == 123
        assert values["rows"][0]["observation_issued_at"] == "2026-10-07T10:00:00+09:00"
        assert values["definition"]["lab_test_item"]["unit"] == "fictional-unit"
        assert values["source"] == "mcs_structured"
    assert len(db.artifacts(metadata.CLINICAL_PROGRESS_KIND)) == 2
    assert all(not any(f"/kartes/{kid}/" in call["url"] for kid in (30, 40, 50)) for call in wire.calls)
    assert "DROP_CANARY" not in "".join(row["content"] for row in db.artifacts(metadata.ARTIFACT_KIND))
    before = len(wire.calls)
    assert run(db, client)["requests"] == 0 and len(wire.calls) == before
    assert client._token is None and client._deadline is None


@pytest.mark.parametrize("option", ["off", "no_cache", "deadline"])
def test_disabled_uncredentialed_or_deadline_stage_does_not_get_or_write(world, option):
    db, client, wire, _ = world
    kwargs = {}
    if option == "off":
        kwargs["enabled"] = False
    elif option == "no_cache":
        client._read_cache = lambda: None
    else:
        kwargs["deadline"] = time.monotonic() + 10
    assert run(db, client, **kwargs)["requests"] == 0
    assert wire.calls == [] and db.artifacts(metadata.ARTIFACT_KIND) == []


def test_budget_hold_keeps_cursor_and_does_not_store_partial_clinical_attempt(world, monkeypatch):
    db, client, wire, _ = world
    with db.db:
        db.db.execute("UPDATE patients SET is_archived=1 WHERE project_id=2")
    monkeypatch.setattr(metadata, "CLINICAL_GET_CAP", 3)
    stats = run(db, client)
    assert stats["held"] == "budget" and stats["requests"] == 3
    assert progress(db)["phase"] == "observation_items"
    attempts = db.artifacts(metadata.ARTIFACT_KIND)
    assert len(attempts) == 1 and json.loads(attempts[0]["content"])["dataset"] == "medication_periods"
    monkeypatch.setattr(metadata, "CLINICAL_GET_CAP", 12)
    assert run(db, client)["failed"] == 0
    assert progress(db)["phase"] == "observation_values" and progress(db)["item_cursor"] > 0


def test_deadline_during_item_walk_is_a_resumable_hold_without_failure(world, monkeypatch):
    db, client, wire, _ = world
    with db.db:
        db.db.execute("UPDATE patients SET is_archived=1 WHERE project_id=2")
    mono = [100.0]
    monkeypatch.setattr(metadata.time, "monotonic", lambda: mono[0])
    wire.on_call = lambda path: mono.__setitem__(0, 126.0) if path.endswith("observation_items") else None
    stats = run(db, client)
    assert stats["held"] == "deadline" and stats["failed"] == 0 and stats["requests"] == 2
    assert len(db.artifacts(metadata.ARTIFACT_KIND)) == 1
    assert progress(db)["phase"] == "observation_items" and progress(db)["next_due_at"] == 0
    wire.on_call = None
    assert run(db, client)["failed"] == 0
    assert progress(db)["item_cursor"] > 0


@pytest.mark.parametrize("raw", ["[", '{"karte_id":10,"next_due_at":"unknown"}',
                                 '{"karte_id":10,"next_due_at":999999999999}'])
def test_unknown_progress_resumes_without_inventing_a_future_wait(world, raw):
    db, client, _, _ = world
    with db.db:
        db.db.execute("UPDATE patients SET is_archived=1 WHERE project_id=2")
    db.artifact_add(metadata.CLINICAL_PROGRESS_KIND, raw, project_id=1)
    assert run(db, client)["complete"] > 0
    assert progress(db)["karte_id"] == 10


def test_failed_refresh_preserves_last_success_and_moves_to_other_patient(world):
    db, client, wire, clock = world
    wire.items = 1
    run(db, client)
    old = get_project_metadata(db.db, 1, "medication_periods", now=clock[0])
    assert old["current_known"]
    clock[0] += metadata.CLINICAL_REFRESH_S
    wire.fail = "/kartes/10/medication_periods"
    stats = run(db, client)
    assert stats["failed"] == 1 and stats["requests"] <= metadata.CLINICAL_GET_CAP
    failed = get_project_metadata(db.db, 1, "medication_periods", now=clock[0])
    assert failed["state"] == "failed" and failed["historical"]
    assert failed["rows"] == old["rows"] and not failed["current_known"]
    assert progress(db)["next_due_at"] == clock[0] + metadata.CLINICAL_BACKOFF_S
    assert progress(db, 2)["next_due_at"] > clock[0]


def test_definition_generation_and_karte_change_reset_old_cursor(world):
    db, client, wire, _ = world
    with db.db:
        db.db.execute("UPDATE patients SET is_archived=1 WHERE project_id=2")
    run(db, client)
    old_generation = progress(db)["items_generation"]
    assert progress(db)["item_cursor"] > 0
    client._token = "synthetic-session"
    wire.generation = "changed"
    metadata.sync_metadata(db, client, 1, "observation_items", enabled=True, per_page=50)
    wire.calls.clear()
    run(db, client)
    assert progress(db)["items_generation"] != old_generation
    values = [call for call in wire.calls if urlsplit(call["url"]).path.endswith("/values")]
    assert "/observation_items/1/values" in values[0]["url"]
    with db.db:
        db.db.execute("UPDATE patients SET karte_id=999 WHERE project_id=1")
    wire.calls.clear()
    run(db, client)
    assert "/kartes/999/medication_periods" in wire.calls[0]["url"]
    assert progress(db)["karte_id"] == 999
    assert get_project_metadata(db.db, 1, "observation_values", item_id=250)["state"] == "unknown"


def test_equal_definition_refresh_keeps_cursor_and_proves_old_value_definition(world):
    db, client, wire, _ = world
    with db.db:
        db.db.execute("UPDATE patients SET is_archived=1 WHERE project_id=2")
    run(db, client)
    previous = progress(db)
    old_value = next(json.loads(row["content"]) for row in db.artifacts(metadata.ARTIFACT_KIND)
                     if json.loads(row["content"])["dataset"] == "observation_values")
    client._token = "synthetic-session"
    metadata.sync_metadata(db, client, 1, "observation_items", enabled=True, per_page=50)
    generation, items = metadata._clinical_items(db.db, 1, 10)
    assert generation != old_value["definition_source_artifact_id"]
    assert metadata.clinical_definition_fingerprint(10, items) == old_value["definition_fingerprint"]
    wire.calls.clear()
    run(db, client)
    values = [call for call in wire.calls if urlsplit(call["url"]).path.endswith("/values")]
    assert f"/observation_items/{previous['item_cursor'] + 1}/values" in values[0]["url"]
    assert progress(db)["items_fingerprint"] == previous["items_fingerprint"]


def test_definition_fingerprint_ignores_row_order_and_equal_number_representation():
    rows = metadata.normalize_rows("observation_items", [
        {"lab_test_item": {"id": 1, "unit": "fictional"}, "upper_reference_limit_scalar": 100},
        {"lab_test_item": {"id": 2, "unit": "fictional"}}])
    before = metadata.clinical_definition_fingerprint(10, {row["lab_test_item"]["id"]: row for row in rows})
    rows[0]["upper_reference_limit_scalar"] = 100.0
    after = metadata.clinical_definition_fingerprint(10, {row["lab_test_item"]["id"]: row for row in reversed(rows)})
    assert before == after
    rows[0]["upper_reference_limit_scalar"] = 101
    assert metadata.clinical_definition_fingerprint(
        10, {row["lab_test_item"]["id"]: row for row in rows}) != before


def test_large_values_stage_until_terminal_page_then_publish_all_rows(world):
    db, client, wire, clock = world
    wire.items = 1
    with db.db:
        db.db.execute("UPDATE patients SET is_archived=1 WHERE project_id=2")
    run(db, client)
    clock[0] += metadata.CLINICAL_REFRESH_S
    wire.value_rows = 300
    stats = run(db, client)
    assert stats["failed"] == 0 and stats["held"] == "page_slice"
    attempts = [json.loads(row["content"]) for row in db.artifacts(metadata.ARTIFACT_KIND)
                if json.loads(row["content"])["dataset"] == "observation_values"]
    assert len(attempts) == 1
    assert attempts[0]["complete"] is True and attempts[0]["rows"]
    assert progress(db)["staging"]["next_page"] == 6
    assert len(progress(db)["staging"]["rows"]) == 250
    calls_before = len(wire.calls)
    assert run(db, client)["failed"] == 0
    resumed = parse_qs(urlsplit(wire.calls[calls_before]["url"]).query)
    assert resumed["page"] == ["6"] and resumed["timestamp"] == ["12345"]
    attempts = [json.loads(row["content"]) for row in db.artifacts(metadata.ARTIFACT_KIND)
                if json.loads(row["content"])["dataset"] == "observation_values"]
    assert len(attempts) == 2 and attempts[-1]["complete"] is True
    assert len(attempts[-1]["rows"]) == 300 and "staging" not in progress(db)
    assert len(get_project_metadata(db.db, 1, "observation_values", item_id=1, now=clock[0])["rows"]) == 300


def test_failed_item_discovery_does_not_use_old_definition_for_new_value_get(world):
    db, client, wire, clock = world
    wire.items = 1
    with db.db:
        db.db.execute("UPDATE patients SET is_archived=1 WHERE project_id=2")
    run(db, client)
    clock[0] += metadata.CLINICAL_REFRESH_S
    wire.fail = "/observation_items"
    wire.calls.clear()
    assert run(db, client)["failed"] == 1
    assert not any(urlsplit(call["url"]).path.endswith("/values") for call in wire.calls)
    clock[0] += metadata.CLINICAL_BACKOFF_S
    wire.fail = None
    wire.calls.clear()
    assert run(db, client)["failed"] == 0
    assert any(urlsplit(call["url"]).path.endswith("/values") for call in wire.calls)


@pytest.mark.parametrize("failure", ["permanent_500", "capacity_10001"])
def test_one_failed_value_item_does_not_hold_later_items_and_is_retried(world, failure):
    db, client, wire, clock = world
    wire.items = 250
    with db.db:
        db.db.execute("UPDATE patients SET is_archived=1 WHERE project_id=2")
    if failure == "permanent_500":
        wire.fail = "/observation_items/1/values"
    else:
        wire.value_rows_by_item[1] = 10001
    stats = run(db, client)
    assert stats["failed"] == 1
    assert progress(db)["next_due_at"] == 0, "one failed item must not pause later items"
    for _ in range(25):
        stats = run(db, client)
        assert stats["requests"] <= metadata.CLINICAL_GET_CAP
        if progress(db)["next_due_at"] > clock[0]:
            break
    assert progress(db)["next_due_at"] == clock[0] + metadata.CLINICAL_BACKOFF_S
    attempts = [json.loads(row["content"]) for row in db.artifacts(metadata.ARTIFACT_KIND)
                if json.loads(row["content"])["dataset"] == "observation_values"]
    assert {attempt["item_id"] for attempt in attempts if attempt["complete"]} == set(range(2, 251))
    failed = [attempt for attempt in attempts if attempt["item_id"] == 1]
    assert len(failed) == 1 and failed[0]["complete"] is False and failed[0]["rows"] == []
    assert failed[0]["reason"] == ("http_error" if failure == "permanent_500" else "capacity_limit")
    calls_before = len(wire.calls)
    assert run(db, client)["requests"] == 0 and len(wire.calls) == calls_before
    clock[0] += metadata.CLINICAL_BACKOFF_S
    stats = run(db, client)
    assert stats["failed"] == 1 and stats["requests"] <= metadata.CLINICAL_GET_CAP
    if failure == "capacity_10001":
        assert stats["limited"] == 1
        assert stats["limits"]["value_rows"] == 10000


@pytest.mark.parametrize("fault", ["checksum", "policy", "stage_definition", "timestamp", "total", "definition", "karte"])
def test_staged_values_never_flow_into_changed_snapshot_or_mapping(world, fault):
    db, client, wire, clock = world
    wire.items, wire.value_rows = 1, 300
    wire.values_definition = True
    with db.db:
        db.db.execute("UPDATE patients SET is_archived=1 WHERE project_id=2")
    assert run(db, client)["held"] == "page_slice"
    if fault in ("checksum", "policy", "stage_definition"):
        state = progress(db)
        if fault == "checksum":
            state["staging"]["rows"][0]["scalar"] = 999
        elif fault == "policy":
            state["staging"]["per_page"] = 20
            state["staging"]["sha256"] = metadata._stage_hash(state["staging"])
        else:
            state["staging"]["definition"]["lab_test_item"]["id"] = 999
            state["staging"]["sha256"] = metadata._stage_hash(state["staging"])
        metadata._clinical_progress(db, 1, state)
    elif fault == "timestamp":
        wire.timestamp += 1
    elif fault == "total":
        wire.value_rows = 350
    elif fault == "definition":
        client._token = "synthetic-session"
        wire.generation = "changed"
        metadata.sync_metadata(db, client, 1, "observation_items", enabled=True, per_page=50)
    else:
        with db.db:
            db.db.execute("UPDATE patients SET karte_id=999 WHERE project_id=1")
    before = len(wire.calls)
    stats = run(db, client)
    if fault in ("checksum", "policy", "stage_definition"):
        assert stats["held"] == "staging_invalid" and "staging" not in progress(db)
        assert len(wire.calls) == before
    elif fault in ("timestamp", "total"):
        assert stats["failed"] == 1 and "staging" not in progress(db)
        clock[0] += metadata.CLINICAL_BACKOFF_S
        before = len(wire.calls)
        assert run(db, client)["held"] == "page_slice"
        restarted = next(call for call in wire.calls[before:] if urlsplit(call["url"]).path.endswith("/values"))
        assert parse_qs(urlsplit(restarted["url"]).query)["page"] == ["1"]
    else:
        value_calls = [call for call in wire.calls[before:] if urlsplit(call["url"]).path.endswith("/values")]
        assert parse_qs(urlsplit(value_calls[0]["url"]).query)["page"] == ["1"]
        if fault == "karte":
            assert "/kartes/999/" in value_calls[0]["url"]
    assert not any(json.loads(row["content"])["dataset"] == "observation_values"
                   and json.loads(row["content"])["complete"] for row in db.artifacts(metadata.ARTIFACT_KIND))


def test_staging_holds_rotate_to_other_patient_and_resume_from_checkpoint(world, monkeypatch):
    db, client, wire, _ = world
    wire.items, wire.value_rows = 1, 300
    monkeypatch.setattr(metadata, "CLINICAL_GET_CAP", 5)
    for _ in range(10):
        stats = run(db, client)
        assert stats["requests"] <= 5
        completed = [json.loads(row["content"]) for row in db.artifacts(metadata.ARTIFACT_KIND)
                     if json.loads(row["content"])["dataset"] == "observation_values"]
        if len(completed) == 2:
            break
    assert {row["entity_id"] for row in completed} == {10, 20}
    assert all(row["complete"] is True and len(row["rows"]) == 300 for row in completed)
    for kid in (10, 20):
        pages = [int(parse_qs(urlsplit(call["url"]).query)["page"][0]) for call in wire.calls
                 if f"/kartes/{kid}/" in call["url"] and urlsplit(call["url"]).path.endswith("/values")]
        assert pages == [1, 2, 3, 4, 5, 6]


def test_value_capacity_boundary_collects_ten_thousand_rows(world):
    db, client, wire, _ = world
    wire.items, wire.value_rows = 1, metadata.MAX_VALUE_ROWS
    with db.db:
        db.db.execute("UPDATE patients SET is_archived=1 WHERE project_id=2")
    for _ in range(45):
        stats = run(db, client)
        assert stats["requests"] <= metadata.CLINICAL_GET_CAP and stats["failed"] == 0
        complete = [json.loads(row["content"]) for row in db.artifacts(metadata.ARTIFACT_KIND)
                    if json.loads(row["content"])["dataset"] == "observation_values"]
        if complete:
            break
    assert len(complete) == 1 and complete[0]["complete"] is True
    assert len(complete[0]["rows"]) == metadata.MAX_VALUE_ROWS


def test_value_timeout_without_rows_never_becomes_registered_empty(world, monkeypatch):
    db, client, wire, _ = world
    wire.items = 1
    worker = wire.__call__

    def timeout(request, **kwargs):
        if urlsplit(request["url"]).path.endswith("/values"):
            raise TimeoutError("synthetic")
        return worker(request, **kwargs)

    client._worker = timeout
    with db.db:
        db.db.execute("UPDATE patients SET is_archived=1 WHERE project_id=2")
    stats = run(db, client)
    assert stats["failed"] == 1
    viewed = get_project_metadata(db.db, 1, "observation_values", item_id=1)
    assert viewed["state"] != "empty" and not viewed["current_known"]
    assert viewed["reason"] == "network_error"


def test_mapping_changes_during_get_cannot_publish_clinical_rows(world):
    db, client, wire, _ = world
    wire.on_call = lambda path: db.db.execute(
        "UPDATE patients SET karte_id=999 WHERE project_id=1") if "/kartes/10/" in path else None
    stats = run(db, client)
    assert stats["failed"] >= 1
    attempt = json.loads(db.artifacts(metadata.ARTIFACT_KIND, project_id=1)[0]["content"])
    assert attempt["complete"] is False and attempt["rows"] == []
    assert attempt["reason"] == "scope_changed"
