"""Attempt frames use wholly synthetic, prior, source-grounded pharmacy reports."""
import json
import sqlite3
from dataclasses import replace

import pytest

import extract_llm
from inference_version import CURRENT_VERSION, VERSION_KEY
from inference_dependency_loss import source_binding
import extraction_context as state
from extract_testkit import _hash, _ledger, _message
from test_pharmacy_state import context


def seed(led, mid=1, *, body="本人の合成薬Aの残薬は3錠。", value="3錠", subject="patient",
         names=("合成薬A",), date="2026-10-01T10:00:00+09:00", pid=1, clean=True):
    led.save_messages([_message(mid=mid, body=body, project_id=pid, posted_at=date)])
    meta = {VERSION_KEY: CURRENT_VERSION, "hash": _hash(led, mid), "extract_version": extract_llm.EXTRACT_VERSION,
            "actual_model": "synthetic-model", "source_binding": source_binding(led.db, pid, mid)}
    if clean:
        meta["integrity"] = {"validation": {"items_dropped": 0, "evidence_dropped": 0},
            "completeness_chunks": 1,
            "completeness": {"0": {"state": "hint_gaps_resolved", "remaining_hint_candidates": 0}}}
    aid = led.artifact_add("extract_llm", json.dumps({"patient_context": [
        context(body, subject=subject, names=names, values=(value,))]}),
        project_id=pid, message_id=mid, model="synthetic-model", meta=meta)
    return aid


def target(led):
    led.save_messages([_message(mid=999, body="残薬は2錠になりました。",
                               posted_at="2026-10-03T10:00:00+09:00")])
    row = dict(led.db.execute("SELECT * FROM messages WHERE message_id=999").fetchone())
    row["_source_binding"] = source_binding(led.db, row["project_id"], row["message_id"])
    return row


def frame(led, row, **kwargs):
    return state.prepare(led, row, model="synthetic-model", contract="synthetic-contract/1",
                         attempt={"human": None, "qc": None, "thin": None}, enabled=True,
                         base_context=None, version=extract_llm.EXTRACT_VERSION, **kwargs)


def test_prior_reports_are_bounded_dated_and_bound_to_the_exact_source(tmp_path):
    led = _ledger(tmp_path)
    try:
        seed(led)
        row = target(led)
        seed(led, 2, date=row["posted_at"])
        seed(led, 3, date="2026-10-04T10:00:00+09:00")
        seed(led, 4, pid=2)
        result = frame(led, row)
        assert len(result["refs"]) == 1
        ref = result["refs"]["S1"]
        assert ref["project_id"] == ref["message_id"] == 1
        assert ref["content_hash"] == _hash(led) and len(ref["fingerprint"]) == 64
        assert "2026-10-01" in result["text"] and '値="3錠"' in result["text"]
        assert state.load(led.db, result["artifact_id"], row) == result
    finally:
        led.close()


@pytest.mark.parametrize("options", [
    {"clean": False}, {"subject": "family"}, {"subject": "other"}, {"subject": "unspecified"},
    {"body": "家族の合成薬Aの残薬は3錠。", "subject": "patient"},
    {"body": "以前は本人の合成薬Aの残薬は3錠。"},
    {"body": "本人の合成薬Aの残薬は3錠の予定。"},
    {"body": "本人の合成薬Aの残薬は3錠か未確認。"},
    {"body": "本人の合成薬Aと合成薬Bの残薬は3錠。", "names": ("合成薬A", "合成薬B")},
])
def test_uncertain_other_subject_and_ambiguous_reports_are_not_references(tmp_path, options):
    led = _ledger(tmp_path)
    try:
        seed(led, **options)
        assert frame(led, target(led))["refs"] == {}
    finally:
        led.close()


def test_newer_ambiguous_report_does_not_resurrect_older_positive(tmp_path):
    led = _ledger(tmp_path)
    try:
        seed(led)
        seed(led, 2, body="本人の合成薬Aの残薬は3錠か未確認。", date="2026-10-02T10:00:00+09:00")
        assert frame(led, target(led))["refs"] == {}
    finally:
        led.close()


