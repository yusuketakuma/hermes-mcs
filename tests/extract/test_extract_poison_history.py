"""Unreadable stored extraction history cannot stop healthy sibling work."""
import json
import time

import drug_map
import extract_llm
import extract
import rollup
from extract_testkit import _extract_artifact, _hash, _ledger, _message, _seed_qc_flagged
from test_drug_map import _dictionary, _seed


def poison(extra=""):
    return '{' + extra + '"synthetic_unused":' + '9' * 5000 + '}'


def test_revival_skips_unreadable_error_metadata_and_revives_sibling(tmp_path):
    ledger = _ledger(tmp_path)
    try:
        ledger.save_messages([_message(mid=i, body=f"SYNTH-{i}") for i in (1, 2)])
        ids = []
        for mid in (1, 2):
            aid = ledger.artifact_add(extract_llm.KIND, '{}', project_id=1, message_id=mid,
                                      meta={"error": True, "extract_version": extract_llm.EXTRACT_VERSION, "attempts": 5})
            ids.append(aid)
        raw = poison(f'"error":true,"extract_version":{extract_llm.EXTRACT_VERSION},"attempts":5,')
        ledger.db.execute("UPDATE artifacts SET meta=? WHERE artifact_id=?", (raw, ids[0]))
        now = time.time()
        ledger.db.execute("UPDATE artifacts SET created_at=? WHERE artifact_id IN (?,?)",
                          (now - extract_llm.REVIVE_COOLDOWN_S - 1, *ids))
        ledger.db.commit()
        assert extract_llm.revive_failed(ledger, now)["revived"] == 1
    finally:
        ledger.close()


def test_unreadable_qc_feedback_is_unavailable(tmp_path):
    ledger = _ledger(tmp_path)
    try:
        _seed_qc_flagged(ledger)
        src = ledger.artifacts(extract_llm.KIND)[-1]["artifact_id"]
        assert extract_llm._qc_feedback(ledger, src)
        raw = poison('"qc":"done","items":[{"verdict":"NO_MATCH","section":"meds","item":{"name":"SYNTH"}}],')
        ledger.db.execute("UPDATE artifacts SET content=? WHERE kind='extract_qc'", (raw,))
        ledger.db.commit()
        assert extract_llm._qc_feedback(ledger, src) is None
    finally:
        ledger.close()


def test_valid_thin_retry_replaces_unreadable_prior_result(tmp_path, monkeypatch):
    ledger = _ledger(tmp_path)
    try:
        ledger.save_messages([_message(body="合成本文" * 100)])
        aid = _extract_artifact(ledger, 1, {"summary": "SYNTH-old"}, _hash(ledger))
        ledger.db.execute("UPDATE artifacts SET content=? WHERE artifact_id=?",
                          (poison('"summary":"SYNTH-old",'), aid))
        ledger.db.commit()
        monkeypatch.setattr(extract_llm, "llm_extract", lambda *args, **kwargs: {"summary": "SYNTH-new"})
        assert extract_llm.run_pending(ledger, budget_s=180)["done"] == 1
        assert json.loads(ledger.artifacts(extract_llm.KIND)[-1]["content"])["summary"] == "SYNTH-new"
    finally:
        ledger.close()


def test_drug_refs_and_derivation_reject_unreadable_current_source(tmp_path):
    ledger = _ledger(tmp_path)
    try:
        dictionary = _dictionary(tmp_path)
        source = _seed(ledger)
        drug_map.derive(ledger, dictionary)
        assert drug_map.current_refs(ledger.db, 1)
        ledger.db.execute("UPDATE artifacts SET content=? WHERE artifact_id=?",
                          (poison('"meds":[], '), source))
        ledger.db.commit()
        assert drug_map.current_refs(ledger.db, 1) == []
        assert drug_map.derive(ledger, dictionary)["status"] == "ok"
    finally:
        ledger.close()


def test_rollup_unreadable_source_does_not_stop_patient_aggregation(tmp_path):
    ledger = _ledger(tmp_path)
    try:
        source = _seed(ledger)
        ledger.db.execute("UPDATE artifacts SET content=? WHERE artifact_id=?",
                          (poison('"summary":"SYNTH",'), source))
        ledger.db.commit()
        assert rollup.build_rollup(ledger, 1)["msg_count"] == 1
        rollup.rebuild(ledger, 1)
        ledger.db.execute("UPDATE artifacts SET content=? WHERE kind=?",
                          ('[' * 2000 + '0' + ']' * 2000, rollup.KIND))
        ledger.db.commit()
        rollup.rebuild(ledger, 1)
        assert json.loads(ledger.artifacts(rollup.KIND)[0]["content"])["msg_count"] == 1
    finally:
        ledger.close()


def test_rule_stats_cli_skips_poison_and_invalid_event_entries(tmp_path, monkeypatch, capsys):
    ledger = _ledger(tmp_path)
    ledger.artifact_add(extract.KIND, poison())
    ledger.artifact_add(extract.KIND, '{"events":[{},"synthetic-valid"]}')
    monkeypatch.setattr(extract, "LedgerReader", lambda path: ledger)
    monkeypatch.setattr(extract.sys, "argv", ["extract", "--stats"])
    try:
        assert extract.main() == 0
        assert '"synthetic-valid": 1' in capsys.readouterr().out
    finally:
        ledger.close()
