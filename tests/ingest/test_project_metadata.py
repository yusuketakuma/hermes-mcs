"""Synthetic wire, normalization and real artifact storage contracts."""
import base64
import copy
import json
import time
import urllib.parse

import pytest

from ledger import Ledger
from mcs_adapter import MCSAdapter
from mcs_contract import MCSError
from project_metadata_contract import ARTIFACT_KIND, normalize_rows
from project_metadata import sync_metadata


def page(key, rows, *, number=1, has_next=False, timestamp: int | None = 12345, **extra):
    return {key: rows, "paginate": {"current_page": number, "per_page": 20,
            "has_next": has_next, "timestamp": timestamp, **extra}}


def adapter(responses):
    """Fake at the existing reaped I/O boundary, not at the normalizer/store."""
    pending = iter(copy.deepcopy(responses))
    calls = []

    def worker(request, **kwargs):
        calls.append(request)
        response = next(pending)
        if isinstance(response, Exception):
            raise response
        status, body = response if isinstance(response, tuple) else (200, response)
        return {"status": status, "body": base64.b64encode(json.dumps(body).encode()).decode(),
                "headers": {}}

    client = MCSAdapter(worker=worker)
    client._token = "synthetic-session"
    return client, calls


@pytest.fixture
def store(tmp_path):
    db = Ledger(str(tmp_path / "synthetic.db"))
    for pid, kind, kid in [(1, "medical", 10), (2, "group", None), (3, "medical", None)]:
        db.ensure_patient(pid)
        db.db.execute("UPDATE patients SET project_type=?,karte_id=? WHERE project_id=?",
                      (kind, kid, pid))
    db.db.commit()
    yield db
    db.close()


MEMBER = {"id": 44, "type": "medical", "is_director": True, "is_self": False,
          "specialist_categories": [{"name": "synthetic nurse", "secret": "drop"}],
          "station": {"name": "synthetic facility", "phone": "drop"},
          "last_name": "NAME_CANARY", "first_name": "FIRST_CANARY",
          "icon_url": "PHOTO_CANARY", "email": "CONTACT_CANARY"}
LAB = {"lab_test_item": {"id": 55, "name": "synthetic analyte", "analyte_tag": "synthetic",
                        "input_type": "scalar", "unit": "synthetic-unit"},
       "upper_reference_limit_scalar": 100, "lower_reference_limit_scalar": 10,
       "summary_image_file_url": "PHOTO_CANARY"}
MEDICATION = {"begin_date": "2026-10-01", "end_date": None,
              "medicine_informations": [{"id": 66, "name": "synthetic medicine",
                                         "url": "CONTACT_CANARY"}],
              "prescriber": "NAME_CANARY"}


@pytest.mark.parametrize("dataset,key,pid,raw,path", [
    ("care_team", "users", 1, MEMBER, "/projects/1/members"),
    ("medication_periods", "medication_periods", 1, MEDICATION, "/kartes/10/medication_periods"),
    ("observation_items", "observation_items", 1, LAB, "/kartes/10/observation_items"),
    ("consultations", "consultations", 2,
     {"id": 77, "purpose": "question", "status": "open", "is_unread": True,
      "comment": "BODY_CANARY", "patient": {"id": 1}}, "/projects/2/consultations"),
])
def test_sync_uses_real_normalizers_storage_and_safe_get(store, dataset, key, pid, raw, path):
    # Given
    response = {key: [raw]} if dataset == "medication_periods" else page(key, [raw])
    client, calls = adapter([response])
    before = dict(store.db.execute("SELECT * FROM patients WHERE project_id=?", (pid,)).fetchone())
    # When
    result = sync_metadata(store, client, pid, dataset, enabled=True, now=100)
    # Then
    assert result["state"] == "complete"
    artifacts = store.artifacts(ARTIFACT_KIND, project_id=pid)
    payload = json.loads(artifacts[0]["content"])
    assert payload["complete"] and payload["attempted_at"] == 100
    serialized = json.dumps(payload)
    for canary in ("NAME_CANARY", "FIRST_CANARY", "PHOTO_CANARY", "CONTACT_CANARY", "BODY_CANARY"):
        assert canary not in serialized
    url = urllib.parse.urlsplit(calls[0]["url"])
    assert url.path == "/api/v2t" + path and url.hostname == "www.medical-care.net"
    assert urllib.parse.parse_qs(url.query)["no_extend_session"] == ["1"]
    assert calls[0]["method"] == "GET" and calls[0]["data"] is None
    assert len(calls) == 1
    assert dict(store.db.execute("SELECT * FROM patients WHERE project_id=?", (pid,)).fetchone()) == before
    assert payload["scope"] == ("group" if dataset == "consultations" else
                                 "project" if dataset == "care_team" else "karte")