def test_resume_reuses_saved_context_after_background_and_base_change(tmp_path):
    led = _ledger(tmp_path)
    try:
        seed(led)
        row = target(led)
        first = frame(led, row)
        seed(led, 2, body="本人の合成薬Aの残薬は8錠。", value="8錠", date="2026-10-02T10:00:00+09:00")
        later = state.prepare(led, row, model="synthetic-model", contract="synthetic-contract/1",
                              attempt={"human": None, "qc": None, "thin": None}, enabled=True,
                              base_context="更新後の合成背景", version=extract_llm.EXTRACT_VERSION)
        assert later == first and len(led.artifacts(state.KIND)) == 1
        new_attempt = state.prepare(led, row, model="synthetic-model", contract="synthetic-contract/1",
                                    attempt={"human": 123}, enabled=True, base_context=None,
                                    version=extract_llm.EXTRACT_VERSION)
        assert new_attempt["digest"] != first["digest"]
        assert new_attempt["refs"]["S1"]["value"] == "8錠"
    finally:
        led.close()


@pytest.mark.parametrize("base", [None, "", "合成thread\n" * 100])
def test_disabled_context_is_byte_identical_but_still_durable(tmp_path, base):
    led = _ledger(tmp_path)
    try:
        seed(led)
        row = target(led)
        result = state.prepare(led, row, model="synthetic-model", contract="v1", attempt={},
                               enabled=False, base_context=base, version=extract_llm.EXTRACT_VERSION)
        assert result["text"] == base and result["refs"] == {} and result["enabled"] is False
    finally:
        led.close()


def test_budget_keeps_whole_reference_values_and_bounds_combined_context(tmp_path):
    led = _ledger(tmp_path)
    try:
        for mid in range(1, 30):
            seed(led, mid, body=f"本人の合成薬{mid}の残薬は3錠。", names=(f"合成薬{mid}",))
        row = target(led)
        result = state.prepare(led, row, model="synthetic-model", contract="v1", attempt={},
                               enabled=True, base_context="背" * 600, version=extract_llm.EXTRACT_VERSION)
        block, refs = state.format_state(state.history(led.db, row, version=extract_llm.EXTRACT_VERSION))
        assert len(block) <= 600 and len(result["text"]) <= 1200
        assert len(refs) < 29 and all(ref["value"] == "3錠" for ref in refs.values())
        assert result["text"].startswith("背" * 600)
    finally:
        led.close()


def test_only_newest_200_source_messages_can_contribute(tmp_path):
    led = _ledger(tmp_path)
    try:
        seed(led)
        for mid in range(2, 203):
            led.save_messages([_message(mid=mid, posted_at="2026-10-02T10:00:00+09:00")])
        assert frame(led, target(led))["refs"] == {}
    finally:
        led.close()


def test_edited_deleted_or_foreign_artifact_never_contributes(tmp_path):
    led = _ledger(tmp_path)
    try:
        aid = seed(led)
        row = target(led)
        with pytest.raises(sqlite3.IntegrityError, match="artifacts message relation"):
            with led.db:
                led.db.execute("UPDATE artifacts SET project_id=2 WHERE artifact_id=?", (aid,))
        with led.db:
            led.db.execute("UPDATE artifacts SET project_id=1 WHERE artifact_id=?", (aid,))
            led.db.execute("UPDATE messages SET content_hash=? WHERE message_id=1", ("f" * 64,))
        assert state.history(led.db, row, version=extract_llm.EXTRACT_VERSION) == []
        with led.db:
            led.db.execute("UPDATE messages SET body_state='deleted' WHERE message_id=1")
        assert state.history(led.db, row, version=extract_llm.EXTRACT_VERSION) == []
    finally:
        led.close()


