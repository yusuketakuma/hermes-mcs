"""Fully synthetic alias approval, crash recovery and export isolation."""
from copy import deepcopy
import json
from pathlib import Path
import uuid

import pytest

import drug_alias
import safe_io
import drug_map
import ledger
import mcs_requests
from test_drug_map import DOCUMENT, _seed


@pytest.fixture
def corpus(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    store = ledger.Ledger(str(data / "ledger.db"))
    store.ensure_patient(1)
    _seed(store, names=("キラナン",))
    path = tmp_path / "before.json"
    raw = mcs_requests.canonical(DOCUMENT)
    path.write_bytes(raw)
    path.chmod(0o600)
    import hashlib
    cfg = {"drug_map": {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest()},
           "unrelated": "preserved"}
    config = tmp_path / "config.json"
    config.write_text(json.dumps(cfg))
    config.chmod(0o600)
    yield store, cfg, config
    store.close()


def request(proposal):
    return {"version": 1, "cmd": "ops.drug_alias_apply", "command_id": str(uuid.uuid4()),
            "actor": "local-administrator", "human_confirmed": True,
            "reason": "dictionary_alias_reviewed", "proposal": proposal}


def rollback(approval):
    return {"version": 1, "cmd": "ops.drug_alias_rollback", "command_id": str(uuid.uuid4()),
            "actor": "local-administrator", "human_confirmed": True,
            "reason": "dictionary_alias_rollback", "approval_id": approval}


def test_approval_receipt_replay_and_pinned_rollback(corpus):
    store, cfg, config = corpus
    proposed = drug_alias.proposal(store.db, cfg, "キラナン", "fiction-i1")
    assert proposed["impact_counts"]["mentions_changed"] == 1
    assert not any(k in json.dumps(proposed) for k in ("message_id", "body_text", "fictional source"))
    req = request(proposed)
    before_jobs = list(store.db.execute("SELECT * FROM fetch_jobs"))
    receipt = mcs_requests.apply_command(store, req)
    assert receipt["outcome"] == "applied", receipt
    assert mcs_requests.apply_command(store, req) == receipt
    after = json.loads(config.read_text())
    assert after["unrelated"] == "preserved"
    dictionary, error = drug_map.configured(after)
    assert not error and dictionary.resolve("キラナン")["cands"][0]["code"] == "fiction-i1"
    assert list(store.db.execute("SELECT * FROM fetch_jobs")) == before_jobs
    assert mcs_requests.apply_command(store, request(proposed))["outcome"] == "rejected"
    result = mcs_requests.apply_command(store, rollback(req["command_id"]))
    assert result["outcome"] == "applied", result
    assert json.loads(config.read_text())["drug_map"] == cfg["drug_map"]
    assert mcs_requests.apply_command(store, rollback(req["command_id"]))["outcome"] == "rejected"


@pytest.mark.parametrize("alias", ["田中太郎", "患者123456", "キラナ123456", "キラナ\n", "ID-123", "カ" * 49, "山田キラナ"])
def test_alias_rejects_person_and_identifier_like_input(corpus, alias):
    store, cfg, _ = corpus
    with pytest.raises(ValueError):
        drug_alias.proposal(store.db, cfg, alias, "fiction-i1")


def test_stale_snapshot_collision_and_human_boundary(corpus):
    store, cfg, config = corpus
    with pytest.raises(ValueError):
        drug_alias.proposal(store.db, cfg, "キラナ", "fiction-i2")
    proposed = drug_alias.proposal(store.db, cfg, "キラナン", "fiction-i1")
    req = request(proposed)
    req["human_confirmed"] = False
    assert mcs_requests.apply_command(store, req)["outcome"] == "rejected"
    _seed(store, mid=2)
    receipt = mcs_requests.apply_command(store, request(proposed))
    assert receipt["error"] == "alias_stale_proposal"
    assert json.loads(config.read_text())["drug_map"] == cfg["drug_map"]
    tampered = deepcopy(proposed)
    tampered["target_entry"] = "fiction-i2"
    assert mcs_requests.validate(request(tampered)) == "bad_alias_proposal"


def test_failed_commit_compensates_config_and_preserves_old_dictionary(corpus):
    store, cfg, config = corpus
    proposed = drug_alias.proposal(store.db, cfg, "キラナン", "fiction-i1")
    store.db.execute("CREATE TRIGGER reject_receipt BEFORE INSERT ON command_receipts BEGIN SELECT RAISE(ABORT,'synthetic failure'); END")
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        mcs_requests.apply_command(store, request(proposed))
    assert json.loads(config.read_text())["drug_map"] == cfg["drug_map"]
    assert Path(cfg["drug_map"]["path"]).exists()
    assert not (config.parent / "data/drug-map/alias-switch.pending.json").exists()


def test_crash_journal_blocks_retry_and_allows_explicit_rollback(corpus):
    store, cfg, config = corpus
    proposed = drug_alias.proposal(store.db, cfg, "キラナン", "fiction-i1")
    req = request(proposed)
    receipt = mcs_requests.apply_command(store, req)
    journal = config.parent / "data/drug-map/alias-switch.pending.json"
    safe_io.publish_new_bytes(journal, mcs_requests.canonical(receipt))
    fresh = drug_alias.proposal(store.db, json.loads(config.read_text()), "キラノーン", "fiction-i1")
    assert mcs_requests.apply_command(store, request(fresh))["error"] == "alias_recovery_required"
    assert mcs_requests.apply_command(store, rollback(req["command_id"]))["outcome"] == "applied"
    assert json.loads(config.read_text())["drug_map"] == cfg["drug_map"]
    assert not journal.exists()


@pytest.mark.parametrize("kind", ["drug_timeline", "drug_alias_proposal", "drug_alias_receipt"])
def test_new_drug_records_are_not_exportable(kind):
    from export_schema import project_record
    with pytest.raises(ValueError):
        project_record({"type": kind, "alias": "キラナン", "items": []})


def test_rollback_does_not_require_the_broken_new_dictionary(corpus):
    store, cfg, config = corpus
    req = request(drug_alias.proposal(store.db, cfg, "キラナン", "fiction-i1"))
    receipt = mcs_requests.apply_command(store, req)
    Path(receipt["after_pin"]["path"]).write_text("broken synthetic dictionary")
    assert mcs_requests.apply_command(store, rollback(req["command_id"]))["outcome"] == "applied"
    assert json.loads(config.read_text())["drug_map"] == cfg["drug_map"]


def test_alias_preview_counts_stem_changes_and_apply_matches(corpus):
    store, cfg, config = corpus
    _seed(store, mid=2, names=("キラナン錠5mg", "キラナン錠10mg", "別キラナ"))
    proposed = drug_alias.proposal(store.db, cfg, "キラナン", "fiction-i1")
    assert proposed["impact_counts"]["mentions_changed"] == 3
    before, _ = drug_map.configured(cfg)
    assert before.resolve("キラナン錠5mg")["status"] == "unresolved"
    assert mcs_requests.apply_command(store, request(proposed))["outcome"] == "applied"
    after, _ = drug_map.configured(json.loads(config.read_text()))
    assert after.resolve("キラナン錠5mg")["cands"][0]["code"] == "fiction-i1"
    assert after.resolve("別キラナ") == before.resolve("別キラナ")


@pytest.mark.parametrize("failure", ["partial_write", "file_sync", "published_sync"])
def test_journal_publication_failure_is_retryable_or_rollbackable(corpus, monkeypatch, failure):
    import errno
    import os
    import stat
    store, cfg, config = corpus
    proposed = drug_alias.proposal(store.db, cfg, "キラナン", "fiction-i1")
    req = request(proposed)
    folder = config.parent / "data/drug-map"
    folder.mkdir(mode=0o700)
    # Finish the candidate file so the injected failure concerns only the journal.
    candidate = drug_alias._candidate_payload(cfg["drug_map"], proposed["alias"], proposed["target_entry"])
    safe_io.publish_new_bytes(folder / ("alias-" + proposed["after_dict_sha"] + ".json"), candidate)
    journal = folder / "alias-switch.pending.json"
    fdopen, sync = os.fdopen, os.fsync

    class PartialWriter:
        def __init__(self, stream):
            self.stream = stream
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return self.stream.__exit__(*args)
        def write(self, raw):
            self.stream.write(raw[:7])
            self.stream.flush()
            raise OSError(errno.ENOSPC, "synthetic partial journal")

    def fail_sync(fd):
        directory = stat.S_ISDIR(os.fstat(fd).st_mode)
        if (failure == "file_sync" and not directory) or (failure == "published_sync" and directory):
            raise OSError("synthetic sync failure")
        return sync(fd)

    with monkeypatch.context() as patch:
        if failure == "partial_write":
            patch.setattr(os, "fdopen", lambda fd, mode, *a, **k:
                PartialWriter(fdopen(fd, mode, *a, **k)) if mode == "wb" else fdopen(fd, mode, *a, **k))
        else:
            patch.setattr(os, "fsync", fail_sync)
        assert mcs_requests.apply_command(store, req)["error"] == "alias_switch_failed"
    assert json.loads(config.read_text()) == cfg
    assert not list(folder.glob(".alias-*.tmp"))
    if failure == "published_sync":
        assert drug_alias.private_json(journal)[0]["approval_id"] == req["command_id"]
        assert mcs_requests.apply_command(store, request(proposed))["error"] == "alias_recovery_required"
        assert mcs_requests.apply_command(store, rollback(req["command_id"]))["outcome"] == "applied"
    else:
        assert not journal.exists()
        retry = request(proposed)
        assert mcs_requests.apply_command(store, retry)["outcome"] == "applied"
        assert mcs_requests.apply_command(store, rollback(retry["command_id"]))["outcome"] == "applied"
    assert not journal.exists() and json.loads(config.read_text()) == cfg


def test_new_file_publication_never_overwrites_existing_destination(tmp_path):
    path = tmp_path / "synthetic.json"
    safe_io.publish_new_bytes(path, b'{"original":true}')
    with pytest.raises(FileExistsError):
        safe_io.publish_new_bytes(path, b'{"replacement":true}')
    assert path.read_bytes() == b'{"original":true}'
    assert not list(tmp_path.glob(".alias-*.tmp"))


def test_process_exit_during_staging_does_not_publish_torn_journal(corpus):
    import subprocess
    import sys
    store, cfg, config = corpus
    proposed = drug_alias.proposal(store.db, cfg, "キラナン", "fiction-i1")
    folder = config.parent / "data/drug-map"
    folder.mkdir(mode=0o700)
    journal = folder / "alias-switch.pending.json"
    code = '''
import os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import _mcs_path
import drug_alias
import safe_io
original = os.fdopen
class Interrupted:
    def __init__(self, stream): self.stream = stream
    def __enter__(self): return self
    def __exit__(self, *args): return self.stream.__exit__(*args)
    def write(self, raw):
        self.stream.write(raw[:7])
        self.stream.flush()
        os._exit(73)
os.fdopen = lambda fd, mode, *args, **kwargs: Interrupted(original(fd, mode, *args, **kwargs))
safe_io.publish_new_bytes(Path(sys.argv[2]), b'{"approval_id":"synthetic"}')
'''
    result = subprocess.run([sys.executable, "-B", "-c", code,
        str(Path(drug_alias.__file__).parents[1]), str(journal)],
        cwd=config.parent, capture_output=True, timeout=10)
    assert result.returncode == 73, result.stderr
    assert not journal.exists()
    staging = list(folder.glob(".alias-*.tmp"))
    assert len(staging) == 1 and staging[0].stat().st_mode & 0o077 == 0
    req = request(proposed)
    assert mcs_requests.apply_command(store, req)["outcome"] == "applied"
    assert mcs_requests.apply_command(store, rollback(req["command_id"]))["outcome"] == "applied"
    assert json.loads(config.read_text()) == cfg
