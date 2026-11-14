"""Prove, approve and permit the local rebuild of paid reextract units whose answer was never stored.

A unit qualifies only when its whole retained cloud history (every model and unit key of
the message) verifies, every reservation predates input binding (the key is absent) and
settled as exactly one successful HTTP 200 result whose stored answer is the empty object.
A human approves a bounded bundle of such units; the approval grants a local attempt from
the current source only, never a cloud send, reservation or change to accounting.
"""
import math
import os
import sqlite3
from types import SimpleNamespace

import cloud_policy
import inference_dependency_loss
from extract_llm_qc import pending_pred
from extract_llm_source_contract import EXTRACT_VERSION, KIND
from mcs_util import payload_hash, strict_json, valid_hash

# Proposed initial bundle size. Raise it only with dry-run evidence that a bundle of
# the new size verifies every unit's whole history inside the shared read deadline.
BUNDLE_LIMIT = 20
COMMAND = 'ops.cloud_local_rebuild_approve'
RECEIPT_KIND = 'cloud_local_rebuild_approval'
PLAN_SCHEMA = 'cloud_local_rebuild_plan_v1'
ROUTE = 'reextract'
# Why a candidate is not eligible (fixed set; never identifiers or values).
EXCLUSIONS = frozenset({
    'source_missing', 'already_current', 'tally_invalid', 'history_pruned', 'no_history',
    'receipt_inconsistent', 'binding_present', 'disposition', 'result_missing',
    'not_success', 'answer_stored', 'unverified'})
# What a unit's approval means right now (permit). Only 'active' allows local work.
UNAPPROVED, ACTIVE, ADOPTED = 'unapproved', 'active', 'adopted'
PROOF_CHANGED, UNVERIFIED = 'local_rebuild_proof_changed', 'local_rebuild_unverified'
PERMIT_STATES = frozenset({UNAPPROVED, ACTIVE, ADOPTED, PROOF_CHANGED, UNVERIFIED})
_USAGE_KINDS = ('cloud_usage_reserve', 'cloud_usage_result', 'cloud_usage_disposition')


class _Excluded(Exception):
    pass


def _history_pruned(db, message_id):
    """Retention may have deleted this message's oldest usage rows: fail closed."""
    marks = [row[0] for row in db.execute(
        "SELECT content FROM artifacts WHERE kind=?", (cloud_policy.PRUNE_MARK_KIND,))]
    if not marks:
        return False
    try:
        cutoffs = [float(mark) for mark in marks]
    except (TypeError, ValueError):
        return True
    if len(cutoffs) != 1 or not math.isfinite(cutoffs[0]):
        return True
    seen = db.execute("SELECT first_seen FROM messages WHERE message_id=?", (message_id,)).fetchone()
    first = seen[0] if seen else None
    return type(first) not in (int, float) or not math.isfinite(first) or first < cutoffs[0]


def _check_unit(ledger, project_id, message_id):
    """-> (source_binding, history digest); raises _Excluded(reason)."""
    db = ledger.db
    binding = inference_dependency_loss.source_binding(db, project_id, message_id)
    if not isinstance(binding, str) or not binding:
        raise _Excluded('source_missing')
    pending = db.execute(f"SELECT ({pending_pred(db)}) FROM messages m WHERE m.project_id=? AND m.message_id=?",
                         (project_id, message_id)).fetchone()
    if pending is None or not pending[0]:
        raise _Excluded('already_current')
    with cloud_policy._accounting_snapshot(db):
        if cloud_policy._tally(ledger).invalid:
            raise _Excluded('tally_invalid')
        if _history_pruned(db, message_id):
            raise _Excluded('history_pruned')
        try:
            rows = cloud_policy._verified_request_history(
                ledger, route=ROUTE, project_id=project_id, message_id=message_id, unit_key=str(message_id))
        except cloud_policy.AnthropicError:
            raise _Excluded('receipt_inconsistent') from None
        if not rows:
            raise _Excluded('no_history')
        history = db.execute(
            "SELECT artifact_id,kind,model,meta,content FROM artifacts WHERE kind IN (?,?,?) "
            "AND project_id=? AND message_id IS ? ORDER BY artifact_id",
            (*_USAGE_KINDS, project_id, message_id)).fetchall()
        results, discarded = {}, set()
        try:
            for row in history:
                if row['kind'] == 'cloud_usage_reserve':
                    continue
                meta = cloud_policy._receipt_meta(row['meta'])
                rid = meta.get('reservation_id')
                if not isinstance(rid, str):
                    raise _Excluded('receipt_inconsistent')
                if row['kind'] == 'cloud_usage_disposition':
                    discarded.add(rid)
                else:
                    results.setdefault(rid, []).append((meta, row))
            for row in rows:
                reservation = cloud_policy._receipt_meta(row['meta'])
                if reservation.get('route') != ROUTE:
                    raise _Excluded('receipt_inconsistent')
                if 'input_binding' in reservation:
                    raise _Excluded('binding_present')
                rid = reservation['reservation_id']
                if rid in discarded:
                    raise _Excluded('disposition')
                matches = results.get(rid, [])
                if not matches:
                    raise _Excluded('result_missing')
                if len(matches) != 1:
                    raise _Excluded('receipt_inconsistent')
                maximum = cloud_policy._reserve_valid(reservation, row)
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
        except _Excluded:
            raise
        except (cloud_policy.AnthropicError, ArithmeticError, ValueError, TypeError, KeyError, RecursionError):
            raise _Excluded('receipt_inconsistent') from None
        digest = payload_hash([[row['artifact_id'], row['kind'], row['model'], row['meta'], row['content']]
                               for row in history])
    return binding, digest