@pytest.mark.parametrize("relation", [None, "CONTRADICTION", "UNRESOLVED"])
def test_canonical_precedence_requires_complete_verified_uncontested_patient_fact(tmp_path, relation):
    from semantic_projection import PROJECTION_VERSION
    led = _ledger(tmp_path)
    body = "本人の合成薬Aの残薬は3錠。"
    try:
        seed(led)
        row = target(led)
        fact = {"fact_id": "f1", "validation_status": "verified", "subject": "patient:1",
                "polarity": "affirmed", "epistemic": "reported", "workflow_status": "performed",
                "evidence_quote": body}
        doc = {"patient_context": [context(body)], "canonical_facts": [fact],
               "canonical_relations": ([{"type": relation, "left_fact_id": "f1", "right_fact_id": "f2"}]
                                       if relation else []),
               "canonical_quality": {"coverage_status": "complete", "open_obligation_ids": [], "limitations": []}}
        aid = led.artifact_add("canonical_projection", json.dumps(doc), project_id=1, message_id=1,
                              meta={VERSION_KEY: CURRENT_VERSION, "hash": _hash(led), "projection_version": PROJECTION_VERSION})
        block, refs = state.format_state(state.history(led.db, row, version=extract_llm.EXTRACT_VERSION))
        assert bool(refs) is (relation is None)
        with led.db:
            doc["canonical_quality"]["coverage_status"] = "incomplete"
            led.db.execute("UPDATE artifacts SET content=? WHERE artifact_id=?", (json.dumps(doc), aid))
        assert state.format_state(state.history(led.db, row, version=extract_llm.EXTRACT_VERSION)) == ("", {})
    finally:
        led.close()


def test_same_generation_held_report_and_newer_bad_quality_do_not_fall_back(tmp_path):
    led = _ledger(tmp_path)
    try:
        aid = seed(led)
        row = target(led)
        led.artifact_add("extract_upgrade_v1", json.dumps({"state": "held_prior", "hash": _hash(led),
            "prior_artifact_id": aid, "target_version": extract_llm.EXTRACT_VERSION}), project_id=1, message_id=1)
        assert state.format_state(state.history(led.db, row, version=extract_llm.EXTRACT_VERSION)) == ("", {})
        seed(led, 2, clean=False, date="2026-10-02T10:00:00+09:00")
        assert state.format_state(state.history(led.db, row, version=extract_llm.EXTRACT_VERSION)) == ("", {})
    finally:
        led.close()


def test_corrupt_snapshot_cannot_be_silently_replaced_with_changed_background(tmp_path):
    led = _ledger(tmp_path)
    try:
        seed(led)
        row = target(led)
        first = frame(led, row)
        bad = {**first, "text": "合成の改変"}
        with led.db:
            led.db.execute("UPDATE artifacts SET content=? WHERE artifact_id=?",
                           (json.dumps(bad), first["artifact_id"]))
        assert state.load(led.db, first["artifact_id"], row) is None
        with pytest.raises(ValueError, match="context_snapshot_invalid"):
            frame(led, row)
        assert len(led.artifacts(state.KIND)) == 1
    finally:
        led.close()


def test_source_change_before_snapshot_refuses_the_attempt(tmp_path):
    led = _ledger(tmp_path)
    try:
        row = target(led)
        led.save_messages([_message(mid=999, body="合成の編集済み本文")])
        with pytest.raises(ValueError, match="context_source_changed"):
            frame(led, row)
        assert led.artifacts(state.KIND) == []
    finally:
        led.close()


@pytest.mark.parametrize("stamp", [None, True, float("inf"), float("nan"), 10**400])
def test_nonfinite_and_untyped_target_times_cannot_read_background(tmp_path, stamp):
    led = _ledger(tmp_path)
    try:
        seed(led)
        row = {**target(led), "posted_at_ts": stamp}
        assert state.history(led.db, row, version=extract_llm.EXTRACT_VERSION) == []
    finally:
        led.close()


def test_a_full_base_block_is_never_cut_through_its_reference_delimiters(tmp_path):
    led = _ledger(tmp_path)
    try:
        seed(led)
        row = target(led)
        base = "<<<\n" + "背" * 1191 + "\n>>>\n"
        assert len(base) == 1200
        result = state.prepare(led, row, model="synthetic-model", contract="v1", attempt={},
                               enabled=True, base_context=base, version=extract_llm.EXTRACT_VERSION)
        assert result["text"] == base and result["refs"] == {}
    finally:
        led.close()


@pytest.mark.parametrize("mutation", ["counter", "model", "completeness", "resolved_bool",
                                       "resolved_float", "resolved_missing_count", "failed_fix"])
