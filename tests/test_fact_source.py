"""fact_source gate tests (T7): legacy|shadow|canonical config
validation, v2->v1 projection shape, and the evidence-gated activation
command."""
import json


import semantic_facts as sf
from semantic_policy import semantic_config
import semantic_projection as projection
import mcs_setup


def _scfg(block):
    return semantic_config({"semantic": block})


def test_fact_source_defaults_legacy():
    out, errors = _scfg({"mode": "off"})
    assert out["fact_source"] == "legacy"
    assert not errors


def test_fact_source_shadow_ok():
    out, errors = _scfg({"mode": "off", "fact_source": "shadow"})
    assert out["fact_source"] == "shadow"
    assert not errors


def test_fact_source_canonical_requires_gate():
    out, errors = _scfg({"mode": "off", "fact_source": "canonical"})
    assert "config: semantic_fact_source_gate_required" in errors
    assert out["mode"] == "off"          # fail closed


def test_fact_source_canonical_with_gate():
    out, errors = _scfg({
        "mode": "off", "fact_source": "canonical",
        "fact_source_gate": "g6-v1:abc123"})
    assert out["fact_source"] == "canonical"
    assert out["fact_source_gate"] == "g6-v1:abc123"
    assert not errors


def test_fact_source_invalid_value_fails_closed():
    out, errors = _scfg({"mode": "shadow", "fact_source": "bogus"})
    assert "config: semantic_fact_source_invalid" in errors
    assert out["mode"] == "off"
    assert out["fact_source"] == "legacy"


def test_fact_source_gate_shape_validated():
    _, errors = _scfg({"mode": "off", "fact_source": "shadow",
                       "fact_source_gate": 42})
    assert "config: semantic_fact_source_gate_invalid" in errors


def _v2_doc(facts, evidence):
    return {"version": sf.CONTRACT_VERSION,
            "source": {"message_id": "m1", "revision": "r1",
                       "content_hash": "h", "body_codepoints": 10,
                       "content_quality": "full",
                       "attachments_complete": True,
                       "source_fingerprint": "sf_x"},
            "atoms": [], "chunks": [], "obligations": [],
            "evidence": evidence, "facts": facts, "relations": [],
            "coverage": {"category_counts": {},
                         "open_obligation_ids": [], "limitations": [],
                         "status": "complete"}}


def _v2_fact(**kw):
    base = {"fact_id": "fact_a", "kind": "medication_event",
            "subject": "patient:1", "actor": "sender:s1",
            "statement": "アムロジピン5mg継続", "polarity": "affirmed",
            "epistemic": "asserted", "workflow_status": "performed",
            "event_time": "unknown", "valid_time": "unknown",
            "evidence_ids": [], "obligation_ids": [],
            "importance": "T1", "provenance": "local_llm",
            "validation_status": "unverified", "action": "continue"}
    base.update(kw)
    return base


def test_project_v2_facts_shape_and_mapping():
    ev = {"evidence_id": "ev_1", "message_id": "m1", "revision": "r1",
          "start": 0, "end": 9, "quote": "アムロジピン5mg", "atom_id": "atom_x"}
    doc = _v2_doc(
        [_v2_fact(evidence_ids=["ev_1"], validation_status="verified",
                  event_time="2026-09-10")],
        [ev])
    facts = projection.project_v2_facts(doc)
    assert len(facts) == 1
    fact = facts[0]
    assert fact["kind"] == "medication_event"
    assert fact["status"] == "execution_reported"
    assert fact["polarity"] == "affirmed"
    assert fact["occurred_at"] == "2026-09-10"
    assert fact["validation_status"] == "verified"
    assert fact["evidence_refs"] == ["ev_1"]
    assert fact["_evidence"]["start_codepoint"] == 0
    assert fact["_v2_kind"] == "medication_event"
    assert fact["_v2_importance"] == "T1"


def test_project_v2_facts_never_upgrades_unverified():
    doc = _v2_doc([_v2_fact(validation_status="unverified")], [])
    fact = projection.project_v2_facts(doc)[0]
    assert fact["validation_status"] == "unverified"
    assert fact["evidence_refs"] == []
    assert fact["_evidence"] is None


def test_project_v2_kind_mapping():
    doc = _v2_doc([
        _v2_fact(fact_id="f1", kind="symptom_state",
                 statement="頭痛"),
        _v2_fact(fact_id="f2", kind="request_pending",
                 statement="写真依頼"),
        _v2_fact(fact_id="f3", kind="preference",
                 statement="安静希望")], [])
    kinds = [f["kind"] for f in projection.project_v2_facts(doc)]
    assert kinds == ["symptom", "explicit_request", "preference"]


