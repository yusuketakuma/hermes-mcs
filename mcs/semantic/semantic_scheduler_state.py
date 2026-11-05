"""Read and update bounded persistent semantic scheduler state."""
from __future__ import annotations
import json
from contextlib import contextmanager
import math
import time
from mcs_util import loads_dict, strict_json
from semantic_policy import (SCHED_KIND)

from semantic_store import _REPROCESS_JOB_MAX_BYTES as _REPROCESS_JOB_MAX_BYTES

def _sched_state(ledger) -> dict:
    """Persisted fair-schedule accounting — one fetch_jobs row
    (kind='semantic_sched', project_id/message_id 0, state='done' so no
    drain ever claims it) keeps the cohort share and progress counters
    restart-safe instead of living in process memory (T14)."""
    row = ledger.db.execute(
        "SELECT job_id FROM fetch_jobs WHERE kind=? "
        "AND project_id=0 AND message_id=0", (SCHED_KIND,)).fetchone()
    return (loads_dict(_bounded_job_payload(ledger.db, row['job_id'],
                                            _REPROCESS_JOB_MAX_BYTES)) or {}) if row else {}


def _sched_write(ledger, state: dict, *, commit=True) -> None:
    now = time.time()
    payload = json.dumps(state, sort_keys=True)
    if len(payload.encode('utf-8')) > _REPROCESS_JOB_MAX_BYTES:
        raise ValueError('semantic_scheduler_invalid')
    ledger.db.execute("""
      INSERT INTO fetch_jobs(kind,project_id,message_id,parent_id,
        payload,state,next_try,created_at,updated_at)
      VALUES(?,0,0,NULL,?,'done',0,?,?)
      ON CONFLICT(kind,project_id,message_id) DO UPDATE SET
        payload=excluded.payload,updated_at=excluded.updated_at
    """, (SCHED_KIND, payload, now, now))
    if commit:
        ledger.db.commit()


@contextmanager
def _sched_update(ledger):
    """Serialize a short scheduler read/modify/write against concurrent owners."""
    if ledger.db.in_transaction:
        raise ValueError('semantic_scheduler_transaction_active')
    ledger.db.execute('BEGIN IMMEDIATE')
    with ledger.db:
        state, _ = _reservation_sched(ledger)
        yield state
        _sched_write(ledger, state, commit=False)


def _bounded_job_payload(db, job_id, limit):
    if (getattr(db, 'text_factory', str) is not str
            or not callable(getattr(db, 'blobopen', None))
            or db.execute('PRAGMA encoding').fetchone()[0].upper() != 'UTF-8'):
        return None
    row = db.execute('SELECT _rowid_,typeof(payload) FROM fetch_jobs WHERE job_id=?',
                     (job_id,)).fetchone()
    if row is None or row[0] != job_id or row[1] != 'text':
        return None
    with db.blobopen('fetch_jobs', 'payload', job_id, readonly=True) as blob:
        size = len(blob)
        if size > limit:
            return None
        raw = blob.read(size)
        if len(raw) != size:
            return None
    try:
        return raw.decode('utf-8')
    except UnicodeError:
        return None


def _reservation_sched(ledger):
    row = ledger.db.execute(
        "SELECT job_id FROM fetch_jobs WHERE kind=? AND project_id=0 AND message_id=0",
        (SCHED_KIND,)).fetchone()
    if row is None:
        return {}, {}
    raw = _bounded_job_payload(ledger.db, row['job_id'], _REPROCESS_JOB_MAX_BYTES)
    if raw is None:
        raise ValueError('semantic_scheduler_invalid')
    try:
        if len(raw.encode('utf-8')) > _REPROCESS_JOB_MAX_BYTES:
            raise ValueError('semantic_scheduler_invalid')
        sched = strict_json(raw)
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise ValueError('semantic_scheduler_invalid') from None
    if not isinstance(sched, dict):
        raise ValueError('semantic_scheduler_invalid')
    entries = sched.get('long_defers', {})
    if not isinstance(entries, dict) or any(
            not isinstance(key, str) or not key.isascii() or not key.isdigit()
            or len(key) > 19 or str(int(key)) != key or int(key) >= 2**63
            or not isinstance(value, dict) or set(value) != {'g', 'n'}
            or not isinstance(value['g'], str) or not value['g']
            or type(value['n']) is not int or not 0 <= value['n'] < 2**63 - 1
            for key, value in entries.items()):
        raise ValueError('semantic_scheduler_invalid')
    cursors = sched.get('payload_hold_cursors', {})
    if (not isinstance(cursors, dict) or any(
            key not in ('combined', 'realtime', 'backlog')
            or not _held_cursor_valid(value, None if key == 'combined' else key)
            for key, value in cursors.items())):
        raise ValueError('semantic_scheduler_invalid')
    return sched, dict(entries)


def _held_cursor_valid(value, lane):
    if (not isinstance(value, dict) or set(value) != {'job_id', 'rank'}
            or type(value['job_id']) is not int or not 0 < value['job_id'] < 2**63):
        return False
    rank = value['rank']
    return rank is None or (lane == 'realtime' and type(rank) in (int, float)
                            and abs(rank) < 2**63 and math.isfinite(rank))
