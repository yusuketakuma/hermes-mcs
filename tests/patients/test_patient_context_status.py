"""Per-state patient context counts on a synthetic temp ledger: a partition, never done by subtraction."""
import ast
import inspect
import json
import subprocess
import sys
from pathlib import Path

import pytest

import inference_dependency_loss as dependency_loss
import patient_context_refresh as context
import patient_context_refresh_readiness as readiness
import patient_context_status as status
from extract_testkit import _ledger, _message
from inference_version import CURRENT_VERSION, VERSION_KEY

META = {VERSION_KEY: CURRENT_VERSION}
PRIVATE_NAME = "合成患者名-private"
PRIVATE_BODY = "合成本文-private-body"


@pytest.fixture
def ledger(tmp_path):
    led = _ledger(tmp_path)
    yield led
    led.close()


def _patients(ledger, *pids, archived=()):
    ledger.db.executemany("INSERT INTO patients(project_id,patient_name,is_archived) VALUES (?,?,?)",
                          [(pid, PRIVATE_NAME, pid in archived) for pid in pids])


def _source(ledger, pid, mid, body=PRIVATE_BODY):
    ledger.save_messages([_message(mid=mid, project_id=pid, body=body)])
    return dependency_loss.source_binding(ledger.db, pid, mid)


def _scan(ledger, pid, mid, content, *, binding=None, key=None):
    meta = {**META, "source_key": mid if key is None else key,
            **({"source_binding": binding} if binding is not None else {})}
    raw = content if isinstance(content, str) else json.dumps(content)
    return ledger.artifact_add(context.SCAN, raw, project_id=pid, message_id=mid, meta=meta)


def _progress(ledger, pid, content):
    raw = content if isinstance(content, str) else json.dumps(content)
    ledger.artifact_add(context.PROGRESS, raw, project_id=pid, meta=META)


def _published(ledger, pid, published=True):
    ledger.artifact_add(context.KIND, "{}", project_id=pid, meta={**META, "published": published})


def _assert_partition(result):
    assert sum(result["partition"].values()) == result["patients_total"]
    assert status.sanitize(result) == result


def test_every_patient_lands_in_exactly_one_bucket(ledger):
    _patients(ledger, *range(1, 9), archived=(8,))
    _published(ledger, 1)
    _published(ledger, 2, published=False)                                     # recorded, not published
    _progress(ledger, 3, {"failure": "patient_context_unsplittable_assertion", "held": True})
    _progress(ledger, 4, {"failure": "patient_context_length_stop", "held": False})
    _progress(ledger, 5, "{not json")                                          # malformed: never done
    _published(ledger, 6)
    ledger.artifact_add(dependency_loss.KIND, json.dumps({"gap": True}), project_id=6, meta={})
    # 7 has no record at all; 8 is archived and out of the total
    _published(ledger, 8)
    result = status.state_counts(ledger.db)
    assert result["patients_total"] == 7
    assert result["partition"] == {"invalid": 1, "held": 1, "failed": 1, "dependency_gap": 1,
                                   "published": 1, "in_progress": 1, "no_record": 1}
    assert result["summary_held"] == {"patients": 1, "codes": {"patient_context_unsplittable_assertion": 1}}
    assert result["summary_failed"] == {"patients": 1, "codes": {"patient_context_length_stop": 1}}
    # an unprovable gap row is pending per the dependency reader, not a retained record
    assert result["dependency_gap"] == {"patients": 1}
    assert result["dependency_loss"] == {"patients": 0, "releasability": "unclassified"}
    assert result["invalid_rows"] == 1
    _assert_partition(result)


def test_unprovable_coverage_row_is_a_pending_gap_not_a_loss(ledger):
    _patients(ledger, 1)
    _published(ledger, 1)
    ledger.artifact_add(dependency_loss.KIND, json.dumps({"gap": False, "coverage": True}), project_id=1, meta={})
    result = status.state_counts(ledger.db)
    assert result["dependency_loss"]["patients"] == 0
    assert result["partition"] == {**dict.fromkeys(status.PARTITION, 0), "dependency_gap": 1}
    assert status.has_problem(result)


def _retained_karte_gap(ledger, pid=1):
    """A replaced karte comment leaves a gap that only an exact audited scan resolves."""
    from mcs_queries import karte_source_record
    _patients(ledger, pid)
    with ledger.db:
        ledger.db.execute("UPDATE patients SET karte_id=17 WHERE project_id=?", (pid,))
    ledger.karte_summary_store(pid, 17, {"comment": "完全合成の原記録です。", "updated_at": "2026-09-20T09:00:00+09:00"})
    raw = ledger.db.execute("SELECT * FROM artifacts WHERE kind='karte_summary' ORDER BY artifact_id DESC LIMIT 1"
                            ).fetchone()
    source = karte_source_record(raw, pid)
    ledger.karte_summary_store(pid, 17, None)
    return source["source_key"]