def unit_proof(ledger, project_id, message_id):
    """-> {'project_id','message_id','source_binding','extract_version','proof'} or raise ValueError(reason)."""
    try:
        binding, digest = _check_unit(ledger, project_id, message_id)
    except _Excluded as excluded:
        raise ValueError(excluded.args[0]) from None
    return {'project_id': project_id, 'message_id': message_id, 'source_binding': binding,
            'extract_version': EXTRACT_VERSION, 'proof': digest}


def _candidates(db):
    """Messages with a reextract reservation that predates input binding (key absent)."""
    return db.execute(
        "SELECT DISTINCT project_id,message_id FROM artifacts WHERE kind='cloud_usage_reserve' "
        "AND typeof(project_id)='integer' AND typeof(message_id)='integer' AND json_valid(meta) "
        "AND json_extract(meta,'$.route')=? AND json_type(meta,'$.input_binding') IS NULL "
        "ORDER BY message_id DESC", (ROUTE,)).fetchall()


def preview(ledger, *, limit=BUNDLE_LIMIT):
    """Read-only dry run: counts with fixed exclusion reasons and one bundle plan.

    The caller's reader enforces the deadline; an interrupted read leaves the rest
    unverified (never eligible) and marks the preview partial."""
    if type(limit) is not int or not 1 <= limit <= BUNDLE_LIMIT:
        raise ValueError('local_rebuild_limit_invalid')
    db = ledger.db
    excluded = dict.fromkeys(sorted(EXCLUSIONS), 0)
    units, rows, done, eligible, state = [], [], 0, 0, 'complete'
    try:
        approvals = approved_units(db)
        rows = _candidates(db)
        for project_id, message_id in rows:
            try:
                if _approval_state(ledger, project_id, message_id, approvals) in (ACTIVE, ADOPTED):
                    excluded['already_current'] += 1
                else:
                    units.append(unit_proof(ledger, project_id, message_id))
                    eligible += 1
            except ValueError as error:
                excluded[str(error) if str(error) in EXCLUSIONS else 'receipt_inconsistent'] += 1
            done += 1
    except sqlite3.OperationalError:
        # the reader's deadline interrupted: the rest stays unverified, never eligible
        state = 'partial'
        excluded['unverified'] += len(rows) - done
    units = units[:limit]
    plan = None
    if units:
        plan = {'schema': PLAN_SCHEMA, 'database': _database_identity(db), 'units': units}
        plan['bundle_digest'] = payload_hash({k: plan[k] for k in ('schema', 'database', 'units')})
    return {'state': state, 'candidates': len(rows), 'eligible': eligible,
            'excluded': excluded, 'bundle_limit': BUNDLE_LIMIT, 'plan': plan}


def _database_identity(db):
    path = db.execute('PRAGMA database_list').fetchone()[2]
    if not path:
        raise ValueError('local_rebuild_database_unverified')
    stat = os.stat(path)
    return [stat.st_dev, stat.st_ino]


def _valid_unit(unit):
    return (type(unit) is dict and set(unit) == {'project_id', 'message_id', 'source_binding',
                                                 'extract_version', 'proof'}
            and type(unit['project_id']) is int and unit['project_id'] > 0
            and type(unit['message_id']) is int and unit['message_id'] > 0
            and isinstance(unit['source_binding'], str) and 0 < len(unit['source_binding']) <= 128
            and type(unit['extract_version']) is int and valid_hash(unit['proof']))


