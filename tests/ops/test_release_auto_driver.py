"""v3 owner tick driver(release_auto_driver)の合成検証。

実Store・実network・実serviceには触れず、store/deps/owner/healthをfakeへ差し替える。
"""
import json

import pytest

import release_auto_driver
import runtime_entry
import update_cli


@pytest.fixture
def control_root(tmp_path, monkeypatch):
    root = tmp_path / 'ctl'
    monkeypatch.setattr(runtime_entry, 'control_root', lambda *a, **k: root)
    return root


class _Store:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def test_run_tick_calls_owner_and_health_once():
    store = _Store()
    calls = []

    def deps_builder(*, store):
        calls.append(('deps', store))
        return {'deps': True}

    def owner_tick(s, deps):
        calls.append(('owner', s, deps))
        return {'action': 'idle'}

    def health_report(s, deps):
        calls.append(('health', s, deps))
        return {'action': 'no_binding'}

    result = release_auto_driver.run_tick(
        '/synthetic/control', store_factory=lambda root: store,
        deps_builder=deps_builder, owner_tick=owner_tick,
        health_report=health_report)
    assert result['schema_version'] == release_auto_driver.RESULT_SCHEMA
    assert result['action'] == 'idle'
    assert result['owner'] == {'action': 'idle'}
    assert result['health'] == {'action': 'no_binding'}
    assert calls == [('deps', store), ('owner', store, {'deps': True}),
                     ('health', store, {'deps': True})]
    assert store.closed is True


def test_run_tick_open_store_failure_is_fixed_hold():
    class _OpenError(RuntimeError):
        code = 'STORE_DENIED'

    def open_store(root):
        raise _OpenError('x')

    result = release_auto_driver.run_tick('/bad', store_factory=open_store)
    assert result['action'] == 'hold'
    assert result['stage'] == 'open_store'
    assert result['code'] == 'STORE_DENIED'


def test_run_tick_dependency_failure_is_fixed_hold_and_closes():
    store = _Store()

    class _DepsError(RuntimeError):
        code = 'DEPS_DENIED'

    result = release_auto_driver.run_tick(
        '/synthetic/control', store_factory=lambda root: store,
        deps_builder=lambda *, store: (_ for _ in ()).throw(_DepsError('x')))
    assert result['action'] == 'hold'
    assert result['stage'] == 'build_deps'
    assert result['code'] == 'DEPS_DENIED'
    assert store.closed is True


def test_run_tick_owner_failure_does_not_crash_scheduler():
    store = _Store()

    class _WireError(RuntimeError):
        code = 'WIRE_UNAVAILABLE'

    def owner_tick(s, deps):
        raise _WireError('x')

    result = release_auto_driver.run_tick(
        '/synthetic/control', store_factory=lambda root: store,
        deps_builder=lambda *, store: object(), owner_tick=owner_tick,
        health_report=lambda s, d: {'action': 'acked'})
    assert result['action'] == 'hold'
    assert result['stage'] == 'owner_tick'
    assert result['code'] == 'WIRE_UNAVAILABLE'
    assert result['health'] is None
    assert store.closed is True


def test_run_tick_health_failure_is_hold_but_owner_result_is_kept():
    store = _Store()

    class _HealthError(RuntimeError):
        code = 'HEALTH_DENIED'

    def health_report(s, deps):
        raise _HealthError('x')

    result = release_auto_driver.run_tick(
        '/synthetic/control', store_factory=lambda root: store,
        deps_builder=lambda *, store: object(),
        owner_tick=lambda s, d: {'action': 'observe_only', 'phase': 'APPLIED'},
        health_report=health_report)
    assert result['action'] == 'observe_only'
    assert result['owner']['phase'] == 'APPLIED'
    assert result['health'] == {'action': 'hold', 'code': 'HEALTH_DENIED'}


def test_run_tick_mutex_refuses_overlap(tmp_path):
    lock = tmp_path / 'driver.lock'
    store = _Store()
    with release_auto_driver.driver_mutex(str(lock)):
        result = release_auto_driver.run_tick(
            '/synthetic/control', mutex_path=str(lock),
            store_factory=lambda root: store,
            deps_builder=lambda *, store: object(),
            owner_tick=lambda s, d: {'action': 'idle'},
            health_report=lambda s, d: {'action': 'no_binding'})
    assert result == {'schema_version': release_auto_driver.RESULT_SCHEMA,
                      'owner': None, 'health': None,
                      'action': 'busy', 'code': 'DRIVER_BUSY'}
    assert store.closed is False


def test_run_tick_mutex_failure_is_hold(tmp_path):
    missing_parent = tmp_path / 'missing' / 'driver.lock'
    result = release_auto_driver.run_tick(
        '/synthetic/control', mutex_path=str(missing_parent),
        store_factory=lambda root: _Store())
    assert result['action'] == 'hold'
    assert result['stage'] == 'driver_mutex'
    assert result['code'] == 'DRIVER_MUTEX_UNAVAILABLE'


