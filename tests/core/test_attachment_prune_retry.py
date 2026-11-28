"""Attachment retention keeps durable cleanup paths through unlink failures."""
import json
import os
import hashlib
import sqlite3
import time
from contextlib import closing

import pytest

import attachment_ocr
import health_report
import health_watch
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


DAY = 86400


def _aged(db, days, attachment_id=1):
    with db.db:
        db.db.execute("UPDATE attachments SET downloaded_at=? WHERE attachment_id=?",
                      (time.time() - days * DAY - 60, attachment_id))


def _healthy_ocr_status(data, **updates):
    """A fresh private OCR run record that on its own reads as healthy."""
    path = data / "attachment_ocr_status.json"
    path.write_text(json.dumps({"at": time.time(), "complete": 0, "empty": 0, "failed": 0,
                                "unsupported": 0, "deferred": 0, "pending_failed": 0,
                                "queue_status": "known", **updates}))
    path.chmod(0o600)


def _ocr_health(tmp_path, cfg=OCR_CFG):
    return health_report.attachment_ocr_health(str(tmp_path), cfg, time.time())


def _complete_record(db, path):
    pages = [{"page": 1, "engine": "pdf_text", "text": "合成"}]
    pages[0]["text_hash"] = attachment_ocr._hash(pages[0]["text"])
    _ocr_record(db, path, "complete", engine_version=attachment_ocr.ENGINE_VERSION, pages=pages,
                text_hash=attachment_ocr._hash(json.dumps(pages, ensure_ascii=False, sort_keys=True)))


@pytest.mark.parametrize(("days", "warned"), [(31, True), (29, False)])
def test_original_kept_for_ocr_past_warning_age_is_reported_by_count_only(tmp_path, monkeypatch, days, warned):
    db, path = _ocr_asset(tmp_path, monkeypatch)
    _aged(db, days)
    _healthy_ocr_status(tmp_path)
    report = _ocr_health(tmp_path)
    assert maintenance.prune_attachments(str(tmp_path / "ledger.db")) == 0 and path.exists()
    if warned:
        assert (report["state"], report["reason"]) == ("deferred", "attachment_ocr_original_retained")
        assert report["retained_originals"] == {"count": 1, "lower_bound": False,
                                                "oldest_days": 31, "warn_days": 30}
        text = "\n".join(health_watch._reason_rows({"attachment_ocr": report}, [report["reason"]]))
        assert "30日を超えて残っている原本が1件あります（最も古いものは取得から31日）" in text
        assert "自動では削除しません" in text
        assert "file.pdf" not in text + json.dumps(report) and "attachment_id" not in json.dumps(report)
    else:
        assert report["state"] == "ok" and "retained_originals" not in report
    db.close()


@pytest.mark.parametrize(("state", "counted"), [("complete", False), ("unsupported", False), ("failed", True)])
def test_counted_originals_are_exactly_the_ones_retention_keeps(tmp_path, monkeypatch, state, counted):
    db, path = _ocr_asset(tmp_path, monkeypatch)
    _aged(db, 40)
    if state == "complete":
        _complete_record(db, path)
    else:
        _ocr_record(db, path, state, attempts=3, next_try=0, reason="timeout")
    _healthy_ocr_status(tmp_path)
    report = _ocr_health(tmp_path)
    assert ("retained_originals" in report) is counted
    assert maintenance.prune_attachments(str(tmp_path / "ledger.db")) == int(not counted)
    assert path.exists() is counted
    db.close()


@pytest.mark.parametrize(("setting", "warn_days"), [(10, 10), ("10", None), (0, None)])
def test_warning_age_follows_the_health_setting_and_defaults_when_invalid(tmp_path, monkeypatch, setting, warn_days):
    db, _ = _ocr_asset(tmp_path, monkeypatch)
    _aged(db, 11)
    _healthy_ocr_status(tmp_path)
    report = _ocr_health(tmp_path, {**OCR_CFG, "health": {"attachment_ocr_retained_warn_days": setting}})
    if warn_days is None:   # an invalid value falls back to the 30-day default
        assert report["state"] == "ok"
    else:
        assert report["retained_originals"]["warn_days"] == warn_days
        assert report["reason"] == "attachment_ocr_original_retained"
    db.close()