def valid_plan(plan):
    if (type(plan) is not dict or set(plan) != {'schema', 'database', 'units', 'bundle_digest'}
            or plan['schema'] != PLAN_SCHEMA or type(plan['units']) is not list
            or not 1 <= len(plan['units']) <= BUNDLE_LIMIT
            or not all(_valid_unit(unit) for unit in plan['units'])
            or type(plan['database']) is not list or len(plan['database']) != 2
            or not all(type(value) is int for value in plan['database'])):
        return False
    keys = [(unit['project_id'], unit['message_id']) for unit in plan['units']]
    return len(set(keys)) == len(keys) and plan['bundle_digest'] == payload_hash(
        {k: plan[k] for k in ('schema', 'database', 'units')})


def validate_operation(req, base):
    """Operation rules for the shared human-confirmed envelope (projectless)."""
    from mcs_util import valid_text
    if req.get('project_id') is not None:
        return 'bad_project_id'
    if req.keys() - (base | {'reason', 'plan'}):
        return 'unknown_field'
    if not valid_text(req.get('reason'), 2000):
        return 'bad_reason'
    return None if valid_plan(req.get('plan')) else 'local_rebuild_plan_invalid'


def approve_tx(db, req, now):
    """Inside apply_command's BEGIN IMMEDIATE: re-verify every unit; any change rejects the bundle."""
    plan = req['plan']
    ledger = SimpleNamespace(db=db)
    try:
        if _database_identity(db) != plan['database']:
            return 'local_rebuild_database_changed', {}
        approvals = approved_units(db)
        for unit in plan['units']:
            if _approval_state(ledger, unit['project_id'], unit['message_id'], approvals) in (ACTIVE, ADOPTED):
                return 'local_rebuild_already_approved', {}
            if unit_proof(ledger, unit['project_id'], unit['message_id']) != unit:
                return 'local_rebuild_bundle_changed', {}
    except ValueError:
        return 'local_rebuild_bundle_changed', {}
    return None, {'kind': RECEIPT_KIND, 'reason': req['reason'], 'bundle_digest': plan['bundle_digest'],
                  'units': plan['units'], 'approved': len(plan['units'])}


def approved_units(db):
    """Latest applied approval per (project_id, message_id), read from command receipts."""
    units = {}
    for (raw,) in db.execute(
            "SELECT receipt_json FROM command_receipts WHERE outcome='applied' AND project_id IS NULL "
            "AND json_valid(receipt_json) AND json_extract(receipt_json,'$.kind')=? "
            "ORDER BY processed_at,command_id", (RECEIPT_KIND,)):
        try:
            receipt = strict_json(raw)
        except (ValueError, TypeError, RecursionError):
            receipt = None
        listed = receipt.get('units') if type(receipt) is dict else None
        if type(listed) is not list:
            continue   # a broken receipt grants nothing and hides nothing
        for unit in listed:
            if _valid_unit(unit):
                units[(unit['project_id'], unit['message_id'])] = {
                    **unit, 'command_id': receipt.get('command_id')}
    return units


def permit(ledger, project_id, message_id, *, approvals=None):
    """The shared local-rebuild verdict for one unit (one of PERMIT_STATES); an unreadable
    state is UNVERIFIED, so callers fail closed and never read it as a grant."""
    try:
        return _approval_state(ledger, project_id, message_id,
                               approved_units(ledger.db) if approvals is None else approvals)
    except sqlite3.Error:
        return UNVERIFIED


def _approval_state(ledger, project_id, message_id, approvals):
    unit = approvals.get((project_id, message_id))
    if unit is None:
        return UNAPPROVED
    if unit['extract_version'] != EXTRACT_VERSION:
        return PROOF_CHANGED
    binding = inference_dependency_loss.source_binding(ledger.db, project_id, message_id)
    if binding != unit['source_binding']:
        return PROOF_CHANGED
    try:
        current = unit_proof(ledger, project_id, message_id)
    except ValueError as error:
        reason = str(error)
        if reason == 'already_current':
            return ADOPTED if adopted(ledger.db, unit) else UNAPPROVED
        return UNVERIFIED if reason in ('unverified', 'tally_invalid') else PROOF_CHANGED
    return ACTIVE if current['proof'] == unit['proof'] else PROOF_CHANGED


def adopted(db, unit):
    """A current final extract bound to this approval (never the receipt alone)."""
    return db.execute(
        "SELECT 1 FROM artifacts WHERE kind=? AND project_id=? AND message_id=? "
        "AND json_valid(meta) AND json_extract(meta,'$.local_rebuild.command_id')=? "
        "AND json_extract(meta,'$.local_rebuild.proof')=? "
        "AND json_extract(meta,'$.extract_version')=? LIMIT 1",
        (KIND, unit['project_id'], unit['message_id'], unit['command_id'], unit['proof'],
         EXTRACT_VERSION)).fetchone() is not None