def test_incomplete_validation_or_provenance_never_becomes_a_reference(tmp_path, mutation):
    led = _ledger(tmp_path)
    try:
        aid = seed(led)
        meta = json.loads(led.db.execute("SELECT meta FROM artifacts WHERE artifact_id=?", (aid,)).fetchone()[0])
        if mutation == "counter":
            meta["integrity"]["validation"]["items_dropped"] = True
        elif mutation == "model":
            meta["actual_model"] = "another-synthetic-model"
        elif mutation == "completeness":
            meta["integrity"]["completeness"] = {"0": {"state": "unresolved", "remaining_hint_candidates": 1}}
        elif mutation.startswith("resolved_"):
            entry = {"state": "hint_gaps_resolved"}
            if mutation != "resolved_missing_count":
                entry["remaining_hint_candidates"] = False if mutation == "resolved_bool" else 0.0
            meta["integrity"]["completeness"] = {"0": entry}
        else:
            meta["human_fix"] = {"applied": False}
        with led.db:
            led.db.execute("UPDATE artifacts SET meta=? WHERE artifact_id=?", (json.dumps(meta), aid))
        assert frame(led, target(led))["refs"] == {}
    finally:
        led.close()


def test_a_malformed_source_date_does_not_abort_the_attempt(tmp_path):
    led = _ledger(tmp_path)
    try:
        seed(led)
        row = target(led)
        with led.db:
            led.db.execute("UPDATE messages SET posted_at_ts=-1e100 WHERE message_id=1")
        assert frame(led, row)["refs"] == {}
    finally:
        led.close()


def test_two_values_in_one_parent_are_not_distributed_or_resolved_arbitrarily(tmp_path):
    led = _ledger(tmp_path)
    body = "本人の合成薬Aの残薬は3錠と8錠。"
    try:
        aid = seed(led, body=body)
        with led.db:
            led.db.execute("UPDATE artifacts SET content=? WHERE artifact_id=?", (json.dumps({
                "patient_context": [context(body, values=("3錠", "8錠"))]}), aid))
        assert frame(led, target(led))["refs"] == {}
    finally:
        led.close()


@pytest.mark.parametrize("field,value", [("content", "not json"), ("meta", "not json"),
                                         ("content", "[1]"), ("meta", "null"),
                                         ("meta", "{}"), ("meta", '{"hash":1}')])
def test_unparseable_snapshot_is_reported_without_an_untyped_worker_exception(tmp_path, field, value):
    led = _ledger(tmp_path)
    try:
        row = target(led)
        result = frame(led, row)
        with led.db:
            led.db.execute(f"UPDATE artifacts SET {field}=? WHERE artifact_id=?", (value, result["artifact_id"]))
        assert state.load(led.db, result["artifact_id"], row) is None
        with pytest.raises(ValueError, match="context_snapshot_invalid"):
            frame(led, row)
        assert len(led.artifacts(state.KIND)) == 1
    finally:
        led.close()


@pytest.mark.parametrize("change", ["edited", "deleted", "extraction", "quality", "held", "time"])
def test_reference_current_checks_the_exact_source_without_substituting_a_new_value(tmp_path, change):
    led = _ledger(tmp_path)
    try:
        aid = seed(led)
        row = target(led)
        ref = {**frame(led, row)["refs"]["S1"], "token": "S1"}
        original = json.loads(json.dumps(ref))
        assert state.reference_current(led.db, row, ref, version=extract_llm.EXTRACT_VERSION)
        if change == "edited":
            led.save_messages([_message(mid=1, body="本人の合成薬Aの残薬は8錠。")])
        elif change == "extraction":
            with led.db:
                led.db.execute("UPDATE artifacts SET content='{}' WHERE artifact_id=?", (aid,))
        elif change == "quality":
            meta = json.loads(led.db.execute("SELECT meta FROM artifacts WHERE artifact_id=?", (aid,)).fetchone()[0])
            meta["integrity"]["validation"]["items_dropped"] = 1
            with led.db:
                led.db.execute("UPDATE artifacts SET meta=? WHERE artifact_id=?", (json.dumps(meta), aid))
        elif change == "held":
            led.artifact_add("extract_upgrade_v1", json.dumps({"state": "held_prior", "hash": _hash(led),
                "prior_artifact_id": aid, "target_version": extract_llm.EXTRACT_VERSION}), project_id=1, message_id=1)
        else:
            with led.db:
                led.db.execute("UPDATE messages SET " + ("body_state='deleted'" if change == "deleted"
                                                       else "posted_at_ts=posted_at_ts+1") + " WHERE message_id=1")
        assert state.reference_current(led.db, row, ref, version=extract_llm.EXTRACT_VERSION) is False
        assert ref == original
    finally:
        led.close()