@pytest.mark.parametrize("dataset,raw", [
    ("medication_periods", {"medicine_informations": [{"id": 1}] * 21}),
    ("observation_items", {**LAB, "upper_reference_limit_scalar": "100"}),
    ("observation_values", {"scalar": float("nan")}),
    ("consultations", {"id": 1, "is_unread": 1})])
def test_invalid_structured_values_fail_normalization(dataset, raw):
    # Given / When / Then
    from mcs_contract import SchemaError
    with pytest.raises(SchemaError):
        normalize_rows(dataset, [raw])


@pytest.mark.parametrize("pid,dataset,enabled,reason", [
    (1, "care_team", False, None), (1, "consultations", True, "association_unverified"),
    (2, "medication_periods", True, "association_unverified"),
    (3, "observation_items", True, "association_unverified"),
    (99, "care_team", True, "association_unverified")])
def test_unverified_or_disabled_targets_never_start_get(store, pid, dataset, enabled, reason):
    # Given
    client, calls = adapter([])
    # When
    result = sync_metadata(store, client, pid, dataset, enabled=enabled)
    # Then
    assert result.get("reason") == reason and calls == [] and not store.artifacts(ARTIFACT_KIND)


def test_sync_requires_cached_session_without_bootstrap(store):
    # Given
    client, calls = adapter([])
    client._token = None
    # When
    result = sync_metadata(store, client, 1, "care_team", enabled=True)
    # Then
    assert result["reason"] == "cached_session_required" and calls == []


def test_network_failure_is_safe_and_not_empty(store):
    # Given
    client, _ = adapter([])
    def fail(*args, **kwargs):
        raise MCSError("network_error", "BODY_CANARY")
    client._get = fail
    # When
    result = sync_metadata(store, client, 1, "care_team", enabled=True)
    # Then
    assert result["state"] == "failed" and result["reason"] == "network_error"
    assert "BODY_CANARY" not in store.artifacts(ARTIFACT_KIND)[0]["content"]


def test_parent_deadline_stops_reader_before_network(store):
    # Given
    client, calls = adapter([])
    deadline = time.monotonic() - 1
    client.set_deadline(deadline)
    # When
    result = sync_metadata(store, client, 1, "care_team", enabled=True)
    # Then
    assert result["reason"] == "deadline_exceeded" and calls == []
    assert client._deadline == deadline


def test_cli_name_opt_ins_are_explicit_and_care_team_only(store, tmp_path, capsys):
    # Given: a complete roster stored with the owner's sync-side name opt-in
    from project_metadata import main
    client, _ = adapter([page("users", [MEMBER])])
    sync_metadata(store, client, 1, "care_team", enabled=True, retain_names=True)
    store.db.commit()
    db = str(tmp_path / "synthetic.db")
    view = ["view", "--database", db, "--project-id", "1", "--dataset", "care_team"]
    # When / Then: names stay hidden unless the view also opts in
    assert main(view) == 0
    assert "NAME_CANARY" not in capsys.readouterr().out
    assert main([*view, "--show-names"]) == 0
    shown = capsys.readouterr().out
    assert "NAME_CANARY" in shown and "CONTACT_CANARY" not in shown
    for bad in ([*view, "--retain-names"],
                ["view", "--database", db, "--project-id", "1",
                 "--dataset", "medication_periods", "--show-names"]):
        with pytest.raises(SystemExit) as raised:
            main(bad)
        assert raised.value.code == 2


@pytest.mark.parametrize("dataset,key,valid,bad,item_id", [
    ("observation_items", "observation_items", LAB,
     {**LAB, "upper_reference_limit_scalar": 10**400}, None),
    ("observation_values", "observation_values", {"scalar": 12},
     {"scalar": 10**400}, 55),
])
def test_out_of_range_number_records_failure_without_losing_complete_artifact(
        store, dataset, key, valid, bad, item_id):
    client, calls = adapter([page(key, [valid]), page(key, [bad])])
    result = sync_metadata(store, client, 1, dataset, enabled=True, item_id=item_id, now=100)
    assert result["state"] == "complete"
    complete_id = result["artifact_id"]
    result = sync_metadata(store, client, 1, dataset, enabled=True, item_id=item_id, now=200)
    assert result["state"] == "failed" and result["reason"] == "schema_error"
    artifacts = store.artifacts(ARTIFACT_KIND, project_id=1)
    assert len(calls) == 2 and len(artifacts) == 2
    previous = next(a for a in artifacts if a["artifact_id"] == complete_id)
    assert json.loads(previous["content"])["complete"] is True
    failed = next(a for a in artifacts if a["artifact_id"] == result["artifact_id"])
    payload = json.loads(failed["content"])
    assert payload["rows"] == [] and payload["definition"] is None
    assert not payload["complete"]


