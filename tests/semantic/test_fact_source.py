"""fact_source gate tests (T7): legacy|shadow|canonical config
validation, v2->v1 projection shape, and the evidence-gated activation
command."""
import mcs_util
import json
from functools import cache
from pathlib import Path

import pytest

import semantic_evaluation as evaluation
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
    assert out["fact_source"] == "legacy"
    assert errors


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
        self.confirm_human = True
        self.reason = "reviewed_sealed_holdout"


def _cmd_cfg(tmp_path, monkeypatch, cfg):
    conf = tmp_path / "config.json"
    conf.write_text(json.dumps(cfg), encoding="utf-8")
    monkeypatch.setattr(mcs_setup, "CONF_PATH", str(conf))
    monkeypatch.setattr(mcs_util, 'load_config',
                        lambda: json.loads(conf.read_text()))
    return conf


@cache
def _shipped_criteria_identity():
    """(version, criteria_sha256) as the real scorer stamps them when run
    against the shipped G6 criteria file (synthetic record)."""
    from semantic_testkit import MANIFEST, _record
    criteria = json.loads((Path(__file__).resolve().parents[2] / "evaluation"
                           / "g6-criteria-v2.json").read_text())
    report = evaluation.evaluate_records([_record("human")], MANIFEST,
                                         criteria)
    return report["criteria_version"], report["criteria_sha256"]


