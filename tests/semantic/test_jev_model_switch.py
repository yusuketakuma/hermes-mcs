"""A Jev model is chosen by a probe-gated human command and only named, never switched, when newer."""
from contextlib import closing, contextmanager
import json
from types import SimpleNamespace

import pytest

from ledger import Ledger
import jev_model_setup
import jev_model_updates
import mcs_util
import provider_settings
import semantic_bench
import semantic_jev as jev
from semantic_policy import policy_fingerprint, semantic_config

NEXT = "jev-1.15.0"


def test_fixed_version_is_the_effective_model_everywhere():
    scfg, errors = semantic_config({"semantic": {"model": NEXT}})
    assert errors == [] and scfg["model"] == NEXT == jev.resolve_model(NEXT)
    assert policy_fingerprint(scfg) != policy_fingerprint(semantic_config({})[0])
    assert jev.JevClient(api_key="synthetic", model=NEXT).model == NEXT
    for retired in provider_settings.JEV_RETIRED_PINS:
        assert jev.resolve_model(retired) == jev.JEV_MODEL
        with pytest.raises(ValueError, match="model_not_allowed"):
            jev.JevClient(api_key="synthetic", model=retired)
    for alias in ("jev-latest", "jev-1.15", "jev-1.15.0-beta"):
        assert jev.resolve_model(alias) is None
        assert "config: semantic_model_invalid" in semantic_config({"semantic": {"model": alias}})[1]


def test_newer_models_are_fixed_versions_above_the_one_in_use():
    listed = ["jev-latest", "jev-1.13.0", jev.JEV_MODEL, NEXT, "jev-2.0.0"]
    assert jev_model_updates.newer_models(listed, jev.JEV_MODEL) == ["jev-2.0.0", NEXT]
    assert jev_model_updates.newer_models(listed, "jev-2.0.0") == []


@pytest.fixture
def home(tmp_path, monkeypatch):
    (tmp_path / "data").mkdir()
    Ledger(str(tmp_path / "data" / "ledger.db")).close()
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"mcs_login_id": "synthetic@example.invalid", "notify_target": "slack:C0SYNTHETIC",
                                   "semantic": {"daily_request_budget": 10}}))
    config.chmod(0o600)
    monkeypatch.setattr(mcs_util, "HOME", str(tmp_path))
    monkeypatch.setattr(mcs_util, "CONF_PATH", str(config))
    monkeypatch.setattr(jev, "api_key", lambda *_: "synthetic")
    return tmp_path


def _lister(calls, listed=None, error=None):
    @contextmanager
    def factory(key, *, operation, root, **options):
        calls.append(operation)
        def models():
            if error is not None:
                raise error
            return listed
        yield SimpleNamespace(models=models)
    return factory


def test_model_list_is_asked_at_most_once_a_day_and_named_offline(home):
    calls = []
    factory = _lister(calls, ["jev-latest", NEXT, jev.JEV_MODEL])
    cfg = json.loads((home / "config.json").read_text())
    notice = jev_model_updates.check(cfg, str(home), now=1000.0, client_factory=factory)
    assert NEXT in notice and "--to " + NEXT in notice and "自動では切り替えません" in notice
    assert jev_model_updates.check(cfg, str(home), now=1000.0 + 3600, client_factory=factory) == notice
    assert calls == ["jev_model_check"]
    assert jev_model_updates.read_state(str(home))["models"] == [NEXT, jev.JEV_MODEL]
    jev_model_updates.check(cfg, str(home), now=1000.0 + 86400, client_factory=factory)
    assert calls == ["jev_model_check"] * 2
    # once in use, the same listing names nothing
    assert jev_model_updates.notice(NEXT, jev_model_updates.read_state(str(home))) is None


def test_failed_listing_waits_a_day_but_a_busy_writer_does_not(home):
    calls = []
    busy = _lister(calls, error=jev.JevError("budget_exceeded", "usage_lock_busy"))
    assert jev_model_updates.refresh(str(home), "synthetic", now=10.0, client_factory=busy) is None
    assert not jev_model_updates.state_path(home).exists()
    failed = _lister(calls, error=jev.JevError("timeout", "TimeoutError"))
    state = jev_model_updates.refresh(str(home), "synthetic", now=20.0, client_factory=failed)
    assert state["error"] == "timeout" and state["models"] == []
    assert jev_model_updates.refresh(str(home), "synthetic", now=30.0, client_factory=failed) == state
    assert calls == ["jev_model_check"] * 2


def test_list_is_not_asked_without_a_key_or_budget(home, monkeypatch):
    factory = _lister(calls := [], [NEXT])
    monkeypatch.setattr(jev, "api_key", lambda *_: None)
    assert jev_model_updates.check({}, str(home), client_factory=factory) is None
    monkeypatch.setattr(jev, "api_key", lambda *_: "synthetic")
    off = {"semantic": {"mode": "off", "project_ids": []}}
    assert jev_model_updates.check(off, str(home), client_factory=factory) is None
    assert calls == []


