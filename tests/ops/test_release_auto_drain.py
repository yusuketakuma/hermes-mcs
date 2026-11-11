"""v3 launchd drain実装: inventory/stop/probe/RetiredProcess写像の検証。"""
import sys
import types

import pytest

from release_auto_drain import (LaunchdDrain, ServiceError, birth_witness,
                                native_launchd_drain, process_instance_id)

LABELS = ['ai.mcs.extract-drainer', 'ai.mcs.extract-drainer-2']


def make_drain(**over):
    state = {'pids': {}, 'births': {}, 'absent': set(), 'booted': [],
             'stray': [], 'generation': 7, 'boot': 'b' * 64}

    def agent_pid(label):
        return state['pids'].get(label)

    def agent_absent(label):
        return label in state['absent'] or label not in state['pids']

    def bootout(label):
        state['booted'].append(label)
        state['pids'].pop(label, None)
        state['absent'].add(label)

    def started_at(pid):
        return state['births'].get(pid)

    def stray():
        return list(state['stray'])

    params = dict(labels=list(LABELS), agent_pid=agent_pid,
                  agent_absent=agent_absent, bootout=bootout,
                  process_started_at=started_at, stray_drainer_pids=stray,
                  runtime_generation=state['generation'],
                  boot_id=lambda: state['boot'])
    params.update(over)
    return LaunchdDrain(**params), state


def running(state, label, pid, birth):
    state['pids'][label] = pid
    state['births'][pid] = birth


def test_inventory_returns_only_running_with_birth():
    drain, state = make_drain()
    running(state, LABELS[0], 101, 1000.5)
    running(state, LABELS[1], 102, 1001.5)
    services = drain.services()
    assert [s['id'] for s in services] == LABELS
    assert services[0]['pid'] == 101 and services[0]['birth'] == 1000.5

    # 非稼働labelは対象外
    drain2, state2 = make_drain()
    running(state2, LABELS[0], 101, 1000.5)
    assert [s['id'] for s in drain2.services()] == [LABELS[0]]


def test_inventory_rejects_unverifiable_birth():
    drain, state = make_drain()
    state['pids'][LABELS[0]] = 101  # birth未登録 → None
    with pytest.raises(ServiceError) as e:
        drain.services()
    assert e.value.code == 'SERVICE_BIRTH'

    drain2, state2 = make_drain()
    state2['pids'][LABELS[0]] = 'not-int'
    with pytest.raises(ServiceError):
        drain2.services()


def test_stopper_verifies_same_instance_then_bootouts():
    drain, state = make_drain()
    running(state, LABELS[0], 101, 1000.5)
    services = drain.services()
    drain.stopper(services[0])
    assert state['booted'] == [LABELS[0]]
    # capture済み → proberが証拠を返す
    proof = drain.prober(services[0])
    assert proof['retired'] is True and proof['pid'] == 101


def test_stopper_rejects_changed_instance():
    drain, state = make_drain()
    running(state, LABELS[0], 101, 1000.5)
    services = drain.services()
    # bootout前にpid/birthが変化 → 別instanceとして拒否
    state['pids'][LABELS[0]] = 999
    with pytest.raises(ServiceError) as e:
        drain.stopper(services[0])
    assert e.value.code == 'SERVICE_STOP'
    assert state['booted'] == []

    drain2, state2 = make_drain()
    running(state2, LABELS[0], 101, 1000.5)
    services2 = drain2.services()
    state2['births'][101] = 2000.0  # 同pid別birth(再起動)
    with pytest.raises(ServiceError):
        drain2.stopper(services2[0])


def test_stopper_rejects_already_absent():
    drain, state = make_drain()
    running(state, LABELS[0], 101, 1000.5)
    services = drain.services()
    state['pids'].pop(LABELS[0])  # inventory後に消失
    with pytest.raises(ServiceError):
        drain.stopper(services[0])


def test_prober_requires_capture():
    drain, state = make_drain()
    running(state, LABELS[0], 101, 1000.5)
    services = drain.services()
    state['absent'].add(LABELS[0])
    state['pids'].pop(LABELS[0])
    # stop未実施でabsent → instance未捕捉で未証明
    assert drain.prober(services[0]) is None


def test_retired_processes_maps_schema_fields():
    drain, state = make_drain()
    running(state, LABELS[0], 101, 1000.5)
    services = drain.services()
    drain.stopper(services[0])
    evidence = {'services': {LABELS[0]: {'retired': True,
                                          'proof': drain.prober(services[0])},
                             LABELS[1]: {'retired': False, 'proof': None}}}
    out = drain.retired_processes(evidence)
    assert len(out) == 1
    row = out[0]
    assert row['service_id'] == LABELS[0]
    assert row['retired'] is True
    assert row['generation'] == 7 and row['boot_id'] == 'b' * 64
    assert row['process_instance_id'] == process_instance_id(
        LABELS[0], 101, 1000.5)
    assert row['birth_witness'] == birth_witness(LABELS[0], 101, 1000.5)
    # 決定論的: 同じ入力で同じ値
    assert out == drain.retired_processes(evidence)


