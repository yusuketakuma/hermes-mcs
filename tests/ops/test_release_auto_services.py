"""v3 service drain deps面: stop/await/complete/restartのorchestration検証。"""
import pytest

from release_auto_services import ServiceDrain, ServiceError

SERVICES = [{'id': 'ai.mcs.extract-drainer', 'kind': 'launchagent'},
            {'id': 'ai.mcs.extract-drainer-2', 'kind': 'launchagent'},
            {'id': 'mcs-standalone', 'kind': 'supervisor'}]


def make_drain(tmp_path=None, **kw):
    state = {'now': 1000, 'mono': 0.0, 'stopped': [], 'restarted': 0,
             'intake': 0, 'proofs': {}}
    sleeps = []

    def now_ms():
        return state['now']

    def mono():
        return state['mono']

    def sleep(s):
        sleeps.append(s)
        state['mono'] += s
        state['now'] += int(s * 1000)

    def stopper(service):
        state['stopped'].append(service['id'])
        state['proofs'][service['id']] = {'pid': 1, 'birth': 'b'}

    def prober(service):
        proof = state['proofs'].get(service['id'])
        return dict(proof) if proof else None

    def restarter():
        state['restarted'] += 1

    def intake():
        state['intake'] += 1

    params = dict(services=[dict(s) for s in SERVICES],
                  stoppers={s['id']: stopper for s in SERVICES},
                  probers={s['id']: prober for s in SERVICES},
                  restarter=restarter, intake_stop=intake,
                  now_ms=now_ms, mono=mono, sleep=sleep, poll_interval_s=0.5)
    params.update(kw)
    drain = ServiceDrain(**params)
    return drain, state, sleeps


def test_inventory_validation():
    with pytest.raises(ServiceError):
        make_drain(services=[])
    with pytest.raises(ServiceError):
        make_drain(services=[{'id': 'a'}, {'id': 'a'}])
    with pytest.raises(ServiceError):
        make_drain(services=[{'kind': 'x'}])
    with pytest.raises(ServiceError):
        make_drain(stoppers={})
    with pytest.raises(ServiceError):
        make_drain(probers={'ai.mcs.extract-drainer': lambda s: None})


def test_managed_services_returns_copies():
    drain, state, _ = make_drain()
    services = drain.managed_services()
    assert [s['id'] for s in services] == [s['id'] for s in SERVICES]
    services[0]['id'] = 'mutated'
    assert drain.managed_services()[0]['id'] == SERVICES[0]['id']


def test_request_stop_each_service():
    drain, state, _ = make_drain()
    for service in drain.managed_services():
        drain.request_stop(service)
    assert sorted(state['stopped']) == sorted(s['id'] for s in SERVICES)

    with pytest.raises(ServiceError) as e:
        drain.request_stop({'id': 'unknown'})
    assert e.value.code == 'SERVICE_UNKNOWN'

    def bad_stop(service):
        raise RuntimeError('stop failed')

    drain2, _, _ = make_drain(
        stoppers={**{s['id']: bad_stop for s in SERVICES}})
    with pytest.raises(ServiceError) as e:
        drain2.request_stop(SERVICES[0])
    assert e.value.code == 'SERVICE_STOP'


def test_await_retirement_collects_proofs():
    drain, state, _ = make_drain()
    for service in drain.managed_services():
        drain.request_stop(service)
    evidence = drain.await_retirement(state['now'] + 60_000)
    assert evidence['observed_at_ms'] == state['now']
    assert drain.retirement_complete(evidence, drain.managed_services()) is True
    for sid, row in evidence['services'].items():
        assert row['retired'] is True and row['proof']


def test_await_retirement_waits_for_late_proof():
    # 1件だけ遅れて退役するprober
    drain, state, sleeps = make_drain()
    late = {'done': False}

    def slow_prober(service):
        if service['id'] == 'mcs-standalone' and not late['done']:
            return None
        return {'pid': 1}

    drain._probers['mcs-standalone'] = slow_prober
    drain.request_stop({'id': 'mcs-standalone'})

    # 2回pollしてから退役
    calls = {'n': 0}
    orig_sleep = drain._sleep

    def counting_sleep(s):
        calls['n'] += 1
        if calls['n'] == 2:
            late['done'] = True
        orig_sleep(s)

    drain._sleep = counting_sleep
    evidence = drain.await_retirement(state['now'] + 60_000)
    assert evidence['services']['mcs-standalone']['retired'] is True
    assert len(sleeps) >= 1


