"""Plan one bounded patient scan without owning its durable bookmark."""
import math

import patient_priority

_MAX_ID = 2**63 - 1
_VERSION = 1


def _integer(value, *, minimum=0, maximum=_MAX_ID):
    return type(value) is int and minimum <= value <= maximum


def _activity(value):
    return value is None or (type(value) in (int, float) and -_MAX_ID <= value <= _MAX_ID
                             and math.isfinite(value))


def _bookmark(value):
    if value is None:
        return {'version': _VERSION, 'turn': 0, 'activity_after': None,
                'fair_after': 0, 'fair_upper': None}
    if not isinstance(value, dict) or set(value) != {
            'version', 'turn', 'activity_after', 'fair_after', 'fair_upper'}:
        raise ValueError('patient_scan_bookmark_invalid')
    after, upper, activity = value['fair_after'], value['fair_upper'], value['activity_after']
    if (type(value['version']) is not int or value['version'] != _VERSION
            or not _integer(value['turn'], maximum=3) or not _integer(after)
            or upper is not None and (not _integer(upper, minimum=1) or after > upper)
            or upper is None and after != 0):
        raise ValueError('patient_scan_bookmark_invalid')
    if activity is not None and (not isinstance(activity, list) or len(activity) != 3
            or not _integer(activity[0], maximum=1) or not _activity(activity[1])
            or not _integer(activity[2], minimum=1)):
        raise ValueError('patient_scan_bookmark_invalid')
    return {**value, 'activity_after': None if activity is None else list(activity)}


def advance_turn(patient_scan):
    """Reserve only the next turn before a cancellable candidate query."""
    state = _bookmark(patient_scan)
    state['turn'] = (state['turn'] + 1) % 4
    return state


def plan(db, patient_scan):
    """Read one patient and return the next bookmark for the caller's CAS."""
    state = _bookmark(patient_scan)
    turn = state['turn']
    state['turn'] = (turn + 1) % 4
    if turn == 3:
        upper, after = state['fair_upper'], state['fair_after']
        if upper is None:
            last = db.execute('SELECT project_id FROM patients WHERE project_id>0 '
                              'ORDER BY project_id DESC LIMIT 1').fetchone()
            if last is None:
                return {'project_id': None, 'next': state, 'exhausted': True, 'reason': 'empty'}
            upper = last[0]
            if not _integer(upper, minimum=1):
                raise ValueError('patient_scan_source_invalid')
            state['fair_upper'] = upper
        row = db.execute('SELECT project_id FROM patients WHERE project_id>? AND project_id<=? '
                         'ORDER BY project_id LIMIT 1', (after, upper)).fetchone()
        if row is None:
            state.update(fair_after=0, fair_upper=None)
            return {'project_id': None, 'next': state, 'exhausted': True, 'reason': 'cycle_end'}
        pid = row[0]
        if not _integer(pid, minimum=1) or not after < pid <= upper:
            raise ValueError('patient_scan_source_invalid')
        state['fair_after'] = pid
    else:
        latest = patient_priority.latest_activity_sql('p.project_id')
        archive = '(COALESCE(p.is_archived,0)!=0)'
        activity = state['activity_after']
        where, params = '', ()
        if activity is not None:
            archived, timestamp, pid = activity
            where = (f' AND ({archive}>? OR ({archive}=? AND '
                     f'((? IS NOT NULL AND ({latest}<? OR {latest} IS NULL)) '
                     f'OR ({latest} IS ? AND p.project_id>?))))')
            params = (archived, archived, timestamp, timestamp, timestamp, pid)
        row = db.execute(f'SELECT p.project_id,{archive},{latest} FROM patients p '
                         f'WHERE p.project_id>0{where} ORDER BY {archive}, '
                         + patient_priority.activity_order_sql('p.project_id') + ' LIMIT 1', params).fetchone()
        if row is None:
            state['activity_after'] = None
            return {'project_id': None, 'next': state, 'exhausted': True,
                    'reason': 'empty' if activity is None else 'cycle_end'}
        pid, archived, timestamp = row
        if (not _integer(pid, minimum=1) or not _integer(archived, maximum=1)
                or not _activity(timestamp)):
            raise ValueError('patient_scan_source_invalid')
        state['activity_after'] = [archived, timestamp, pid]
    return {'project_id': pid, 'next': state, 'exhausted': False, 'reason': 'candidate'}
