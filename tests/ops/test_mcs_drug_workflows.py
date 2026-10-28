"""Real local CLI wiring with only synthetic published snapshots and command inboxes."""
import json
from pathlib import Path
import uuid

import pytest

import brain_export
import ledger
import mcs_drug
import mcs_requests
from safe_io import publish_new_bytes
from test_drug_alias import corpus as corpus
from test_drug_timeline import current


def test_cli_propose_enqueue_consume_receipt_and_timeline(corpus, tmp_path, monkeypatch, capsys):
    store, cfg, _ = corpus
    current(store, 1, [{"name": "キラナン", "subject": "unknown", "status": "planned"}])
    snapshot = ledger.publish_snapshot(str(tmp_path / "data/ledger.db"), str(tmp_path / "snapshots"))
    monkeypatch.setattr(mcs_drug, "load_config", lambda: cfg)
    assert mcs_drug.main(["timeline", "--project", "1", "--snapshot", snapshot, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["items"][0]["mention"]["status"] == "planned"
    proposed = tmp_path / "proposal.json"
    assert mcs_drug.main(["alias-propose", "キラナン", "fiction-i1", "--out", str(proposed),
                          "--snapshot", snapshot]) == 0
    assert proposed.stat().st_mode & 0o077 == 0
    capsys.readouterr()
    inbox = tmp_path / "data/cmd"
    inbox.mkdir()
    command_id = str(uuid.uuid4())
    args = ["alias-apply", "--proposal", str(proposed), "--confirm-human", "--actor", "local-admin",
            "--reason", "dictionary_alias_reviewed", "--command-id", command_id,
            "--snapshot", snapshot, "--cmd-dir", str(inbox)]
    assert mcs_drug.main(args) == 0
    assert json.loads(capsys.readouterr().out)["outcome"] == "queued"
    req = json.loads(next(inbox.glob("*.json")).read_text())
    assert mcs_requests.apply_command(store, req)["outcome"] == "applied"
    snapshot = ledger.publish_snapshot(str(tmp_path / "data/ledger.db"), str(tmp_path / "snapshots"))
    assert mcs_drug.main(["alias-receipt", command_id, "--snapshot", snapshot]) == 0
    assert json.loads(capsys.readouterr().out)["outcome"] == "applied"


def test_brain_export_does_not_read_alias_or_timeline_artifacts(corpus, tmp_path):
    import rollup
    store, _, _ = corpus
    for kind in ("drug_timeline", "drug_alias_proposal", "drug_alias_receipt"):
        store.artifact_add(kind, json.dumps({"alias": "EXPORT_FORBIDDEN_CANARY"}), project_id=1)
    assert rollup.rebuild(store, 1)
    snapshot = ledger.publish_snapshot(str(tmp_path / "data/ledger.db"), str(tmp_path / "snapshots"))
    out = tmp_path / "export"
    assert brain_export.run(out, Path(snapshot))["ok"]
    assert not any("EXPORT_FORBIDDEN_CANARY" in p.read_text() for p in out.rglob("*") if p.is_file())


@pytest.mark.parametrize("snapshot_state", ["missing", "corrupt"])
def test_alias_apply_and_rollback_do_not_require_snapshot(corpus, tmp_path, capsys, snapshot_state):
    import drug_alias
    store, cfg, config = corpus
    snapshot = tmp_path / "unavailable-snapshot.db"
    if snapshot_state == "corrupt":
        snapshot.write_bytes(b"synthetic invalid snapshot")
    proposed = tmp_path / "proposal.json"
    publish_new_bytes(proposed, mcs_requests.canonical(
        drug_alias.proposal(store.db, cfg, "キラナン", "fiction-i1")))
    inbox = tmp_path / "data/cmd"
    inbox.mkdir()
    approval_id = str(uuid.uuid4())
    for command, fields, command_id, reason in (
            ("alias-apply", ["--proposal", str(proposed)], approval_id, "dictionary_alias_reviewed"),
            ("alias-rollback", ["--approval-id", approval_id], str(uuid.uuid4()), "dictionary_alias_rollback")):
        args = [command, *fields, "--actor", "local-admin", "--reason", reason,
                "--command-id", command_id, "--snapshot", str(snapshot), "--cmd-dir", str(inbox)]
        with pytest.raises(SystemExit) as refused:
            mcs_drug.main(args)
        assert refused.value.code == 2
        assert not list(inbox.glob("*.json"))
        bad_reason = args.copy()
        bad_reason[bad_reason.index("--reason") + 1] = "unreviewed"
        with pytest.raises(SystemExit):
            mcs_drug.main([*bad_reason, "--confirm-human"])
        assert not list(inbox.glob("*.json"))
        capsys.readouterr()
        assert mcs_drug.main([*args, "--confirm-human"]) == 0
        assert json.loads(capsys.readouterr().out)["outcome"] == "queued"
        path = next(inbox.glob("*.json"))
        req = json.loads(path.read_text())
        assert req["human_confirmed"] is True and req["reason"] == reason
        assert req["command_id"] == command_id and req["actor"] == "local-admin"
        receipt = mcs_requests.apply_command(store, req)
        assert receipt["outcome"] == "applied"
        assert mcs_requests.apply_command(store, req) == receipt
        path.unlink()  # synthetic inbox consumption; receipt remains durable
    assert json.loads(config.read_text()) == cfg