def test_a_stored_reference_does_not_depend_on_later_history_window_changes(tmp_path):
    led = _ledger(tmp_path)
    try:
        seed(led)
        row = target(led)
        ref = {**frame(led, row)["refs"]["S1"], "token": "S1"}
        for mid in range(2, 203):
            led.save_messages([_message(mid=mid, posted_at="2026-10-02T10:00:00+09:00")])
        assert state.history(led.db, row, version=extract_llm.EXTRACT_VERSION) == []
        assert state.reference_current(led.db, row, ref, version=extract_llm.EXTRACT_VERSION)
    finally:
        led.close()


def test_a_newly_recognized_second_value_invalidates_an_ambiguous_same_body_reference(tmp_path):
    led = _ledger(tmp_path)
    body = "本人の合成薬Aの残薬は3錠と8錠。"
    try:
        aid = seed(led, body=body)
        row = target(led)
        ref = {**frame(led, row)["refs"]["S1"], "token": "S1"}
        assert state.reference_current(led.db, row, ref, version=extract_llm.EXTRACT_VERSION)
        with led.db:
            led.db.execute("UPDATE artifacts SET content=? WHERE artifact_id=?", (json.dumps({
                "patient_context": [context(body, values=("3錠", "8錠"))]}), aid))
        assert state.reference_current(led.db, row, ref, version=extract_llm.EXTRACT_VERSION) is False
    finally:
        led.close()


def test_reference_occurrence_rejects_aba_even_after_same_dto_is_legitimately_reextracted(tmp_path):
    led = _ledger(tmp_path)
    try:
        seed(led, 2)
        row = target(led)
        error_id = led.artifact_add('extract_llm', '{}', project_id=1, message_id=999,
            meta={VERSION_KEY: CURRENT_VERSION, 'hash': row['content_hash'], 'error': True,
                  'extract_version': extract_llm.EXTRACT_VERSION, 'attempts': 2, 'auto_retry': 1})
        error_before = tuple(led.db.execute('SELECT * FROM artifacts WHERE artifact_id=?', (error_id,)).fetchone())
        frozen = frame(led, row)
        old = {**frozen['refs']['S1'], 'token': 'S1'}
        assert state.reference_current(led.db, row, old, version=extract_llm.EXTRACT_VERSION)
        seed(led, 2, body='本人の合成薬Aの残薬は4錠。', value='4錠')
        edited_frame = frame(led, row)
        assert edited_frame['binding'] == frozen['binding']
        assert edited_frame['artifact_id'] != frozen['artifact_id'] and edited_frame['digest'] != frozen['digest']
        edited_ref = {**edited_frame['refs']['S1'], 'token': 'S1'}
        assert edited_ref['value'] == '4錠'
        assert state.reference_current(led.db, row, edited_ref, version=extract_llm.EXTRACT_VERSION)
        assert state.load(led.db, frozen['artifact_id'], row) == frozen
        seed(led, 2)
        assert _hash(led, 2) == old['content_hash']
        assert led.db.execute('SELECT MAX(seq) FROM message_revisions WHERE message_id=2').fetchone()[0] == 3
        assert not state.reference_current(led.db, row, old, version=extract_llm.EXTRACT_VERSION)
        _, refs = state.format_state(state.history(led.db, row, version=extract_llm.EXTRACT_VERSION))
        current = {**refs['S1'], 'token': 'S1'}
        assert set(current) == set(old) and current['value'] == old['value'] == '3錠'
        assert current['fingerprint'] != old['fingerprint']
        assert current['parent_fingerprint'] != old['parent_fingerprint']
        assert state.reference_current(led.db, row, current, version=extract_llm.EXTRACT_VERSION)
        assert not state.reference_current(led.db, {**row, 'project_id': 2}, current,
                                           version=extract_llm.EXTRACT_VERSION)
        assert state.load(led.db, frozen['artifact_id'], row) == frozen
        restored_frame = frame(led, row)
        assert restored_frame['binding'] == frozen['binding']
        assert restored_frame['artifact_id'] not in (frozen['artifact_id'], edited_frame['artifact_id'])
        assert restored_frame['refs']['S1'] == refs['S1']
        assert restored_frame['digest'] not in (frozen['digest'], edited_frame['digest'])
        assert frame(led, row) == restored_frame
        assert len(led.artifacts(state.KIND)) == 3
        assert state.load(led.db, edited_frame['artifact_id'], row) == edited_frame
        assert tuple(led.db.execute('SELECT * FROM artifacts WHERE artifact_id=?', (error_id,)).fetchone()) == error_before
    finally:
        led.close()


