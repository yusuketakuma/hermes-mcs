"""Attachment retention keeps durable cleanup paths through unlink failures."""
import json
import os
import hashlib
import time

import pytest

import attachment_ocr
import maintenance
import notify_flush
from ingest_testkit import _ledger, _message


def _asset(tmp_path, state="downloaded"):
    db = _ledger(tmp_path)
    db.save_messages([_message(1)])
    root = tmp_path / "attachments"
    root.mkdir()
    path = root / "1"
    path.write_bytes(b"synthetic attachment")
    db.db.execute(
        "INSERT INTO attachments(attachment_id,message_id,name,local_path,state,downloaded_at) "
        "VALUES(1,1,'file.pdf',?,?,?)",
        (str(path), state, time.time() - maintenance.ATTACHMENT_KEEP_S - 1))
    db.db.commit()
    return db, path


@pytest.mark.parametrize("state", ["downloaded", "withdrawn"])
@pytest.mark.parametrize("failed_alias", [False, True])
def test_failed_unlink_keeps_retry_path_without_a_sendable_payload(
        tmp_path, monkeypatch, state, failed_alias):
    db, path = _asset(tmp_path, state)
    alias = path.with_suffix(".pdf")
    os.link(path, alias)
    blocked = alias if failed_alias else path
    unlink = os.unlink

    def fail(candidate, *args, **kwargs):
        if str(candidate) == str(blocked):
            raise PermissionError("synthetic permission failure")
        unlink(candidate, *args, **kwargs)

    monkeypatch.setattr(maintenance.os, "unlink", fail)
    with pytest.raises(maintenance.MaintenanceError, match="attachment_prune_failed"):
        maintenance.prune_attachments(str(tmp_path / "ledger.db"))
    row = db.db.execute("SELECT state,local_path FROM attachments").fetchone()
    assert tuple(row) == ("withdrawn" if state == "withdrawn" else "pruned", str(path))
    assert path.exists()                   # raw is retained when an alias failed
    assert notify_flush._collect_files(notify_flush._attachments_map(db, [1]), [1]) == []
    monkeypatch.setattr(maintenance.os, "unlink", unlink)
    assert maintenance.prune_attachments(str(tmp_path / "ledger.db")) == 1
    assert not path.exists() and not alias.exists()
    assert db.db.execute("SELECT local_path FROM attachments").fetchone()[0] is None
    assert maintenance.prune_attachments(str(tmp_path / "ledger.db")) == 0
    db.close()


OCR_CFG = {"attachment_ocr": {"enabled": True, "executable": "/synthetic/ocr"}}


def _ocr_asset(tmp_path, monkeypatch, cfg=OCR_CFG, state="read"):
    """An expired original of a valid patient whose OCR settings are read as given."""
    db, path = _asset(tmp_path)
    db.ensure_patient(1)
    blob = path.read_bytes()
    db.db.execute("UPDATE attachments SET sha256=?,bytes=? WHERE attachment_id=1",
                  (hashlib.sha256(blob).hexdigest(), len(blob)))
    db.db.commit()
    monkeypatch.setattr(maintenance, "config_read_state", lambda: (cfg, state))
    return db, path


def _ocr_record(db, path, state, **fields):
    blob = path.read_bytes()
    doc = {"schema": attachment_ocr.SCHEMA, "runtime_contract": attachment_ocr.RUNTIME_CONTRACT,
           "engine": "yomitoku", "attachment_id": 1, "input_sha256": hashlib.sha256(blob).hexdigest(),
           "input_bytes": len(blob), "state": state, "pages": [], **fields}
    db.artifact_add(attachment_ocr.KIND, json.dumps(doc), project_id=1,
                    message_id=1, meta={"attachment_id": 1})
    db.db.commit()


@pytest.mark.parametrize(("state", "expected"), [
    # A due retry of any recorded failure keeps the original OCR still needs.
    ("failed", 0), ("unavailable", 0),
    ("unsupported", 1),
])
def test_recorded_ocr_outcome_decides_retention_of_expired_original(tmp_path, monkeypatch, state, expected):
    db, path = _ocr_asset(tmp_path, monkeypatch)
    _ocr_record(db, path, state, attempts=1, next_try=0, reason="timeout")
    assert maintenance.prune_attachments(str(tmp_path / "ledger.db")) == expected
    assert path.exists() is not bool(expected)
    db.close()


