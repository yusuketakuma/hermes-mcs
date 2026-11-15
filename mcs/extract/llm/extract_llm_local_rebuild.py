"""Prove, approve and permit the local rebuild of paid reextract units whose answer was never stored.

A unit qualifies only when its whole retained cloud history (every model and unit key of
the message) verifies, every reservation predates input binding (the key is absent) and
settled as exactly one successful HTTP 200 result whose stored answer is the empty object,
no checkpoint row of any model, generation or state is left (a frame alone is an input and
does not count), and no valid material final exists. A human approves a bounded bundle of such units; the approval
grants a local attempt from the current source only, never a cloud send, reservation or
change to accounting.

Limit: retention writes its prune mark in the deleting transaction, so a database without
the mark has never pruned usage rows through that path; any mark at all fails closed, so
once retention has pruned, no further unit can be approved and every existing approval
turns into PROOF_CHANGED. A deletion outside that
maintenance path leaves no evidence and cannot be ruled out.
"""
from contextlib import contextmanager
import os
import re
import sqlite3
import time
from types import SimpleNamespace

import cloud_policy
import inference_dependency_loss
import inference_version as inference
import mcs_queries
from extract_llm_context import _context_contract
from extract_llm_qc import _current, pending_pred
from extract_llm_source_contract import KIND, _INFERENCE_VERSION
from mcs_queries import json_or_null
from mcs_util import payload_hash, strict_json, valid_hash, valid_text

# Proposed initial bundle size. Raise it only with dry-run evidence that a bundle of
# the new size verifies every unit's whole history inside APPROVE_SECONDS.
BUNDLE_LIMIT = 20
# The approval's re-verification budget inside the writer transaction, chosen below the
# resident workers' 3-second lock wait. Not a hard wall-time bound: SQL is interrupted at the
# deadline, but Python JSON/hash work between statements is not, so the final check rejects a
# bundle that finished late instead of guaranteeing the workers were never kept waiting.
APPROVE_SECONDS = 2.0
COMMAND = 'ops.cloud_local_rebuild_approve'
RECEIPT_KIND = 'cloud_local_rebuild_approval'
PLAN_SCHEMA = 'cloud_local_rebuild_plan_v2'
ROUTE = 'reextract'
# Why a candidate is not eligible (fixed set; never identifiers or values).
EXCLUSIONS = frozenset({
    'source_missing', 'already_current', 'not_pending', 'tally_invalid', 'history_pruned',
    'no_history', 'receipt_inconsistent', 'binding_present', 'disposition', 'result_missing',
    'not_success', 'answer_stored', 'checkpoint_present', 'unverified'})
# What a unit's approval means right now (permit). Only ACTIVE allows local work.
UNAPPROVED, ACTIVE, ADOPTED = 'unapproved', 'active', 'adopted'
PROOF_CHANGED, UNVERIFIED = 'local_rebuild_proof_changed', 'local_rebuild_unverified'
PERMIT_STATES = frozenset({UNAPPROVED, ACTIVE, ADOPTED, PROOF_CHANGED, UNVERIFIED})
# The states whose unit runs under its approval's own local guards and tag: an adopted unit that
# QC, a human report or a thin retry makes pending again is re-processed the same way.
RUNNABLE = frozenset({ACTIVE, ADOPTED})
_USAGE_KINDS = ('cloud_usage_reserve', 'cloud_usage_result', 'cloud_usage_disposition')


_ABSENT = object()


class _Excluded(Exception):
    pass


def _message(db, project_id, message_id):
    row = db.execute("SELECT * FROM messages WHERE project_id=? AND message_id=?",
                     (project_id, message_id)).fetchone()
    return dict(row) if row is not None else None