@pytest.mark.parametrize('field,value', [
    ('parent_id', 999), ('sender_id', 12), ('sender_name', '完全合成の表示名'),
    ('body_text', '本人の合成薬Aの残薬は3錠。完全合成の補足です。'),
])
def test_reference_fingerprint_covers_actual_source_inputs_even_when_hash_and_dto_stay_same(tmp_path, field, value):
    led = _ledger(tmp_path)
    try:
        seed(led, 2)
        row = target(led)
        old = {**frame(led, row)['refs']['S1'], 'token': 'S1'}
        with led.db:
            led.db.execute('UPDATE messages SET ' + field + '=? WHERE message_id=2', (value,))
        assert _hash(led, 2) == old['content_hash']
        assert not state.reference_current(led.db, row, old, version=extract_llm.EXTRACT_VERSION)
        _, refs = state.format_state(state.history(led.db, row, version=extract_llm.EXTRACT_VERSION))
        assert refs['S1']['value'] == '3錠' and refs['S1']['fingerprint'] != old['fingerprint']
    finally:
        led.close()


def test_snapshot_without_revision_table_preserves_reports_but_cannot_offer_or_confirm_references(tmp_path):
    led = _ledger(tmp_path)
    try:
        seed(led)
        row = target(led)
        old = {**frame(led, row)['refs']['S1'], 'token': 'S1'}
        with led.db:
            led.db.execute('DROP TABLE message_revisions')
        changes = led.db.total_changes
        reports = state.history(led.db, row, version=extract_llm.EXTRACT_VERSION)
        assert reports and reports[0]['value'] == '3錠'
        assert reports[0]['reference_eligible'] is False
        assert state.format_state(reports) == ('', {})
        assert not state.reference_current(led.db, row, old, version=extract_llm.EXTRACT_VERSION)
        assert led.db.total_changes == changes
    finally:
        led.close()


