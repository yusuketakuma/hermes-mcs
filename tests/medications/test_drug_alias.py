"""Fully synthetic alias approval, crash recovery and export isolation."""
from copy import deepcopy
import json
from pathlib import Path
import uuid

import pytest

import config_edit
import drug_alias
import safe_io
import drug_map
import ledger
import mcs_requests
from medications_testkit import DOCUMENT, _seed


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


def test_shared_config_lock_conflict_records_rejection_and_allows_new_command(corpus):
    store, cfg, config = corpus
    proposed = drug_alias.proposal(store.db, cfg, "キラナン", "fiction-i1")
    req = request(proposed)
    with config_edit._edit_lock(config):
        receipt = mcs_requests.apply_command(store, req)
    assert receipt['error'] == 'alias_switch_failed'
    assert json.loads(config.read_text()) == cfg
    assert not (config.parent / 'data/drug-map/alias-switch.pending.json').exists()
    assert mcs_requests.apply_command(store, req) == receipt
    assert mcs_requests.apply_command(store, request(proposed))['outcome'] == 'applied'


def test_shared_lock_remains_held_while_receipt_is_inserted(corpus):
    store, cfg, config = corpus
    proposed = drug_alias.proposal(store.db, cfg, "キラナン", "fiction-i1")
    blocked = []
    def concurrent_writer():
        try:
            with config_edit._edit_lock(config):
                blocked.append(False)
        except config_edit.ConfigConflict:
            blocked.append(True)
        return 0
    store.db.create_function('synthetic_config_editor', 0, concurrent_writer)
    store.db.execute('CREATE TRIGGER observe_config_lock BEFORE INSERT ON command_receipts '
                     'BEGIN SELECT synthetic_config_editor(); END')
    assert mcs_requests.apply_command(store, request(proposed))['outcome'] == 'applied'
    assert blocked == [True]
    with config_edit._edit_lock(config):
        assert json.loads(config.read_text())['unrelated'] == 'preserved'