def _selected_final(db, row):
    """The final every reader selects for this message (the progress/material contract's row)."""
    return db.execute(
        "SELECT artifact_id,meta FROM artifacts WHERE kind=? AND message_id=? AND (project_id IS NULL OR project_id=?) AND "
        + inference.current_meta_pred("meta", target=_INFERENCE_VERSION)
        + " AND COALESCE(json_extract(" + json_or_null("meta") + ",'$.invalidated'),0)=0 "
        "ORDER BY artifact_id DESC LIMIT 1", (KIND, row["message_id"], row["project_id"])).fetchone()


def _valid_material(db, row):
    final = _selected_final(db, row)
    return final is not None and mcs_queries.selected_artifact_proof_valid(db, final[0], material=True)


def _chunk_tags(db, row):
    """The local_rebuild tag of every raw checkpoint row of the message (every model, generation
    and invalidated state, quality repair started/origin records included); None when untagged.
    A reader's empty answer is never taken as absence: the raw rows decide."""
    tags = []
    for (raw,) in db.execute("SELECT meta FROM artifacts WHERE kind='extract_llm_chunk' AND project_id=? "
                             "AND message_id=? ORDER BY artifact_id", (row["project_id"], row["message_id"])):
        try:
            meta = strict_json(raw) if isinstance(raw, str) else None
        except (ValueError, TypeError, RecursionError):
            meta = None
        tags.append(meta.get('local_rebuild') if type(meta) is dict else None)
    return tags


def _work_left(db, row, stop):
    """Initial eligibility: no checkpoint row of any kind is left. A frame alone is an input, not
    work, and never excludes."""
    if _chunk_tags(db, row):
        raise _Excluded('checkpoint_present')


def _tally_valid(ledger, stop=()):
    """The month's accounting tally verifies; checked once per dry run, approval or permit."""
    try:
        return not cloud_policy._tally(ledger).invalid
    except stop:
        raise
    except (cloud_policy.AnthropicError, ArithmeticError, ValueError, TypeError, KeyError):
        return False


def _history_pruned(db):
    """Any prune mark (or an unreadable one) fails closed; no mark means never pruned."""
    return db.execute("SELECT 1 FROM artifacts WHERE kind=? LIMIT 1",
                      (cloud_policy.PRUNE_MARK_KIND,)).fetchone() is not None


def _check_unit(ledger, project_id, message_id, stop=()):
    """-> (source_binding, history digest); raises _Excluded(reason). The caller has already
    verified the tally; `stop` are the reader's limit errors, never classified as a reason."""
    db = ledger.db
    row = _message(db, project_id, message_id)
    binding = inference_dependency_loss.source_binding(db, project_id, message_id)
    if row is None or not isinstance(binding, str) or not binding:
        raise _Excluded('source_missing')
    if _valid_material(db, row) or _current(ledger, message_id, row["content_hash"]):
        raise _Excluded('already_current')
    pending = db.execute(f"SELECT ({pending_pred(db)}) FROM messages m WHERE m.project_id=? AND m.message_id=?",
                         (project_id, message_id)).fetchone()
    if pending is None or not pending[0]:
        raise _Excluded('not_pending')
    _work_left(db, row, stop)
    return binding, _history_proof(ledger, project_id, message_id, stop)