def test_gap_resolved_by_an_audited_coverage_row_is_not_a_problem(ledger):
    key = _retained_karte_gap(ledger)
    _published(ledger, 1)
    pending = status.state_counts(ledger.db)
    assert pending["partition"] == {**dict.fromkeys(status.PARTITION, 0), "dependency_gap": 1}
    assert status.has_problem(pending)
    with ledger.db:
        new_id = ledger.artifact_add_tx("patient_context_scan_chunk_v1", '{"facts":[]}', project_id=1,
                                        meta={**META, "source_key": key})
        snapshot = dependency_loss.capture_tx(ledger.db, 1, 0, source_key=key, artifact_ids=[new_id])
        assert dependency_loss.record_tx(ledger.db, snapshot, audited_source=True) > 0
    resolved = status.state_counts(ledger.db)
    assert resolved["dependency_gap"] == {"patients": 0}
    assert resolved["partition"] == {**dict.fromkeys(status.PARTITION, 0), "published": 1}
    assert not status.has_problem(resolved)
    _assert_partition(resolved)


def test_retained_loss_record_is_counted_but_not_a_problem(ledger):
    _patients(ledger, 1)
    binding = _source(ledger, 1, 11)
    content_hash = ledger.db.execute("SELECT content_hash FROM messages WHERE message_id=11").fetchone()[0]
    origin = {"message_id": 11, "content_hash": content_hash, "revision": 1, "binding": binding}
    with ledger.db:
        ledger.db.execute("UPDATE patients SET is_archived=0 WHERE project_id=1")
        assert dependency_loss.record_tx(ledger.db, {
            "project_id": 1, "message_id": 11, "gaps": [], "records": [{
                "origin": origin, "entity": "a" * 64, "axis": "value", "day": None, "support": "s"}]}) == 1
    _published(ledger, 1)
    assert dependency_loss.read(ledger.db, 1)["losses"]
    result = status.state_counts(ledger.db)
    assert result["dependency_loss"] == {"patients": 1, "releasability": "unclassified"}
    assert result["dependency_gap"] == {"patients": 0}
    assert result["partition"] == {**dict.fromkeys(status.PARTITION, 0), "published": 1}
    assert not status.has_problem(result)


def test_plain_tuple_connection_reads_the_same_and_keeps_its_row_factory(ledger, tmp_path):
    import sqlite3
    _patients(ledger, 1, 2)
    _scan(ledger, 1, 11, {"failure": "patient_context_invalid_items"}, binding=_source(ledger, 1, 11))
    with ledger.db:
        ledger.db.execute("UPDATE patients SET karte_id=18 WHERE project_id=2")
    ledger.karte_summary_store(2, 18, {"comment": "完全合成の原記録です。", "updated_at": "2026-09-20T09:00:00+09:00"})
    karte = ledger.db.execute("SELECT MAX(artifact_id) FROM artifacts WHERE kind='karte_summary'").fetchone()[0]
    _scan(ledger, 2, None, {"held": True}, key=f"karte:{karte}")    # original-source rows are read by column name
    ledger.db.commit()
    expected = status.state_counts(ledger.db)
    path = ledger.db.execute("PRAGMA database_list").fetchone()[2]
    plain = sqlite3.connect(path)
    try:
        assert plain.row_factory is None
        assert status.state_counts(plain) == expected
        assert plain.row_factory is None
    finally:
        plain.close()
    assert expected["invalid_rows"] == 0 and expected["scan_failed"]["sources"] == 1
    assert expected["scan_held"]["sources"] == 1


@pytest.mark.parametrize("kind", ["progress", "scan", "dependency"])
def test_malformed_rows_are_counted_and_never_done(ledger, kind):
    _patients(ledger, 1)
    _published(ledger, 1)
    before = status.state_counts(ledger.db)
    if kind == "progress":
        _progress(ledger, 1, "[1, 2")
    elif kind == "scan":
        _source(ledger, 1, 11)
        _scan(ledger, 1, 11, "{broken")
    else:
        ledger.artifact_add(dependency_loss.KIND, "{broken", project_id=1, meta={})
    result = status.state_counts(ledger.db)
    assert before["partition"]["published"] == 1
    assert result["invalid_rows"] == 1
    assert result["partition"]["invalid"] == 1 and result["partition"]["published"] == 0
    for done_like in ("published", "in_progress"):
        assert result["partition"][done_like] <= before["partition"][done_like]
    _assert_partition(result)


def test_wrongly_typed_stored_fields_are_unreadable(ledger):
    _patients(ledger, 1, 2)
    _progress(ledger, 1, {"failure": 7})
    _source(ledger, 2, 21)
    _scan(ledger, 2, 21, {"held": "yes"})
    result = status.state_counts(ledger.db)
    assert result["invalid_rows"] == 2 and result["partition"]["invalid"] == 2


