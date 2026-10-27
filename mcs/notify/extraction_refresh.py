"""Queue source-safe card updates after committed extraction progress, without sending to chat."""
import json
import sqlite3
import sys
import time

from mcs_requests import positive
from mcs_util import load_config, loads_dict


def mark_history_change_tx(db, project_id, message_id, *, now=None):
    """Record a history refresh on the caller's existing writer transaction."""
    if not getattr(db, 'in_transaction', False):
        raise ValueError('history_refresh_transaction_required')
    if not positive(project_id) or not positive(message_id):
        return
    source = db.execute(
        "SELECT j.payload, mm.content FROM messages m "
        "LEFT JOIN fetch_jobs j ON j.kind='semantic' AND j.project_id=m.project_id "
        "AND j.message_id=COALESCE(m.parent_id,m.message_id) "
        "LEFT JOIN message_metadata mm ON mm.message_id=m.message_id AND mm.source='capture' "
        "WHERE m.project_id=? AND m.message_id=?", (project_id, message_id)).fetchone()
    if source is None:
        return
    policy = loads_dict(source[0]) or {}
    if policy.get('reprocess_only') is True and policy.get('notification_free') is True:
        return
    role = ((loads_dict(source[1]) or {}).get('_initial_import') or {})
    if not isinstance(role, dict) or role.get('role') != 'history':
        return
    now = time.time() if now is None else now
    cards = db.execute("SELECT card_id,ui_state FROM notification_cards WHERE kind='patient_history' "
                       "AND project_id=? AND delivery_state!='revoked'", (project_id,)).fetchall()
    expected_cards = []
    for card in cards:
        ui = loads_dict(card['ui_state']) if card['ui_state'] else {}
        if ui is None or (ui.get('history_latest') is not None
                          and not isinstance(ui['history_latest'], dict)):
            raise RuntimeError('history_refresh_invalid_state')
        ui.setdefault('history_dirty_since', now)
        if (ui.get('history_latest') or {}).get('phase') == 'complete':
            ui['history_source_dirty'] = True
        expected = json.dumps(ui)
        db.execute("UPDATE notification_cards SET ui_state=? WHERE card_id=?", (expected, card['card_id']))
        expected_cards.append((card['card_id'], expected))
    if cards:
        import notify_cards
        notify_cards.mark_snapshot_dirty(db)
    for card_id, expected in expected_cards:
        actual = db.execute("SELECT ui_state FROM notification_cards WHERE card_id=?", (card_id,)).fetchone()
        if actual is None or actual[0] != expected:
            raise RuntimeError('history_refresh_unverified')


def refresh_extraction_cards(ledger, project_id, message_id, *, cfg=None):
    """Reuse the durable renderer after commit; in-flight sends coalesce at its existing gate."""
    db = getattr(ledger, "db", None)
    if db is None or getattr(db, "in_transaction", True) or not positive(project_id) or not positive(message_id):
        return []
    try:
        import notify_cards
        import notify_scope
        cfg = load_config() if cfg is None else cfg
        if not notify_scope.interactive_enabled(cfg):
            return []
        return notify_cards.rerender_message_cards(ledger, cfg, project_id, message_id)
    except Exception as error:
        if getattr(db, 'in_transaction', True) or isinstance(error, (sqlite3.Error, OSError)):
            raise
        # The atomic history marker and the existing bounded sweep retain the
        # work; log only the exception type, not its possibly private text.
        print(json.dumps({"event": "card_rerender_failed", "error": type(error).__name__}),
              file=sys.stderr)
        return []