def _history_proof(ledger, project_id, message_id, stop=()):
    """The usage-history proof alone (shared by eligibility and every later re-check): -> digest
    of the message's reextract usage rows; raises _Excluded(reason). The caller verified the tally."""
    db = ledger.db
    with cloud_policy._accounting_snapshot(db):
        if _history_pruned(db):
            raise _Excluded('history_pruned')
        try:
            rows = cloud_policy._verified_request_history(
                ledger, route=ROUTE, project_id=project_id, message_id=message_id, unit_key=str(message_id))
        except cloud_policy.AnthropicError:
            raise _Excluded('receipt_inconsistent') from None
        if not rows:
            raise _Excluded('no_history')
        history = db.execute(
            "SELECT artifact_id,kind,project_id,message_id,model,meta,content,created_at FROM artifacts "
            "WHERE kind IN (?,?,?) "
            "AND project_id=? AND message_id IS ? ORDER BY artifact_id",
            (*_USAGE_KINDS, project_id, message_id)).fetchall()
        results, discarded, routes, proven = {}, set(), {}, []
        try:
            # Every raw usage row is attributed through its own reservation: reextract rows (every
            # model, unit key, piece and repair) form the proof; rows that link to another route's
            # valid reservation are outside it; an unknown route, a broken reservation or an
            # orphan result/disposition fails closed instead of being filtered away.
            for item in history:
                if item['kind'] != 'cloud_usage_reserve':
                    continue
                meta = cloud_policy._receipt_meta(item['meta'])
                rid, route = meta.get('reservation_id'), meta.get('route')
                if (not isinstance(rid, str) or re.fullmatch(r'[0-9a-f]{32}', rid) is None
                        or not isinstance(route, str) or not route or rid in routes):
                    raise _Excluded('receipt_inconsistent')
                routes[rid] = route
                if route == ROUTE:
                    proven.append(item)
            for item in history:
                if item['kind'] == 'cloud_usage_reserve':
                    continue
                meta = cloud_policy._receipt_meta(item['meta'])
                rid = meta.get('reservation_id')
                if not isinstance(rid, str) or rid not in routes:
                    raise _Excluded('receipt_inconsistent')   # an orphan or malformed row is never ignored
                if routes[rid] != ROUTE:
                    continue
                proven.append(item)
                if item['kind'] == 'cloud_usage_disposition':
                    discarded.add(rid)
                else:
                    results.setdefault(rid, []).append((meta, item))
            for item in rows:
                reservation = cloud_policy._receipt_meta(item['meta'])
                if reservation.get('route') != ROUTE:
                    raise _Excluded('receipt_inconsistent')
                if 'input_binding' in reservation:
                    raise _Excluded('binding_present')
                fingerprint = reservation.get('logical_request_fp', _ABSENT)
                if fingerprint is not _ABSENT and not valid_hash(fingerprint):
                    raise _Excluded('receipt_inconsistent')   # an explicit null or malformed fp is not absent
                rid = reservation['reservation_id']
                if rid in discarded:
                    raise _Excluded('disposition')
                matches = results.get(rid, [])
                if not matches:
                    raise _Excluded('result_missing')
                if len(matches) != 1:
                    raise _Excluded('receipt_inconsistent')
                maximum = cloud_policy._reserve_valid(reservation, item)
                _, settled = cloud_policy._settled(reservation, maximum, matches)
                result, result_row = matches[0]
                if result.get('disposition'):
                    raise _Excluded('disposition')
                if not (settled and result.get('http_class') == 'http_200'
                        and result.get('usage') is not None and result.get('error_kind') is None):
                    raise _Excluded('not_success')
                raw = result_row['content']
                answer = strict_json(raw) if isinstance(raw, str) else None
                if type(answer) is not dict:
                    raise _Excluded('receipt_inconsistent')
                if answer:
                    raise _Excluded('answer_stored')
        except (_Excluded, *stop):
            raise
        except (cloud_policy.AnthropicError, ArithmeticError, ValueError, TypeError, KeyError, RecursionError):
            raise _Excluded('receipt_inconsistent') from None
        digest = payload_hash([[item['artifact_id'], item['kind'], item['model'], item['meta'], item['content']]
                               for item in sorted(proven, key=lambda item: item['artifact_id'])])
    return digest


def unit_proof(ledger, project_id, message_id, stop=()):
    """-> {'project_id','message_id','source_binding','contract','proof'} or raise ValueError(reason).
    Verify the tally first (_tally_valid); `stop` errors propagate unclassified."""
    try:
        binding, digest = _check_unit(ledger, project_id, message_id, stop)
    except _Excluded as excluded:
        raise ValueError(excluded.args[0]) from None
    return {'project_id': project_id, 'message_id': message_id, 'source_binding': binding,
            'contract': _context_contract(), 'proof': digest}


