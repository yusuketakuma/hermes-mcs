"""Per-evaluation Jev guards and explicit deadlines stay isolated on one reused client."""
import json
import time

import pytest

import semantic_jev as jev
import semantic_runtime as runtime


def answer(body):
    return 200, {}, json.dumps({"model": jev.JEV_MODEL,
        "answers": {key: {"type": "noul", "noul": .9} for key in body["questions"]},
        "usage": {"input_tokens": 1, "output_tokens": 1}}).encode()


def test_nested_same_client_keeps_hooks_deadlines_and_reservations_per_evaluation(monkeypatch):
    client = jev.JevClient(api_key="synthetic", max_attempts=1)
    events, deadlines = [], {}
    end = time.monotonic() + 30
    question = {"q": jev.noul_question("synthetic", "yes", "no")}
    inner = runtime.bind_jev(client, lambda stage: events.append("inner:" + stage),
        lambda body, timeout: events.append("inner:reserve"))
    def request(method, body, timeout, *, deadline):
        name = body["state"]["target"]["id"]
        deadlines[name] = deadline
        events.append(name + ":wire")
        if name == "outer":
            inner.evaluate({"target": {"id": "inner"}}, question, end - 1)
        return answer(body)
    monkeypatch.setattr(client, "request", request)
    outer = runtime.bind_jev(client, lambda stage: events.append("outer:" + stage),
        lambda body, timeout: events.append("outer:reserve"))
    outer.evaluate({"target": {"id": "outer"}}, question, end)
    assert events == ["outer:jev_attempt", "outer:reserve", "outer:jev_attempt", "outer:wire",
                      "inner:jev_attempt", "inner:reserve", "inner:jev_attempt", "inner:wire",
                      "inner:jev_result", "outer:jev_result"]
    assert deadlines == {"outer": end, "inner": end - 1}
    assert client.requests_made == 2
    assert client.usage_totals == {"input_tokens": 2, "output_tokens": 2, "reported_requests": 2}
    assert not any(name in vars(client) for name in ("before_attempt", "after_result", "reserve_fn", "_active_deadline"))


def test_pre_dispatch_guard_failure_leaves_next_evaluation_unbound():
    sent = []
    client = jev.JevClient(api_key="synthetic", max_attempts=1,
        post_fn=lambda body, timeout: sent.append(body) or answer(body))
    def refuse(stage):
        raise runtime.RuntimeStale(stage)
    held = runtime.bind_jev(client, refuse, lambda body, timeout: pytest.fail("reservation after stale guard"))
    end = time.monotonic() + 30
    question = {"q": jev.noul_question("synthetic", "yes", "no")}
    with pytest.raises(runtime.RuntimeStale):
        held.evaluate({}, question, end)
    assert client.requests_made == 0 and not sent
    allowed = runtime.bind_jev(client, lambda stage: None)
    allowed.evaluate({}, question, end)
    assert client.requests_made == 1 and len(sent) == 1


def test_fake_evaluation_timing_excludes_guard_work(monkeypatch):
    clock = [0.0]
    class Client:
        requests_made = 0
        def evaluate(self, state, questions, deadline):
            clock[0] += 2
            return {"answers": {}}
    def guard(stage):
        clock[0] += 3
    monkeypatch.setattr(runtime.time, "perf_counter", lambda: clock[0])
    timing = {"s": 0.0}
    evaluation = runtime.bind_jev(Client(), guard, timing=timing)
    assert evaluation.evaluate({}, {}, 100) == {"answers": {}}
    assert timing == {"s": 2.0}
    assert clock[0] == 8.0