def test_await_retirement_deadline_leaves_unproven():
    drain, state, _ = make_drain()
    # proberが常にNone → deadlineまで待って未証明で返す
    drain._probers = {s['id']: (lambda svc: None) for s in SERVICES}
    evidence = drain.await_retirement(state['now'] + 2000)
    assert all(row['retired'] is False for row in evidence['services'].values())
    assert drain.retirement_complete(evidence, drain.managed_services()) is False


def test_await_retirement_probe_error_is_recorded():
    drain, state, _ = make_drain()

    def bad(service):
        raise OSError('gone')

    drain._probers['ai.mcs.extract-drainer'] = bad
    evidence = drain.await_retirement(state['now'] + 1000)
    row = evidence['services']['ai.mcs.extract-drainer']
    assert row['retired'] is False and row['error'] == 'OSError'


def test_retirement_complete_requires_all_ids():
    drain, state, _ = make_drain()
    drain.request_stop(SERVICES[0])
    evidence = drain.await_retirement(state['now'] + 60_000)
    # 1件しかstopしていない → 残りは未証明
    assert drain.retirement_complete(evidence, drain.managed_services()) is False
    # 要求subsetなら完了
    assert drain.retirement_complete(evidence, [SERVICES[0]]) is True
    # 壊れたevidence
    assert drain.retirement_complete({}, drain.managed_services()) is False
    assert drain.retirement_complete(evidence, 'x') is False


def test_stop_intake_and_restart():
    drain, state, _ = make_drain()
    drain.stop_new_intake()
    assert state['intake'] == 1
    drain.restart_same_version()
    assert state['restarted'] == 1

    def bad_intake():
        raise OSError('marker')

    drain2, _, _ = make_drain(intake_stop=bad_intake)
    with pytest.raises(ServiceError) as e:
        drain2.stop_new_intake()
    assert e.value.code == 'INTAKE_STOP'

    def bad_restart():
        raise RuntimeError('launchctl')

    drain3, _, _ = make_drain(restarter=bad_restart)
    with pytest.raises(ServiceError) as e:
        drain3.restart_same_version()
    assert e.value.code == 'SERVICE_RESTART'


def test_callable_services_fresh_inventory():
    # services providerは呼出毎にfresh評価される
    state = {'svcs': [dict(SERVICES[0])], 'calls': 0}

    def provider():
        state['calls'] += 1
        return [dict(s) for s in state['svcs']]

    drain, st, _ = make_drain(services=provider,
                              stoppers=lambda svc: lambda s: None,
                              probers=lambda svc: lambda s: {'ok': True})
    first = drain.managed_services()
    assert [s['id'] for s in first] == [SERVICES[0]['id']]
    # providerが増えたserviceを返すと次回呼出で反映
    state['svcs'] = [dict(s) for s in SERVICES]
    second = drain.managed_services()
    assert len(second) == 3
    assert state['calls'] == 2


def test_callable_stoppers_probers():
    drain, state, _ = make_drain(
        stoppers=lambda svc: state['stopped'].append(svc['id']),
        probers=lambda svc: {'pid': 1})
    for service in drain.managed_services():
        drain.request_stop(service)
    assert sorted(state['stopped']) == sorted(s['id'] for s in SERVICES)
    evidence = drain.await_retirement(state['now'] + 60_000)
    assert drain.retirement_complete(evidence, drain.managed_services())


def test_await_uses_managed_services_snapshot():
    # managed_servicesで得た集合がawait/completeの対象
    drain, state, _ = make_drain()
    svcs = drain.managed_services()
    drain.request_stop(svcs[0])
    evidence = drain.await_retirement(state['now'] + 60_000)
    # request_stopしていない残りは未証明
    assert drain.retirement_complete(evidence, svcs) is False