def _candidates(db):
    """Messages with a reextract reservation that predates input binding (key absent)."""
    return db.execute(
        "SELECT DISTINCT project_id,message_id FROM artifacts WHERE kind='cloud_usage_reserve' "
        "AND typeof(project_id)='integer' AND typeof(message_id)='integer' AND CASE WHEN json_valid(meta) "
        "THEN json_extract(meta,'$.route')=? AND json_type(meta,'$.input_binding') IS NULL END "
        "ORDER BY message_id DESC", (ROUTE,)).fetchall()


def preview(ledger, *, limit=BUNDLE_LIMIT, stop=(sqlite3.OperationalError,)):
    """Read-only dry run: counts with fixed exclusion reasons and one bundle plan.

    The caller's reader enforces the deadline and budget and names its limit errors in
    `stop`; hitting one leaves the rest unverified (never eligible) and the preview partial."""
    if type(limit) is not int or not 1 <= limit <= BUNDLE_LIMIT:
        raise ValueError('local_rebuild_limit_invalid')
    db = ledger.db
    excluded = dict.fromkeys(sorted(EXCLUSIONS), 0)
    units, rows, done, eligible, state = [], [], 0, 0, 'complete'
    stop = tuple(stop) + (sqlite3.OperationalError,)
    try:
        approvals = approved_units(db)
        rows = _candidates(db)
        tally = _tally_valid(ledger, stop)
        for project_id, message_id in rows:
            try:
                if not tally:
                    raise ValueError('tally_invalid')
                if _approval_state(ledger, project_id, message_id, approvals, stop, tally=tally) in (ACTIVE, ADOPTED):
                    excluded['already_current'] += 1
                else:
                    units.append(unit_proof(ledger, project_id, message_id, stop))
                    eligible += 1
            except stop:
                raise
            except ValueError as error:
                excluded[str(error) if str(error) in EXCLUSIONS else 'receipt_inconsistent'] += 1
            done += 1
    except stop:
        # the reader's deadline interrupted: the rest stays unverified, never eligible
        state = 'partial'
        excluded['unverified'] += len(rows) - done
    units = units[:limit]
    plan = None
    if units:
        plan = {'schema': PLAN_SCHEMA, 'database': _database_identity(db), 'units': units}
        plan['bundle_digest'] = _bundle_digest(plan)
    return {'state': state, 'candidates': len(rows), 'eligible': eligible,
            'excluded': excluded, 'bundle_limit': BUNDLE_LIMIT, 'plan': plan}


def _bundle_digest(plan):
    return payload_hash({key: plan[key] for key in ('schema', 'database', 'units')})


def _database_identity(db):
    path = db.execute('PRAGMA database_list').fetchone()[2]
    if not path:
        raise ValueError('local_rebuild_database_unverified')
    stat = os.stat(path)
    return [stat.st_dev, stat.st_ino]


def _valid_unit(unit):
    return (type(unit) is dict and set(unit) == {'project_id', 'message_id', 'source_binding', 'contract', 'proof'}
            and type(unit['project_id']) is int and unit['project_id'] > 0
            and type(unit['message_id']) is int and unit['message_id'] > 0
            and isinstance(unit['source_binding'], str) and 0 < len(unit['source_binding']) <= 128
            and valid_hash(unit['contract']) and valid_hash(unit['proof']))


def valid_plan(plan):
    if (type(plan) is not dict or set(plan) != {'schema', 'database', 'units', 'bundle_digest'}
            or plan['schema'] != PLAN_SCHEMA or type(plan['units']) is not list
            or not 1 <= len(plan['units']) <= BUNDLE_LIMIT
            or not all(_valid_unit(unit) for unit in plan['units'])
            or type(plan['database']) is not list or len(plan['database']) != 2
            or not all(type(value) is int for value in plan['database'])):
        return False
    keys = [(unit['project_id'], unit['message_id']) for unit in plan['units']]
    return len(set(keys)) == len(keys) and plan['bundle_digest'] == _bundle_digest(plan)


