"""Regression: once a re-login fails during the initial import, the run
stops instead of re-submitting the login for every remaining patient."""

import json
import sys
import time

import mcs_adapter


class _ExpiredAdapter:
    def __init__(self):
        self.fetches = self.logins = 0

    def list_projects(self):
        out = []
        for pid in (11, 12):
            p = mcs_adapter.UnreadPatient(
                project_id=pid, project_type="medical",
                patient_name="synthetic patient", disease="d",
                station_name="s", url="u")
            p.last_activity = int(time.time())
            out.append(p)
        return out

    def auto_login(self, **kw):
        self.logins += 1
        return "failed"

    def set_deadline(self, deadline):
        pass

    def fetch_history(self, pid, since, max_pages=1, start_page=1):
        self.fetches += 1
        return mcs_adapter.MessageBatch(
            [], pages=0, reached=False,
            error=mcs_adapter.SessionExpired("expired"))


def test_failed_relogin_is_not_retried_per_patient(
        monkeypatch, tmp_path, capsys):
    import init_data
    adapter = _ExpiredAdapter()
    monkeypatch.setattr(init_data, "MCSAdapter", lambda **kw: adapter)
    monkeypatch.setattr(init_data, "DB", str(tmp_path / "ledger.db"))
    monkeypatch.setattr(init_data, "LOCKFILE", str(tmp_path / "run.lock"))
    monkeypatch.setattr(sys, "argv", [
        "init_data.py", "--days", "1", "--delay", "0", "--pages", "1"])
    rc = init_data.main()
    result = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert adapter.logins == 1 and adapter.fetches == 1
    assert rc == 1 and not result["ok"]
    assert "history 11: auto_login=failed" in result["errors"]
