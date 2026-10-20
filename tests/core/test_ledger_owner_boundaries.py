"""Synthetic borrowed observation, approved policy and atomic writer entrances."""
import builtins
import json

from common_testkit import ledger_fixture
import message_observations
import stamp_read
import signal_policy

led = ledger_fixture()


def test_observation_readers_keep_borrowed_transaction_and_savepoint(led):
    db = led.db
    db.execute("BEGIN IMMEDIATE")
    try:
        db.execute("SAVEPOINT observations")
        before = db.total_changes
        assert message_observations.metadata_watch_targets(db, now=100) == []
        assert stamp_read.reaction_actor_targets(db, now=100) == []
        assert stamp_read.reaction_actor_summary(db, 1, now=100)["state"] == "not_fetched"
        assert db.total_changes == before and db.in_transaction
        db.execute("ROLLBACK TO observations")
        db.execute("RELEASE observations")
    finally:
        db.rollback()
    assert db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0


def test_latest_invalid_policy_does_not_revive_older_approval(led):
    db = led.db
    db.execute("BEGIN IMMEDIATE")
    try:
        db.execute("SAVEPOINT approved_policy")
        doc = {"actor": "synthetic-operator", "command_id": "synthetic-command",
               "policy": {"fyi_max_age_d": 7, "followup_days": True,
                          "conc_min_posts": 999}}
        db.execute("INSERT INTO artifacts(kind,content,meta,created_at) VALUES(?,?,?,?)",
                   (signal_policy.POLICY_KIND, json.dumps(doc), "{}", 1))
        approved = signal_policy.resolved_thresholds(db)
        assert approved["fyi_max_age_d"] == 7
        assert approved["followup_days"] == signal_policy.FOLLOWUP_DAYS
        assert approved["conc_min_posts"] == signal_policy.CONC_MIN_POSTS
        db.execute("INSERT INTO artifacts(kind,content,meta,created_at) VALUES(?,?,?,?)",
                   (signal_policy.POLICY_KIND, '{"policy":{"fyi_max_age_d":9}}', "{}", 2))
        before = db.total_changes
        assert signal_policy.approved_policy(db) is None
        assert signal_policy.resolved_thresholds(db)["fyi_max_age_d"] == signal_policy.FYI_MAX_AGE_D
        assert db.in_transaction and db.total_changes == before
        db.execute("ROLLBACK TO approved_policy")
        db.execute("RELEASE approved_policy")
    finally:
        db.rollback()


def test_public_job_and_semantic_tx_entrances_do_not_commit_outer_work(led):
    led.ensure_patient(1)
    with led.db:
        led.db.execute("INSERT INTO messages(message_id,project_id,body_state,body_text,content_hash) "
                       "VALUES(1,1,'full','synthetic source',?)", ("a" * 64,))
    db = led.db
    db.execute("BEGIN IMMEDIATE")
    try:
        db.execute("SAVEPOINT intake")
        cursor = led.job_add_tx("history", 1, payload={"since": 0})
        assert cursor.rowcount == 1
        assert led.semantic_seed_tx(1, [1], {"source": "synthetic", "notification_free": True}) is None
        assert db.in_transaction
        assert db.execute("SELECT COUNT(*) FROM fetch_jobs").fetchone()[0] == 2
        db.execute("ROLLBACK TO intake")
        assert db.execute("SELECT COUNT(*) FROM fetch_jobs").fetchone()[0] == 0
        assert db.execute("SELECT body_text FROM messages").fetchone()[0] == "synthetic source"
        db.execute("RELEASE intake")
    finally:
        db.rollback()


def test_outbox_route_contract_does_not_import_card_rendering(led, monkeypatch):
    real_import = builtins.__import__

    def guarded(name, *args, **kwargs):
        if name in {"notify_cards", "notify_render"}:
            raise AssertionError("raw outbox route must not load card rendering")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded)
    db = led.db
    db.execute("BEGIN IMMEDIATE")
    try:
        db.execute("SAVEPOINT routing")
        for kind in ("new_messages", "signal", "daily_digest", "urgent_notice"):
            event = led.outbox_add_tx(kind, None, {})
            assert db.execute("SELECT route FROM notify_outbox WHERE event_id=?", (event,)).fetchone()[0] == "interactive"
        for kind, override in (("synthetic", None), ("new_messages", "text")):
            event = led.outbox_add_tx(kind, None, {}, route=override)
            assert db.execute("SELECT route FROM notify_outbox WHERE event_id=?", (event,)).fetchone()[0] == "text"
        assert db.in_transaction
        db.execute("ROLLBACK TO routing")
        db.execute("RELEASE routing")
    finally:
        db.rollback()
    assert db.execute("SELECT COUNT(*) FROM notify_outbox").fetchone()[0] == 0
