"""Observe and publish restore evidence and read exact bound human consent.

Connections belong to this service and close before backup hashing/schema
observation and report publication. Update/run locks, writer quiescence,
markers, journal transitions, replacement and reconcile remain caller-owned.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
import time

from mcs_requests import read_scheduled_receipt
from mcs_util import file_sha256
from safe_io import atomic_write


RESTORE_LOSS_NOTICE = (
    "件数は対象表の行数差です。訂正、同数の追加と削除、配送状態の変更や対象外のデータは分かりません。"
    "0件でも損失がないとは言えません。"
    "復元で推論の使用履歴や費用の記録が失われる場合があります。復元への同意は外部推論の再開を意味しません。"
)


class RestoreEvidenceError(Exception):
    """A restore evidence observation or mandatory report publication failed."""


_STORED_TABLES = ("messages", "attachments")

_EFFECT_TABLES = ("notification_cards", "notification_renders",
                  "notification_delivery_attempts",
                  "notification_render_parts", "notification_restore_holds",
                  "notification_view_manifests", "notify_outbox")


_INTAKE_EVIDENCE_ROWS = 4096
_INTAKE_EVIDENCE_ROW_BYTES = 65536
_INTAKE_EVIDENCE_SECONDS = 1.0


def _intake_evidence(con, table, kind):
    """Compare bounded raw records without exposing their contents."""
    columns = con.execute(f"PRAGMA table_info({table})").fetchall()
    if not columns:
        return []
    if not any(column[1] == 'kind' for column in columns):
        raise sqlite3.DatabaseError('restore_intake_schema_unverified')
    previous_limit = con.getlimit(sqlite3.SQLITE_LIMIT_LENGTH)
    con.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, min(previous_limit, _INTAKE_EVIDENCE_ROW_BYTES))
    deadline = time.monotonic() + _INTAKE_EVIDENCE_SECONDS
    steps = 0

    def stop():
        nonlocal steps
        steps += 1
        return steps > 200 or time.monotonic() >= deadline

    con.set_progress_handler(stop, 1000)
    try:
        if con.execute(f"SELECT 1 FROM {table} WHERE kind=? LIMIT 1", (kind,)).fetchone() is None:
            return []
        key = 'artifact_id' if table == 'artifacts' else 'job_id'
        pk = [column for column in columns if column[5]]
        all_columns = con.execute(f"PRAGMA table_xinfo({table})").fetchall()
        indexes = con.execute(f"PRAGMA index_list({table})").fetchall()
        if (len(pk) != 1 or pk[0][1] != key or pk[0][2].upper() != 'INTEGER'
                or any(column[1].lower() in ('rowid', '_rowid_', 'oid') for column in all_columns)
                or any(index[3] == 'pk' for index in indexes)):
            raise sqlite3.DatabaseError('restore_intake_identity_unverified')
        identities = []
        cursor = con.execute(
            f"SELECT rowid,* FROM {table} WHERE kind=? ORDER BY rowid LIMIT ?",
            (kind, _INTAKE_EVIDENCE_ROWS + 1))
        for row in cursor:
            if len(identities) >= _INTAKE_EVIDENCE_ROWS or time.monotonic() >= deadline:
                raise sqlite3.DatabaseError('restore_intake_evidence_limit')
            raw = json.dumps([all_columns, row], ensure_ascii=False, separators=(',', ':'),
                             default=lambda value: {'blob': value.hex()}).encode('utf-8')
            identities.append(hashlib.sha256(raw).hexdigest())
        return identities
    finally:
        con.set_progress_handler(None, 0)
        con.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, previous_limit)


def _intake_comparison(live, back):
    """Report live evidence not identically present in the backup."""
    compared = {}
    for label, table, kind in (
            ('held_intents', 'artifacts', 'semantic_intake_hold_v1'),
            ('unstored_intake', 'fetch_jobs', 'semantic_intake_unstored_v1'),
            ('unstored_epoch', 'fetch_jobs', 'semantic_intake_unstored_epoch_v1'),
            ('unstored_roots', 'fetch_jobs', 'semantic_intake_unstored_root_v1')):
        current = _intake_evidence(live, table, kind)
        saved = _intake_evidence(back, table, kind)
        if current or saved:
            present = set(saved)
            compared[label] = {
                'live': len(current), 'backup': len(saved),
                'live_not_identical_in_backup': sum(item not in present for item in current),
                'live_sha256': hashlib.sha256(''.join(current).encode('ascii')).hexdigest(),
            }
    return compared


def database_version(path: str) -> int | None:
    try:
        con = sqlite3.connect(
            Path(path).resolve().as_uri() + "?mode=ro", uri=True)
        try:
            return con.execute("PRAGMA user_version").fetchone()[0]
        finally:
            con.close()
    except sqlite3.Error:
        return None


def _table_count(con, table):
    """Return zero only when the table is absent; propagate read failures."""
    columns = con.execute(f"PRAGMA table_info({table})").fetchall()
    if not columns:
        return 0
    return con.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]


def _watermark(con):
    """An absent timestamp column is unknown; read failures must propagate."""
    columns = con.execute("PRAGMA table_info(messages)").fetchall()
    if not any(column[1] == "posted_at_ts" for column in columns):
        return None
    return con.execute("SELECT MAX(posted_at_ts) FROM messages").fetchone()[0]


def _restore_content_digest(con):
    """Bind consent to stored data, delivery and artifact history."""
    digest = hashlib.sha256()
    for table in (*_STORED_TABLES, *_EFFECT_TABLES, "artifacts"):
        columns = con.execute(f"PRAGMA table_info({table})").fetchall()
        digest.update(json.dumps([table, columns], separators=(",", ":")).encode())
        if not columns:
            continue
        if table == "artifacts":
            digest.update(b"artifacts-schema\0")
            for schema in con.execute(
                    "SELECT type,name,tbl_name,sql FROM sqlite_master "
                    "WHERE tbl_name='artifacts' AND type IN ('table','index','trigger') ORDER BY type,name"):
                encoded = json.dumps(schema, ensure_ascii=False, separators=(",", ":")).encode()
                digest.update(len(encoded).to_bytes(8, "big"))
                digest.update(encoded)
        for row in con.execute(f"SELECT * FROM {table} ORDER BY rowid"):
            encoded = json.dumps(
                row, ensure_ascii=False, separators=(",", ":"),
                default=lambda value: {"blob": value.hex()}).encode()
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
    return digest.hexdigest()


def create_loss_report(*, live_path: str, backup_path: str, report_path: str) -> dict:
    """Loss report a human must approve before the live DB is replaced.
    Binds backup bytes/schema, row deltas, and the contents of the stored-data,
    delivery tables and artifact history. Changes to an existing row invalidate stale consent."""
    try:
        live = sqlite3.connect(
            Path(live_path).resolve().as_uri() + "?mode=ro", uri=True)
    except sqlite3.Error as e:
        raise RestoreEvidenceError(f"restore_report_live_db: {type(e).__name__}") from None
    try:
        back = sqlite3.connect(
            Path(backup_path).resolve().as_uri() + "?mode=ro", uri=True)
    except sqlite3.Error as e:
        live.close()
        raise RestoreEvidenceError(f"restore_report_backup_db: {type(e).__name__}") from None
    try:
        live.execute("BEGIN")
        back.execute("BEGIN")
        intake = _intake_comparison(live, back)
        live_digest = _restore_content_digest(live)
        watermark = _watermark(back)
        stored = {t: _table_count(live, t) - _table_count(back, t)
                  for t in _STORED_TABLES}
        effects = {t: _table_count(live, t) - _table_count(back, t)
                   for t in _EFFECT_TABLES}
    except (sqlite3.Error, OSError):
        raise RestoreEvidenceError("restore_report_read_failed") from None
    finally:
        live.close()
        back.close()
    try:
        metrics = {"v": 1,
                   "live_content_sha256": live_digest,
                   "backup_sha256": file_sha256(backup_path),
                   "backup_schema": database_version(backup_path),
                   "watermark_ts": watermark,
                   "stored_since_backup": stored,
                   "external_effects": effects,
                   "intervening_messages": sum(max(0, n)
                                               for n in stored.values()),
                   "external_effect_rows": sum(max(0, n)
                                               for n in effects.values())}
        if intake:
            metrics['semantic_intake_evidence'] = intake
            metrics['semantic_intake_notice'] = (
                '保留・未保存の記録について、同一の内容が復元先にない件数です。'
                '保存できなかった投稿は復元されず、元の投稿を再取得できるかは分かりません。')
        metrics["report_id"] = hashlib.sha256(
            json.dumps(metrics, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=False).encode("utf-8")).hexdigest()
        report = dict(metrics, computed_at=time.time())
        atomic_write(report_path,
                     lambda f: json.dump(report, f, ensure_ascii=False,
                                         sort_keys=True),
                     tmp_prefix=".rreport.")
        return report
    except OSError as e:
        raise RestoreEvidenceError("restore_report_publish_failed") from e


def find_restore_consent(report: dict, *, live_path: str) -> str | None:
    """Find exact restore consent while consuming the entire read-only cursor.

    Scan for an ops.restore_approve approval bound
    to THIS exact loss report — an earlier update/rollback approval
    does not substitute (the human must see the loss numbers first)."""
    try:
        con = sqlite3.connect(
            Path(live_path).resolve().as_uri() + "?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    found = None
    try:
        rows = con.execute(
            "SELECT command_id, receipt_json FROM command_receipts"
            " WHERE outcome='applied' ORDER BY processed_at DESC,"
            " rowid DESC")
        for cid, rj in rows:
            if found is None and bound_restore_consent(rj, cid, report):
                found = cid   # newest bound receipt; keep reading (unparsed)
        # so an unreadable later row still fails closed, as fetchall() did
    except sqlite3.Error:
        return None
    finally:
        con.close()
    return found


def bound_restore_consent(raw, command_id, report: dict) -> bool:
    rec = read_scheduled_receipt(raw, command_id)
    return (rec is not None and rec.get("cmd") == "ops.restore_approve"
            and type(rec.get("backup_schema")) is int
            and rec.get("report_id") == report["report_id"]
            and rec.get("backup_sha256") == report["backup_sha256"]
            and rec.get("backup_schema") == report["backup_schema"])
