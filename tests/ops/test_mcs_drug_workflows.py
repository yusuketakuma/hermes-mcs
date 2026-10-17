"""Real local CLI wiring with only synthetic published snapshots and command inboxes."""
import json
from pathlib import Path
import uuid

import brain_export
import ledger
import mcs_drug
import mcs_requests
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
    store, _, _ = corpus
    for kind in ("drug_timeline", "drug_alias_proposal", "drug_alias_receipt"):
        store.artifact_add(kind, json.dumps({"alias": "EXPORT_FORBIDDEN_CANARY"}), project_id=1)
    snapshot = ledger.publish_snapshot(str(tmp_path / "data/ledger.db"), str(tmp_path / "snapshots"))
    out = tmp_path / "export"
    assert brain_export.run(out, Path(snapshot))["ok"]
    assert not any("EXPORT_FORBIDDEN_CANARY" in p.read_text() for p in out.rglob("*") if p.is_file())
