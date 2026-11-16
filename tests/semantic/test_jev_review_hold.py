"""Synthetic checks that a facility review withholding Jev stops only Jev."""
import json
import time

import pytest

import cloud_policy
import external_transfer
import mcs_util
import semantic_drain
import semantic_jev
import semantic_policy
from semantic_testkit import _FakeJev, _cfg, _ledger, _llm, _message, _patient, _seeded


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(mcs_util, "HOME", str(tmp_path))
    return tmp_path


def _review(root, services):
    external_transfer.require(root)
    external_transfer.acknowledge(by="管理者", reason="契約確認", services=services,
                                  confirm_human=True, root=root)


def test_without_a_record_jev_runs_as_before(home):
    db = _seeded(home)
    fake = _FakeJev()
    try:
        out = semantic_drain.run_due(db, _cfg("assist"), {"errors": []}, time.monotonic() + 300,
                                     jev_client=fake, llm_fn=_llm)
        assert out["done"] == 1 and fake.requests_made > 0
        assert "jev_skipped" not in out
        assert semantic_policy.runtime_config(_cfg("assist"))[0]["jev_held"] is False
    finally:
        db.close()


def test_a_hold_keeps_local_semantic_work_and_sends_nothing_to_jev(home):
    _review(home, ["anthropic"])
    cfg = _cfg("assist")
    scfg, errors = semantic_policy.runtime_config(cfg)
    assert errors == [] and scfg["mode"] == "assist" and scfg["jev_held"] is True
    db = _seeded(home)
    fake = _FakeJev()
    try:
        out = semantic_drain.run_due(db, cfg, {"errors": []}, time.monotonic() + 300,
                                     jev_client=fake, llm_fn=_llm)
        assert out["mode"] == "assist" and out["done"] == 1
        assert fake.requests_made == 0 and fake.calls == []
        assert out["jev_skipped"] == {semantic_jev.REVIEW_HELD: 1}
        # the local pipeline ran and records why Jev did not
        for kind in ("semantic_bundle", "semantic_facts", "semantic_summary", "semantic_audit"):
            assert db.artifacts(kind, message_id=1), kind
        assess = json.loads(db.artifacts("semantic_assess", message_id=1)[-1]["meta"])
        assert assess["technical_status"] == "skipped"
        assert assess["jev_skip_reason"] == semantic_jev.REVIEW_HELD
        audit = json.loads(db.artifacts("semantic_audit", message_id=1)[-1]["meta"])
        assert audit["audit_status"] == "SKIPPED" and audit["jev_skip_reason"] == semantic_jev.REVIEW_HELD
        assert not db.artifacts("semantic_usage")
    finally:
        db.close()


def test_a_hold_never_builds_a_jev_client(home, monkeypatch):
    _review(home, ["anthropic"])
    built = []
    monkeypatch.setattr(semantic_jev, "JevClient", lambda *a, **k: built.append(k))
    db = _seeded(home)
    try:
        out = semantic_drain.run_due(db, _cfg("assist"), {"errors": []}, time.monotonic() + 300,
                                     llm_fn=_llm)
        assert out["done"] == 1 and built == []
        assert out["jev_skipped"] == {semantic_jev.REVIEW_HELD: 1}
    finally:
        db.close()


def test_omitted_mode_stays_off_under_a_hold(home):
    _review(home, ["anthropic"])
    scfg, _ = semantic_policy.semantic_config({}, key_present=True)
    assert scfg["mode"] == "off" and scfg["jev_held"] is True


def test_cloud_backlog_route_follows_its_own_approval(home):
    env = home / ".env"
    env.write_text("MCS_ANTHROPIC_API_KEY=synthetic-a\nTYPESAFE_API_KEY=synthetic-j\n")
    env.chmod(0o600)
    cloud = {"cloud_llm": {"mode": "on", "provider": "anthropic", "project_ids": [1]}}
    cfg = {**_cfg("assist"), **cloud}
    _review(home, ["anthropic"])
    assert semantic_policy.runtime_config(cfg)[0]["mode"] == "assist"
    assert semantic_policy.runtime_config(cfg)[0]["jev_held"] is True
    assert cloud_policy.resolve_settings(cfg, home)[0]["mode"] == "on"
    external_transfer.acknowledge(by="管理者", reason="Jevのみ", services=["jev"],
                                  confirm_human=True, root=home)
    assert semantic_policy.runtime_config(cfg)[0]["jev_held"] is False
    assert cloud_policy.resolve_settings(cfg, home)[0]["mode"] == "off"


def test_enforce_under_a_hold_publishes_as_skipped_never_as_passed(home, monkeypatch):
    import semantic_send_gate
    import semantic_store
    from semantic_testkit import _canonical_cfg, _llm_v2, _seeded_two
    _review(home, ["openai"])
    db = _seeded_two(home)
    cfg = _canonical_cfg()
    fake = _FakeJev()
    try:
        out = semantic_drain.run_due(db, cfg, {"errors": []}, time.monotonic() + 300,
                                     jev_client=fake, llm_fn=_llm_v2)
        assert out["done"] == 1 and fake.requests_made == 0
        rows = db.db.execute("SELECT meta FROM artifacts WHERE kind='semantic_audit'").fetchall()
        assert rows and all(json.loads(r["meta"])["audit_status"] == "SKIPPED"
                            and json.loads(r["meta"])["jev_skip_reason"] == semantic_jev.REVIEW_HELD
                            for r in rows)
        scfg, _ = semantic_drain.runtime_config(cfg)
        bundle = semantic_store.thread_bundle(db.db, 1, 1, [1])
        policy = semantic_drain.policy_fingerprint(scfg)
        current = semantic_send_gate.current_summary(db.db, 1, 1, bundle, policy)
        assert current is not None and current["meta"]["audit_status"] == "SKIPPED"
        member = next(m for m in bundle["members"] if m["message_id"] == 1)
        assert not semantic_send_gate.passed_summary_current(
            db.db, 1, 1, bundle["source_fingerprint"], policy, member["revision"], "enforce", bundle=bundle)
    finally:
        db.close()


def test_withdrawal_mid_drain_stops_jev_at_the_next_request(home):
    _review(home, ["jev"])
    db = _ledger(home)
    p = _patient(db)
    for i in (10, 11):                      # two separate threads -> two jobs
        p.messages = [_message(i)]
        db.save_patient(p, notify={"source": "unread"}, semantic=True)
    cfgf = home / "cfg.json"
    cfgf.write_text(json.dumps(_cfg("assist")))

    class Withdrawing(_FakeJev):
        def evaluate(self, state, questions, deadline):
            result = super().evaluate(state, questions, deadline)
            if self.requests_made == 1:
                external_transfer.acknowledge(by="管理者", reason="Jev取消", services=["anthropic"],
                                              confirm_human=True, root=home)
            return result

    fake = Withdrawing()
    try:
        out = semantic_drain.run_due(db, json.loads(cfgf.read_text()), {"errors": []},
                                     time.monotonic() + 300, jev_client=fake, llm_fn=_llm,
                                     cfg_path=str(cfgf), max_jobs=4)
        assert fake.requests_made == 1               # nothing after the withdrawal
        assert out["jev_skipped"].get(semantic_jev.REVIEW_HELD, 0) >= 1
        assert out["mode"] == "assist"
    finally:
        db.close()