def test_scan_failure_counts_only_while_the_source_is_unchanged(ledger):
    _patients(ledger, 1)
    binding = _source(ledger, 1, 11)
    _scan(ledger, 1, 11, {"complete": False, "failure": "patient_context_invalid_items"}, binding=binding)
    result = status.state_counts(ledger.db)
    assert result["scan_failed"] == {"patients": 1, "sources": 1, "codes": {"patient_context_invalid_items": 1}}
    assert result["partition"]["failed"] == 1
    _source(ledger, 1, 11, body="合成の書き換え後の本文")          # same message, new occurrence
    changed = status.state_counts(ledger.db)
    assert changed["scan_failed"]["sources"] == 0
    # the edit invalidates the stale scan and records an unanalysed dependency gap: never done
    assert changed["partition"] == {**dict.fromkeys(status.PARTITION, 0), "dependency_gap": 1}
    _assert_partition(changed)


def test_scan_failure_bound_to_another_occurrence_is_not_counted(ledger):
    _patients(ledger, 1)
    _source(ledger, 1, 11)
    _scan(ledger, 1, 11, {"failure": "patient_context_invalid_items"}, binding="0" * 64)
    result = status.state_counts(ledger.db)
    assert result["scan_failed"]["sources"] == 0
    assert result["partition"] == {**dict.fromkeys(status.PARTITION, 0), "in_progress": 1}
    _assert_partition(result)


def test_scan_failure_of_deleted_or_emptied_source_is_not_counted(ledger):
    _patients(ledger, 1, 2)
    for pid, mid in ((1, 11), (2, 21)):
        _scan(ledger, pid, mid, {"failure": "patient_context_invalid_items"}, binding=_source(ledger, pid, mid))
    ledger.db.execute("UPDATE messages SET body_state='deleted' WHERE message_id=11")
    ledger.db.execute("UPDATE messages SET body_text='  ' WHERE message_id=21")
    result = status.state_counts(ledger.db)
    assert result["scan_failed"]["sources"] == 0
    assert result["partition"] == {**dict.fromkeys(status.PARTITION, 0), "in_progress": 2}


def test_latest_scan_supersedes_and_audited_scan_is_healthy(ledger):
    _patients(ledger, 1)
    binding = _source(ledger, 1, 11)
    _scan(ledger, 1, 11, {"failure": "patient_context_invalid_items"}, binding=binding)
    _scan(ledger, 1, 11, {"complete": True, "audited": True}, binding=binding)
    result = status.state_counts(ledger.db)
    assert result["scan_failed"]["sources"] == 0 and result["partition"]["in_progress"] == 1


def test_scan_hold_and_quality_states_are_reported_by_reason(ledger):
    _patients(ledger, 1)
    _scan(ledger, 1, 11, {"complete": True, "quality_state": "held"}, binding=_source(ledger, 1, 11))
    _scan(ledger, 1, 12, {"failure": "nightly_original_unit_unverified", "held": True},
          binding=_source(ledger, 1, 12))
    _scan(ledger, 1, 13, {"complete": True, "quality_state": "unavailable:patient_summary_quality_source_changed"},
          binding=_source(ledger, 1, 13))
    result = status.state_counts(ledger.db)
    assert result["scan_held"] == {"patients": 1, "sources": 2, "codes": {
        "quality_held": 1, "patient_context_original_unit_unverified": 1}}
    assert result["scan_quality"]["held"] == 1 and result["scan_quality"]["unavailable"] == 1
    assert result["partition"]["held"] == 1


def test_unrelated_registered_codes_collapse_to_other(ledger):
    assert status.reason_code("run_lock") == "other"
    assert status.reason_code("actor") == "other"
    assert status.reason_code("ValueError") == "other"
    assert status.reason_code("synthetic free text") == "other"
    assert status.reason_code("nightly_length_stop") == "patient_context_length_stop"
    _patients(ledger, 1)
    _progress(ledger, 1, {"failure": "run_lock"})
    assert status.state_counts(ledger.db)["summary_failed"]["codes"] == {"other": 1}


def test_unbound_legacy_scan_keeps_its_stored_failure(ledger):
    _patients(ledger, 1)
    _source(ledger, 1, 11)
    _scan(ledger, 1, 11, {"failure": "patient_context_invalid_items"})
    assert status.state_counts(ledger.db)["partition"]["failed"] == 1


def test_invalid_original_source_key_is_unreadable(ledger):
    _patients(ledger, 1)
    _scan(ledger, 1, None, {"failure": "patient_context_invalid_items"}, key="karte:not-a-number")
    result = status.state_counts(ledger.db)
    assert result["invalid_rows"] == 1 and result["partition"]["invalid"] == 1