def test_unreadable_config_fails_closed_and_keeps_downloaded_original(tmp_path, monkeypatch):
    db, path = _ocr_asset(tmp_path, monkeypatch, cfg={}, state="unreadable")
    assert maintenance.prune_attachments(str(tmp_path / "ledger.db")) == 0
    assert path.exists()
    db.close()


def test_invalid_ocr_setting_keeps_originals_and_continues_other_cleanup(tmp_path, monkeypatch):
    db, path = _ocr_asset(tmp_path, monkeypatch, cfg={"attachment_ocr": None})
    withdrawn = path.with_name("2")
    withdrawn.write_bytes(b"synthetic withdrawn attachment")
    db.db.execute("INSERT INTO attachments(attachment_id,message_id,name,local_path,state,downloaded_at) "
                  "VALUES(2,1,'other.pdf',?,'withdrawn',?)",
                  (str(withdrawn), time.time() - maintenance.ATTACHMENT_KEEP_S - 1))
    db.db.commit()
    assert maintenance.prune_attachments(str(tmp_path / "ledger.db")) == 1
    assert path.exists() and not withdrawn.exists()
    db.close()


def test_disabled_ocr_prunes_unprocessed_original_as_before(tmp_path, monkeypatch):
    db, path = _ocr_asset(tmp_path, monkeypatch, cfg={"attachment_ocr": {"enabled": False}})
    assert attachment_ocr.view(db.db, 1)["state"] == "unprocessed"
    assert maintenance.prune_attachments(str(tmp_path / "ledger.db")) == 1
    assert not path.exists()
    db.close()


def test_real_ocr_record_controls_retention_of_expired_original(tmp_path, monkeypatch):
    db, path = _ocr_asset(tmp_path, monkeypatch)
    assert attachment_ocr.view(db.db, 1, cfg=OCR_CFG)["state"] == "unprocessed"
    assert maintenance.prune_attachments(str(tmp_path / "ledger.db")) == 0
    assert path.exists()

    pages = [{"page": 1, "engine": "pdf_text", "text": "合成"}]
    pages[0]["text_hash"] = attachment_ocr._hash(pages[0]["text"])
    _ocr_record(db, path, "complete", engine_version=attachment_ocr.ENGINE_VERSION, pages=pages,
                text_hash=attachment_ocr._hash(json.dumps(pages, ensure_ascii=False, sort_keys=True)))
    assert attachment_ocr.view(db.db, 1, cfg=OCR_CFG)["state"] == "complete"
    assert maintenance.prune_attachments(str(tmp_path / "ledger.db")) == 1
    assert not path.exists()
    db.close()


def test_deleted_parent_payload_is_not_retained_for_ocr(tmp_path, monkeypatch):
    db, path = _ocr_asset(tmp_path, monkeypatch)
    with db.db:
        db.db.execute("UPDATE messages SET body_state='deleted' WHERE message_id=1")
    assert maintenance.prune_attachments(str(tmp_path / "ledger.db")) == 1
    assert not path.exists()
    db.close()


def test_crash_after_unlink_is_resumable_and_does_not_count_missing_payload(tmp_path, monkeypatch):
    db, path = _asset(tmp_path)
    unlink = os.unlink

    def crash(candidate, *args, **kwargs):
        unlink(candidate, *args, **kwargs)
        raise KeyboardInterrupt

    monkeypatch.setattr(maintenance.os, "unlink", crash)
    with pytest.raises(KeyboardInterrupt):
        maintenance.prune_attachments(str(tmp_path / "ledger.db"))
    assert tuple(db.db.execute("SELECT state,local_path FROM attachments").fetchone()) == (
        "pruned", str(path))
    assert not path.exists()
    assert notify_flush._collect_files(notify_flush._attachments_map(db, [1]), [1]) == []
    monkeypatch.setattr(maintenance.os, "unlink", unlink)
    assert maintenance.prune_attachments(str(tmp_path / "ledger.db")) == 0
    assert db.db.execute("SELECT local_path FROM attachments").fetchone()[0] is None
    db.close()


