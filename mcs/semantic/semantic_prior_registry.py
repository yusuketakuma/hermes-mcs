"""Pre-deploy count of posts still missing a current-registry semantic assessment (N_sem)."""
from __future__ import annotations

import semantic_jev
import semantic_policy
import semantic_store
from mcs_util import loads_dict

_PAGE = 500

# Reprocess states a semantic pass can never take over; reported as one
# informational bucket (BLK-X plan X11c: 対象外として件数だけ出す).
_EXCLUDED_STATES = frozenset(
    ('permission', 'scope', 'deleted', 'empty_body', 'body_unavailable'))


def _primary_assessment_meta(db, project_id, message_id):
    """Newest post-level ``semantic_assess`` meta.

    Fact-detail assessments (``meta.fact_id`` set) are skipped like the
    reprocess pass does; a row whose meta cannot be read is unknown
    evidence and reported as ``'unknown'`` rather than silently skipped."""
    for stored in db.execute(
            'SELECT meta FROM artifacts WHERE kind=? AND project_id=? '
            'AND message_id=? ORDER BY artifact_id DESC',
            (semantic_policy.KIND_ASSESS, project_id, message_id)):
        meta = loads_dict(stored['meta'])
        if meta is None:
            return 'unknown'
        if meta.get('fact_id') is not None:
            continue
        return meta
    return None


def prior_registry_remaining(ledger, cfg):
    """配備前投稿で現行 registry の意味解析判定・成果物が無いものを状態別に数える（N_sem）。

    Read-only. The snapshot is bounded at the ``MAX(message_id)`` seen
    when the call starts, so arrivals during the scan never change the
    counted set. ``runnable`` is N_sem itself; the other keys are the
    breakdown of posts that do or could still need a redo."""
    scfg = semantic_policy.runtime_config(cfg)[0]
    db = ledger.db
    upper = db.execute('SELECT COALESCE(MAX(message_id),0) FROM messages').fetchone()[0]
    counts = {"runnable": 0, "held": 0, "failed": 0, "paused": 0,
              "excluded": 0, "unassessed": 0}
    cursor = 0
    while cursor < upper:
        rows = db.execute(
            'SELECT m.message_id,m.project_id,m.parent_id,m.body_state,m.body_text,'
            'p.project_id AS patient_id,p.fetch_reason FROM messages m '
            'LEFT JOIN patients p ON p.project_id=m.project_id '
            'WHERE m.message_id>? AND m.message_id<=? ORDER BY m.message_id LIMIT ?',
            (cursor, upper, _PAGE)).fetchall()
        if not rows:
            break
        for row in rows:
            cursor = row['message_id']
            state = semantic_store.reprocess_target_state(ledger, row, scfg, cfg=cfg)
            if state in _EXCLUDED_STATES:
                counts['excluded'] += 1
                continue
            meta = _primary_assessment_meta(db, row['project_id'], row['message_id'])
            if meta == 'unknown':
                counts['held'] += 1
                continue
            if meta is None:
                counts['unassessed'] += 1
                continue
            if meta.get('registry') == semantic_jev.REGISTRY_VERSION:
                continue
            if state in ('pending', 'repair_required'):
                counts['runnable'] += 1
            elif state in ('held', 'failed', 'paused'):
                counts[state] += 1
            # current / current_skipped / classification states already hold
            # a current artifact: not counted anywhere.
        if len(rows) < _PAGE:
            break
    return counts