def validate_operation(req, base):
    """Operation rules for the shared human-confirmed envelope (projectless)."""
    if req.get('project_id') is not None:
        return 'bad_project_id'
    if req.keys() - (base | {'reason', 'plan'}):
        return 'unknown_field'
    if not valid_text(req.get('reason'), 2000):
        return 'bad_reason'
    return None if valid_plan(req.get('plan')) else 'local_rebuild_plan_invalid'


@contextmanager
def _bounded(db, seconds):
    """The approval's whole re-verification runs under one deadline (SELECTs only, so an
    interrupt never rolls back the caller's write transaction)."""
    deadline = time.monotonic() + seconds
    db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
    try:
        yield deadline
    finally:
        db.set_progress_handler(None, 0)


def approve_tx(db, req, now):
    """Inside apply_command's BEGIN IMMEDIATE: re-verify every unit; any change rejects the bundle."""
    plan = req['plan']
    ledger = SimpleNamespace(db=db)
    try:
        with _bounded(db, APPROVE_SECONDS) as deadline:
            if _database_identity(db) != plan['database']:
                return 'local_rebuild_database_changed', {}
            approvals = approved_units(db)
            if not _tally_valid(ledger):
                return UNVERIFIED, {}
            for unit in plan['units']:
                if time.monotonic() >= deadline:
                    return UNVERIFIED, {}
                if _approval_state(ledger, unit['project_id'], unit['message_id'], approvals,
                                   tally=True) in (ACTIVE, ADOPTED):
                    return 'local_rebuild_already_approved', {}
                if unit_proof(ledger, unit['project_id'], unit['message_id']) != unit:
                    return 'local_rebuild_bundle_changed', {}
            if time.monotonic() >= deadline:
                return UNVERIFIED, {}
    except sqlite3.OperationalError:
        return UNVERIFIED, {}   # the bundle could not be re-verified in time: reject it whole
    except (OSError, ValueError):
        return 'local_rebuild_bundle_changed', {}
    return None, {'kind': RECEIPT_KIND, 'reason': req['reason'], 'bundle_digest': plan['bundle_digest'],
                  'database': plan['database'], 'units': plan['units'], 'approved': len(plan['units'])}


def _receipt_units(command_id, digest, raw):
    """The units of one applied approval receipt, or None when any part fails to verify."""
    try:
        receipt = strict_json(raw) if isinstance(raw, str) and len(raw) <= 65536 else None
    except (ValueError, TypeError, RecursionError):
        return None
    if (type(receipt) is not dict or receipt.get('kind') != RECEIPT_KIND or receipt.get('outcome') != 'applied'
            or receipt.get('error') is not None or receipt.get('command_id') != command_id
            or receipt.get('payload_hash') != digest or not valid_text(receipt.get('actor'), 120)):
        return None
    plan = {'schema': PLAN_SCHEMA, 'database': receipt.get('database'), 'units': receipt.get('units'),
            'bundle_digest': receipt.get('bundle_digest')}
    if not valid_plan(plan) or receipt.get('approved') != len(plan['units']):
        return None
    # The stored payload hash must be the exact human-confirmed envelope that was applied.
    envelope = {'version': 1, 'cmd': COMMAND, 'command_id': command_id, 'actor': receipt['actor'],
                'human_confirmed': True, 'project_id': None, 'reason': receipt.get('reason'), 'plan': plan}
    if payload_hash(envelope) != digest:
        return None
    return [{**unit, 'command_id': command_id, 'database': plan['database']} for unit in plan['units']]