@pytest.mark.parametrize("failure", [OSError, KeyboardInterrupt, SystemExit])
def test_cli_writer_close_failure_still_releases_owned_run_lock(tmp_path, monkeypatch, failure):
    import errno
    import os
    from types import SimpleNamespace
    import ledger
    import mcs_adapter
    import project_metadata
    import writer_lock

    database = tmp_path / "synthetic.db"
    database.write_bytes(b"synthetic, never opened")
    lock_path = str(tmp_path / "run.lock")
    acquire = writer_lock.acquire_run_lock
    owned = probe = None
    error = failure("synthetic close refusal")
    class BrokenStore:
        def close(self):
            raise error
    try:
        owned = acquire(lock_path)
        assert owned is not None
        monkeypatch.setattr(writer_lock, "acquire_run_lock", lambda path: owned)
        monkeypatch.setattr(ledger, "Ledger", lambda path: BrokenStore())
        monkeypatch.setattr(mcs_adapter, "MCSAdapter", lambda **kwargs: SimpleNamespace(
            _read_cache=lambda: "synthetic", set_deadline=lambda deadline: None))
        monkeypatch.setattr(project_metadata, "sync_metadata", lambda *args, **kwargs: {"state": "complete"})
        with pytest.raises(failure) as caught:
            project_metadata.main(["sync", "--database", str(database), "--project-id", "1",
                                   "--dataset", "medication_periods", "--read-only-get",
                                   "--token-cache", str(tmp_path / "synthetic-token.json")])
        assert caught.value is error
        with pytest.raises(OSError) as closed:
            os.fstat(owned)
        assert closed.value.errno == errno.EBADF
        probe = acquire(lock_path)
        assert probe is not None
        os.close(probe)
        probe = None
        assert database.read_bytes() == b"synthetic, never opened"
    finally:
        try:
            if probe is not None:
                try:
                    os.close(probe)
                except OSError as cleanup:
                    if cleanup.errno != errno.EBADF:
                        raise
        finally:
            if owned is not None:
                try:
                    os.close(owned)
                except OSError as cleanup:
                    if cleanup.errno != errno.EBADF:
                        raise


@pytest.mark.parametrize("witness", ["restored", "awaiting", "corrupt", "journal"])
@pytest.mark.parametrize("alias", [False, True])
def test_cli_sync_restore_hold_precedes_writer_and_adapter(tmp_path, monkeypatch, capsys, witness, alias):
    import os
    import ledger
    import mcs_adapter
    import project_metadata
    import update_journal
    import writer_lock

    if witness == "journal":
        state = update_journal.default_state()
        state["applying"] = {"restore_incomplete": True}
        (tmp_path / "update_state.json").write_text(json.dumps(state))
    elif witness == "corrupt":
        (tmp_path / "restore_pending.json").write_text("{invalid")
    else:
        (tmp_path / "restore_pending.json").write_text(json.dumps(
            {"by": "mcs_update", "phase": "restored" if witness == "restored" else "awaiting_consent"}))
    database = tmp_path / "synthetic.db"
    database.write_bytes(b"synthetic, never opened")
    directory = tmp_path
    if alias:
        directory = tmp_path / "alias"
        directory.symlink_to(tmp_path, target_is_directory=True)

    def forbidden(*args, **kwargs):
        pytest.fail("restore hold must precede writer and adapter construction")

    monkeypatch.setattr(ledger, "Ledger", forbidden)
    monkeypatch.setattr(mcs_adapter, "MCSAdapter", forbidden)
    assert project_metadata.main(["sync", "--database", str(directory / "synthetic.db"),
                                  "--project-id", "1", "--dataset", "medication_periods",
                                  "--read-only-get", "--token-cache", str(tmp_path / "t.json")]) == 1
    assert json.loads(capsys.readouterr().out) == {"state": "held", "reason": "restore_in_progress"}
    assert database.read_bytes() == b"synthetic, never opened"
    probe = writer_lock.acquire_run_lock(str(tmp_path / "run.lock"))
    assert probe is not None
    os.close(probe)
