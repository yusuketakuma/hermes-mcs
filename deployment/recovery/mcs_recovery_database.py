"""Evidence-bound database restore, human consent and crash-safe staging."""
import ast
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile
import time
from contextlib import suppress
from pathlib import Path
from . import mcs_recovery_core
from . import mcs_recovery_gateway


def _restored_writer_compatible():
    """Prove a restored DB matches the exact clean checkout being resumed.

    Read the canonical schema literal as data; never import restored code.
    Unknown journal, source, target or DB identity retains the writer hold.
    """
    state = mcs_recovery_core._load_state()
    applying = state.get("applying")
    marker = mcs_recovery_core._unreconciled_restore()
    if state.get("_corrupt") or not isinstance(applying, dict) \
            or applying.get("rollback") is not True or not applying.get("sha") \
            or not isinstance(applying.get("backup_path"), str) \
            or marker is None or marker.get("backup_path") != applying["backup_path"]:
        return False
    path = os.path.join(mcs_recovery_core.REPO, "mcs", "core", "ledger.py")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with mcs_recovery_gateway._fdopen_owned(fd, "rb") as stream:
            metadata = os.fstat(stream.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 1024 * 1024:
                return False
            raw = stream.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            return False
        tree = ast.parse(raw.decode("utf-8"))
        stores = [node for node in ast.walk(tree) if isinstance(node, ast.Name)
                  and isinstance(node.ctx, ast.Store) and node.id == "SCHEMA_VERSION"]
        values = [node.value for node in tree.body if isinstance(node, ast.Assign)
                  and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
                  and node.targets[0].id == "SCHEMA_VERSION"]
        if len(stores) != 1 or len(values) != 1 or not isinstance(values[0], ast.Constant) \
                or type(values[0].value) is not int or values[0].value < 0:
            return False
        head = mcs_recovery_core._head()
        if not head or head != applying["sha"] or mcs_recovery_core._clean() is not True:
            return False
        committed = mcs_recovery_core._git(["show", head + ":mcs/core/ledger.py"])
        if committed is None or committed.returncode != 0 \
                or committed.stdout.encode("utf-8") != raw:
            return False
        return (mcs_recovery_core._head() == head and mcs_recovery_core._clean() is True
                and _db_version(mcs_recovery_core.LEDGER) == values[0].value)
    except (OSError, ValueError, SyntaxError, RecursionError):
        return False


def _restore_reconcile_pending():
    """Keep the existing incomplete flag; the durable restore witness holds on save failure."""
    state = mcs_recovery_core._load_state()
    applying = state.get("applying")
    if not state.get("_corrupt") and isinstance(applying, dict):
        applying["restore_incomplete"] = True
        try:
            mcs_recovery_core._save_state(state)
        except OSError:
            pass  # never authorize resume because the journal could not be saved
    return "restore_reconcile_pending"


def _db_version(path):
    try:
        con = sqlite3.connect(
            Path(path).resolve().as_uri() + "?mode=ro", uri=True)
        try:
            return con.execute("PRAGMA user_version").fetchone()[0]
        finally:
            con.close()
    except sqlite3.Error:
        return None


def _mark_restored(backup_path, phase="restored", report_id=None):
    """restore_pending marker — the runner holds every send grant
    until its journal-vs-DB reconcile consumes this. Written BEFORE
    the file swap (a crash between replace and write must not leave
    senders running against a rewound DB; a stale marker is harmless).
    Duplicated from notify_cards.mark_restored because this script
    must keep working when the repo's own modules are broken.
    phase='awaiting_consent' holds sends while the schema-bump DB
    replace waits on a bound ops.restore_approve receipt."""
    marker = os.path.join(mcs_recovery_core.DATA, "restore_pending.json")
    payload = {"v": 1, "phase": phase, "backup_path": backup_path,
               "by": "mcs_recover", "at": time.time()}
    if phase == "restored":
        payload["restored_at"] = payload["at"]
    if report_id is not None:
        payload["report_id"] = report_id
    mcs_recovery_gateway._atomic_json(marker, payload, ".restore.", sort_keys=True)


_STORED_TABLES = ("messages", "attachments")


RESTORE_LOSS_NOTICE = (
    "件数は対象表の行数差です。訂正、同数の追加と削除、配送状態の変更や対象外のデータは分かりません。"
    "0件でも損失がないとは言えません。"
    "復元で推論の使用履歴や費用の記録が失われる場合があります。復元への同意は外部推論の再開を意味しません。"
)


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
            ('released_intents', 'artifacts', 'semantic_intake_released_v1'),
            ('release_receipts', 'artifacts', 'semantic_intake_release_receipt_v1'),
            ('archived_intake', 'fetch_jobs', 'semantic_intake_archive_v1'),
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


def _file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1 << 20)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _loss_report(backup_path):
    """Same deterministic loss report as restore_evidence.create_loss_report
    — a drift between report and live state invalidates every consent
    receipt bound to the stale report_id."""
    try:
        live = sqlite3.connect(
            Path(mcs_recovery_core.LEDGER).resolve().as_uri() + "?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        back = sqlite3.connect(
            Path(backup_path).resolve().as_uri() + "?mode=ro", uri=True)
    except sqlite3.Error:
        live.close()
        return None
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
    finally:
        live.close()
        back.close()
    metrics = {"v": 1,
               "live_content_sha256": live_digest,
               "backup_sha256": _file_sha256(backup_path),
               "backup_schema": _db_version(backup_path),
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
    with suppress(OSError):             # report file is best-effort
        mcs_recovery_gateway._atomic_json(os.path.join(mcs_recovery_core.DATA, "restore_report.json"), report,
                     ".rreport.", ensure_ascii=False, sort_keys=True)
    return report


_MAX_RECEIPT_BYTES = 16384      # = mcs_requests.MAX_COMMAND_BYTES


def _approval_receipt(raw, command_id):
    """= mcs_requests.read_scheduled_receipt, self-contained (this tool must run
    without the checkout): a scheduled receipt without ambiguous JSON
    (duplicate keys, NaN/Infinity, oversized) or contradictory identity."""
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate_key")
            result[key] = value
        return result

    def constant(_):
        raise ValueError("invalid_number")

    try:
        if len(raw) > _MAX_RECEIPT_BYTES:
            return None
        receipt = json.loads(raw, object_pairs_hook=pairs,
                             parse_constant=constant)
    except (ValueError, TypeError, RecursionError):
        return None
    if (not isinstance(command_id, str) or not command_id.strip()
            or not isinstance(receipt, dict) or receipt.get("scheduled") is not True
            or receipt.get("command_id", command_id) != command_id
            or receipt.get("outcome", "applied") != "applied"
            or receipt.get("error") is not None):
        return None
    return receipt


def _consent_for(report):
    """The newest ops.restore_approve receipt bound to this exact loss
    report, or None — an earlier update/rollback approval never counts."""
    try:
        con = sqlite3.connect(
            Path(mcs_recovery_core.LEDGER).resolve().as_uri() + "?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    found = None
    try:
        rows = con.execute(
            "SELECT command_id, receipt_json FROM command_receipts"
            " WHERE outcome='applied'"
            " ORDER BY processed_at DESC, rowid DESC")
        for cid, rj in rows:
            if found is None and _bound_consent(rj, cid, report):
                found = cid   # newest bound receipt; keep reading (unparsed)
        # so an unreadable later row still fails closed, as fetchall() did
    except sqlite3.Error:
        return None
    finally:
        con.close()
    return found


def _bound_consent(raw, command_id, report):
    """= restore_evidence.bound_restore_consent."""
    rec = _approval_receipt(raw, command_id)
    return (rec is not None and rec.get("cmd") == "ops.restore_approve"
            and type(rec.get("backup_schema")) is int
            and rec.get("report_id") == report["report_id"]
            and rec.get("backup_sha256") == report["backup_sha256"]
            and rec.get("backup_schema") == report["backup_schema"])


_STAGING_NAME = re.compile(r"\.restore\.[A-Za-z0-9_]{8}\.db")


_STAGING_KEYS = ("dev", "ino", "uid")


def _staging_record(state):
    """(state, applying, record) of a usable journal — OSError otherwise."""
    if state.get("_corrupt"):
        raise OSError("restore_staging_journal_corrupt")
    applying = state.get("applying")
    if not isinstance(applying, dict):
        raise OSError("restore_staging_unjournaled")
    record = applying.get("restore_staging")
    if record is not None and not (
            isinstance(record, dict) and set(record) == {"name", *_STAGING_KEYS}
            and isinstance(record["name"], str) and _STAGING_NAME.fullmatch(record["name"])
            and all(type(record[key]) is int for key in _STAGING_KEYS)):
        raise OSError("restore_staging_record_invalid")
    return applying, record


def _staging_matches(dfd, record):
    """None when the recorded name is gone; whether it is provably ours."""
    try:
        st = os.stat(record["name"], dir_fd=dfd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    return (stat.S_ISREG(st.st_mode) and st.st_nlink == 1
            and st.st_uid == os.getuid() == record["uid"]
            and (st.st_dev, st.st_ino) == (record["dev"], record["ino"]))


def _settle_staging(directory):
    """Under both locks, before a new copy: remove the recorded copy of an
    interrupted restore, or keep everything and refuse when it cannot be
    proven to be that copy."""
    state = mcs_recovery_core._load_state()
    applying, record = _staging_record(state)
    if record is None:
        return
    dfd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        matches = _staging_matches(dfd, record)
        if matches is False:
            raise OSError("restore_staging_identity_changed")
        if matches:
            os.unlink(record["name"], dir_fd=dfd)
            os.fsync(dfd)
    finally:
        os.close(dfd)
    applying.pop("restore_staging")
    mcs_recovery_core._save_state(state)


def _release_staging(directory, record, journaled=True):
    """After this attempt: remove our copy if it is still exactly ours and
    drop its record; a mismatch keeps both for a human."""
    dfd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        matches = _staging_matches(dfd, record)
        if matches is False:
            return
        if matches:
            os.unlink(record["name"], dir_fd=dfd)
            os.fsync(dfd)
    finally:
        os.close(dfd)
    if not journaled:
        return
    state = mcs_recovery_core._load_state()
    applying, current = _staging_record(state)
    if current == record:
        applying.pop("restore_staging")
        mcs_recovery_core._save_state(state)


def _replace_database(backup_path, expected_sha, before_replace):
    """Stage and verify the backup before checkpointing and replacing the live DB.
    The staging copy is journaled (applying.restore_staging) before any byte
    of the backup is copied; there is no unjournaled fallback."""
    directory = os.path.dirname(mcs_recovery_core.LEDGER)
    _settle_staging(directory)
    fd, temporary = tempfile.mkstemp(dir=directory, prefix=".restore.", suffix=".db")
    try:
        created = os.fstat(fd)
    except BaseException:
        os.close(fd)
        raise
    own = {"name": os.path.basename(temporary), "dev": created.st_dev,
           "ino": created.st_ino, "uid": created.st_uid}
    journaled = False
    try:
        with mcs_recovery_gateway._fdopen_owned(fd, "wb") as dst:
            state = mcs_recovery_core._load_state()
            applying, _ = _staging_record(state)
            applying["restore_staging"] = own
            mcs_recovery_core._save_state(state)
            journaled = True
            with open(backup_path, "rb") as src:
                shutil.copyfileobj(src, dst)
            dst.flush()
            os.fsync(dst.fileno())
        if _file_sha256(temporary) != expected_sha:
            raise OSError("backup_changed_during_restore")
        candidate = sqlite3.connect(
            Path(temporary).resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
        try:
            if candidate.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise OSError("backup_integrity_failed")
        finally:
            candidate.close()
        # Preserve committed WAL contents even if the subsequent replace fails.
        # SQLite owns journal removal; never unlink a live WAL by hand.
        live = sqlite3.connect(Path(mcs_recovery_core.LEDGER).resolve().as_uri() + "?mode=rw",
                               uri=True, timeout=5)
        try:
            if live.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] != 0:
                raise OSError("live_checkpoint_busy")
            if live.execute("PRAGMA journal_mode=DELETE").fetchone()[0] != "delete":
                raise OSError("live_journal_busy")
        finally:
            live.close()
        # = mcs_update._replace_database: same lexists sidecar rule, kept as an
        # independent copy (recovery must run without the repo's modules)
        if any(os.path.lexists(mcs_recovery_core.LEDGER + suffix)
               for suffix in ("-wal", "-shm", "-journal")):
            raise OSError("live_sidecars_remaining")
        before_replace()
        os.replace(temporary, mcs_recovery_core.LEDGER)
        dfd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except sqlite3.Error as exc:
        raise OSError("restore_database_unverifiable") from exc
    finally:
        with suppress(OSError):          # never mask the restore's own failure
            _release_staging(directory, own, journaled)


def _record_hold(state, report, backup_path):
    """Journal the hold like mcs_update._consent_hold (same record) — a
    later pass of either tool then sees state['restore_consent'] and
    keeps the freeze on a git failure instead of escalating."""
    state["restore_consent"] = {
        "report_id": report["report_id"], "backup_path": backup_path,
        "backup_sha256": report["backup_sha256"],
        "backup_schema": report["backup_schema"],
        "intervening_messages": report["intervening_messages"],
        "external_effect_rows": report["external_effect_rows"],
        "at": time.time()}
    mcs_recovery_core._save_state(state)


def _restore_db(backup_path, on_hold=None):
    """Restore only when the live schema differs, with a verified staged copy.
    Checkpoint the live DB before replacement to preserve committed WAL data.
    Returns an error string for the caller to escalate, or None on a
    completed or verified-skipped restore — an unreadable backup during
    rollback recovery is never a silent 'nothing to do' (the mcs_update
    copy escalates backup_invalid in the same situation; a silent skip
    here would leave OLD code running against a NEWER schema while the
    report claims success)."""
    mcs_recovery_core._check_recovery_root()
    marker = os.path.join(mcs_recovery_core.DATA, "restore_pending.json")
    try:
        current = mcs_recovery_gateway._read_local_json(marker)
    except FileNotFoundError:
        if os.path.lexists(marker):
            return "restore_marker_unreadable"
        current = None
    except (OSError, ValueError, RecursionError):
        return "restore_marker_unreadable"
    else:
        if not isinstance(current, dict) or not (
                current.get("phase") in ("restored", "awaiting_consent")
                or ("phase" not in current and "restored_at" in current)):
            return "restore_marker_unreadable"
    live = _db_version(mcs_recovery_core.LEDGER)
    back = _db_version(backup_path)
    # SQLite opens a zero-byte file as an empty schema-0 DB; reject it like ledger.valid_mcs_db
    if back is None or not os.path.isfile(backup_path) \
            or os.path.getsize(backup_path) == 0:
        return "backup_unreadable: " + str(backup_path)
    if live is None:
        return "live_db_unreadable"
    if live == back:
        # already at backup schema — but a crash between the marker's
        # 'restored' rewrite and the swap can leave an awaiting_consent
        # marker pinning every send; promote it so the runner-side
        # reconcile releases the hold.
        if isinstance(current, dict) \
                and current.get("phase") == "awaiting_consent":
            try:
                _mark_restored(backup_path)
            except OSError as e:
                return f"restore_marker_failed: {e}"
        if mcs_recovery_core._unreconciled_restore() is not None and not _restored_writer_compatible():
            return _restore_reconcile_pending()
        return None
    # Per-restore human consent (R2): an earlier update/rollback
    # approval does not substitute. The sender hold lands before the
    # loss report so no grant slips between measurement and decision.
    try:
        _mark_restored(backup_path, phase="awaiting_consent")
    except OSError as e:
        return f"restore_marker_failed: {e}"
    try:
        report = _loss_report(backup_path)
    except (OSError, sqlite3.Error):
        return "restore_report_failed: cannot measure loss"
    if report is None:
        return "restore_report_failed: cannot measure loss"
    try:
        # re-pin the hold with the report identity so operators can
        # correlate marker <-> restore_report.json
        _mark_restored(backup_path, phase="awaiting_consent",
                       report_id=report["report_id"])
    except OSError as e:
        return f"restore_marker_failed: {e}"
    if on_hold is not None:
        # Persist the freeze even for an already approved restore: the swap
        # can still fail after its 'restored' marker has been written.
        on_hold(report)
    if _consent_for(report) is None:
        return "restore_consent_pending:" + report["report_id"]
    try:
        _replace_database(backup_path, report["backup_sha256"],
                          lambda: _mark_restored(
                              backup_path, report_id=report["report_id"]))
    except OSError as e:
        with suppress(OSError):
            _mark_restored(backup_path, phase="awaiting_consent",
                           report_id=report["report_id"])
        return f"restore_failed: {e}"
    if _db_version(mcs_recovery_core.LEDGER) != back:
        return "restore_verify_failed"
    # The compatible restored runner reconciles journals before delivery.
    # Keep the marker; never open this DB with the watchdog's/newer writer.
    return None if _restored_writer_compatible() else _restore_reconcile_pending()
