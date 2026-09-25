"""Shared synthetic fixtures for the extract test family — ledger
handle, ``mcs_adapter.Message`` factory, content-hash probe.  Not a
test module (no ``test_`` prefix); sibling files import it via the
tests/ sys.path bootstrap."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "mcs"))

import ledger
import mcs_adapter


def _ledger(tmp_path):
    return ledger.Ledger(str(tmp_path / "ledger.db"))


def _message(mid=1, body="body", state="full", project_id=1,
             parent_id=None, unread=False,
             posted_at="2026-09-19T00:00:00+09:00", profession=""):
    return mcs_adapter.Message(
        message_id=mid, project_id=project_id, parent_id=parent_id,
        sender_id=1, sender_name="sender", sender_type="user",
        profession=profession, organization="", posted_at=posted_at,
        body_html=body, body_state=state, is_unread=unread,
        reply_count=0,
    )


def _hash(db, mid=1):
    return db.db.execute(
        "SELECT content_hash FROM messages WHERE message_id=?",
        (mid,)).fetchone()[0]
