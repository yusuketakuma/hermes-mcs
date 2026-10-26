"""Explicit live tools share scheduled Jev reservations without sending any HTTP."""
from contextlib import closing
import json
import time

import pytest

from ledger import Ledger
import mcs_util
import semantic_jev as jev
import semantic_runtime as runtime
from semantic_store import jev_usage_today


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(mcs_util, "HOME", str(tmp_path))
    (tmp_path / "data").mkdir()
    Ledger(str(tmp_path / "data" / "ledger.db")).close()
    monkeypatch.setattr(jev.time, "sleep", lambda seconds: None)
    return tmp_path


def _answer(body, timeout):
    return 200, {}, json.dumps({"model": jev.JEV_MODEL,
        "answers": {key: {"type": "noul", "noul": .9} for key in body["questions"]},
        "usage": {"input_tokens": 1, "output_tokens": 1}}).encode()


def _evaluate(client):
    return client.evaluate({}, {"q": jev.noul_question("synthetic", "yes", "no")}, time.monotonic() + 10)


def _used(home):
    with closing(Ledger(str(home / "data" / "ledger.db"))) as db:
        return jev_usage_today(db)


@pytest.mark.parametrize("cap", [None, 1000])
def test_manual_post_and_models_get_cannot_exceed_shared_500(home, monkeypatch, cap):
    if cap is not None:
        (home / "config.json").write_text(json.dumps({"semantic": {"daily_request_budget": cap}}))
    with closing(Ledger(str(home / "data" / "ledger.db"))) as db:
        db.artifact_add("semantic_usage", "{}", meta={"jev_requests": 499})
    calls = []
    with jev.live_client("synthetic", operation="jev_value", root=str(home),
                         post_fn=lambda *args: calls.append("post") or _answer(*args)) as client:
        _evaluate(client)
        monkeypatch.setattr(client._client, "request", lambda *a, **k: pytest.fail("GET beyond daily cap"))
        with pytest.raises(jev.JevError, match="budget_exceeded"):
            client.models()
    assert calls == ["post"] and _used(home) == 500


def test_lower_explicit_budget_zero_is_respected_without_mode_or_scope_gate(home):
    (home / "config.json").write_text(json.dumps({"semantic": {
        "mode": "off", "project_ids": [], "daily_request_budget": 0}}))
    with jev.live_client("synthetic", operation="jev_value", root=str(home),
                         post_fn=lambda *a: pytest.fail("zero budget must not send")) as client:
        with pytest.raises(jev.JevError, match="budget_exceeded"):
            _evaluate(client)
    assert _used(home) == 0


def test_retries_reserve_each_http_and_preserve_unknown_attempt(home):
    (home / "config.json").write_text(json.dumps({"semantic": {"daily_request_budget": 2}}))
    sent = []

    def fail(*args):
        sent.append(1)
        raise OSError("synthetic lost response")

    with jev.live_client("synthetic", operation="semantic_bench", root=str(home), post_fn=fail) as client:
        with pytest.raises(jev.JevError, match="budget_exceeded"):
            _evaluate(client)
    assert len(sent) == 2 and _used(home) == 2


def test_models_get_consumes_same_shared_reservation(home, monkeypatch):
    with jev.live_client("synthetic", operation="wire_smoke", root=str(home)) as client:
        monkeypatch.setattr(client._client, "request", lambda *a, **k:
            (200, {}, json.dumps({"data": [{"id": jev.JEV_MODEL}]}).encode()))
        assert client.models() == [jev.JEV_MODEL]
    assert _used(home) == 1


def test_scheduled_and_manual_reservations_do_not_double_count(home):
    with closing(Ledger(str(home / "data" / "ledger.db"))) as db:
        from extract_testkit import _message
        db.save_messages([_message(1, body="synthetic scheduled source")], project_id=1)
        db.job_add("semantic", 1, 1, payload={"generation": "synthetic"})
        token = runtime.JobToken.from_row(db.db.execute("SELECT * FROM fetch_jobs WHERE kind='semantic'").fetchone())
        runtime.usage_reserver(db, token, kind="semantic_usage", model=jev.JEV_MODEL,
                              project_id=1, message_id=1)({}, 1)
    with jev.live_client("synthetic", operation="semantic_detect", root=str(home), post_fn=_answer) as client:
        _evaluate(client)
    assert _used(home) == 2