def test_one_failed_payload_does_not_block_other_cleanup(tmp_path, monkeypatch):
    db, path = _asset(tmp_path)
    db.save_messages([_message(2)])
    other = path.parent / "2"
    other.write_bytes(b"synthetic second attachment")
    db.db.execute(
        "INSERT INTO attachments(attachment_id,message_id,name,local_path,state,downloaded_at) "
        "VALUES(2,2,'second.pdf',?,'downloaded',?)",
        (str(other), time.time() - maintenance.ATTACHMENT_KEEP_S - 1))
    db.db.commit()
    unlink = os.unlink

    def fail(candidate, *args, **kwargs):
        if str(candidate) == str(path):
            raise PermissionError("synthetic unavailable first payload")
        unlink(candidate, *args, **kwargs)

    monkeypatch.setattr(maintenance.os, "unlink", fail)
    with pytest.raises(maintenance.MaintenanceError):
        maintenance.prune_attachments(str(tmp_path / "ledger.db"))
    assert path.exists() and not other.exists()
    assert db.db.execute("SELECT local_path FROM attachments WHERE attachment_id=2").fetchone()[0] is None
    monkeypatch.setattr(maintenance.os, "unlink", unlink)
    assert maintenance.prune_attachments(str(tmp_path / "ledger.db")) == 1
    db.close()


def test_unreadable_alias_directory_never_silently_strands_aliases(tmp_path, monkeypatch):
    db, path = _asset(tmp_path)
    alias = path.with_suffix(".pdf")
    os.link(path, alias)
    scandir = os.scandir

    def deny(directory):
        if str(directory) == str(path.parent):
            raise PermissionError("synthetic directory enumeration failure")
        return scandir(directory)

    monkeypatch.setattr(maintenance.os, "scandir", deny)
    with pytest.raises(maintenance.MaintenanceError):
        maintenance.prune_attachments(str(tmp_path / "ledger.db"))
    assert path.exists() and alias.exists()
    assert db.db.execute("SELECT local_path FROM attachments").fetchone()[0] == str(path)
    monkeypatch.setattr(maintenance.os, "scandir", scandir)
    assert maintenance.prune_attachments(str(tmp_path / "ledger.db")) == 1
    db.close()


@pytest.mark.parametrize("reference", ["new_messages", "attachment_followup", "pending", "unknown", "held"])
def test_unsent_and_uncertain_references_keep_raw_and_alias(tmp_path, reference):
    db, path = _asset(tmp_path)
    alias = path.with_suffix(".pdf")
    os.link(path, alias)
    if reference in ("new_messages", "attachment_followup"):
        payload = {"message_ids": [1]} if reference == "new_messages" else {"attachment_id": 1}
        db.outbox_add(reference, 1, payload)
        db.db.execute("UPDATE notify_outbox SET state='failed'")
    else:
        db.db.execute(
            "INSERT INTO notification_render_parts(delivery_id,part_id,kind,idx,attachment_id,state) "
            "VALUES('synthetic','file:1','attachment_part',0,1,?)", (reference,))
    db.db.commit()
    assert maintenance.prune_attachments(str(tmp_path / "ledger.db")) == 0
    assert path.exists() and alias.exists()
    assert tuple(db.db.execute("SELECT state,local_path FROM attachments").fetchone()) == (
        "downloaded", str(path))
    db.close()


@pytest.mark.parametrize("kind", ["outside", "payload_symlink", "alias_symlink", "root_symlink"])
def test_prune_refuses_unowned_and_symlink_paths(tmp_path, kind):
    db, path = _asset(tmp_path)
    other = tmp_path / "outside"
    other.write_bytes(b"synthetic protected data")
    if kind == "outside":
        path = other
        db.db.execute("UPDATE attachments SET local_path=?", (str(path),))
    elif kind == "payload_symlink":
        path.unlink()
        path.symlink_to(other)
    elif kind == "alias_symlink":
        path.with_suffix(".pdf").symlink_to(other)
    else:
        path.parent.rename(tmp_path / "moved")
        path.parent.symlink_to(tmp_path / "moved", target_is_directory=True)
    db.db.commit()
    with pytest.raises(maintenance.MaintenanceError, match="attachment_prune_failed"):
        maintenance.prune_attachments(str(tmp_path / "ledger.db"))
    assert other.read_bytes() == b"synthetic protected data"
    assert path.exists()
    stored = db.db.execute("SELECT local_path FROM attachments").fetchone()[0]
    if kind in ("outside", "payload_symlink"):
        # a foreign pointer is retired once (file untouched), not retried forever
        assert stored is None
        assert maintenance.prune_attachments(str(tmp_path / "ledger.db")) == 0
        assert other.read_bytes() == b"synthetic protected data" and path.exists()
    else:
        assert stored == str(path)         # in-store faults stay retryable
    db.close()