@pytest.mark.parametrize('field', ['parent', 'sender', 'name', 'time', 'html'])
def test_legitimate_metadata_aba_cannot_revive_old_reference_or_frame(tmp_path, field):
    led = _ledger(tmp_path)
    body = '本人の合成薬Aの残薬は3錠。'
    try:
        led.save_messages([_message(mid=11, body='完全合成の親投稿A', posted_at='2026-09-29T10:00:00+09:00'),
                           _message(mid=12, body='完全合成の親投稿B', posted_at='2026-09-29T11:00:00+09:00')])
        original = _message(mid=2, body=body, parent_id=11, posted_at='2026-10-01T10:00:00+09:00')
        led.save_messages([original])
        aid = seed(led, 2)
        template = led.db.execute('SELECT content,meta FROM artifacts WHERE artifact_id=?', (aid,)).fetchone()
        document, metadata = json.loads(template[0]), json.loads(template[1])
        target_row = target(led)
        before_binding = source_binding(led.db, 1, 2)
        before_frame = frame(led, target_row)
        before_ref = {**before_frame['refs']['S1'], 'token': 'S1'}
        assert state.reference_current(led.db, target_row, before_ref, version=extract_llm.EXTRACT_VERSION)
        if field == 'parent':
            changed = replace(original, parent_id=12)
        elif field == 'sender':
            changed = replace(original, sender_id=2)
        elif field == 'name':
            changed = replace(original, sender_name='完全合成の別表示')
        elif field == 'time':
            changed = replace(original, posted_at='2026-10-01T01:00:00+00:00')
        else:
            changed = replace(original, body_html='<span data-fictional="x">' + body + '</span>')

        def adopt(message):
            led.save_messages([message])
            source = led.db.execute('SELECT * FROM messages WHERE message_id=2').fetchone()
            assert source['body_text'] == body  # no clinical assertion is changed by the fixture
            assert source['posted_at_ts'] == before_ref['posted_at_ts']
            meta = {**metadata, 'hash': source['content_hash'], 'source_binding': source_binding(led.db, 1, 2)}
            # The same fully synthetic DTO is a fresh success for this actual source occurrence.
            new_id = led.artifact_add('extract_llm', json.dumps(document), project_id=1, message_id=2,
                                     model='synthetic-model', meta=meta)
            assert json.loads(led.db.execute('SELECT content FROM artifacts WHERE artifact_id=?', (new_id,)).fetchone()[0]) == document
            return meta['source_binding']

        middle_binding = adopt(changed)
        assert middle_binding != before_binding
        if field != 'html':
            assert _hash(led, 2) == before_ref['content_hash']
        assert not state.reference_current(led.db, target_row, before_ref, version=extract_llm.EXTRACT_VERSION)
        middle_frame = frame(led, target_row)
        middle_ref = {**middle_frame['refs']['S1'], 'token': 'S1'}
        assert middle_frame['artifact_id'] != before_frame['artifact_id']
        assert state.reference_current(led.db, target_row, middle_ref, version=extract_llm.EXTRACT_VERSION)
        after_binding = adopt(original)
        assert len({before_binding, middle_binding, after_binding}) == 3
        assert _hash(led, 2) == before_ref['content_hash']
        assert not state.reference_current(led.db, target_row, before_ref, version=extract_llm.EXTRACT_VERSION)
        assert not state.reference_current(led.db, target_row, middle_ref, version=extract_llm.EXTRACT_VERSION)
        after_frame = frame(led, target_row)
        after_ref = {**after_frame['refs']['S1'], 'token': 'S1'}
        assert len({before_frame['artifact_id'], middle_frame['artifact_id'], after_frame['artifact_id']}) == 3
        assert len({before_frame['digest'], middle_frame['digest'], after_frame['digest']}) == 3
        assert before_frame['binding'] == middle_frame['binding'] == after_frame['binding']
        assert before_ref['value'] == middle_ref['value'] == after_ref['value'] == '3錠'
        assert state.reference_current(led.db, target_row, after_ref, version=extract_llm.EXTRACT_VERSION)
        assert frame(led, target_row) == after_frame
        assert state.load(led.db, before_frame['artifact_id'], target_row) == before_frame
        assert state.load(led.db, middle_frame['artifact_id'], target_row) == middle_frame
    finally:
        led.close()


def _history_before_latest_join_change(db, source, *, version):
    """Independent compatibility oracle: original ordered LEFT JOIN and first-row rule."""
    from mcs_queries import ELIGIBLE_MESSAGE, FACT_KINDS_SQL, current_fact_pred
    occurrence = state._source_occurrence_sql(db)
    rows = db.execute(f'''
        WITH earlier AS MATERIALIZED (
          SELECT * FROM messages m WHERE m.project_id=? AND m.message_id!=?
          AND {ELIGIBLE_MESSAGE} AND m.posted_at_ts < ?
          AND m.posted_at_ts > -1e308 AND m.posted_at_ts < 1e308
          ORDER BY m.posted_at_ts DESC,m.message_id DESC LIMIT 200)
        SELECT m.*,a.artifact_id,a.kind,a.content,a.meta,a.model,
          {state._held_sql(version)} AS held,{occurrence} AS source_occurrence
        FROM earlier m
        LEFT JOIN artifacts a ON a.message_id=m.message_id
          AND a.kind IN ({FACT_KINDS_SQL})
          {current_fact_pred()}
        ORDER BY m.posted_at_ts DESC,m.message_id DESC,a.artifact_id DESC
    ''', (source['project_id'], source['message_id'], source['posted_at_ts']))
    reports, seen = [], set()
    for raw in rows:
        value = dict(raw)
        if value['message_id'] in seen:
            continue
        seen.add(value['message_id'])
        reports.extend(state._reports(value, version))
    return reports