@pytest.mark.parametrize("contents", ['{"semantic":{"daily_request_budget":null}}', '{broken',
                                       '{"semantic":{"daily_request_budget":true}}'])
def test_invalid_budget_configuration_never_sends_or_resets_count(home, contents):
    (home / "config.json").write_text(contents)
    with pytest.raises(jev.JevError, match="budget_exceeded"):
        with jev.live_client("synthetic", operation="jev_value", root=str(home), post_fn=_answer):
            pytest.fail("invalid config must not enter live scope")
    assert _used(home) == 0


def test_bench_live_entry_uses_budget_context_and_offline_entry_does_not(tmp_path, monkeypatch):
    from contextlib import contextmanager
    from types import SimpleNamespace
    import semantic_bench as bench
    case = bench._synthetic_cases()[0]
    corpus, output = tmp_path / "cases.json", tmp_path / "out.json"
    corpus.write_text(json.dumps({"corpus_version": 1, "source": "fully_synthetic", "cases": [case]}))
    events = []

    @contextmanager
    def live(key, **kwargs):
        events.append(("enter", key, kwargs["operation"]))
        yield object()
        events.append("exit")

    monkeypatch.setattr(jev, "api_key", lambda: "synthetic")
    monkeypatch.setattr(jev, "live_client", live)
    monkeypatch.setattr(bench, "_run_case", lambda *a, **k: {"case_id": case["case_id"], "findings": [], "n_claims": 0})
    args = SimpleNamespace(corpus=corpus, out=str(output), tag="synthetic", jev=True, job_budget=1)
    assert bench.cmd_run(args) == 0
    assert events == [("enter", "synthetic", "semantic_bench"), "exit"]
    monkeypatch.setattr(jev, "api_key", lambda: pytest.fail("offline run must not read key"))
    monkeypatch.setattr(jev, "live_client", lambda *a, **k: pytest.fail("offline run must not reserve"))
    args.jev = False
    assert bench.cmd_run(args) == 0