@pytest.mark.parametrize("cfg", [{"attachment_ocr": {"enabled": False}}, {"attachment_ocr": None}])
def test_disabled_or_invalid_ocr_settings_never_read_the_ledger(tmp_path, monkeypatch, cfg):
    db, _ = _ocr_asset(tmp_path, monkeypatch)
    _aged(db, 40)

    def forbidden(*args, **kwargs):
        raise AssertionError("retained originals were read without valid enabled OCR")

    monkeypatch.setattr(health_report, "_ocr_ledger_snapshot", forbidden)
    assert "retained_originals" not in _ocr_health(tmp_path, cfg)
    db.close()


def test_retained_count_rides_along_when_another_ocr_reason_wins(tmp_path, monkeypatch):
    db, _ = _ocr_asset(tmp_path, monkeypatch)
    _aged(db, 31)
    missing = _ocr_health(tmp_path)
    assert missing["reason"] == "attachment_ocr_status_missing" and missing["retained_originals"]["count"] == 1
    _healthy_ocr_status(tmp_path, failed=1)
    failed = _ocr_health(tmp_path)
    assert failed["reason"] == "attachment_ocr_processing_failed" and failed["retained_originals"]["count"] == 1
    for report in (missing, failed, {**failed, "state": "failed", "reason": "attachment_ocr_acknowledged_gap"}):
        text = "\n".join(health_watch._reason_rows({"attachment_ocr": report}, [report["reason"]]))
        assert "残っている原本が1件あります" in text
    db.close()


def _stored_originals(db, tmp_path, ids, days, message_id=1):
    """Downloaded rows with a stored original path (no file is needed to be counted)."""
    with db.db:
        db.db.executemany(
            "INSERT INTO attachments(attachment_id,message_id,name,local_path,state,downloaded_at) "
            "VALUES(?,?,'extra.pdf',?,'downloaded',?)",
            [(n, message_id, str(tmp_path / "attachments" / str(n)), time.time() - days * DAY)
             for n in ids])


def test_full_observation_window_is_rendered_as_a_lower_bound(tmp_path, monkeypatch):
    db, _ = _ocr_asset(tmp_path, monkeypatch)
    _stored_originals(db, tmp_path, range(2, 259), 50)
    _healthy_ocr_status(tmp_path)
    report = _ocr_health(tmp_path)
    assert report["retained_originals"] == {"count": 256, "lower_bound": True,
                                            "oldest_days": 50, "warn_days": 30}
    text = "\n".join(health_watch._reason_rows({"attachment_ocr": report}, [report["reason"]]))
    assert "256件以上あります" in text
    db.close()