@pytest.mark.parametrize('latest', ['ambiguous', 'empty', 'invalid_json', 'held', 'foreign'])
def test_history_latest_join_preserves_original_current_and_ambiguity_contract(tmp_path, latest):
    led = _ledger(tmp_path)
    try:
        old_id = seed(led)
        source = target(led)
        digest = _hash(led, 1)
        meta = {VERSION_KEY: CURRENT_VERSION, 'hash': digest,
                'extract_version': extract_llm.EXTRACT_VERSION,
                'actual_model': 'synthetic-model', 'source_binding': source_binding(led.db, 1, 1)}
        if latest == 'ambiguous':
            newest = led.artifact_add('extract_llm', json.dumps({'patient_context': [
                context('本人の合成薬Aの残薬は3錠。', subject='patient', names=('合成薬A',), values=('3錠',))]}),
                project_id=1, message_id=1, model='synthetic-model', meta=meta)
        elif latest == 'empty':
            newest = led.artifact_add('extract_llm', '{}', project_id=1, message_id=1,
                                      model='synthetic-model', meta=meta)
        elif latest == 'invalid_json':
            newest = led.artifact_add('extract_llm', '{bad', project_id=1, message_id=1,
                                      model='synthetic-model', meta=meta)
        elif latest == 'held':
            newest = old_id
            led.artifact_add('extract_upgrade_v1', json.dumps({'state': 'held_prior', 'hash': digest,
                'prior_artifact_id': old_id, 'target_version': extract_llm.EXTRACT_VERSION}),
                project_id=1, message_id=1)
        else:
            newest = led.artifact_add('extract_llm', '{}', project_id=1, message_id=1,
                                      model='synthetic-model', meta=meta)
            with led.db:
                # Simulate a corrupt legacy row without weakening production guards.
                led.db.execute('DROP TRIGGER g1_artifacts_msg_upd')
                led.db.execute('UPDATE artifacts SET project_id=2 WHERE artifact_id=?', (newest,))
        version = extract_llm.EXTRACT_VERSION
        expected = _history_before_latest_join_change(led.db, source, version=version)
        actual = state.history(led.db, source, version=version)
        assert actual == expected
        if latest in ('empty', 'ambiguous'):
            assert state.format_state(actual)[1] == {}  # valid empty/unclean latest does not fall back
        if latest in ('invalid_json', 'foreign'):
            assert actual and actual[0]['reference_eligible'] is True  # rejected row was never eligible
        if latest == 'held':
            assert actual and actual[0]['reference_eligible'] is False
    finally:
        led.close()


def test_history_does_not_materialize_large_older_artifacts_in_python(tmp_path):
    led = _ledger(tmp_path)
    try:
        seed(led)
        source = target(led)
        huge = json.dumps({'synthetic_unused': 'x' * (1024 * 1024)})
        meta = {VERSION_KEY: CURRENT_VERSION, 'hash': _hash(led, 1),
                'extract_version': extract_llm.EXTRACT_VERSION, 'actual_model': 'synthetic-model',
                'source_binding': source_binding(led.db, 1, 1)}
        led.artifact_add('extract_llm', huge, project_id=1, message_id=1, model='synthetic-model', meta=meta)
        seed(led)  # a newer eligible, source-bound artifact remains the only selected row
        expected = _history_before_latest_join_change(led.db, source, version=extract_llm.EXTRACT_VERSION)
        original_factory = led.db.row_factory
        def observe(cursor, values):
            columns = [column[0] for column in cursor.description]
            if 'artifact_id' in columns and 'content' in columns:
                assert values[columns.index('content')] != huge
            return sqlite3.Row(cursor, values)
        led.db.row_factory = observe
        try:
            actual = state.history(led.db, source, version=extract_llm.EXTRACT_VERSION)
        finally:
            led.db.row_factory = original_factory
        assert actual == expected and actual[0]['reference_eligible'] is True
    finally:
        led.close()