def test_shadow_cli_live_entry_uses_shared_budget_context(tmp_path, monkeypatch):
    from contextlib import contextmanager
    from types import SimpleNamespace
    import importlib.util
    from pathlib import Path
    import sys
    script = Path(__file__).resolve().parents[2] / "scripts/evaluation/semantic_shadow_e2e.py"
    spec = importlib.util.spec_from_file_location("synthetic_shadow_driver", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    cases, output = tmp_path / "cases.json", tmp_path / "out.json"
    cases.write_text(json.dumps({"source": "synthetic", "cases": [{"id": "synthetic", "body": "synthetic"}]}))
    calls = []

    @contextmanager
    def live(key, **kwargs):
        calls.append(kwargs["operation"])
        yield "synthetic-client"

    def run(cases, model, client, **kwargs):
        assert client == "synthetic-client"
        return {"cases": 1, "complete": 0}

    monkeypatch.setattr(jev, "api_key", lambda: "synthetic")
    monkeypatch.setattr(jev, "live_client", live)
    monkeypatch.setitem(sys.modules, "semantic_evaluation", SimpleNamespace(run_shadow_e2e=run))
    monkeypatch.setattr(sys, "argv", ["shadow", "--cases", str(cases), "--out", str(output), "--jev"])
    assert module.main() == 0
    assert calls == ["semantic_shadow_e2e"]


def test_live_scope_has_no_writer_or_run_lock_during_http(home, monkeypatch):
    import os
    import writer_lock
    from ledger import Ledger as LedgerType
    opened = []
    def open_ledger(path):
        ledger = LedgerType(path)
        opened.append(ledger)
        return ledger
    monkeypatch.setattr("ledger.Ledger", open_ledger)
    def transport_unlocked():
        fd = writer_lock.acquire_run_lock(str(home / "data" / "run.lock"))
        assert fd is not None
        os.close(fd)
        for ledger in opened:
            import sqlite3
            with pytest.raises(sqlite3.ProgrammingError):
                ledger.db.execute("SELECT 1")
    def post(body, timeout):
        transport_unlocked()
        return _answer(body, timeout)
    with jev.live_client("synthetic", operation="wire_smoke", root=str(home), post_fn=post) as client:
        assert not opened
        _evaluate(client)
        def get(*args, **kwargs):
            transport_unlocked()
            return 200, {}, json.dumps({"data": [{"id": jev.JEV_MODEL}]}).encode()
        monkeypatch.setattr(client._client, "request", get)
        assert client.models() == [jev.JEV_MODEL]
    assert len(opened) == 2 and _used(home) == 2


@pytest.mark.parametrize("blocked", ["lock", "update", "code"])
def test_blocked_live_reservation_opens_no_ledger_and_sends_nothing(home, monkeypatch, blocked):
    import os
    import writer_lock
    stamp = [123]
    monkeypatch.setattr(writer_lock, "code_stamp", lambda: stamp[0])
    fd = writer_lock.acquire_run_lock(str(home / "data" / "run.lock")) if blocked == "lock" else None
    try:
        with jev.live_client("synthetic", operation="jev_value", root=str(home),
                             post_fn=lambda *a: pytest.fail("blocked attempt sent HTTP")) as client:
            monkeypatch.setattr("ledger.Ledger", lambda *a, **kw: pytest.fail("writer opened before safety guard"))
            if blocked == "update":
                (home / "data" / "update_in_progress.marker").write_text("synthetic")
            elif blocked == "code":
                stamp[0] += 1
            with pytest.raises(jev.JevError, match="budget_exceeded") as error:
                _evaluate(client)
            assert error.value.detail == ("usage_lock_busy" if blocked == "lock" else "usage_code_changed")
    finally:
        if fd is not None:
            os.close(fd)
    assert _used(home) == 0


@pytest.mark.parametrize("failure", ["constructor", "reservation"])
def test_live_reservation_exception_always_releases_run_lock(home, monkeypatch, failure):
    import os
    import writer_lock
    def fail(*args, **kwargs):
        raise ValueError("synthetic_" + failure)
    if failure == "constructor":
        monkeypatch.setattr("ledger.Ledger", fail)
    else:
        monkeypatch.setattr(runtime, "manual_usage_reserver", fail)
    with jev.live_client("synthetic", operation="jev_value", root=str(home),
                         post_fn=lambda *a: pytest.fail("failed reservation sent HTTP")) as client:
        with pytest.raises(ValueError, match="synthetic_" + failure):
            _evaluate(client)
    fd = writer_lock.acquire_run_lock(str(home / "data" / "run.lock"))
    assert fd is not None
    os.close(fd)
    assert _used(home) == 0


def test_lowered_budget_is_read_before_each_request_in_same_live_scope(home):
    sent = []
    with jev.live_client("synthetic", operation="jev_value", root=str(home),
                         post_fn=lambda *args: sent.append(1) or _answer(*args)) as client:
        _evaluate(client)
        (home / "config.json").write_text(json.dumps({"semantic": {"daily_request_budget": 0}}))
        with pytest.raises(jev.JevError, match="budget_exceeded"):
            _evaluate(client)
    assert sent == [1] and _used(home) == 1


def test_update_between_retries_preserves_first_unknown_reservation(home):
    sent = []
    def failed_post(*args):
        sent.append(1)
        (home / "data" / "update_in_progress.marker").write_text("synthetic")
        raise OSError("synthetic_lost_response")
    with jev.live_client("synthetic", operation="semantic_bench", root=str(home), post_fn=failed_post) as client:
        with pytest.raises(jev.JevError, match="budget_exceeded") as error:
            _evaluate(client)
        assert error.value.detail == "usage_code_changed"
    assert sent == [1] and _used(home) == 1



def test_budget_lowered_while_reserving_keeps_reservation_without_http(home, monkeypatch):
    original = runtime.manual_usage_reserver
    def factory(*args, **kwargs):
        reserve = original(*args, **kwargs)
        def lowered(body, timeout):
            reserved = reserve(body, timeout)
            (home / "config.json").write_text(json.dumps({"semantic": {"daily_request_budget": 0}}))
            return reserved
        return lowered
    monkeypatch.setattr(runtime, "manual_usage_reserver", factory)
    with jev.live_client("synthetic", operation="jev_value", root=str(home),
                         post_fn=lambda *a: pytest.fail("lower cap must stop reserved HTTP")) as client:
        with pytest.raises(jev.JevError, match="budget_exceeded") as error:
            _evaluate(client)
        assert error.value.detail == "semantic_daily_budget_changed"
    assert _used(home) == 1