def test_public_result_is_fixed_shape_without_extra_internals():
    public = release_auto_driver.public_result({
        'schema_version': release_auto_driver.RESULT_SCHEMA,
        'action': 'hold', 'stage': 'owner_tick', 'code': 'WIRE_UNAVAILABLE',
        'owner': {'action': 'hold', 'phase': 'STAGED', 'code': 'WIRE_UNAVAILABLE',
                  'operation_id': 'op', 'raw': 'secret-like'},
        'health': {'action': 'send_failed', 'pending': True, 'raw': 'secret-like'}})
    assert json.dumps(public)
    assert public == {
        'schema_version': release_auto_driver.RESULT_SCHEMA,
        'action': 'hold', 'stage': 'owner_tick', 'code': 'WIRE_UNAVAILABLE',
        'owner': {'action': 'hold', 'phase': 'STAGED',
                  'code': 'WIRE_UNAVAILABLE', 'operation_id': 'op'},
        'health': {'action': 'send_failed', 'pending': True}}


def test_cmd_auto_tick_unmigrated_is_status_only(control_root, capsys):
    assert not control_root.exists()
    assert update_cli.cmd_auto_tick(object()) == 0
    out = json.loads(capsys.readouterr().out)
    assert out == {'action': 'status_only', 'code': 'UNKNOWN_BASELINE'}


def test_cmd_auto_tick_open_store_failure_exits_2(control_root, monkeypatch, capsys):
    control_root.mkdir(mode=0o700)
    monkeypatch.setattr(release_auto_driver, 'run_tick', lambda root, **kw: {
        'schema_version': release_auto_driver.RESULT_SCHEMA,
        'action': 'hold', 'stage': 'open_store', 'code': 'STORE_DENIED',
        'owner': None, 'health': None})
    assert update_cli.cmd_auto_tick(object()) == 2
    out = json.loads(capsys.readouterr().out)
    assert out['stage'] == 'open_store'
    assert out['code'] == 'STORE_DENIED'


def test_cmd_auto_tick_success_exits_0(control_root, monkeypatch, capsys):
    control_root.mkdir(mode=0o700)
    monkeypatch.setattr(release_auto_driver, 'run_tick', lambda root, **kw: {
        'schema_version': release_auto_driver.RESULT_SCHEMA,
        'action': 'idle', 'owner': {'action': 'idle'},
        'health': {'action': 'no_binding'}})
    assert update_cli.cmd_auto_tick(object()) == 0
    out = json.loads(capsys.readouterr().out)
    assert out['action'] == 'idle'
    assert out['owner'] == {'action': 'idle'}
    assert out['health'] == {'action': 'no_binding'}


def test_cmd_auto_tick_invalid_jitter_exits_2(control_root, capsys):
    control_root.mkdir(mode=0o700)
    args = type('Args', (), {'jitter': 601})()
    assert update_cli.cmd_auto_tick(args) == 2
    out = json.loads(capsys.readouterr().out)
    assert out == {'action': 'hold', 'code': 'JITTER_INVALID'}


def test_cmd_auto_tick_applies_bounded_jitter(control_root, monkeypatch, capsys):
    control_root.mkdir(mode=0o700)
    sleeps = []
    monkeypatch.setattr(update_cli.time, 'sleep', sleeps.append)
    monkeypatch.setattr(update_cli.secrets, 'randbelow', lambda upper: upper - 1)
    monkeypatch.setattr(release_auto_driver, 'run_tick', lambda root, **kw: {
        'schema_version': release_auto_driver.RESULT_SCHEMA,
        'action': 'idle', 'owner': {'action': 'idle'},
        'health': {'action': 'no_binding'}})
    args = type('Args', (), {'jitter': 120})()
    assert update_cli.cmd_auto_tick(args) == 0
    assert sleeps == [120]
    json.loads(capsys.readouterr().out)


def test_cmd_auto_tick_hold_exits_3(control_root, monkeypatch, capsys):
    control_root.mkdir(mode=0o700)
    monkeypatch.setattr(release_auto_driver, 'run_tick', lambda root, **kw: {
        'schema_version': release_auto_driver.RESULT_SCHEMA,
        'action': 'hold', 'stage': 'owner_tick', 'code': 'WIRE_UNAVAILABLE',
        'owner': {'action': 'hold', 'code': 'WIRE_UNAVAILABLE'},
        'health': {'action': 'hold', 'code': 'WIRE_UNAVAILABLE'}})
    assert update_cli.cmd_auto_tick(object()) == 3
    json.loads(capsys.readouterr().out)


def test_cmd_auto_tick_unreadable_status_exits_3(control_root, monkeypatch, capsys):
    control_root.mkdir(mode=0o700)
    monkeypatch.setattr(release_auto_driver, 'run_tick', lambda root, **kw: {
        'schema_version': release_auto_driver.RESULT_SCHEMA,
        'action': 'status_only',
        'owner': {'action': 'status_only', 'code': 'STORE_UNREADABLE'},
        'health': {'action': 'no_binding'}})
    assert update_cli.cmd_auto_tick(object()) == 3
    json.loads(capsys.readouterr().out)
