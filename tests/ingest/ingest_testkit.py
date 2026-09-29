"""Shared fixtures for the split ingestion suite — ledger handle and
``mcs_adapter`` record factories.  Not a test module (no ``test_``
prefix); sibling files import it via the tests/ sys.path bootstrap."""
import time
from datetime import datetime

import ledger
import mcs_adapter


def _ledger(tmp_path):
    return ledger.Ledger(str(tmp_path / "ledger.db"))


def _message(mid=1, body="body", state="full", project_id=1,
             parent_id=None, unread=False):
    return mcs_adapter.Message(
        message_id=mid, project_id=project_id, parent_id=parent_id,
        sender_id=1, sender_name="sender", sender_type="user",
        profession="", organization="", posted_at="2026-09-19T00:00:00+09:00",
        body_html=body, body_state=state, is_unread=unread,
        reply_count=0,
    )


def _unread_patient(pid, name="test patient"):
    return mcs_adapter.UnreadPatient(
        project_id=pid, project_type="medical", patient_name=name,
        disease="d", station_name="s", url="u")


def _att(file_id, name="f", url="https://www.medical-care.net/f"):
    return mcs_adapter.Attachment(file_id=file_id, name=name, url=url)


def _msg_at(mid, project_id, posted_at, unread=True):
    return mcs_adapter.Message(
        message_id=mid, project_id=project_id, parent_id=None,
        sender_id=1, sender_name="sender", sender_type="user",
        profession="", organization="", posted_at=posted_at,
        body_html="b", body_state="full", is_unread=unread,
        reply_count=0)


def _iso(days_ago):
    from datetime import timezone, timedelta
    return datetime.fromtimestamp(
        time.time() - days_ago * 86400,
        tz=timezone(timedelta(hours=9))).isoformat(timespec="seconds")