def test_failed_receipt_keeps_shared_lock_until_compensation(corpus):
    import sqlite3
    store, cfg, config = corpus
    proposed = drug_alias.proposal(store.db, cfg, "キラナン", "fiction-i1")
    blocked = []
    def competing_editor():
        try:
            with config_edit._edit_lock(config):
                blocked.append(False)
        except config_edit.ConfigConflict:
            blocked.append(True)
        return 0
    store.db.create_function('synthetic_config_editor', 0, competing_editor)
    store.db.execute('CREATE TRIGGER reject_with_editor BEFORE INSERT ON command_receipts '
                     "BEGIN SELECT synthetic_config_editor(); SELECT RAISE(ABORT,'synthetic failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        mcs_requests.apply_command(store, request(proposed))
    assert blocked == [True] and json.loads(config.read_text()) == cfg
    assert not (config.parent / 'data/drug-map/alias-switch.pending.json').exists()
    with config_edit._edit_lock(config):
        assert json.loads(config.read_text()) == cfg


def test_failed_receipt_preserves_uncooperative_edit_for_explicit_recovery(corpus):
    store, cfg, config = corpus
    proposed = drug_alias.proposal(store.db, cfg, "キラナン", "fiction-i1")
    req = request(proposed)
    def edit_other_setting():
        current = json.loads(config.read_text())
        current['unrelated'] = 'newer'
        config.write_text(json.dumps(current))
        return 0
    store.db.create_function('synthetic_config_edit', 0, edit_other_setting)
    store.db.execute('CREATE TRIGGER reject_after_edit BEFORE INSERT ON command_receipts '
                     "BEGIN SELECT synthetic_config_edit(); SELECT RAISE(ABORT,'synthetic failure'); END")
    with pytest.raises(RuntimeError, match='alias_recovery_required'):
        mcs_requests.apply_command(store, req)
    assert json.loads(config.read_text())['unrelated'] == 'newer'
    journal = config.parent / 'data/drug-map/alias-switch.pending.json'
    assert journal.exists()
    store.db.execute('DROP TRIGGER reject_after_edit')
    assert mcs_requests.apply_command(store, rollback(req['command_id']))['outcome'] == 'applied'
    recovered = json.loads(config.read_text())
    assert recovered['drug_map'] == cfg['drug_map'] and recovered['unrelated'] == 'newer'
    assert not journal.exists()


def _commit_fault(connection, committed):
    class CommitFault:
        def __getattr__(self, name):
            return getattr(connection, name)
        def __enter__(self):
            connection.__enter__()
            return self
        def __exit__(self, kind, value, tb):
            if kind is not None:
                return connection.__exit__(kind, value, tb)
            if committed is True:
                connection.__exit__(None, None, None)
            elif committed is False:
                connection.rollback()
            raise OSError('synthetic indeterminate commit')
    return CommitFault()


@pytest.mark.parametrize('committed', [False, True])
def test_commit_outcome_unknown_preserves_config_receipt_and_journal(corpus, committed):
    store, cfg, config = corpus
    proposed = drug_alias.proposal(store.db, cfg, "キラナン", "fiction-i1")
    req = request(proposed)
    connection = store.db
    store.db = _commit_fault(connection, committed)
    try:
        with pytest.raises(mcs_requests.AliasCommitUnknown, match='alias_commit_unknown'):
            mcs_requests.apply_command(store, req)
    finally:
        store.db = connection
    switched = json.loads(config.read_text())
    assert switched['drug_map']['sha256'] == proposed['after_dict_sha']
    assert switched['unrelated'] == cfg['unrelated']
    journal = config.parent / 'data/drug-map/alias-switch.pending.json'
    assert journal.exists()
    row = store.db.execute('SELECT outcome, receipt_json FROM command_receipts WHERE command_id=?',
                           (req['command_id'],)).fetchone()
    assert (row is not None) is committed
    if committed:
        assert row['outcome'] == 'applied'
        original_receipt = row['receipt_json']
        assert mcs_requests.apply_command(store, req)['outcome'] == 'applied'
        assert journal.exists()
    else:
        assert mcs_requests.apply_command(store, req)['error'] == 'alias_recovery_required'
    assert mcs_requests.apply_command(store, rollback(req['command_id']))['outcome'] == 'applied'
    assert json.loads(config.read_text()) == cfg and not journal.exists()
    if committed:
        assert store.db.execute('SELECT receipt_json FROM command_receipts WHERE command_id=?',
                                (req['command_id'],)).fetchone()[0] == original_receipt


@pytest.mark.parametrize('committed', [False, True])
def test_rollback_commit_unknown_preserves_both_receipts_and_journal(corpus, committed):
    store, cfg, config = corpus
    proposed = drug_alias.proposal(store.db, cfg, "キラナン", "fiction-i1")
    approval = request(proposed)
    assert mcs_requests.apply_command(store, approval)['outcome'] == 'applied'
    original_receipt = store.db.execute('SELECT receipt_json FROM command_receipts WHERE command_id=?',
                                        (approval['command_id'],)).fetchone()[0]
    request_rollback = rollback(approval['command_id'])
    connection = store.db
    store.db = _commit_fault(connection, committed)
    try:
        with pytest.raises(mcs_requests.AliasCommitUnknown, match='alias_commit_unknown'):
            mcs_requests.apply_command(store, request_rollback)
    finally:
        store.db = connection
    assert json.loads(config.read_text()) == cfg
    journal = config.parent / 'data/drug-map/alias-switch.pending.json'
    assert journal.exists()
    recorded = store.db.execute('SELECT outcome FROM command_receipts WHERE command_id=?',
                                (request_rollback['command_id'],)).fetchone()
    assert (recorded is not None) is committed
    if committed:
        assert recorded['outcome'] == 'applied'
    assert store.db.execute('SELECT receipt_json FROM command_receipts WHERE command_id=?',
                            (approval['command_id'],)).fetchone()[0] == original_receipt
    assert mcs_requests.apply_command(store, rollback(approval['command_id']))['outcome'] == 'applied'
    assert json.loads(config.read_text()) == cfg and not journal.exists()


def test_commit_unknown_with_open_transaction_is_held_until_rollback(corpus):
    import sqlite3
    store, cfg, config = corpus
    proposed = drug_alias.proposal(store.db, cfg, "キラナン", "fiction-i1")
    req = request(proposed)
    connection = store.db
    store.db = _commit_fault(connection, None)
    try:
        with pytest.raises(mcs_requests.AliasCommitUnknown, match='alias_commit_unknown'):
            mcs_requests.apply_command(store, req)
    finally:
        store.db = connection
    assert connection.in_transaction
    journal = config.parent / 'data/drug-map/alias-switch.pending.json'
    assert journal.exists() and json.loads(config.read_text())['drug_map']['sha256'] == proposed['after_dict_sha']
    with sqlite3.connect(str(config.parent / 'data/ledger.db')) as observer:
        assert observer.execute('SELECT 1 FROM command_receipts WHERE command_id=?',
                                (req['command_id'],)).fetchone() is None
    connection.rollback()
    assert mcs_requests.apply_command(store, rollback(req['command_id']))['outcome'] == 'applied'
    assert json.loads(config.read_text()) == cfg and not journal.exists()


@pytest.mark.parametrize('change', ['edited', 'same_bytes_replacement'])
def test_concurrent_config_change_before_lock_is_preserved(corpus, monkeypatch, change):
    store, cfg, config = corpus
    proposed = drug_alias.proposal(store.db, cfg, "キラナン", "fiction-i1")
    original = drug_alias.proposal
    expected = {**cfg, 'unrelated': 'newer'} if change == 'edited' else cfg
    def concurrent(*args):
        result = original(*args)
        if change == 'edited':
            config.write_text(json.dumps(expected))
        else:
            replacement = config.with_name('replacement.json')
            replacement.write_bytes(config.read_bytes())
            replacement.chmod(0o600)
            replacement.replace(config)
        return result
    monkeypatch.setattr(drug_alias, 'proposal', concurrent)
    assert mcs_requests.apply_command(store, request(proposed))['error'] == 'alias_switch_failed'
    assert json.loads(config.read_text()) == expected
    assert not (config.parent / 'data/drug-map/alias-switch.pending.json').exists()


def test_concurrent_config_change_before_publish_is_preserved(corpus, monkeypatch):
    store, cfg, config = corpus
    proposed = drug_alias.proposal(store.db, cfg, "キラナン", "fiction-i1")
    snapshot = drug_alias._config_snapshot
    expected = {**cfg, 'unrelated': 'newer'}
    calls = []
    def concurrent(path):
        calls.append(True)
        if len(calls) == 3:
            config.write_text(json.dumps(expected))
        return snapshot(path)
    monkeypatch.setattr(drug_alias, '_config_snapshot', concurrent)
    assert mcs_requests.apply_command(store, request(proposed))['error'] == 'alias_switch_failed'
    assert json.loads(config.read_text()) == expected
    assert not (config.parent / 'data/drug-map/alias-switch.pending.json').exists()


def test_alias_accepts_private_config_above_general_limit(corpus):
    store, cfg, config = corpus
    cfg['unrelated'] = 'x' * (1024 * 1024 + 1)
    config.write_text(json.dumps(cfg))
    proposed = drug_alias.proposal(store.db, cfg, "キラナン", "fiction-i1")
    assert mcs_requests.apply_command(store, request(proposed))['outcome'] == 'applied'
    assert json.loads(config.read_text())['unrelated'] == cfg['unrelated']


@pytest.mark.parametrize('extra', [0, 1])
def test_alias_checks_serialized_config_limit_before_journal(corpus, extra):
    store, cfg, config = corpus
    proposed = drug_alias.proposal(store.db, cfg, "キラナン", "fiction-i1")
    destination = config.parent / 'data/drug-map' / ('alias-' + proposed['after_dict_sha'] + '.json')
    target = {'path': str(destination), 'sha256': proposed['after_dict_sha']}
    cfg['unrelated'] = ''
    remaining = drug_map.MAX_BYTES + extra - len(mcs_requests.canonical({**cfg, 'drug_map': target}))
    cfg['unrelated'] = 'x' * remaining
    raw = mcs_requests.canonical(cfg)
    assert len(raw) <= drug_map.MAX_BYTES
    assert len(mcs_requests.canonical({**cfg, 'drug_map': target})) == drug_map.MAX_BYTES + extra
    config.write_bytes(raw)
    req = request(proposed)
    receipt = mcs_requests.apply_command(store, req)
    journal = config.parent / 'data/drug-map/alias-switch.pending.json'
    if extra:
        assert receipt['outcome'] == 'rejected' and receipt['error'] == 'alias_switch_failed'
        assert config.read_bytes() == raw and not journal.exists()
        assert mcs_requests.apply_command(store, req) == receipt
        stored = store.db.execute('SELECT outcome FROM command_receipts WHERE command_id=?',
                                  (req['command_id'],)).fetchone()
        assert stored['outcome'] == 'rejected'
    else:
        assert receipt['outcome'] == 'applied'
        assert config.stat().st_size == drug_map.MAX_BYTES and not journal.exists()
        assert mcs_requests.apply_command(store, rollback(req['command_id']))['outcome'] == 'applied'
        assert json.loads(config.read_text()) == cfg


@pytest.mark.parametrize('fail_compensation', [False, True])
def test_config_and_compensation_directory_sync_failure_preserves_recovery(corpus, monkeypatch,
                                                                           fail_compensation):
    store, cfg, config = corpus
    proposed = drug_alias.proposal(store.db, cfg, "キラナン", "fiction-i1")
    req = request(proposed)
    original_write = drug_alias.atomic_write
    original_sync = safe_io.fsync_dir
    calls = []
    def fail_sync(path, **kwargs):
        raise OSError('synthetic directory sync failure')
    def write_with_failed_sync(*args, **kwargs):
        calls.append(True)
        with monkeypatch.context() as patch:
            if len(calls) == 1 or fail_compensation:
                patch.setattr(safe_io, 'fsync_dir', fail_sync)
            return original_write(*args, **kwargs)
    monkeypatch.setattr(drug_alias, 'atomic_write', write_with_failed_sync)
    receipt = mcs_requests.apply_command(store, req)
    assert receipt['outcome'] == 'rejected' and receipt['error'] == 'alias_switch_failed'
    assert json.loads(config.read_text()) == cfg
    journal = config.parent / 'data/drug-map/alias-switch.pending.json'
    assert journal.exists() is fail_compensation
    if fail_compensation:
        monkeypatch.setattr(drug_alias, 'atomic_write', original_write)
        assert mcs_requests.apply_command(store, rollback(req['command_id']))['outcome'] == 'applied'
        assert json.loads(config.read_text()) == cfg and not journal.exists()
    assert safe_io.fsync_dir is original_sync


def test_journal_recovery_respects_shared_config_lock(corpus):
    store, cfg, config = corpus
    proposed = drug_alias.proposal(store.db, cfg, "キラナン", "fiction-i1")
    req = request(proposed)
    receipt = mcs_requests.apply_command(store, req)
    assert receipt['outcome'] == 'applied'
    journal = config.parent / 'data/drug-map/alias-switch.pending.json'
    safe_io.publish_new_bytes(journal, mcs_requests.canonical(receipt))
    before = config.read_bytes()
    with config_edit._edit_lock(config):
        blocked = mcs_requests.apply_command(store, rollback(req['command_id']))
    assert blocked['error'] == 'alias_switch_failed'
    assert journal.exists() and config.read_bytes() == before
    assert mcs_requests.apply_command(store, rollback(req['command_id']))['outcome'] == 'applied'
    assert json.loads(config.read_text())['drug_map'] == cfg['drug_map']
    assert not journal.exists()


def test_journal_recovery_preserves_newer_config(corpus, monkeypatch):
    store, cfg, config = corpus
    proposed = drug_alias.proposal(store.db, cfg, "キラナン", "fiction-i1")
    req = request(proposed)
    receipt = mcs_requests.apply_command(store, req)
    assert receipt['outcome'] == 'applied'
    journal = config.parent / 'data/drug-map/alias-switch.pending.json'
    safe_io.publish_new_bytes(journal, mcs_requests.canonical(receipt))
    snapshot = drug_alias._config_snapshot
    calls, expected = [], {}
    def concurrent(path):
        calls.append(True)
        if len(calls) == 2:
            expected.update(json.loads(config.read_text()))
            expected['unrelated'] = 'newer'
            config.write_text(json.dumps(expected))
        return snapshot(path)
    monkeypatch.setattr(drug_alias, '_config_snapshot', concurrent)
    assert mcs_requests.apply_command(store, rollback(req['command_id']))['error'] == 'alias_switch_failed'
    assert json.loads(config.read_text()) == expected and journal.exists()


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


@pytest.fixture
def split_alias(corpus, monkeypatch):
    import hashlib
    import cloud_policy
    import provider_settings
    import semantic_jev

    store, cfg, config = corpus
    root = config.parent
    root.chmod(0o700)
    cfg.update({"cloud_llm": {"mode": "on", "project_ids": [1], "provider": "openai"},
                "semantic": {"mode": "off"}})
    config.write_text(json.dumps(cfg, ensure_ascii=False, indent=2))
    directory = root / "data" / "provider_settings"
    directory.mkdir(mode=0o700)
    zero = {"daily_cost_limit_usd": 0, "monthly_cost_limit_usd": 0}
    providers = {"anthropic": {"model": cloud_policy.MODEL, "budget": zero},
                 "openai": {"model": cloud_policy.openai_client.MODEL, "budget": zero},
                 "jev": {"model": semantic_jev.JEV_MODEL, "daily_request_budget": 0}}
    files = {}
    for name, value in providers.items():
        path = directory / (name + ".json")
        path.write_text(json.dumps(value, ensure_ascii=False))
        path.chmod(0o600)
        files[name] = path.read_bytes()
    authority = root / "data" / ".provider-settings-authority.json"
    authority.write_text(json.dumps({"version": 2, "state": "committed",
        "files": {name: hashlib.sha256(raw).hexdigest() for name, raw in files.items()},
        "common_policy_sha256": provider_settings.common_policy_digest(cfg)}))
    authority.chmod(0o600)
    original = config.read_bytes()
    authority_raw = authority.read_bytes()
    owners = {cloud_policy.MODEL: "anthropic", cloud_policy.openai_client.MODEL: "openai"}
    monkeypatch.setattr(cloud_policy, "pricing_valid", lambda *args, **kwargs: True)

    def check(expected_pin):
        read = provider_settings.read(root, model_owners=owners, jev_model=semantic_jev.JEV_MODEL,
                                      config_limit=provider_settings.ALIAS_CONFIG_MAX_BYTES)
        assert read["mode"] == "split" and read["common"]["drug_map"] == expected_pin
        assert {k: v for k, v in read["common"].items() if k != "drug_map"} == {
            k: v for k, v in cfg.items() if k != "drug_map"}
        assert authority.read_bytes() == authority_raw
        assert all((directory / (name + ".json")).read_bytes() == raw for name, raw in files.items())
        effective = provider_settings.compose(read["common"], read["providers"], selected_provider="openai",
                                              model_owners=owners, jev_model=semantic_jev.JEV_MODEL)
        assert cloud_policy.budget_block_reason(store, effective) == "provider_daily_cap"
        assert effective["semantic"]["daily_request_budget"] == 0
        assert not (root / "data" / ".provider-settings-pending.json").exists()

    check(cfg["drug_map"])
    return store, cfg, config, original, check


def test_split_alias_apply_and_rollback_preserve_provider_authority_and_caps(split_alias):
    store, cfg, _, _, check = split_alias
    proposal = drug_alias.proposal(store.db, cfg, "キラナン", "fiction-i1")
    req = request(proposal)
    receipt = mcs_requests.apply_command(store, req)
    assert receipt["outcome"] == "applied"
    check(receipt["after_pin"])
    assert mcs_requests.apply_command(store, rollback(req["command_id"]))["outcome"] == "applied"
    check(cfg["drug_map"])


def test_split_alias_known_receipt_failure_restores_exact_common_bytes(split_alias):
    import sqlite3
    store, cfg, config, original, check = split_alias
    proposal = drug_alias.proposal(store.db, cfg, "キラナン", "fiction-i1")
    store.db.execute("CREATE TRIGGER reject_split_receipt BEFORE INSERT ON command_receipts "
                     "BEGIN SELECT RAISE(ABORT,'synthetic failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        mcs_requests.apply_command(store, request(proposal))
    assert config.read_bytes() == original
    assert not (config.parent / "data/drug-map/alias-switch.pending.json").exists()
    check(cfg["drug_map"])


@pytest.mark.parametrize("committed", [False, True])
def test_split_alias_unknown_commit_preserves_journal_and_provider_caps(split_alias, committed):
    store, cfg, config, _, check = split_alias
    proposal = drug_alias.proposal(store.db, cfg, "キラナン", "fiction-i1")
    req = request(proposal)
    connection = store.db
    store.db = _commit_fault(connection, committed)
    try:
        with pytest.raises(mcs_requests.AliasCommitUnknown):
            mcs_requests.apply_command(store, req)
    finally:
        store.db = connection
    journal = config.parent / "data/drug-map/alias-switch.pending.json"
    assert journal.exists()
    current_pin = json.loads(config.read_text())["drug_map"]
    assert current_pin["sha256"] == proposal["after_dict_sha"]
    check(current_pin)
    assert mcs_requests.apply_command(store, rollback(req["command_id"]))["outcome"] == "applied"
    assert not journal.exists()
    check(cfg["drug_map"])
