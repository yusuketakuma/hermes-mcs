"""SQLite-valid unreadable JSON cannot block unrelated semantic work or replenish usage."""
import json
import time

import pytest

from ledger import Ledger
from mcs_util import loads_dict
import semantic_extraction as extraction
import semantic_store as store
import semantic_drain as drain
from semantic_policy import KIND_FACT_PROJ, KIND_USAGE
from semantic_testkit import _message


@pytest.fixture
def ledger(tmp_path):
    db = Ledger(str(tmp_path / "synthetic.db"))
    db.save_messages([_message(mid=1)])
    yield db
    db.close()


def poison_json(db):
    raw = '{"synthetic_unused":' + '9' * 5000 + '}'
    assert db.db.execute("SELECT json_valid(?)", (raw,)).fetchone()[0] == 1
    with pytest.raises(ValueError):
        json.loads(raw)
    return raw


def test_unreadable_projection_is_revoked_without_blocking_other_rows(ledger):
    raw = poison_json(ledger)
    poison = ledger.artifact_add(KIND_FACT_PROJ, "{}", project_id=1, meta={})
    healthy = ledger.artifact_add(KIND_FACT_PROJ, "{}", project_id=2, meta={})
    ledger.db.execute("UPDATE artifacts SET meta=? WHERE artifact_id=?", (raw, poison))
    ledger.db.commit()
    assert store.invalidate_projections(ledger, {"mode": "off"}) == 2
    for artifact_id in (poison, healthy):
        assert ledger.db.execute("SELECT json_extract(meta,'$.invalidated') FROM artifacts "
                                  "WHERE artifact_id=?", (artifact_id,)).fetchone()[0] == 1


@pytest.mark.parametrize("column", ["meta", "content"])
def test_unreadable_current_artifact_is_unavailable(ledger, column):
    artifact_id = ledger.artifact_add("synthetic_kind", "{}", project_id=1, message_id=1,
                                      meta={"fingerprint": "fp"})
    ledger.db.execute(f"UPDATE artifacts SET {column}=? WHERE artifact_id=?",
                      (poison_json(ledger), artifact_id))
    ledger.db.commit()
    assert store._current(ledger, "synthetic_kind", 1, "fp") is None


def test_unreadable_usage_fails_closed(ledger):
    artifact_id = ledger.artifact_add(KIND_USAGE, "{}", meta={"jev_requests": 1})
    ledger.db.execute("UPDATE artifacts SET meta=? WHERE artifact_id=?",
                      (poison_json(ledger), artifact_id))
    ledger.db.commit()
    with pytest.raises(ValueError, match="semantic_usage_invalid"):
        store.jev_usage_today(ledger)


def test_unreadable_chunk_is_ignored_and_manifest_can_be_rebuilt(ledger):
    raw = poison_json(ledger)
    ledger.artifact_add(extraction.KIND_CHUNK, raw, project_id=1, message_id=1,
                        meta={"chunk_index": 0, "schema": extraction.SCHEMA_VERSION,
                              "generation": "gen", "status": "complete",
                              "source_fingerprint": "fp", "source_hash": "hash",
                              "revision": "r", "chunks_total": 1, "chunk_hash": "h",
                              "start_codepoint": 0, "end_codepoint": 1})
    specs = [{"index": 0, "hash": "h", "start": 0, "end": 1, "text": "x"}]
    assert extraction._cached_chunks(ledger, 1, 1, "fp", "hash", "r", specs, "x",
                                     generation="gen") == {}
    manifest = extraction.build_manifest("x", "fp")
    ledger.artifact_add(extraction.KIND_MANIFEST, raw, project_id=1, message_id=1,
                        meta={"source_fingerprint": "fp", "version": extraction.MANIFEST_VERSION})
    extraction._persist_manifest(ledger, 1, 1, "synthetic-model", "fp", manifest, 1)
    assert loads_dict(ledger.artifacts(extraction.KIND_MANIFEST)[-1]["content"])


def test_unreadable_scheduler_payload_does_not_stop_next_drain(ledger):
    drain._sched_write(ledger, {})
    ledger.db.execute("UPDATE fetch_jobs SET payload=? WHERE kind=?",
                      (poison_json(ledger), drain.SCHED_KIND))
    ledger.db.commit()
    assert drain._sched_state(ledger) == {}


def test_unreadable_job_payload_is_terminal_without_model_calls(ledger):
    ledger.job_add(drain.JOB_KIND, 1, 1, payload={})
    ledger.db.execute("UPDATE fetch_jobs SET payload=? WHERE kind=?",
                      (poison_json(ledger), drain.JOB_KIND))
    ledger.db.commit()
    job = ledger.db.execute("SELECT * FROM fetch_jobs WHERE kind=?", (drain.JOB_KIND,)).fetchone()
    assert drain._process_job_inner(ledger, {}, job, None,
                                    lambda *args: pytest.fail("must not call model"),
                                    time.monotonic() + 30) == "failed"


def test_unreadable_plan_meta_does_not_hide_valid_sibling(ledger):
    poison = ledger.artifact_add(drain.KIND_PLAN, "{}", project_id=1, message_id=1, meta={})
    ledger.db.execute("UPDATE artifacts SET meta=? WHERE artifact_id=?",
                      (poison_json(ledger), poison))
    ledger.db.commit()
    ledger.artifact_add(drain.KIND_PLAN, "{}", project_id=1, message_id=1,
                        meta={"fingerprint": "fp", "audit_status": "PASS"})
    assert drain._plan_exists(ledger, 1, "fp", "PASS")


def test_unreadable_failed_job_does_not_block_revival_of_valid_sibling(ledger):
    now = time.time()
    for mid in (1, 2):
        ledger.job_add(drain.JOB_KIND, 1, mid, payload={})
    ledger.db.execute("UPDATE fetch_jobs SET state='failed',updated_at=? WHERE kind=?",
                      (now - drain.REVIVE_COOLDOWN_S - 1, drain.JOB_KIND))
    ledger.db.execute("UPDATE fetch_jobs SET payload=? WHERE message_id=1 AND kind=?",
                      (poison_json(ledger), drain.JOB_KIND))
    ledger.db.commit()
    assert drain.revive_failed(ledger, now)["revived"] == 1