def _args(**kw):
    return SimpleNamespace(**{"to": None, "confirm_human": True, "reason": "synthetic: reviewed probe",
                              "actor": "synthetic-human", "command_id": None, **kw})


def _probe(monkeypatch, verdict, seen=None):
    counts = {"pass": (8, 0, 0, 1, 0), "fail": (7, 0, 1, 1, 0), "unevaluated": (7, 0, 0, 1, 1)}[verdict]
    result = dict(zip(("tp", "fp", "fn", "tn", "unevaluated"), counts), verdict=verdict, requests=9, rows=[])

    @contextmanager
    def client(key, *, operation, root, model, **options):
        (seen if seen is not None else []).append((operation, model))
        yield SimpleNamespace(model=model)
    monkeypatch.setattr(jev, "live_client", client)
    monkeypatch.setattr(semantic_bench, "detection_probe", lambda c: result)


def _receipts(home):
    with closing(Ledger(str(home / "data" / "ledger.db"))) as ledger:
        return [json.loads(row[0]) for row in ledger.db.execute(
            "SELECT receipt_json FROM command_receipts ORDER BY rowid")]


def test_switch_runs_the_probe_saves_through_config_and_records_a_receipt(home, monkeypatch, capsys):
    seen = []
    _probe(monkeypatch, "pass", seen)
    assert jev_model_setup.command(_args(to=NEXT)) == 0
    assert seen == [("jev_model_probe", NEXT)]
    assert json.loads((home / "config.json").read_text())["semantic"]["model"] == NEXT
    receipt = _receipts(home)[-1]
    assert receipt["outcome"] == "applied" and receipt["jev_model"]["before"] == jev.JEV_MODEL
    assert receipt["jev_model"]["after"] == NEXT and receipt["jev_model"]["probe"]["fn"] == 0
    assert "やり直されます" in capsys.readouterr().out
    # back to the code default: no probe, the key is removed
    seen.clear()
    assert jev_model_setup.command(_args(to="default")) == 0
    assert seen == [] and "model" not in json.loads((home / "config.json").read_text())["semantic"]
    assert _receipts(home)[-1]["jev_model"] == {"before": NEXT, "after": jev.JEV_MODEL, "probe": None}


@pytest.mark.parametrize("verdict, code", [("fail", 1), ("unevaluated", 2)])
def test_probe_that_does_not_pass_changes_nothing(home, monkeypatch, capsys, verdict, code):
    before = (home / "config.json").read_bytes()
    _probe(monkeypatch, verdict)
    assert jev_model_setup.command(_args(to=NEXT)) == code
    assert (home / "config.json").read_bytes() == before and _receipts(home) == []
    assert "変更していません" in capsys.readouterr().out


@pytest.mark.parametrize("args", [_args(to=NEXT, confirm_human=False), _args(to=NEXT, reason=" "),
                                  _args(to="jev-latest"), _args(to=sorted(provider_settings.JEV_RETIRED_PINS)[0])])
def test_unconfirmed_alias_or_retired_target_is_refused_before_any_probe(home, monkeypatch, args):
    monkeypatch.setattr(jev, "live_client", lambda *a, **k: pytest.fail("probe sent"))
    before = (home / "config.json").read_bytes()
    assert jev_model_setup.command(args) == 1
    assert (home / "config.json").read_bytes() == before


def test_operation_requires_a_passing_probe_for_a_non_default_model():
    import mcs_requests
    req = {"version": 1, "cmd": "ops.jev_model_set", "command_id": "00000000-0000-4000-8000-000000000001",
           "actor": "synthetic-human", "human_confirmed": True, "project_id": None,
           "reason": "synthetic", "model": NEXT, "expected_model": jev.JEV_MODEL, "probe": None}
    assert mcs_requests.validate(req) == "jev_model_probe_required"
    probe = {"tp": 1, "fp": 0, "fn": 0, "tn": 1, "unevaluated": 1, "requests": 2}
    assert mcs_requests.validate({**req, "probe": probe}) == "jev_model_probe_required"
    assert mcs_requests.validate({**req, "probe": {**probe, "unevaluated": 0}}) is None
    assert mcs_requests.validate({**req, "model": jev.JEV_MODEL}) == "jev_model_unchanged"


def test_status_shows_the_model_in_use_and_newer_listed_models(home, capsys):
    jev_model_updates._write_state(str(home), {"version": 1, "checked_at": 1.0, "models": [NEXT], "error": None})
    assert jev_model_setup.command(_args()) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["model"] == jev.JEV_MODEL and shown["newer"] == [NEXT]
