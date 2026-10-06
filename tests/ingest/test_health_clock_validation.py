"""Invalid injected clocks cannot make stale health evidence appear healthy."""
import json

import pytest

import health_watch
import run_check


@pytest.mark.parametrize("now", [float("nan"), float("inf"), float("-inf"), True, 10**400])
def test_watcher_rejects_invalid_clock_before_reading_or_persisting(tmp_path, now):
    data = tmp_path / "data"
    data.mkdir()
    (data / "health.json").write_text(json.dumps({"at": 1, "overall": "ok"}))
    with pytest.raises(ValueError, match="health_now_invalid"):
        health_watch.evaluate(home=str(tmp_path), now=now, cfg={})
    assert not (data / "health_watch.json").exists()


def test_cli_invalid_clock_is_rejected_before_configuration_read(tmp_path, monkeypatch):
    monkeypatch.setattr(health_watch, "load_config", lambda *args: pytest.fail("invalid clock must fail first"))
    with pytest.raises(SystemExit) as error:
        health_watch.main(["--home", str(tmp_path), "--now", "nan"])
    assert error.value.code == 2


def test_first_jobs_only_health_cannot_claim_unread_freshness(tmp_path, monkeypatch):
    monkeypatch.setattr(run_check, "_prev_health", lambda: {})
    unread_at = run_check._unread_at({"jobs_only": True}, "ok", 2000)
    assert unread_at is None
    path = tmp_path / "synthetic-health.json"
    path.write_text(json.dumps({"at": 2000, "unread_at": unread_at,
                                "overall": "ok", "state_reasons": []}))
    observed = health_watch.classify_health(str(path), 2001, 1800)
    assert observed["status"] == "degraded"
    assert "unread_collection_unknown" in observed["state_reasons"]