def _report(pass_=True, g6=True, human=200):
    version, sha = _shipped_criteria_identity()
    return {"schema_version": "v1", "criteria_version": version,
            "criteria_sha256": sha,
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


def _synthetic_promotion(tmp_path, monkeypatch):
    from candidate_testkit import approved_config
    monkeypatch.setattr(mcs_util, 'HOME', str(tmp_path / 'evidence-fixture'))
    approved = approved_config({'semantic': {'fact_source': 'canonical'}})
    evidence = tmp_path / 'report.json'
    evidence.write_text(json.dumps(approved['semantic']['fact_source_evidence']))
    monkeypatch.setattr(mcs_util, 'HOME', str(tmp_path / 'approval-root'))
    initial = {'mcs_login_id': 'synthetic', 'notify_target': 'local',
               'semantic': {'fact_source': 'legacy'}, 'unknown': {'keep': True}}
    conf = _cmd_cfg(tmp_path, monkeypatch, initial)
    return conf, evidence, initial


def test_fact_source_concurrent_change_before_approval_preserves_both(tmp_path, monkeypatch, capsys):
    import semantic_candidate
    conf, evidence, initial = _synthetic_promotion(tmp_path, monkeypatch)
    other = {**initial, 'unknown': {'other': True}}
    promotion = semantic_candidate.promotion_problem
    def concurrent(cfg, report):
        conf.write_text(json.dumps(other))
        return promotion(cfg, report)
    monkeypatch.setattr(semantic_candidate, 'promotion_problem', concurrent)
    assert mcs_setup.cmd_fact_source(_Args('canonical', str(evidence))) == 1
    assert json.loads(conf.read_text()) == other
    assert not (Path(mcs_util.HOME) / 'data/semantic_approvals').exists()
    assert '設定は保存していません' in capsys.readouterr().out


def test_fact_source_concurrent_change_after_approval_keeps_receipt(tmp_path, monkeypatch, capsys):
    import semantic_candidate
    conf, evidence, initial = _synthetic_promotion(tmp_path, monkeypatch)
    other = {**initial, 'unknown': {'other': True}}
    record = semantic_candidate.record_approval
    def concurrent(cfg, report):
        identity = record(cfg, report)
        conf.write_text(json.dumps(other))
        return identity
    monkeypatch.setattr(semantic_candidate, 'record_approval', concurrent)
    assert mcs_setup.cmd_fact_source(_Args('canonical', str(evidence))) == 1
    assert json.loads(conf.read_text()) == other
    receipts = list((Path(mcs_util.HOME) / 'data/semantic_approvals').glob('*.json'))
    assert len(receipts) == 1
    out = capsys.readouterr().out
    assert '設定は保存していません' in out and '承認記録が保存された可能性' in out


def _split_promotion(tmp_path, monkeypatch):
    from copy import deepcopy
    from candidate_testkit import approved_config
    from test_mcs_setup_config_boundary import _synthetic_split

    _, providers = _synthetic_split(monkeypatch, tmp_path)
    view, _ = mcs_setup._init_split_snapshot()
    proposed = deepcopy(view)
    proposed['semantic']['fact_source'] = 'canonical'
    monkeypatch.setattr(mcs_util, 'HOME', str(tmp_path / 'synthetic-evidence-root'))
    approved = approved_config(proposed)
    evidence = tmp_path / 'synthetic-report.json'
    evidence.write_text(json.dumps(approved['semantic']['fact_source_evidence']))
    monkeypatch.setattr(mcs_util, 'HOME', str(tmp_path))
    return evidence, providers


def test_split_fact_source_canonical_commits_with_first_approval_receipt(tmp_path, monkeypatch):
    import semantic_candidate
    from test_mcs_setup_config_boundary import _split_state

    evidence, providers = _split_promotion(tmp_path, monkeypatch)
    assert not (tmp_path / 'data' / 'semantic_approvals').exists()
    assert mcs_setup.cmd_fact_source(_Args('canonical', str(evidence))) == 0
    state = _split_state(tmp_path)
    assert state['providers'] == providers
    cfg, _ = mcs_setup._init_split_snapshot()
    sem = cfg['semantic']
    assert sem['fact_source'] == 'canonical'
    assert semantic_candidate.approval_matches(cfg, sem['fact_source_evidence'], sem['fact_source_approval'])
    assert len(list((tmp_path / 'data' / 'semantic_approvals').glob('*.json'))) == 1
    assert not (tmp_path / 'data' / '.provider-settings-pending.json').exists()


def test_split_fact_source_pending_refuses_before_creating_approval(tmp_path, monkeypatch):
    evidence, _ = _split_promotion(tmp_path, monkeypatch)
    common = (tmp_path / 'config.json').read_bytes()
    pending = tmp_path / 'data' / '.provider-settings-pending.json'
    pending.write_text('{"synthetic":true}')
    pending.chmod(0o600)
    assert mcs_setup.cmd_fact_source(_Args('canonical', str(evidence))) == 1
    assert pending.read_text() == '{"synthetic":true}'
    assert (tmp_path / 'config.json').read_bytes() == common
    assert not (tmp_path / 'data' / 'semantic_approvals').exists()


@pytest.mark.parametrize('when', ['before', 'after'])
def test_split_fact_source_preserves_approval_and_competing_provider_edit(tmp_path, monkeypatch, when, capsys):
    import semantic_candidate

    evidence, _ = _split_promotion(tmp_path, monkeypatch)
    common = (tmp_path / 'config.json').read_bytes()
    provider = tmp_path / 'data' / 'provider_settings' / 'openai.json'
    changed = b'{"synthetic":"concurrent"}\n'
    if when == 'before':
        original = semantic_candidate.promotion_problem
        def competing(cfg, report):
            result = original(cfg, report)
            provider.write_bytes(changed)
            return result
        monkeypatch.setattr(semantic_candidate, 'promotion_problem', competing)
    else:
        original = semantic_candidate.record_approval
        def competing(cfg, report):
            result = original(cfg, report)
            provider.write_bytes(changed)
            return result
        monkeypatch.setattr(semantic_candidate, 'record_approval', competing)
    assert mcs_setup.cmd_fact_source(_Args('canonical', str(evidence))) == 1
    assert provider.read_bytes() == changed
    assert (tmp_path / 'config.json').read_bytes() == common
    receipts = list((tmp_path / 'data' / 'semantic_approvals').glob('*.json'))
    assert len(receipts) == (1 if when == 'after' else 0)
    out = capsys.readouterr().out
    assert '設定は保存していません' in out
    if when == 'after':
        assert '承認記録が保存された可能性' in out


def test_fact_source_busy_lock_does_not_create_approval(tmp_path, monkeypatch, capsys):
    import config_edit
    conf, evidence, initial = _synthetic_promotion(tmp_path, monkeypatch)
    with config_edit._edit_lock(conf):
        assert mcs_setup.cmd_fact_source(_Args('canonical', str(evidence))) == 1
    assert json.loads(conf.read_text()) == initial
    assert not (Path(mcs_util.HOME) / 'data/semantic_approvals').exists()
    assert '設定は保存していません' in capsys.readouterr().out


def test_fact_source_approval_failure_may_leave_receipt_without_config(tmp_path, monkeypatch, capsys):
    import semantic_candidate
    conf, evidence, initial = _synthetic_promotion(tmp_path, monkeypatch)
    record = semantic_candidate.record_approval
    def fail_after_record(cfg, report):
        record(cfg, report)
        raise OSError('synthetic receipt sync failure')
    monkeypatch.setattr(semantic_candidate, 'record_approval', fail_after_record)
    assert mcs_setup.cmd_fact_source(_Args('canonical', str(evidence))) == 1
    assert json.loads(conf.read_text()) == initial
    assert len(list((Path(mcs_util.HOME) / 'data/semantic_approvals').glob('*.json'))) == 1
    out = capsys.readouterr().out
    assert '承認記録の保存結果を確認できません' in out and '設定は保存していません' in out


def test_fact_source_config_fsync_failure_reports_unknown_after_publish(tmp_path, monkeypatch, capsys):
    import safe_io
    import semantic_candidate
    conf, evidence, initial = _synthetic_promotion(tmp_path, monkeypatch)
    def fail_sync(*args, **kwargs):
        raise OSError('synthetic directory sync failure')
    monkeypatch.setattr(safe_io, 'fsync_dir', fail_sync)
    assert mcs_setup.cmd_fact_source(_Args('canonical', str(evidence))) == 1
    saved = json.loads(conf.read_text())
    sem = saved['semantic']
    assert saved['unknown'] == initial['unknown'] and sem['fact_source'] == 'canonical'
    assert semantic_candidate.approval_matches(saved, sem['fact_source_evidence'], sem['fact_source_approval'])
    out = capsys.readouterr().out
    assert '設定の保存結果を確認できません' in out and '承認記録が保存された可能性' in out
    assert '設定は保存していません' not in out


def test_fact_source_absent_config_concurrent_creation_is_never_overwritten(tmp_path, monkeypatch, capsys):
    import config_edit
    conf = tmp_path / 'config.json'
    monkeypatch.setattr(mcs_setup, 'CONF_PATH', str(conf))
    snapshot = config_edit._snapshot
    def concurrent(path=None):
        conf.write_text('{"unknown":"kept"}')
        return snapshot(path)
    monkeypatch.setattr(config_edit, '_snapshot', concurrent)
    assert mcs_setup.cmd_fact_source(_Args('shadow')) == 1
    assert json.loads(conf.read_text()) == {'unknown': 'kept'}
    assert '設定は保存していません' in capsys.readouterr().out


def test_fact_source_missing_config_is_exclusively_created(tmp_path, monkeypatch):
    conf = tmp_path / 'config.json'
    monkeypatch.setattr(mcs_setup, 'CONF_PATH', str(conf))
    assert mcs_setup.cmd_fact_source(_Args('shadow')) == 0
    assert json.loads(conf.read_text()) == {'semantic': {'fact_source': 'shadow'}}
    assert conf.stat().st_mode & 0o777 == 0o600


def test_fact_source_symlink_is_never_followed(tmp_path, monkeypatch):
    target = tmp_path / 'target.json'
    target.write_text('{"unknown":"kept"}')
    conf = tmp_path / 'config.json'
    conf.symlink_to(target)
    monkeypatch.setattr(mcs_setup, 'CONF_PATH', str(conf))
    assert mcs_setup.cmd_fact_source(_Args('shadow')) == 1
    assert conf.is_symlink() and target.read_text() == '{"unknown":"kept"}'


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


def test_fact_source_rejects_truthy_nonboolean_gate(tmp_path, monkeypatch):
    conf = _cmd_cfg(tmp_path, monkeypatch, {"mcs_login_id": "x",
                                            "notify_target": "local"})
    before = conf.read_bytes()
    evidence = tmp_path / "report.json"
    for report in (_report(pass_="false"), _report(g6="false"), _report(human=True)):
        evidence.write_text(json.dumps(report))
        assert mcs_setup.cmd_fact_source(_Args("canonical", str(evidence))) == 1
        assert conf.read_bytes() == before


def test_fact_source_canonical_pins_gate(tmp_path, monkeypatch):
    conf = _cmd_cfg(tmp_path, monkeypatch, {"mcs_login_id": "x",
                                            "notify_target": "slack"})
    ev = tmp_path / "report.json"
    from candidate_testkit import approved_config
    approved = approved_config({'semantic': {'fact_source': 'canonical'}})
    ev.write_text(json.dumps(approved['semantic']['fact_source_evidence']), encoding="utf-8")
    assert mcs_setup.cmd_fact_source(
        _Args("canonical", str(ev))) == 0
    sem = json.loads(conf.read_text())["semantic"]
    assert sem["fact_source"] == "canonical"
    assert sem["fact_source_gate"].startswith(
        _shipped_criteria_identity()[1] + ":")
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


@pytest.mark.parametrize("attack", [
    "one_not_tested", "below_min_human", "missing_criteria",
    "foreign_criteria", "missing_count", "missing_provenance",
    "gate_reasons", "gate_reasons_missing",
])
def test_fact_source_canonical_rejects_untrusted_receipts(
        tmp_path, monkeypatch, attack):
    """Gate booleans alone never promote: the report must be scored on
    the shipped criteria, carry >= min_human_labels human labels, and
    have an empty reason list."""
    conf = _cmd_cfg(tmp_path, monkeypatch, {
        "mcs_login_id": "x", "notify_target": "slack",
        "semantic": {"mode": "off", "fact_source": "legacy"}})
    before = conf.read_bytes()
    report = _report()
    if attack == "one_not_tested":
        report["label_provenance"]["human"] = 1
        report["human_validation"] = {"status": "NOT_TESTED",
                                      "verified_human_cases": 0}
    elif attack == "below_min_human":
        report["label_provenance"]["human"] = 199
    elif attack == "missing_criteria":
        report.pop("criteria_version")
        report.pop("criteria_sha256")
    elif attack == "foreign_criteria":
        report["criteria_sha256"] = "0" * 64
    elif attack == "missing_count":
        report["label_provenance"].pop("human")
    elif attack == "missing_provenance":
        report.pop("label_provenance")
    elif attack == "gate_reasons":
        report["gate"]["reasons"] = ["human_labels_insufficient"]
    elif attack == "gate_reasons_missing":
        report["gate"].pop("reasons")
    evidence = tmp_path / "report.json"
    evidence.write_text(json.dumps(report), encoding="utf-8")
    assert mcs_setup.cmd_fact_source(
        _Args("canonical", str(evidence))) == 1
    assert conf.read_bytes() == before


def test_report_alone_never_mints_human_approval(tmp_path, monkeypatch):
    from candidate_testkit import approved_config
    conf = _cmd_cfg(tmp_path, monkeypatch, {'mcs_login_id': 'x', 'notify_target': 'local'})
    before = conf.read_bytes()
    approved = approved_config({'semantic': {'fact_source': 'canonical'}})
    ev = tmp_path / 'report.json'
    ev.write_text(json.dumps(approved['semantic']['fact_source_evidence']))
    args = _Args('canonical', str(ev))
    args.confirm_human = False
    assert mcs_setup.cmd_fact_source(args) == 1
    assert conf.read_bytes() == before
