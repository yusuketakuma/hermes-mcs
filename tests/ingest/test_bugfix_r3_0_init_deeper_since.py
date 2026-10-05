"""init_data: a deeper --since after a finished shallower walk must
re-walk from page 1 so the gap is fetched before the floor deepens."""

import sqlite3
import sys
import time
from datetime import datetime, timezone

import mcs_adapter

NOW = time.time()
MSGS = [{"id": 1000 - i,
         "created_at": datetime.fromtimestamp(
             NOW - i * 86400, timezone.utc).isoformat(),
         "comment": "synthetic", "user": {"id": 1, "type": "user"},
         "files": []} for i in range(1, 51)]


class _Adapter(mcs_adapter.MCSAdapter):
    def _get(self, path, params=None, extend_session=True):
        if path == "/projects":
            return {"projects": [{
                "id": 7, "type": "medical", "karte": {},
                "last_message": {"created_at": MSGS[0]["created_at"]}}],
                "paginate": {"has_next": False}}
        p, pp = params["page"], params["per_page"]
        return {"messages": MSGS[(p - 1) * pp:p * pp],
                "paginate": {"has_next": p * pp < len(MSGS)}}


def test_deeper_since_fetches_gap_before_floor(monkeypatch, tmp_path):
    import init_data
    ad = _Adapter()
    ad._token = "x" * 20
    monkeypatch.setattr(init_data, "MCSAdapter", lambda **kw: ad)
    db = str(tmp_path / "ledger.db")
    monkeypatch.setattr(init_data, "DB", db)
    monkeypatch.setattr(init_data, "LOCKFILE", str(tmp_path / "run.lock"))
    for days in ("14", "60"):
        monkeypatch.setattr(sys, "argv", [
            "init_data.py", "--days", days, "--delay", "0"])
        init_data.main()
    c = sqlite3.connect(db)
    assert c.execute("select count(*) from messages").fetchone()[0] == 50
    floor = c.execute("select history_floor from patients").fetchone()[0]
    assert floor <= NOW - 59 * 86400