@pytest.mark.parametrize('mutation', ['permission', 'source', 'config'])
def test_reserved_evaluation_rechecks_current_authorization_before_post(tmp_path, mutation):
    """A reserved attempt stays counted when a fresh guard refuses the wire."""
    import semantic_policy
    import semantic_store
    from pathlib import Path
    from semantic_testkit import _cfg, _seeded

    db = _seeded(tmp_path)
    try:
        cfg = _cfg('shadow', budget=3000)
        config_path = tmp_path / 'config.json'
        config_path.write_text(json.dumps(cfg))
        row = db.db.execute("SELECT * FROM fetch_jobs WHERE kind='semantic'").fetchone()
        token = runtime.JobToken.from_row(row)
        fingerprint = semantic_store.thread_bundle(db.db, 1, 1)['source_fingerprint']
        end = time.monotonic() + 30
        def guard(stage):
            runtime.guard(db, token, deadline=end,
                expected_config_generation=runtime.config_generation(cfg), expected_mode='shadow',
                cfg_path=str(config_path), load_cfg=lambda path: json.loads(Path(path).read_text()),
                parse_cfg=semantic_policy.runtime_config, source_fingerprint=fingerprint,
                current_source=lambda: semantic_store.thread_bundle(db.db, 1, 1)['source_fingerprint'],
                stage=stage)
        actual_reserve = runtime.usage_reserver(db, token, kind='semantic_usage',
            model=jev.JEV_MODEL, project_id=1, message_id=1, daily_request_budget=3000)
        def reserve(body, timeout):
            actual_reserve(body, timeout)
            if mutation == 'permission':
                db.db.execute("UPDATE patients SET fetch_reason='access_revoked' WHERE project_id=1")
                db.db.commit()
            elif mutation == 'source':
                db.db.execute("UPDATE messages SET body_text='合成の変更' WHERE message_id=1")
                db.db.commit()
            else:
                config_path.write_text(json.dumps({'semantic': {'mode': 'off'}}))
        client = jev.JevClient(api_key='synthetic', max_attempts=1,
            post_fn=lambda *args: pytest.fail('revoked request reached POST'))
        guarded = runtime.bind_jev(client, guard, reserve)
        expected = runtime.RuntimeStale if mutation == 'source' else runtime.RuntimeOff
        with pytest.raises(expected):
            guarded.evaluate({}, {'q': jev.noul_question('synthetic', 'yes', 'no')}, end)
        assert semantic_store.jev_usage_today(db) == 1
        assert client.requests_made == 0 and client.usage_totals['reported_requests'] == 0
    finally:
        db.close()


