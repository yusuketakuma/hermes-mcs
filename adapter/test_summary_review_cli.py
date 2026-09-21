"""Assist comparison and adoption use the snapshot CLI and existing inbox."""
import io
import json
import uuid

import job_ops
import ledger
import mcs_view
import semantic
from test_mcs_semantic import _cfg, _seeded


def test_comparison_cli_and_confirmed_adoption(tmp_path, monkeypatch, capsys):
    db = _seeded(tmp_path)
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    try:
        config, _ = semantic.semantic_config(_cfg("assist"))
        policy = semantic.policy_fingerprint(config)
        db.artifact_add("semantic_policy", policy)
        bundle = semantic.thread_bundle(db, 1, 1, [1])
        member = next(item for item in bundle["members"] if item["message_id"] == 1)
        db.artifact_add("extract_llm", json.dumps({"summary": "Old summary", "points": []}),
                        project_id=1, message_id=1, meta={"hash": member["revision"]})
        with db.db:
            semantic._write_result(db, 1, 1, {
                "summary": {"target_message_id": 1, "claims": [{"section": "medication", "text": member["body_original"],
                            "claim_kind": "reported_fact", "fact_refs": [0],
                            "status": "planned", "polarity": "affirmed"}], "limitations": []},
                "findings": [], "repaired": False}, bundle["source_fingerprint"],
                {1: member}, "PASS", policy, "assist")
        snapshot = ledger.publish_snapshot(str(tmp_path / "ledger.db"), str(tmp_path / "snap"))
        args = ["--snapshot", str(snapshot), "--cmd-dir", str(inbox)]
        assert mcs_view.main(args + ["comparison", "--project", "1", "--message-id", "1"]) == 0
        comparison = json.loads(capsys.readouterr().out)
        assert comparison["adoptable"] and comparison["diff"]
        assert not list(inbox.iterdir())
        command = {"command_id": str(uuid.uuid4()), "actor": "synthetic human",
                   "message_id": 1, "summary_artifact_id": comparison["candidate"]["artifact_id"],
                   "comparison_hash": comparison["comparison_hash"], "reason": "Compared source and summaries"}
        monkeypatch.setattr(mcs_view.sys, "stdin", io.TextIOWrapper(io.BytesIO(json.dumps(command).encode())))
        assert mcs_view.main(args + ["control", "adopt_summary", "--project", "1", "--confirm-human"]) == 0
        assert json.loads(capsys.readouterr().out)["outcome"] == "queued"
        job_ops.drain_commands(db, {"errors": []}, str(inbox))
        ledger.publish_snapshot(str(tmp_path / "ledger.db"), str(tmp_path / "snap"))
        assert mcs_view.main(args + ["comparison", "--project", "1", "--message-id", "1"]) == 0
        assert json.loads(capsys.readouterr().out)["candidate"]["adopted"]
    finally:
        db.close()
