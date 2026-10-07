"""Queue source-safe card updates after committed extraction progress, without sending to chat."""
import json
import sys

from mcs_requests import positive
from mcs_util import load_config


def refresh_extraction_cards(ledger, project_id, message_id, *, cfg=None):
    """Reuse the durable renderer after commit; in-flight sends coalesce at its existing gate."""
    db = getattr(ledger, "db", None)
    if db is None or getattr(db, "in_transaction", True) or not positive(project_id) or not positive(message_id):
        return []
    try:
        import notify_cards
        cfg = load_config() if cfg is None else cfg
        if not notify_cards.interactive_enabled(cfg):
            return []
        return notify_cards.rerender_message_cards(ledger, cfg, project_id, message_id)
    except Exception as error:
        # The committed checkpoint remains usable; the bounded sweep retries presentation.
        print(json.dumps({"event": "card_rerender_failed", "error": type(error).__name__}),
              file=sys.stderr)
        return []