def test_retained_count_stops_at_its_deadline_and_reports_it_as_unconfirmed(tmp_path, monkeypatch):
    db, _ = _ocr_asset(tmp_path, monkeypatch)
    _stored_originals(db, tmp_path, range(2, 6), 50)
    _aged(db, 50)
    _healthy_ocr_status(tmp_path)
    clock = [0.0]
    monkeypatch.setattr(health_report.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(maintenance, "monotonic", lambda: clock[0])
    calls = []
    real = attachment_ocr.awaiting_result

    def slow_row(*args, **kwargs):
        # The real check runs; the synthetic clock then passes the 2-second budget.
        calls.append(args[1])
        try:
            return real(*args, **kwargs)
        finally:
            clock[0] = 10.0

    monkeypatch.setattr(attachment_ocr, "awaiting_result", slow_row)
    report = _ocr_health(tmp_path)
    assert len(calls) == 1                 # without the per-row deadline every row is visited
    # A walk the deadline stopped is neither a count nor healthy: it is reported as unconfirmed.
    assert (report["state"], report["reason"]) == ("unknown", "attachment_ocr_original_retained_unverified")
    assert report["retained_originals"] == {"unverified": True}
    text = "\n".join(health_watch._reason_rows({"attachment_ocr": report}, [report["reason"]]))
    assert "確認できませんでした" in text and "件あります" not in text
    db.close()


def test_unconfirmed_walk_rides_along_when_another_ocr_reason_wins(tmp_path, monkeypatch):
    db, _ = _ocr_asset(tmp_path, monkeypatch)
    _aged(db, 31)
    _healthy_ocr_status(tmp_path, failed=1)
    clock = [0.0]
    monkeypatch.setattr(health_report.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(maintenance, "monotonic", lambda: clock[0])
    real = attachment_ocr.awaiting_result

    def slow_row(*args, **kwargs):
        try:
            return real(*args, **kwargs)
        finally:
            clock[0] = 10.0

    monkeypatch.setattr(attachment_ocr, "awaiting_result", slow_row)
    report = _ocr_health(tmp_path)
    assert report["reason"] == "attachment_ocr_processing_failed"
    assert report["retained_originals"] == {"unverified": True}
    text = "\n".join(health_watch._reason_rows({"attachment_ocr": report}, [report["reason"]]))
    assert "件数は、確認できませんでした" in text
    db.close()


def test_row_without_a_stored_original_is_not_counted_as_kept(tmp_path, monkeypatch):
    # Still 'downloaded' but its stored path was cleared and the original is gone:
    # retention never selects it, so the warning must not count it either.
    db, path = _ocr_asset(tmp_path, monkeypatch)
    _aged(db, 31)
    path.unlink()
    with db.db:
        db.db.execute("UPDATE attachments SET local_path=NULL WHERE attachment_id=1")
    _healthy_ocr_status(tmp_path)
    report = _ocr_health(tmp_path)
    assert report["state"] == "ok" and "retained_originals" not in report
    db.close()


def test_rows_retention_would_not_keep_never_hide_a_kept_original(tmp_path, monkeypatch):
    # 256 older originals of a deleted post are not OCR work; the walk goes past them.
    db, _ = _ocr_asset(tmp_path, monkeypatch)
    db.save_messages([_message(2)])
    with db.db:
        db.db.execute("UPDATE messages SET body_state='deleted' WHERE message_id=2")
    _stored_originals(db, tmp_path, range(2, 258), 50, message_id=2)
    _aged(db, 31)
    _healthy_ocr_status(tmp_path)
    report = _ocr_health(tmp_path)
    assert (report["state"], report["reason"]) == ("deferred", "attachment_ocr_original_retained")
    assert report["retained_originals"] == {"count": 1, "lower_bound": False,
                                            "oldest_days": 31, "warn_days": 30}
    db.close()


def test_unreadable_ledger_is_not_reported_as_zero_or_as_a_warning(tmp_path):
    _healthy_ocr_status(tmp_path)
    assert not (tmp_path / "ledger.db").exists()
    report = _ocr_health(tmp_path)
    assert report["state"] == "ok" and "retained_originals" not in report


@pytest.mark.parametrize("schema", [
    "CREATE TABLE attachments(attachment_id INTEGER PRIMARY KEY)",  # the columns retention reads are missing
    "CREATE TABLE unrelated(x INTEGER)",                              # no attachments table at all
])
def test_failed_read_of_an_opened_ledger_is_unconfirmed_not_healthy(tmp_path, schema):
    # The ledger opens, but the retention read fails long before the 2-second deadline.
    with closing(sqlite3.connect(tmp_path / "ledger.db")) as db:
        db.execute(schema)
        db.commit()
    _healthy_ocr_status(tmp_path)
    report = _ocr_health(tmp_path)
    # Without the fix the read error returned "not observed" and the result stayed 'ok'.
    assert (report["state"], report["reason"]) == ("unknown", "attachment_ocr_original_retained_unverified")
    assert report["retained_originals"] == {"unverified": True}
    text = "\n".join(health_watch._reason_rows({"attachment_ocr": report}, [report["reason"]]))
    assert "確認できませんでした" in text and "時間内" not in text and "件あります" not in text


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
