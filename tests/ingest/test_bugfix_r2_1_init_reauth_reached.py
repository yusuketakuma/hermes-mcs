"""init_data: a re-login on the final (reached) history chunk must retry
the chunk instead of exiting the walk with replies unfetched."""

import datetime
import json
import sqlite3
import sys
import time

import mcs_adapter


def _msg(mid, parent_id=None, reply_count=0):
    ts = (datetime.datetime.now() - datetime.timedelta(hours=1)).strftime(
        "%Y-%m-%dT%H:%M:%S+09:00")
    return mcs_adapter.Message(
        message_id=mid, project_id=7, parent_id=parent_id, sender_id=1,
        sender_name="u", sender_type="x", profession="", organization="",
        posted_at=ts, body_html="body", body_state="full", is_unread=False,
        reply_count=reply_count)


class _Adapter:
    def __init__(self):
        self.fetches = self.threads = self.logins = 0

    def list_projects(self):
        p = mcs_adapter.UnreadPatient(
            project_id=7, project_type="medical", patient_name="S T",
            disease="d", station_name="s", url="u")
        p.last_activity = int(time.time())
        return [p]

    def auto_login(self, **kw):
        self.logins += 1
        return "ok"

    def set_deadline(self, deadline):
        pass

    def fetch_history(self, pid, since, max_pages=1, start_page=1):
        self.fetches += 1
        return mcs_adapter.MessageBatch(
            [_msg(100, reply_count=1)], pages=1, reached=True)

    def fetch_thread(self, pid, mid, **kw):
        self.threads += 1
        if self.threads == 1:
            raise mcs_adapter.SessionExpired("expired")
        return [_msg(101, parent_id=100)]


def test_reauth_on_reached_chunk_retries_and_stores_reply(
        monkeypatch, tmp_path, capsys):
    import init_data
    adapter = _Adapter()
    monkeypatch.setattr(init_data, "MCSAdapter", lambda **kw: adapter)
    monkeypatch.setattr(init_data, "DB", str(tmp_path / "ledger.db"))
    monkeypatch.setattr(init_data, "LOCKFILE", str(tmp_path / "run.lock"))
    monkeypatch.setattr(sys, "argv", [
        "init_data.py", "--days", "1", "--delay", "0", "--pages", "1"])
    rc = init_data.main()
    result = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert adapter.logins == 1 and adapter.fetches == 2
    assert adapter.threads == 2
    c = sqlite3.connect(str(tmp_path / "ledger.db"))
    ids = {r[0] for r in c.execute("select message_id from messages")}
    assert 101 in ids
    assert rc == 0 and result["ok"]