def approved_units(db):
    """Latest verified approval per (project_id, message_id); a broken receipt grants nothing."""
    units = {}
    for command_id, digest, raw in db.execute(
            "SELECT command_id,payload_hash,receipt_json FROM command_receipts WHERE outcome='applied' "
            "AND project_id IS NULL AND request_id IS NULL AND CASE WHEN json_valid(receipt_json) "
            "THEN json_extract(receipt_json,'$.kind')=? END ORDER BY processed_at,command_id", (RECEIPT_KIND,)):
        for unit in _receipt_units(command_id, digest, raw) or ():
            units[(unit['project_id'], unit['message_id'])] = unit
    return units


def permit(ledger, project_id, message_id, *, approvals=None):
    """The shared local-rebuild verdict for one unit (one of PERMIT_STATES); an unreadable
    state is UNVERIFIED, so callers fail closed and never read it as a grant."""
    try:
        return _approval_state(ledger, project_id, message_id,
                               approved_units(ledger.db) if approvals is None else approvals)
    except sqlite3.Error:
        return UNVERIFIED


def approval(ledger, project_id, message_id, *, approvals):
    """-> (permit state, the approved unit or None): the prepare-time verdict for one row."""
    try:
        return _approval_state(ledger, project_id, message_id, approvals), approvals.get((project_id, message_id))
    except sqlite3.Error:
        return UNVERIFIED, approvals.get((project_id, message_id))


def _approval_state(ledger, project_id, message_id, approvals, stop=(), *, tally=None):
    """`tally` is the caller's single check for its whole pass (None: check here once)."""
    unit = approvals.get((project_id, message_id))
    if unit is None:
        return UNAPPROVED
    try:
        if _database_identity(ledger.db) != unit['database']:
            return PROOF_CHANGED   # a copied or replaced database never inherits the approval
    except (OSError, ValueError):
        return UNVERIFIED
    if unit['contract'] != _context_contract():
        return PROOF_CHANGED
    if inference_dependency_loss.source_binding(ledger.db, project_id, message_id) != unit['source_binding']:
        return PROOF_CHANGED
    if not (_tally_valid(ledger, stop) if tally is None else tally):
        return UNVERIFIED
    # The usage history is re-proven before any verdict, adopted included: a new reservation
    # or a changed usage row after the local final lifts the cloud suppression.
    try:
        if _history_proof(ledger, project_id, message_id, stop) != unit['proof']:
            return PROOF_CHANGED
    except _Excluded:
        return PROOF_CHANGED   # e.g. a prune mark appeared: an approved unit stops qualifying
    row = _message(ledger.db, project_id, message_id)
    if row is None:
        return PROOF_CHANGED
    # After approval only this approval's own tagged work may exist, adopted or not; any other
    # row (untagged, another approval, broken meta) or a final it did not adopt invalidates it.
    if any(item != tag(unit) for item in _chunk_tags(ledger.db, row)):
        return PROOF_CHANGED
    if adopted(ledger.db, unit):
        return ADOPTED
    if _valid_material(ledger.db, row) or _current(ledger, message_id, row["content_hash"]):
        return PROOF_CHANGED
    return ACTIVE


def tag(unit):
    """The closed tag every local-rebuild write carries (checkpoints and the final)."""
    return {'command_id': unit['command_id'], 'proof': unit['proof']}


def adopted(db, unit):
    """The selected final itself is this approval's local result, still authentic for the current
    source and contract (the material proof without the adverse QC/human review, which decides
    re-processing, never the adoption itself); a tag on any other row, or the receipt alone,
    never counts."""
    row = _message(db, unit['project_id'], unit['message_id'])
    final = _selected_final(db, row) if row is not None else None
    if final is None:
        return False
    try:
        meta = strict_json(final[1])
    except (ValueError, TypeError, RecursionError):
        return False
    written = meta.get('local_rebuild') if type(meta) is dict else None
    return (written == tag(unit)
            and meta.get('hash') == row['content_hash']
            and mcs_queries.selected_artifact_proof_valid(db, final[0], material=True, adverse_review=False))