class _Args:
    def __init__(self, fact_source, gate_evidence=None):
        self.fact_source = fact_source
        self.gate_evidence = gate_evidence


def _cmd_cfg(tmp_path, monkeypatch, cfg):
    conf = tmp_path / "config.json"
    conf.write_text(json.dumps(cfg), encoding="utf-8")
    monkeypatch.setattr(mcs_setup, "CONF_PATH", str(conf))
    monkeypatch.setattr(mcs_setup, "load_config",
                        lambda: json.loads(conf.read_text()))
    return conf


def _report(pass_=True, g6=True, human=200):
    return {"schema_version": "v1", "criteria_version": "g6-v1",
            "label_provenance": {"human": human, "synthetic": 0,
                                 "missing": 0},
            "gate": {"pass": pass_, "g6_eligible": g6, "reasons": []}}


def test_fact_source_shadow_writes_without_gate(tmp_path, monkeypatch):
    conf = _cmd_cfg(tmp_path, monkeypatch, {"mcs_login_id": "x",
                                            "notify_target": "slack"})
    assert mcs_setup.cmd_fact_source(_Args("shadow")) == 0
    cfg = json.loads(conf.read_text())
    assert cfg["semantic"]["fact_source"] == "shadow"
    assert "fact_source_gate" not in cfg["semantic"]


def test_fact_source_canonical_requires_evidence(tmp_path, monkeypatch):
    conf = _cmd_cfg(tmp_path, monkeypatch, {"mcs_login_id": "x",
                                            "notify_target": "slack"})
    assert mcs_setup.cmd_fact_source(_Args("canonical")) == 1
    assert "fact_source" not in json.loads(conf.read_text()).get(
        "semantic", {})


def test_fact_source_canonical_rejects_failing_evidence(
        tmp_path, monkeypatch):
    _cmd_cfg(tmp_path, monkeypatch, {"mcs_login_id": "x",
                                   "notify_target": "slack"})
    ev = tmp_path / "report.json"
    ev.write_text(json.dumps(_report(pass_=False)), encoding="utf-8")
    assert mcs_setup.cmd_fact_source(
        _Args("canonical", str(ev))) == 1


def test_fact_source_canonical_rejects_synthetic(tmp_path, monkeypatch):
    _cmd_cfg(tmp_path, monkeypatch, {"mcs_login_id": "x",
                                   "notify_target": "slack"})
    ev = tmp_path / "report.json"
    ev.write_text(json.dumps(_report(human=0)), encoding="utf-8")
    assert mcs_setup.cmd_fact_source(
        _Args("canonical", str(ev))) == 1


def test_fact_source_canonical_pins_gate(tmp_path, monkeypatch):
    conf = _cmd_cfg(tmp_path, monkeypatch, {"mcs_login_id": "x",
                                            "notify_target": "slack"})
    ev = tmp_path / "report.json"
    ev.write_text(json.dumps(_report()), encoding="utf-8")
    assert mcs_setup.cmd_fact_source(
        _Args("canonical", str(ev))) == 0
    sem = json.loads(conf.read_text())["semantic"]
    assert sem["fact_source"] == "canonical"
    assert sem["fact_source_gate"].startswith("g6-v1:")
    # The pinned config passes the production validator.
    out, errors = semantic_config(json.loads(conf.read_text()))
    assert out["fact_source"] == "canonical" and not errors


def test_fact_source_refuses_nondict_semantic(tmp_path, monkeypatch):
    """A malformed `semantic` block must fail closed — the gate-changing
    command never silently discards an existing config value, and must
    return 1 rather than crash on `sem.pop`."""
    conf = _cmd_cfg(tmp_path, monkeypatch, {
        "mcs_login_id": "x", "notify_target": "slack",
        "semantic": "off"})
    assert mcs_setup.cmd_fact_source(_Args("legacy")) == 1
    assert json.loads(conf.read_text())["semantic"] == "off"


def test_fact_source_back_to_legacy_clears_gate(tmp_path, monkeypatch):
    conf = _cmd_cfg(tmp_path, monkeypatch, {
        "mcs_login_id": "x", "notify_target": "slack",
        "semantic": {"fact_source": "canonical",
                     "fact_source_gate": "g6-v1:old"}})
    assert mcs_setup.cmd_fact_source(_Args("legacy")) == 0
    sem = json.loads(conf.read_text())["semantic"]
    assert sem["fact_source"] == "legacy"
    assert "fact_source_gate" not in sem
