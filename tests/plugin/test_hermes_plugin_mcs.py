"""Synthetic Hermes/MCS plugin contracts; no live DB, network, or gateway."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import hermes_plugin
from hermes_plugin import projects
import job_ops
import ledger
import mcs_requests
import semantic
from ops_testkit import _source


class _Context:
    def __init__(self, settings):
        self.settings = settings
        self.command = None

    def get_config(self, key, default=None):
        return self.settings.get(key, default)

    def register_command(self, name, handler, **kwargs):
        self.command = (name, handler, kwargs)


def _settings(tmp_path, *, complete=True):
    values = {
        "snapshot": str(tmp_path / "snapshots" / "ledger-snapshot.db"),
        "inbox": str(tmp_path / "cmd"),
        "allowed_user_ids": ["user-1"],
        "allowed_chat_ids": ["chat-1"],
        "project_ids": [1],
    }
    if not complete:
        values.pop("project_ids")
    return values


def _context(**changes):
    value = {
        "platform": "discord", "authorized": True, "internal": False,
        "is_bot": False, "via_upstream_relay": False, "native_input": True,
        "user_id": "user-1", "chat_id": "chat-1", "scope_id": "scope-1",
        "profile": "default", "message_id": "discord-message-1",
    }
    value.update(changes)
    return value


def _handler(tmp_path, *, complete=True):
    ctx = _Context(_settings(tmp_path, complete=complete))
    hermes_plugin.register(ctx)
    assert ctx.command is not None
    assert ctx.command[0] == "mcs"
    return ctx.command[1]


def _call(handler, payload, context):
    return json.loads(handler(json.dumps(payload), command_context=context))


def test_native_identity_and_scope_fail_closed(tmp_path):
    handler = _handler(tmp_path)
    command = {"op": "status", "project_id": 1}
    missing_profile = _context()
    missing_profile.pop("profile")
    missing_scope = _context()
    missing_scope.pop("scope_id")
    missing_message = _context()
    missing_message.pop("message_id")
    denied = (
        (None, "native_context_required"),
        (_context(internal=True), "native_context_rejected"),
        (_context(is_bot=True), "native_context_rejected"),
        (_context(via_upstream_relay=True), "native_context_rejected"),
        (_context(native_input=False), "native_context_rejected"),
        (_context(user_id="other-user"), "user_not_allowed"),
        (_context(chat_id="other-chat"), "chat_not_allowed"),
        (missing_profile, "native_context_required"),
        (missing_scope, "native_context_required"),
        (missing_message, "native_context_required"),
    )
    for context, expected in denied:
        result = _call(handler, command, context)
        assert result == {"ok": False, "error": expected}
    assert _call(handler, command, _context()) == {
        "ok": False, "error": "operation_failed"}
    incomplete = _handler(tmp_path, complete=False)
    assert _call(incomplete, command, _context()) == {
        "ok": False, "error": "plugin_config_incomplete"}
    assert _call(handler, {**command, "project_id": 2}, _context()) == {
        "ok": False, "error": "project_not_allowed"}
    assert _call(handler, {**command, "actor": "self-declared"}, _context()) == {
        "ok": False, "error": "unknown_field"}


def test_auto_project_scope_tracks_snapshot_replacement(tmp_path):
    snapshot = tmp_path / "snapshot.db"

    def publish(project_id, dest):
        db = sqlite3.connect(dest)
        try:
            db.execute("CREATE TABLE patients (project_id INTEGER)")
            db.execute("INSERT INTO patients VALUES (?)", (project_id,))
            db.commit()
        finally:
            db.close()

    publish(1, snapshot)
    settings = {"project_ids": set(), "project_ids_auto": True,
                "snapshot": str(snapshot)}
    assert projects.project_allowed(settings, 1)
    replacement = tmp_path / "replacement.db"
    publish(2, replacement)
    os.replace(replacement, snapshot)
    assert not projects.project_allowed(settings, 1)
    assert projects.project_allowed(settings, 2)


def test_snapshot_read_preview_confirm_and_receipt_pipeline(tmp_path):
    db = _source(tmp_path)
    inbox = tmp_path / "cmd"
    inbox.mkdir()
    snapshot = ledger.publish_snapshot(str(tmp_path / "source.db"),
                                       str(tmp_path / "snapshots"))
    assert snapshot
    ctx = _Context(_settings(tmp_path))
    hermes_plugin.register(ctx)
    handler = ctx.command[1]
    context = _context()
    try:
        status = _call(handler, {"op": "status", "project_id": 1}, context)
        assert status["ok"] is True
        optional_scope = _call(
            handler, {"op": "status", "project_id": 1},
            _context(scope_id=None, profile=None, message_id=None))
        assert optional_scope["ok"] is True
        evidence = _call(handler, {
            "op": "read", "kind": "evidence", "project_id": 1,
            "message_id": 1,
        }, context)
        assert evidence["ok"] is True
        assert evidence["result"]["message"]["message_id"] == 1

        before = sorted(inbox.iterdir())
        missing_reason = _call(handler, {
            "op": "request", "phase": "preview", "action": "create",
            "project_id": 1, "source_message_id": 1,
            "title": "synthetic confirmed request",
        }, context)
        assert missing_reason == {"ok": False, "error": "reason_required"}
        invalid_reason = _call(handler, {
            "op": "request", "phase": "preview", "action": "create",
            "project_id": 1, "source_message_id": 1,
            "title": "synthetic confirmed request", "reason": "\x00",
        }, context)
        assert invalid_reason == {"ok": False, "error": "bad_reason"}
        preview = _call(handler, {
            "op": "request", "phase": "preview", "action": "create",
            "project_id": 1, "source_message_id": 1,
            "title": "synthetic confirmed request",
            "reason": "synthetic human reason",
        }, context)
        assert preview["ok"] is True
        assert preview["payload"]["actor"] == "discord:user-1"
        assert preview["payload"]["human_confirmed"] is True
        assert preview["payload"]["reason"] == "synthetic human reason"
        assert preview["queued"] is False
        assert preview["confirmation_required"] is True
        assert preview["payload_hash"] == mcs_requests.payload_hash({
            "payload": preview["payload"], "origin": preview["origin"]})
        assert sorted(inbox.iterdir()) == before

        malformed_payload = {**preview["payload"], "title": {"nested": [1]}}
        malformed = _call(handler, {
            "op": "request", "phase": "confirm",
            "payload": malformed_payload,
            "payload_hash": mcs_requests.payload_hash({
                "payload": malformed_payload, "origin": preview["origin"]}),
            "origin": preview["origin"],
        }, context)
        assert malformed == {"ok": False, "error": "invalid_command"}

        tampered_payload = {**preview["payload"], "reason": "changed reason"}
        tampered = _call(handler, {
            "op": "request", "phase": "confirm",
            "payload": tampered_payload,
            "payload_hash": preview["payload_hash"],
            "origin": preview["origin"],
        }, context)
        assert tampered == {"ok": False, "error": "payload_hash_mismatch"}
        missing_payload = {
            key: value for key, value in preview["payload"].items()
            if key != "reason"
        }
        missing_confirm = _call(handler, {
            "op": "request", "phase": "confirm",
            "payload": missing_payload,
            "payload_hash": mcs_requests.payload_hash({
                "payload": missing_payload, "origin": preview["origin"]}),
            "origin": preview["origin"],
        }, context)
        assert missing_confirm == {"ok": False, "error": "reason_required"}

        original = db.db.execute(
            "SELECT body_html,body_text,content_hash FROM messages "
            "WHERE message_id=1").fetchone()
        changed_body = "<p>source changed after preview</p>"
        changed_hash = hashlib.sha256(changed_body.encode()).hexdigest()
        with db.db:
            db.db.execute(
                "UPDATE messages SET body_html=?,body_text=?,content_hash=? "
                "WHERE message_id=1",
                (changed_body, "source changed after preview", changed_hash))
        assert ledger.publish_snapshot(
            str(tmp_path / "source.db"), str(tmp_path / "snapshots"))
        stale_confirm = _call(handler, {
            "op": "request", "phase": "confirm",
            "payload": preview["payload"],
            "payload_hash": preview["payload_hash"],
            "origin": preview["origin"],
        }, context)
        assert stale_confirm == {"ok": False, "error": "source_changed"}
        assert sorted(inbox.iterdir()) == before
        with db.db:
            db.db.execute(
                "UPDATE messages SET body_html=?,body_text=?,content_hash=? "
                "WHERE message_id=1",
                tuple(original))
        assert ledger.publish_snapshot(
            str(tmp_path / "source.db"), str(tmp_path / "snapshots"))

        ctx.settings["allowed_user_ids"] = ["other-user"]
        revoked = _call(handler, {
            "op": "request", "phase": "confirm",
            "payload": preview["payload"],
            "payload_hash": preview["payload_hash"],
            "origin": preview["origin"],
        }, context)
        assert revoked == {"ok": False, "error": "user_not_allowed"}
        ctx.settings["allowed_user_ids"] = ["user-1"]

        confirm = _call(handler, {
            "op": "request", "phase": "confirm",
            "payload": preview["payload"],
            "payload_hash": preview["payload_hash"],
            "origin": preview["origin"],
        }, context)
        assert confirm["ok"] is True
        assert confirm["receipt"]["outcome"] == "queued"
        command_file = next(inbox.glob("*.json"))
        assert json.loads(command_file.read_text()) == preview["payload"]

        drain_result = {"errors": []}
        job_ops.drain_commands(db, drain_result, str(inbox))
        assert drain_result["errors"] == []
        request = db.db.execute("SELECT * FROM requests WHERE project_id=1").fetchone()
        assert request["revision"] == 1
        receipt = db.db.execute(
            "SELECT receipt_json FROM command_receipts WHERE command_id=?",
            (preview["payload"]["command_id"],)).fetchone()
        assert json.loads(receipt["receipt_json"])["reason"] == \
            "synthetic human reason"

        ledger.publish_snapshot(str(tmp_path / "source.db"),
                                str(tmp_path / "snapshots"))
        update_preview = _call(handler, {
            "op": "request", "phase": "preview", "action": "update",
            "project_id": 1, "request_id": request["request_id"],
            "patch": {"status": "done"},
            "reason": "synthetic completion reason",
        }, context)
        assert update_preview["ok"] is True
        assert update_preview["payload"]["reason"] == \
            "synthetic completion reason"
        assert update_preview["payload"]["expected_revision"] == 1
        assert (update_preview["payload"]["expected_source_hash"]
                == request["source_hash"])
        update_confirm = _call(handler, {
            "op": "request", "phase": "confirm",
            "payload": update_preview["payload"],
            "payload_hash": update_preview["payload_hash"],
            "origin": update_preview["origin"],
        }, context)
        assert update_confirm["ok"] is True
        job_ops.drain_commands(db, {"errors": []}, str(inbox))
        updated = db.db.execute(
            "SELECT revision,status FROM requests WHERE request_id=?",
            (request["request_id"],)).fetchone()
        assert tuple(updated) == (2, "done")
    finally:
        db.close()


def test_request_loop_ref_binds_candidate_and_rejects_stale_preview(tmp_path):
    db = _source(tmp_path)
    inbox = tmp_path / "cmd"
    inbox.mkdir()
    bundle = semantic.thread_bundle(db, 1, 1)
    policy = "a" * 64
    db.artifact_add("semantic_policy", policy)
    member = next(item for item in bundle["members"]
                   if item["message_id"] == 1)
    body = member["body_original"]
    evidence = {
        "evidence_id": "e1", "message_id": 1,
        "revision_id": member["revision"], "start_codepoint": 0,
        "end_codepoint": len(body), "quote": body,
    }
    candidate = {
        "loop_id": "loop_synthetic",
        "account_scope": "mcs",
        "project_id": 1,
        "root_id": 1,
        "kind": "pending_item",
        "description": "確認依頼が未回答",
        "drug_ref": None,
        "origin": {
            "message_id": 1, "revision": member["revision"],
            "evidence_refs": ["e1"], "evidence": evidence,
        },
    }
    artifact_id = db.artifact_add(
        "loop_candidate", json.dumps(candidate, ensure_ascii=False),
        project_id=1, message_id=1, model="jev",
        meta={"fingerprint": bundle["source_fingerprint"],
              "policy_fingerprint": policy})
    missing_evidence = {
        **candidate,
        "origin": {"message_id": 1, "revision": member["revision"]},
    }
    missing_evidence_id = db.artifact_add(
        "loop_candidate", json.dumps(missing_evidence, ensure_ascii=False),
        project_id=1, message_id=1, model="jev",
        meta={"fingerprint": bundle["source_fingerprint"],
              "policy_fingerprint": policy})
    assert artifact_id > 0
    assert missing_evidence_id > artifact_id
    assert ledger.publish_snapshot(str(tmp_path / "source.db"),
                                   str(tmp_path / "snapshots"))
    ctx = _Context(_settings(tmp_path))
    hermes_plugin.register(ctx)
    handler = ctx.command[1]
    context = _context()

    missing_preview = _call(handler, {
        "op": "request", "phase": "preview", "action": "create",
        "project_id": 1, "source_message_id": 1,
        "title": "loop-linked request", "reason": "human checked loop",
        "loop_artifact_id": missing_evidence_id,
        "loop_match_confirmed": True,
    }, context)
    assert missing_preview["ok"] is False

    def preview():
        return _call(handler, {
            "op": "request", "phase": "preview", "action": "create",
            "project_id": 1, "source_message_id": 1,
            "title": "loop-linked request", "reason": "human checked loop",
            "loop_artifact_id": artifact_id,
            "loop_match_confirmed": True,
        }, context)

    def confirm(value):
        return _call(handler, {
            "op": "request", "phase": "confirm",
            "payload": value["payload"],
            "payload_hash": value["payload_hash"],
            "origin": value["origin"],
        }, context)

    try:
        loop_preview = preview()
        assert loop_preview["ok"] is True
        loop_ref = loop_preview["payload"]["loop_ref"]
        assert set(loop_ref) == {
            "artifact_id", "source_fingerprint", "policy_fingerprint",
            "match_confirmed",
        }
        assert loop_ref["artifact_id"] == artifact_id
        assert loop_ref["match_confirmed"] is True
        assert "candidate" not in loop_ref
        assert loop_preview["loop_candidate"] == candidate
        assert loop_preview["payload_hash"] == mcs_requests.payload_hash({
            "payload": loop_preview["payload"],
            "origin": loop_preview["origin"],
        })

        db.artifact_add("semantic_policy", "b" * 64)
        assert ledger.publish_snapshot(str(tmp_path / "source.db"),
                                       str(tmp_path / "snapshots"))
        assert confirm(loop_preview) == {
            "ok": False, "error": "loop_policy_stale",
        }
        assert not list(inbox.iterdir())

        db.artifact_add("semantic_policy", policy)
        assert ledger.publish_snapshot(str(tmp_path / "source.db"),
                                       str(tmp_path / "snapshots"))
        accepted = confirm(loop_preview)
        assert accepted["ok"] is True
        assert accepted["receipt"]["outcome"] == "queued"
        job_ops.drain_commands(db, {"errors": []}, str(inbox))
        receipt = db.db.execute(
            "SELECT receipt_json FROM command_receipts WHERE command_id=?",
            (loop_preview["payload"]["command_id"],)).fetchone()
        assert json.loads(receipt["receipt_json"])["loop_ref"] == loop_ref
        link = db.db.execute(
            "SELECT content FROM artifacts WHERE kind='request_loop_link'"
        ).fetchone()
        assert json.loads(link["content"])["loop_artifact_id"] == artifact_id

        assert ledger.publish_snapshot(str(tmp_path / "source.db"),
                                       str(tmp_path / "snapshots"))
        request = db.db.execute(
            "SELECT request_id FROM requests WHERE project_id=1"
        ).fetchone()
        update = _call(handler, {
            "op": "request", "phase": "preview", "action": "update",
            "project_id": 1, "request_id": request["request_id"],
            "patch": {"status": "done"}, "reason": "human checked update",
            "loop_artifact_id": artifact_id,
            "loop_match_confirmed": True,
        }, context)
        assert update["ok"] is True
        assert update["payload"]["loop_ref"] == loop_ref
        assert update["loop_candidate"] == candidate
        assert confirm(update)["ok"] is True
        job_ops.drain_commands(db, {"errors": []}, str(inbox))
        assert db.db.execute(
            "SELECT status FROM requests WHERE request_id=?",
            (request["request_id"],)).fetchone()["status"] == "done"
        assert db.db.execute(
            "SELECT count(*) FROM artifacts WHERE kind='request_loop_link'"
        ).fetchone()[0] == 2
    finally:
        db.close()


def test_summary_comparison_adoption_preview_confirm_and_stale_guard(tmp_path):
    db = _source(tmp_path)
    with db.db:
        db.db.execute("UPDATE messages SET reply_count=1 WHERE message_id=1")
    inbox = tmp_path / "cmd"
    inbox.mkdir()
    source_hash = db.db.execute(
        "SELECT content_hash FROM messages WHERE project_id=1 AND message_id=1"
    ).fetchone()[0]
    policy = "c" * 64
    db.artifact_add("semantic_policy", policy)
    bundle = semantic.thread_bundle(db, 1, 1)
    member = next(item for item in bundle["members"]
                   if item["message_id"] == 1)
    baseline = {"summary": "抽出された基準", "points": ["確認依頼"]}
    db.artifact_add(
        "extract_llm", json.dumps(baseline, ensure_ascii=False),
        project_id=1, message_id=1, model="local",
        meta={"hash": source_hash})
    candidate = {
        "target_message_id": 1,
        "claims": [{"section": "plan", "text": "確認依頼を採用"}],
    }
    candidate_id = db.artifact_add(
        "semantic_summary", json.dumps(candidate, ensure_ascii=False),
        project_id=1, message_id=1, model="local",
        meta={"fingerprint": bundle["source_fingerprint"],
              "policy_fingerprint": policy,
              "target_revision": member["revision"],
              "audit_status": "PASS", "publication_mode": "assist"})
    assert candidate_id > 0
    assert ledger.publish_snapshot(str(tmp_path / "source.db"),
                                   str(tmp_path / "snapshots"))
    ctx = _Context(_settings(tmp_path))
    hermes_plugin.register(ctx)
    handler = ctx.command[1]
    context = _context()

    def confirm(preview):
        return _call(handler, {
            "op": "control", "phase": "confirm",
            "payload": preview["payload"],
            "payload_hash": preview["payload_hash"],
            "origin": preview["origin"],
        }, context)

    try:
        compared = _call(handler, {
            "op": "read", "kind": "comparison", "project_id": 1,
            "message_id": 1,
        }, context)
        assert compared["ok"] is True
        assert compared["result"]["adoptable"] is True
        assert compared["result"]["candidate"]["artifact_id"] \
            == candidate_id

        preview = _call(handler, {
            "op": "control", "phase": "preview",
            "action": "adopt_summary", "project_id": 1,
            "message_id": 1, "reason": "人が比較結果を確認して採用",
        }, context)
        assert preview["ok"] is True
        assert preview["comparison"]["adoptable"] is True
        payload = preview["payload"]
        assert payload["cmd"] == "ops.adopt_summary"
        assert payload["message_id"] == 1
        assert payload["summary_artifact_id"] == candidate_id
        assert payload["comparison_hash"] == preview["comparison"]["comparison_hash"]
        assert payload["reason"] == "人が比較結果を確認して採用"
        assert preview["queued"] is False
        assert preview["payload_hash"] == mcs_requests.payload_hash({
            "payload": payload, "origin": preview["origin"]})

        changed = {
            "target_message_id": 1,
            "claims": [{"section": "plan", "text": "変更された候補"}],
        }
        with db.db:
            db.db.execute(
                "UPDATE artifacts SET content=? WHERE artifact_id=?",
                (json.dumps(changed, ensure_ascii=False), candidate_id))
        assert ledger.publish_snapshot(
            str(tmp_path / "source.db"), str(tmp_path / "snapshots"))
        assert confirm(preview) == {
            "ok": False, "error": "comparison_changed",
        }
        assert not list(inbox.iterdir())

        with db.db:
            db.db.execute(
                "UPDATE artifacts SET content=? WHERE artifact_id=?",
                (json.dumps(candidate, ensure_ascii=False), candidate_id))
        assert ledger.publish_snapshot(
            str(tmp_path / "source.db"), str(tmp_path / "snapshots"))
        fresh = _call(handler, {
            "op": "control", "phase": "preview",
            "action": "adopt_summary", "project_id": 1,
            "message_id": 1, "summary_artifact_id": candidate_id,
            "reason": "人が比較結果を確認して採用",
        }, context)
        assert fresh["ok"] is True
        accepted = confirm(fresh)
        assert accepted["ok"] is True
        assert accepted["receipt"]["outcome"] == "queued"
        job_ops.drain_commands(db, {"errors": []}, str(inbox))
        adoption = db.db.execute(
            "SELECT message_id,content FROM artifacts "
            "WHERE kind='semantic_adoption'"
        ).fetchone()
        assert adoption is not None
        adoption_content = json.loads(adoption["content"])
        assert adoption["message_id"] == 1
        assert adoption_content["summary_artifact_id"] == candidate_id
        assert adoption_content["comparison_hash"] == \
            fresh["payload"]["comparison_hash"]
    finally:
        db.close()


def test_control_preview_confirm_and_operations_read(tmp_path):
    db = _source(tmp_path)
    inbox = tmp_path / "cmd"
    inbox.mkdir()
    payload = {"generation": "g1", "source_generation": "s1",
               "targets": [1]}
    job_id = db.job_add("semantic", 1, 1, payload=payload)
    db.job_fail(job_id)
    snapshot = ledger.publish_snapshot(str(tmp_path / "source.db"),
                                       str(tmp_path / "snapshots"))
    assert snapshot
    ctx = _Context(_settings(tmp_path))
    hermes_plugin.register(ctx)
    handler = ctx.command[1]
    context = _context()

    def confirm(preview):
        return _call(handler, {
            "op": "control", "phase": "confirm",
            "payload": preview["payload"],
            "payload_hash": preview["payload_hash"],
            "origin": preview["origin"],
        }, context)

    try:
        operations = _call(handler, {
            "op": "read", "kind": "operations", "project_id": 1,
        }, context)
        assert operations["ok"] is True
        row = next(item for item in operations["result"]["items"]
                   if item["job_id"] == job_id)
        expected_hash = mcs_requests.payload_hash(payload)
        assert row["payload_hash"] == expected_hash
        assert "generation" not in json.dumps(operations["result"]["items"])

        bad_scan = _call(handler, {
            "op": "control", "phase": "preview", "action": "scan",
            "project_id": 1, "days": 366, "pages": 1,
        }, context)
        assert bad_scan == {"ok": False, "error": "bad_scan_range"}
        scan = _call(handler, {
            "op": "control", "phase": "preview", "action": "scan",
            "project_id": 1, "days": 7, "pages": 2,
        }, context)
        assert scan["ok"] is True
        assert scan["payload"]["cmd"] == "ops.scan"
        assert scan["payload"]["days"] == 7
        assert scan["payload"]["pages"] == 2
        assert confirm(scan)["receipt"]["outcome"] == "queued"
        job_ops.drain_commands(db, {"errors": []}, str(inbox))
        history = db.db.execute(
            "SELECT state FROM fetch_jobs WHERE kind='history' "
            "AND project_id=1").fetchone()
        assert history["state"] == "pending"

        retry = _call(handler, {
            "op": "control", "phase": "preview", "action": "retry",
            "project_id": 1, "job_id": job_id,
            "expected_payload_hash": expected_hash,
        }, context)
        assert retry["ok"] is True
        assert retry["payload"]["cmd"] == "ops.retry"
        assert retry["payload"]["expected_payload_hash"] == expected_hash
        changed_payload = {**payload, "generation": "g2"}
        with db.db:
            db.db.execute(
                "UPDATE fetch_jobs SET payload=? WHERE job_id=?",
                (mcs_requests.canonical(changed_payload).decode(), job_id))
        assert ledger.publish_snapshot(
            str(tmp_path / "source.db"), str(tmp_path / "snapshots"))
        stale_retry = confirm(retry)
        assert stale_retry == {"ok": False, "error": "payload_changed"}
        with db.db:
            db.db.execute(
                "UPDATE fetch_jobs SET payload=? WHERE job_id=?",
                (mcs_requests.canonical(payload).decode(), job_id))
        assert ledger.publish_snapshot(
            str(tmp_path / "source.db"), str(tmp_path / "snapshots"))
        assert confirm(retry)["receipt"]["outcome"] == "queued"
        job_ops.drain_commands(db, {"errors": []}, str(inbox))
        semantic = db.db.execute(
            "SELECT state,attempts,next_try FROM fetch_jobs WHERE job_id=?",
            (job_id,)).fetchone()
        assert tuple(semantic) == ("pending", 0, 0)

        with db.db:
            db.db.execute("UPDATE fetch_jobs SET state='failed',attempts=6 WHERE job_id=?", (job_id,))
        ledger.publish_snapshot(str(tmp_path / "source.db"), str(tmp_path / "snapshots"))
        retry_input = {"op": "control", "phase": "preview", "action": "retry",
                       "project_id": 1, "job_id": job_id}
        assert _call(handler, retry_input, context)["error"] == "attempt_limit"
        extension = _call(handler, {**retry_input, "additional_attempts": 2,
                                   "reason": "Human verified the transient failure is resolved"}, context)
        assert extension["ok"]
        assert extension["payload"]["additional_attempts"] == 2
        assert extension["retry_budget"] == {"attempts": 6, "old_limit": 6, "new_limit": 8}
        for _ in range(2):
            assert confirm(extension)["ok"]
            job_ops.drain_commands(db, {"errors": []}, str(inbox))
        retried = db.db.execute("SELECT attempts,payload FROM fetch_jobs WHERE job_id=?", (job_id,)).fetchone()
        assert retried["attempts"] == 6
        assert json.loads(retried["payload"])["manual_attempt_limit"] == 8

        pause = _call(handler, {
            "op": "control", "phase": "preview", "action": "pause",
            "project_id": 1, "feature": "semantic",
        }, context)
        assert pause["ok"] is True
        assert confirm(pause)["receipt"]["outcome"] == "queued"
        job_ops.drain_commands(db, {"errors": []}, str(inbox))
        assert db.db.execute(
            "SELECT content FROM artifacts WHERE kind='semantic_control' "
            "ORDER BY artifact_id DESC LIMIT 1").fetchone()[0] == "paused"

        resume = _call(handler, {
            "op": "control", "phase": "preview", "action": "resume",
            "project_id": 1, "feature": "semantic",
        }, context)
        assert resume["ok"] is True
        assert confirm(resume)["receipt"]["outcome"] == "queued"
        job_ops.drain_commands(db, {"errors": []}, str(inbox))
        assert db.db.execute(
            "SELECT content FROM artifacts WHERE kind='semantic_control' "
            "ORDER BY artifact_id DESC LIMIT 1").fetchone()[0] == "running"
    finally:
        db.close()


def test_interactive_settings_derives_application_id(tmp_path):
    """application_id omitted: the connected bot's user id fills the
    scope binding — a bot account's user id IS its application id."""
    from types import SimpleNamespace

    settings = _settings(tmp_path)
    settings.update({
        "interactive": True,
        "data_root": str(tmp_path / "data"),
        "channel_id": "42",
    })
    ctx = _Context(settings)
    bot = SimpleNamespace(user=SimpleNamespace(id=123456789))
    resolved = hermes_plugin._interactive_settings(ctx, bot)
    assert resolved["application_id"] == "123456789"
    assert resolved["channel_id"] == "42"

    settings["application_id"] = "999"
    resolved = hermes_plugin._interactive_settings(ctx, bot)
    assert resolved["application_id"] == "999"

    settings.pop("application_id")
    assert hermes_plugin._interactive_settings(
        ctx, SimpleNamespace(user=None)) is None
    settings["interactive"] = False
    assert hermes_plugin._interactive_settings(ctx, bot) is None