def test_query_error_propagates_instead_of_reporting_zero(tmp_path):
    import sqlite3
    db = sqlite3.connect(tmp_path / "empty.db")
    with pytest.raises(sqlite3.Error):
        status.state_counts(db)
    db.close()


def test_sanitize_rejects_extra_codes_negative_and_broken_partition(ledger):
    _patients(ledger, 1)
    good = status.state_counts(ledger.db)
    assert status.sanitize(good) == good
    for mutate in (lambda v: v["scan_failed"]["codes"].update({PRIVATE_NAME: 1}),
                   lambda v: v["partition"].update(no_record=-1),
                   lambda v: v["partition"].update(published=1),
                   lambda v: v.update(invalid_rows=True),
                   lambda v: v.pop("scan_held")):
        broken = json.loads(json.dumps(good))
        mutate(broken)
        assert status.sanitize(broken) is None
    extra = {**json.loads(json.dumps(good)), "patient_name": PRIVATE_NAME}
    assert PRIVATE_NAME not in json.dumps(status.sanitize(extra), ensure_ascii=False)


def test_output_has_no_names_or_bodies(ledger):
    _patients(ledger, 1, 2)
    _scan(ledger, 1, 11, {"failure": "patient_context_invalid_items"}, binding=_source(ledger, 1, 11))
    _progress(ledger, 2, {"failure": "patient_context_length_stop"})
    text = json.dumps(status.state_counts(ledger.db), ensure_ascii=False)
    assert PRIVATE_NAME not in text and PRIVATE_BODY not in text and "private" not in text


def test_counts_are_read_only(ledger):
    _patients(ledger, 1)
    _published(ledger, 1)
    before = ledger.db.total_changes
    status.state_counts(ledger.db)
    assert ledger.db.total_changes == before


def _coverage_states():
    tree = ast.parse(inspect.getsource(readiness._inference_coverage))
    assign = next(node for node in ast.walk(tree) if isinstance(node, ast.Assign)
                  and any(isinstance(t, ast.Name) and t.id == "status" for t in node.targets))
    found, node = [], assign.value
    while isinstance(node, ast.IfExp):
        assert isinstance(node.body, ast.Constant)
        found.append(node.body.value)
        node = node.orelse
    assert isinstance(node, ast.Constant)
    return (*found, node.value)


def test_every_coverage_state_has_wording_drift():
    assert _coverage_states() == status.COVERAGE_STATES
    assert set(status.STATUS_JA) == set(status.COVERAGE_STATES)
    assert all(text and text != status.UNKNOWN_STATUS_JA for text in status.STATUS_JA.values())
    assert status.status_text("synthetic_new_state") == status.UNKNOWN_STATUS_JA
    assert status.status_text(None) == status.UNKNOWN_STATUS_JA


def test_display_constants_import_without_the_refresh_engine():
    root = Path(__file__).resolve().parents[2]
    code = ("import sys; sys.path.insert(0, 'mcs'); import _mcs_path; import patient_context_status; "
            "heavy = {'patient_context_refresh_state', 'local_llm', 'cloud_inference'} & set(sys.modules); "
            "print(sorted(heavy))")
    out = subprocess.run([sys.executable, "-I", "-c", code], cwd=root, capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "[]"


@pytest.mark.parametrize("scope", [[], "7", [0], [True], [7, 7], [1.5], {"a": 1}, ["bad key"], "[7, 8]", 7, False])
def test_a_published_row_with_a_malformed_source_scope_is_invalid_not_published(ledger, scope):
    _patients(ledger, 1, 2)
    ledger.artifact_add(context.KIND, "{}", project_id=1, meta={**META, "published": True, "source_scope": scope})
    ledger.artifact_add(context.KIND, "{}", project_id=2, meta={**META, "published": True, "source_scope": [7, 8]})
    result = status.state_counts(ledger.db)
    assert result["invalid_rows"] == 1
    assert result["partition"]["invalid"] == 1 and result["partition"]["published"] == 1
    _assert_partition(result)


@pytest.mark.parametrize("scope", [None, [7, 8]])
def test_a_published_row_with_no_or_a_valid_source_scope_is_still_published(ledger, scope):
    _patients(ledger, 1)
    ledger.artifact_add(context.KIND, "{}", project_id=1, meta={**META, "published": True, "source_scope": scope})
    result = status.state_counts(ledger.db)
    assert result["invalid_rows"] == 0 and result["partition"]["published"] == 1
    _assert_partition(result)


def test_an_unpublished_row_with_a_malformed_source_scope_keeps_its_in_progress_state(ledger):
    _patients(ledger, 1)
    ledger.artifact_add(context.KIND, "{}", project_id=1, meta={**META, "published": False, "source_scope": "7"})
    result = status.state_counts(ledger.db)
    assert result["invalid_rows"] == 0 and result["partition"]["invalid"] == 0
    _assert_partition(result)
