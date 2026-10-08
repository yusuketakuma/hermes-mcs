"""Unrenderable synthetic health times preserve classification and delivery witnesses."""
import copy
import json
from types import SimpleNamespace

import pytest

import health_watch


NOW = 1900000001
SCENARIOS = [(1900000000, 1e300, "failed"),
             (-1e300, 1900000000, "stale")]


def _health(tmp_path, at, last_ok):
    path = tmp_path / health_watch.HEALTH_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"at": at, "last_ok_at": last_ok,
                               "overall": "failed", "state_reasons": ["run_failed"]}))
    return path


@pytest.mark.parametrize("at,last_ok,status", SCENARIOS)
def test_extreme_stored_time_keeps_the_original_status_and_evidence(tmp_path, at, last_ok, status):
    path = _health(tmp_path, at, last_ok)
    report = health_watch.classify_health(str(path), NOW, 1800)
    report.update(alert=True, disk_alert=False)
    before = copy.deepcopy(report)
    lines = health_watch._alert_lines(report)
    assert report == before
    assert report["status"] == status
    assert report["health_at"] == at and report["last_ok_at"] == last_ok
    assert any("不明" in line for line in lines)
    assert any(f"コード: {status} " in line for line in lines)
    assert report["state_reasons"] == (["run_failed"] if status == "failed" else None)


@pytest.mark.parametrize("at,last_ok,status", SCENARIOS)
@pytest.mark.parametrize("outcome", [True, None])
def test_unknown_time_does_not_stop_alert_or_weaken_unknown_delivery_hold(
        tmp_path, monkeypatch, at, last_ok, status, outcome):
    _health(tmp_path, at, last_ok)
    sent = []

    def send(cfg, text):
        state = json.loads((tmp_path / health_watch.STATE_REL).read_text())
        assert state["delivery"]["outcome"] == "unknown"
        assert state["last"]["status"] == status
        assert state["last"]["health_at"] == at
        sent.append(text)
        return outcome

    monkeypatch.setattr(health_watch, "deliver_alert", send)
    assert health_watch._watch(SimpleNamespace(home=str(tmp_path), now=NOW), {}) == 0
    assert len(sent) == 1 and "不明" in sent[0]
    state = json.loads((tmp_path / health_watch.STATE_REL).read_text())
    report = json.loads((tmp_path / health_watch.STATUS_REL).read_text())
    expected = "delivered" if outcome is True else "unknown"
    assert state["delivery"]["outcome"] == report["delivery"]["outcome"] == expected
    assert report["status"] == status and report["last_ok_at"] == last_ok
    assert health_watch._watch(SimpleNamespace(home=str(tmp_path), now=NOW + 1), {}) == 0
    assert len(sent) == 1