def test_retired_processes_rejects_stray_and_bad_evidence():
    drain, state = make_drain()
    # stray残存 → 拒否
    state['stray'] = [555]
    with pytest.raises(ServiceError) as e:
        drain.retired_processes({'services': {}})
    assert e.value.code == 'SERVICE_STRAY'

    # stray照合不能(None)も拒否
    drain2, _ = make_drain(
        stray_drainer_pids=lambda: (_ for _ in ()).throw(OSError()))
    with pytest.raises(ServiceError):
        drain2.retired_processes({'services': {}})

    # 壊れたevidence
    for bad in (None, {}, {'services': 'x'}):
        with pytest.raises(ServiceError):
            drain.retired_processes(bad)

    # proof形が不正 → 拒否
    drain3, state3 = make_drain()
    running(state3, LABELS[0], 101, 1000.5)
    services3 = drain3.services()
    drain3.stopper(services3[0])
    bad_evidence = {'services': {LABELS[0]: {
        'retired': True, 'proof': {'service_id': LABELS[0],
                                   'pid': 'x', 'birth': 1.0,
                                   'retired': True}}}}
    with pytest.raises(ServiceError):
        drain3.retired_processes(bad_evidence)


def test_birth_witness_deterministic():
    w1 = birth_witness('svc', 1, 2.5)
    w2 = birth_witness('svc', 1, 2.5)
    assert w1 == w2 and len(w1) == 64
    assert birth_witness('svc', 1, 2.5) != birth_witness('svc', 2, 2.5)
    iid = process_instance_id('svc', 1, 2.5)
    assert iid == process_instance_id('svc', 1, 2.5)
    assert iid != process_instance_id('svc', 1, 2.6)


# --- native_launchd_drain: 実primitive結線のargv/rc処理 -------------------


class _R:
    def __init__(self, rc, out=''):
        self.returncode = rc
        self.stdout = out


def _fake_diag(started_at):
    mod = types.ModuleType('environment_diagnostics')
    mod.process_started_at = lambda pid, **kw: started_at.get(pid)
    return mod


def test_native_agent_pid_parsing():
    calls = []

    def run(argv, timeout=10):
        calls.append(argv)
        if 'print' in argv:
            return _R(0, 'state = running\n\tpid = 4321\n')
        return _R(0)

    sys.modules['environment_diagnostics'] = _fake_diag({4321: 99.0})
    try:
        drain = native_launchd_drain(
            ['ai.mcs.extract-drainer'], uid=501, runtime_generation=3,
            boot_id='a' * 64, run=run)
        services = drain.services()
        assert services[0]['pid'] == 4321
        assert calls[0][:3] == ['launchctl', 'print', 'gui/501/ai.mcs.extract-drainer']
    finally:
        del sys.modules['environment_diagnostics']


def test_native_agent_pid_absent_and_errors():
    seq = iter([_R(113), _R(1), _R(0, 'state = waiting\n')])
    drain = native_launchd_drain(
        ['l1'], uid=501, runtime_generation=1, boot_id='a' * 64,
        run=lambda argv, timeout=10: next(seq))
    assert drain._agent_pid('l1') is None      # 113 = 未ロード
    with pytest.raises(ServiceError):           # その他rc = 照合不能
        drain._agent_pid('l1')
    assert drain._agent_pid('l1') is None       # rc=0 pid無し = 非稼働


def test_native_bootout_and_absent():
    booted = []

    def run(argv, timeout=10):
        if argv[1] == 'bootout':
            booted.append(argv[2])
            return _R(0)
        return _R(113)

    drain = native_launchd_drain(
        ['l1'], uid=501, runtime_generation=1, boot_id='a' * 64, run=run)
    drain._bootout('l1')
    assert booted == ['gui/501/l1']
    assert drain._agent_absent('l1') is True

    def bad_run(argv, timeout=10):
        return _R(1)

    drain2 = native_launchd_drain(
        ['l1'], uid=501, runtime_generation=1, boot_id='a' * 64,
        run=bad_run)
    assert drain2._agent_absent('l1') is False  # rc!=113は不在の証拠にしない
    with pytest.raises(ServiceError):
        drain2._bootout('l1')


def test_native_stray_pids():
    def run(argv, timeout=10):
        assert argv[0] == 'pgrep' and '-f' in argv
        return _R(0, '111\n222\n')

    drain = native_launchd_drain(
        ['l1'], uid=501, runtime_generation=1, boot_id='a' * 64, run=run)
    assert set(drain._stray_pids()) == {111, 222}

    def no_match(argv, timeout=10):
        return _R(1, '')

    drain2 = native_launchd_drain(
        ['l1'], uid=501, runtime_generation=1, boot_id='a' * 64,
        run=no_match)
    assert drain2._stray_pids() == []

    def broken(argv, timeout=10):
        return _R(2, '')

    drain3 = native_launchd_drain(
        ['l1'], uid=501, runtime_generation=1, boot_id='a' * 64,
        run=broken)
    assert drain3._stray_pids() is None  # 照合不能