def test_reservation_wait_can_exhaust_deadline_without_starting_post(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(jev.time, 'monotonic', lambda: clock[0])
    reservations = []
    def reserve(body, timeout):
        reservations.append(timeout)
        clock[0] = 106.0
    client = jev.JevClient(api_key='synthetic', max_attempts=1,
        post_fn=lambda *args: pytest.fail('expired request reached POST'))
    with pytest.raises(jev.JevError) as error:
        runtime.bind_jev(client, lambda stage: None, reserve).evaluate(
            {}, {'q': jev.noul_question('synthetic', 'yes', 'no')}, 105.0)
    assert error.value.kind == 'budget_exceeded' and error.value.detail == 'job_budget'
    assert len(reservations) == 1 and client.requests_made == 0


def test_reservation_wait_reduces_remaining_post_timeout(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(jev.time, 'monotonic', lambda: clock[0])
    timeouts = []
    def reserve(body, timeout):
        clock[0] += 3
    def post(body, timeout):
        timeouts.append(timeout)
        return answer(body)
    client = jev.JevClient(api_key='synthetic', max_attempts=1, post_fn=post)
    runtime.bind_jev(client, lambda stage: None, reserve).evaluate(
        {}, {'q': jev.noul_question('synthetic', 'yes', 'no')}, 110.0)
    assert timeouts == [7.0] and client.requests_made == 1


def test_models_rechecks_guard_after_reservation_without_get(tmp_path, monkeypatch):
    import semantic_store
    from semantic_testkit import _ledger

    db = _ledger(tmp_path)
    try:
        events = []
        client = jev.JevClient(api_key='synthetic', max_attempts=1)
        monkeypatch.setattr(client, 'request', lambda *args, **kwargs: pytest.fail('revoked GET'))
        def guard(stage):
            events.append(stage)
            if 'reserve' in events:
                raise runtime.RuntimeStale(stage)
        actual_reserve = runtime.manual_usage_reserver(db, operation='jev_value', daily_request_budget=3000)
        def reserve(body, timeout):
            actual_reserve(body, timeout)
            events.append('reserve')
        with pytest.raises(runtime.RuntimeStale):
            runtime.bind_jev(client, guard, reserve).models()
        assert events == ['jev_attempt', 'reserve', 'jev_attempt']
        assert semantic_store.jev_usage_today(db) == 1 and client.requests_made == 0
    finally:
        db.close()


def test_unreserved_evaluation_keeps_single_before_attempt_callback():
    events = []
    client = jev.JevClient(api_key='synthetic', max_attempts=1,
        post_fn=lambda body, timeout: answer(body))
    client.evaluate({}, {'q': jev.noul_question('synthetic', 'yes', 'no')},
        time.monotonic() + 10, before_attempt=lambda: events.append('before'),
        after_result=lambda: events.append('after'))
    assert events == ['before', 'after'] and client.requests_made == 1


@pytest.mark.parametrize('operation', ['scheduled', 'manual', 'models'])
def test_midnight_after_committed_reservation_refuses_send_and_keeps_charge(tmp_path, monkeypatch, operation):
    import semantic_store
    from semantic_testkit import _seeded

    db = _seeded(tmp_path)
    start = semantic_store._jst_day_start(1_800_000_000)
    clock = [start + 86399]
    monkeypatch.setattr(jev.time, 'time', lambda: clock[0])
    try:
        row = db.db.execute("SELECT * FROM fetch_jobs WHERE kind='semantic'").fetchone()
        reserve = (runtime.usage_reserver(db, runtime.JobToken.from_row(row), kind='semantic_usage',
                   model=jev.JEV_MODEL, project_id=1, message_id=1, daily_request_budget=3000)
                   if operation == 'scheduled' else
                   runtime.manual_usage_reserver(db, operation='jev_value', daily_request_budget=3000))
        def cross(body, timeout):
            receipt = reserve(body, timeout)
            clock[0] += 2
            return receipt
        client = jev.JevClient(api_key='synthetic', max_attempts=1,
            post_fn=lambda *args: pytest.fail('POST after midnight'))
        monkeypatch.setattr(client, 'request', lambda *args, **kwargs: pytest.fail('GET after midnight'))
        bound = runtime.bind_jev(client, lambda stage: None, cross)
        with pytest.raises(jev.JevError) as error:
            if operation == 'models':
                bound.models()
            else:
                bound.evaluate({}, {'q': jev.noul_question('synthetic', 'yes', 'no')},
                               time.monotonic() + 10)
        assert error.value.detail == 'reservation_day_changed' and error.value.retryable
        assert len(db.artifacts('semantic_usage')) == 1
        clock[0] = start + 86399
        assert semantic_store.jev_usage_today(db) == 1
        assert client.requests_made == 0
    finally:
        db.close()


@pytest.mark.parametrize('models', [False, True])
def test_worker_day_refusal_is_held_without_counting_a_sent_request(monkeypatch, models):
    import bounded_http
    day = bounded_http._jst_day(time.time())
    receipt = runtime.JevReservation(1, day)
    client = jev.JevClient(api_key='synthetic', max_attempts=1)
    def refuse(*args, **kwargs):
        assert kwargs['reservation_day'] == day
        raise bounded_http.ReservationDayChanged('synthetic')
    refuse.__mcs_guarded_keywords__ = ('reservation_day',)
    monkeypatch.setattr(client, 'request', refuse)
    bound = runtime.bind_jev(client, lambda stage: None, lambda body, timeout: receipt)
    with pytest.raises(jev.JevError) as error:
        if models:
            bound.models()
        else:
            bound.evaluate({}, {'q': jev.noul_question('synthetic', 'yes', 'no')},
                           time.monotonic() + 10)
    assert error.value.detail == 'reservation_day_changed' and error.value.retryable
    assert client.requests_made == 0


def test_legacy_models_hook_with_reservation_is_rejected_before_dispatch():
    class Legacy:
        supports_attempt_hooks = True  # This promises only evaluate hooks.
        def models(self, timeout, *, reserve_fn):
            pytest.fail('legacy model listing must not run with a reservation')
    def reserve(*args):
        pytest.fail('unsupported model call must not reserve')
    with pytest.raises(jev.JevError) as error:
        runtime.bind_jev(Legacy(), lambda stage: None, reserve).models()
    assert error.value.detail == 'models_guard_unsupported'


def test_legacy_models_hook_without_reservation_keeps_existing_call():
    class Legacy:
        supports_attempt_hooks = True
        def models(self, timeout, *, reserve_fn):
            assert reserve_fn is None
            return ['synthetic']
    assert runtime.bind_jev(Legacy(), lambda stage: None).models() == ['synthetic']


def test_models_kwargs_hook_requires_and_obeys_explicit_guard_contract():
    events = []
    class Extension:
        def models(self, timeout, **kwargs):
            events.append('reserve')
            kwargs['reserve_fn']({}, timeout)
            kwargs['before_attempt']()
            events.append('wire')
            return ['synthetic']
    bound = runtime.bind_jev(Extension(), lambda stage: events.append(stage),
                             lambda body, timeout: events.append('reserved'))
    with pytest.raises(jev.JevError) as error:
        bound.models()
    assert error.value.detail == 'models_guard_unsupported' and events == ['jev_attempt']
    Extension.models.__mcs_guarded_keywords__ = ('before_attempt',)
    events.clear()
    assert bound.models() == ['synthetic']
    assert events == ['jev_attempt', 'reserve', 'reserved', 'jev_attempt', 'wire', 'jev_result']


@pytest.mark.parametrize('models', [False, True])
def test_legacy_request_signature_is_refused_before_count_or_send(tmp_path, monkeypatch, models):
    import semantic_store
    from semantic_testkit import _seeded

    db = _seeded(tmp_path)
    try:
        reserve = runtime.manual_usage_reserver(db, operation='jev_value', daily_request_budget=3000)
        client = jev.JevClient(api_key='synthetic', max_attempts=1)
        def legacy_request(method, body, timeout, endpoint=None, *, deadline=None):
            pytest.fail('unsupported request override reached wire')
        monkeypatch.setattr(client, 'request', legacy_request)
        bound = runtime.bind_jev(client, lambda stage: None, reserve)
        with pytest.raises(jev.JevError) as error:
            if models:
                bound.models()
            else:
                bound.evaluate({}, {'q': jev.noul_question('synthetic', 'yes', 'no')},
                               time.monotonic() + 10)
        assert error.value.detail == 'request_day_guard_unsupported' and error.value.retryable
        assert client.requests_made == 0 and semantic_store.jev_usage_today(db) == 1
    finally:
        db.close()


def test_kwargs_only_request_needs_explicit_guard_protocol():
    def undeclared(*args, **kwargs):
        pass
    assert not runtime._accepts_explicit_keyword(undeclared, 'reservation_day')
    undeclared.__mcs_guarded_keywords__ = ('reservation_day',)
    assert runtime._accepts_explicit_keyword(undeclared, 'reservation_day')


def _private_json(path, value):
    import hashlib

    raw = json.dumps(value, ensure_ascii=False).encode('utf-8')
    path.write_bytes(raw)
    path.chmod(0o600)
    return hashlib.sha256(raw).hexdigest()


def _split_jev_root(root, *, until):
    import provider_settings

    root.mkdir(mode=0o700)
    data = root / 'data'
    data.mkdir(mode=0o700)
    directory = data / 'provider_settings'
    directory.mkdir(mode=0o700)
    common = {'semantic': {'mode': 'off'}, 'cloud_llm': {'mode': 'off', 'project_ids': []}}
    _private_json(root / 'config.json', common)
    providers = {'anthropic': {}, 'openai': {}, 'jev': {'until': until}}
    hashes = {name: _private_json(directory / (name + '.json'), providers[name])
              for name in provider_settings.PROVIDERS}
    _private_json(data / '.provider-settings-authority.json', {
        'version': 2, 'state': 'committed', 'files': hashes,
        'common_policy_sha256': provider_settings.common_policy_digest(common),
    })


@pytest.mark.parametrize('config_present', [False, True])
def test_jev_pending_split_refuses_wire_and_reservation_for_explicit_root(tmp_path, monkeypatch, config_present):
    from contextlib import closing
    import mcs_util
    from ledger import Ledger

    other = tmp_path / 'explicit-root'
    other.mkdir(mode=0o700)
    data = other / 'data'
    data.mkdir(mode=0o700)
    if config_present:
        _private_json(other / 'config.json', {'semantic': {'mode': 'off'}})
    _private_json(data / '.provider-settings-pending.json', {'synthetic': True})
    # A separate, uninitialized default root must not control the explicit root.
    default = tmp_path / 'default-root'
    default.mkdir(mode=0o700)
    monkeypatch.setattr(mcs_util, 'HOME', str(default))
    monkeypatch.setattr(mcs_util, 'CONF_PATH', str(default / 'config.json'))
    sent = []
    client = jev.JevClient(api_key='synthetic', max_attempts=1,
        post_fn=lambda body, timeout: sent.append(body) or answer(body))
    bound = runtime.bind_jev(client, lambda _stage: None, config_path=other / 'config.json')
    with pytest.raises(jev.JevError) as raised:
        bound.evaluate({}, {'q': jev.noul_question('synthetic', 'yes', 'no')}, time.monotonic() + 10)
    assert raised.value.detail == 'semantic_config_unreadable' and not sent
    monkeypatch.setattr(client, 'request', lambda *_args, **_kwargs: pytest.fail('pending GET reached transport'))
    with pytest.raises(jev.JevError) as raised:
        bound.models()
    assert raised.value.detail == 'semantic_config_unreadable'
    with pytest.raises(jev.JevError) as raised:
        runtime._jev_window_check(True, config_path=other / 'config.json')
    assert raised.value.detail == 'semantic_config_unreadable' and raised.value.retryable is True
    with closing(Ledger(str(tmp_path / 'synthetic.db'))) as ledger:
        reserve = runtime.manual_usage_reserver(ledger, operation='wire_smoke',
                                                config_path=other / 'config.json')
        with pytest.raises(jev.JevError) as raised:
            reserve({'model': 'synthetic'}, 1)
        assert raised.value.detail == 'semantic_config_unreadable'
        assert not ledger.artifacts('semantic_usage')


def test_jev_explicit_split_root_uses_its_own_until(tmp_path, monkeypatch):
    import mcs_util

    other = tmp_path / 'explicit-root'
    _split_jev_root(other, until='2000-01-01T00:00:00+00:00')
    default = tmp_path / 'default-root'
    default.mkdir(mode=0o700)
    monkeypatch.setattr(mcs_util, 'HOME', str(default))
    monkeypatch.setattr(mcs_util, 'CONF_PATH', str(default / 'config.json'))
    with pytest.raises(jev.JevError) as raised:
        runtime._jev_window_check(config_path=other / 'config.json')
    assert raised.value.detail == 'semantic_until_expired'


def test_jev_unconfigured_legacy_root_still_permits_window_check(tmp_path):
    root = tmp_path / 'legacy'
    root.mkdir(mode=0o700)
    assert runtime._jev_window_check(config_path=root / 'config.json') is None


def test_jev_legacy_read_rechecks_provider_state_before_allowing(tmp_path, monkeypatch):
    import provider_settings

    root = tmp_path / 'legacy'
    root.mkdir(mode=0o700)
    _private_json(root / 'config.json', {'semantic': {'mode': 'off'}})
    original = provider_settings.probe
    calls = []
    def changed(path):
        current = original(path)
        calls.append(current)
        return {**current, 'state': 'pending'} if len(calls) == 2 else current
    monkeypatch.setattr(provider_settings, 'probe', changed)
    with pytest.raises(jev.JevError) as raised:
        runtime._jev_window_check(config_path=root / 'config.json')
    assert raised.value.detail == 'semantic_config_unreadable' and len(calls) == 2
