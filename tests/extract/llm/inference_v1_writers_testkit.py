"""Synthetic inference v1 writers testkit contracts with isolated shared fixtures."""
import extract_llm_engine
import extract_llm_errors
import extract_llm_provider
import extract_llm_resident
import extract_llm_runner
import extract_llm_source_contract
import extract_llm_store
import json
import pytest
import extract
import extract_llm as llm
import inference_version as inference
import inference_dependency_loss as dependency_loss
from extract_testkit import _ledger, _message


@pytest.fixture
def source(tmp_path, monkeypatch):
    led = _ledger(tmp_path)
    led.save_messages([_message(body="本人は発熱なし。合成の経過報告です。")])
    monkeypatch.setattr(llm.writer_lock, "updater_state", lambda: (False, 1))
    monkeypatch.setattr(llm, "_WRITER_STATE", (False, 1))
    monkeypatch.setattr(extract_llm_runner, '_WRITER_STATE', llm._WRITER_STATE)
    monkeypatch.setattr(extract_llm_source_contract, '_WRITER_STATE', llm._WRITER_STATE)
    monkeypatch.setattr(extract, "_WRITER_STATE", (False, 1))
    monkeypatch.setattr(llm, "_rerender_cards", lambda *a, **k: None)
    monkeypatch.setattr(extract_llm_store, '_rerender_cards', llm._rerender_cards)
    row = dict(led.db.execute("SELECT * FROM messages WHERE message_id=1").fetchone())
    row['_source_revision'] = led.db.execute(
        "SELECT COALESCE(MAX(seq),1) FROM message_revisions WHERE message_id=1").fetchone()[0]
    row['_source_binding'] = dependency_loss.source_binding(led.db,row['project_id'],row['message_id'])
    try:
        yield led, row
    finally:
        led.close()


def add(led, row, *, kind=llm.KIND, version=None, content=None, **meta):
    metadata = {"hash": row["content_hash"], "extract_version": llm.EXTRACT_VERSION, **meta}
    if version is not None:
        metadata[inference.VERSION_KEY] = version
    return led.artifact_add(kind, json.dumps(content or {"summary": "合成の経過報告"}),
        project_id=row["project_id"], message_id=row["message_id"], meta=metadata)


def _synthetic_inference(monkeypatch, answer):
    monkeypatch.setattr(llm, "_resolved_llm", lambda: ("synthetic", "synthetic"))
    monkeypatch.setattr(extract_llm_engine, '_resolved_llm', llm._resolved_llm)
    monkeypatch.setattr(extract_llm_errors, '_resolved_llm', llm._resolved_llm)
    monkeypatch.setattr(extract_llm_provider, '_resolved_llm', llm._resolved_llm)
    monkeypatch.setattr(extract_llm_resident, '_resolved_llm', llm._resolved_llm)
    monkeypatch.setattr(extract_llm_runner, '_resolved_llm', llm._resolved_llm)
    monkeypatch.setattr(llm, "_llm_call", answer)
    monkeypatch.setattr(extract_llm_engine, '_llm_call', llm._llm_call)
    monkeypatch.setattr(extract_llm_runner, '_llm_call', llm._llm_call)


def _adoption_witness(led):
    """A real scheduler row and two public generations of retained cache."""
    with led.db:
        led.db.execute("INSERT INTO fetch_jobs(kind,project_id,message_id,parent_id,payload,state,next_try,created_at,updated_at) "
            "VALUES('semantic_sched',0,0,NULL,?,'done',0,1,1)",
            (json.dumps({'analysis_epoch':0, 'source_epoch':7, 'other_owner':{'cursor':13}}),))
    old = led.artifact_add('patient_rollup', '{"generation":"old"}', project_id=1,
                          meta={inference.VERSION_KEY:'0.1'})
    current = led.artifact_add('patient_rollup', '{"generation":"current"}', project_id=1,
                              meta={inference.VERSION_KEY:inference.CURRENT_VERSION})
    with led.db:
        led.db.execute("INSERT OR IGNORE INTO patients(project_id,patient_name) VALUES(2,'synthetic-other')")
    other = led.artifact_add('patient_rollup', '{"generation":"other-patient"}', project_id=2,
                            meta={inference.VERSION_KEY:inference.CURRENT_VERSION})
    return old, current, other


def _scheduler_payload(db):
    return json.loads(db.execute("SELECT payload FROM fetch_jobs WHERE kind='semantic_sched' "
                                "AND project_id=0 AND message_id=0").fetchone()[0])


def _write_adopted(led, row, writer, document, monkeypatch, *, compact=False, config_generation=None):
    if writer == 'rules':
        monkeypatch.setattr(extract, 'extract_message', lambda *args: dict(document))
        assert extract._extract_rows(led, [row]) == (1, {row['project_id']})
    else:
        content = json.dumps(document, ensure_ascii=compact, sort_keys=compact,
                             separators=(',', ':') if compact else None)
        extra = {'config_generation':config_generation} if config_generation is not None else None
        assert llm._replace_current(led, row, content, extra_meta=extra)


def _intervention_row(led):
    row = dict(led.db.execute("SELECT * FROM messages WHERE message_id=1").fetchone())
    row['_source_revision'] = led.db.execute(
        'SELECT COALESCE(MAX(seq),1) FROM message_revisions WHERE message_id=1').fetchone()[0]
    row['_source_binding'] = dependency_loss.source_binding(led.db, row['project_id'], row['message_id'])
    return row


def _write_intervention_v1(led, row, writer):
    if writer == 'rules':
        assert extract._extract_rows(led, [row]) == (1, {row['project_id']})
    else:
        hints = llm._rule_hints(row)
        assert hints is not None and 'intervention_schedule' not in hints
        llm._ensure_v1(led, row, hints)
    stored = led.db.execute("SELECT content,meta FROM artifacts WHERE kind='extract_v1' "
                            "AND message_id=1 ORDER BY artifact_id DESC LIMIT 1").fetchone()
    assert stored is not None
    content, meta = map(json.loads, stored)
    assert meta['rule_version'] == extract.RULE_VERSION
    assert meta['source_binding'] == row['_source_binding']
    if 'intervention_schedule' in content:
        assert content['intervention_schedule']['version'] == 1
        assert 'intervention_zero' not in meta
    return content.get('intervention_schedule')


def _scoped_error_rows(led, row):
    led.ensure_patient(2)
    meta = {'error': True, 'attempts': 5, 'auto_retry': 0}
    owned = add(led, row, version=inference.CURRENT_VERSION, **meta)
    null_scope = add(led, row, version=inference.CURRENT_VERSION, **meta)
    foreign = add(led, row, version=inference.CURRENT_VERSION, **meta)
    legacy = add(led, row, **meta)
    with led.db:
        led.db.execute("UPDATE ledger_relation_guards SET mode='shadow' WHERE relation='artifacts'")
        led.db.execute('UPDATE artifacts SET project_id=NULL WHERE artifact_id=?', (null_scope,))
        led.db.execute('UPDATE artifacts SET project_id=2 WHERE artifact_id=?', (foreign,))
        led.db.execute('UPDATE artifacts SET created_at=0 WHERE artifact_id IN (?,?,?,?)',
                       (owned, null_scope, foreign, legacy))
    return owned, null_scope, foreign, legacy


def _add_stale_rows(led, row, count):
    return [add(led, row, version=inference.CURRENT_VERSION, hash='synthetic-stale') for _ in range(count)]


def _gc_tick(led, monkeypatch, **kwargs):
    from contextlib import nullcontext
    return llm._gc_stale_artifacts(led, lambda *_args, **_kwargs: nullcontext(True), False, **kwargs)
