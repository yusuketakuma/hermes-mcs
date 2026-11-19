"""Patient-context worker roles, the per-patient lock and per-patient completion (synthetic only)."""
import json
import os
from types import SimpleNamespace

import pytest

import cloud_backlog
import cloud_policy
import extract_llm
import extract_llm_call_context
import extract_llm_provider
import extract_llm_resident as resident
import ledger as ledger_module
import local_llm
import patient_context_plan as plan
import patient_context_refresh as refresh
import patient_context_refresh_readiness as readiness
import patient_context_refresh_state as state
from test_context_cloud_update_identity import cloud, real_cloud, runtime
from test_patient_context_refresh import CFG, document, seed, step
from test_patient_context_cloud_stop_paths import LONG, _local_sources, _partial_cloud_scan, _wire
from common_testkit import follow_cloud_mode

__all__ = ['cloud', 'real_cloud', 'runtime']

IN_SCOPE = '本人は合成の自宅Aで生活しています。'
OUT_OF_SCOPE = '本人は合成の自宅Bで生活しています。'


def _bodies(calls):
    return [payload['source'] for payload in calls if 'source' in payload]


def _until(run, done, limit=40):
    outcomes = []
    for _ in range(limit):
        outcomes.append(run())
        if done():
            break
    return [(o.get('status'), o.get('reason'), o.get('lane'), o.get('done')) for o in outcomes]


def _rotation(ledger):
    return state._latest(ledger.db, state.PROGRESS, 0)[0]


# --- per-patient lock -----------------------------------------------------------------


@pytest.mark.parametrize('patients', [1, 2])
def test_two_local_lanes_never_work_on_one_patient(runtime, tmp_path, monkeypatch, patients):
    """Reproduction: a second worker that runs while the first infers must not take its patient."""
    ledger, clock, calls = runtime
    seed(ledger, mid=1, pid=1, body=IN_SCOPE)
    seed(ledger, mid=3, pid=1, body=IN_SCOPE + '二件目。')
    if patients == 2:
        seed(ledger, mid=2, pid=2, body=OUT_OF_SCOPE)
    other = ledger_module.Ledger(str(tmp_path / 'ledger.db'))
    chat = refresh.local_llm.admitted_chat
    second = []

    def first_call(owner, text, **kwargs):
        if not second:
            second.append(None)
            second[0] = refresh.run_due(other, CFG, deadline=clock.mono + 600, admit=lambda: True)
        return chat(owner, text, **kwargs)

    monkeypatch.setattr(refresh.local_llm, 'admitted_chat', first_call)
    try:
        first = step(runtime)
    finally:
        other.close()
    assert first['status'] == 'progressed', (first, second)
    assert second[0]['status'] in ('progressed', 'deferred'), second[0]
    if second[0]['status'] == 'progressed':
        # The second worker took the other patient.
        assert patients == 2 and OUT_OF_SCOPE in _bodies(calls), second[0]
    else:
        assert second[0].get('wait_patient_busy', 0) >= 1, second[0]


def test_patient_lock_is_released_at_the_completed_unit_yield(runtime, monkeypatch):
    """A completed unit waiting for the run lock does not hold its patient while suspended."""
    ledger, clock, _ = runtime
    seed(ledger, mid=1, pid=1, body=IN_SCOPE)
    held = []
    real = refresh._patient_lock

    def spy(db, pid):
        fd = real(db, pid)
        held.append(fd)
        return fd

    monkeypatch.setattr(refresh, '_patient_lock', spy)
    unit = refresh._run_due(ledger, CFG, deadline=clock.mono + 600, admit=lambda: True)
    # Drive the generator by hand through a synthetic suspension.
    sent = []

    def fake_unit(*args, patient_lock, **kwargs):
        patient_lock.append((1, refresh._patient_lock(ledger.db, 1)))
        sent.append((yield {'status': 'deferred', 'done': 0, 'reason': 'run_lock'}))
        assert patient_lock == [(1, patient_lock[0][1])] and patient_lock[0][1] is not None   # taken again on resume
        return {'status': 'progressed', 'done': 1}

    unit.close()
    monkeypatch.setattr(refresh, '_run_due_unit', fake_unit)
    unit = refresh._run_due(ledger, CFG, deadline=clock.mono + 600, admit=lambda: True)
    assert next(unit)['reason'] == 'run_lock'
    # While suspended, another worker may take the patient's lock.
    probe = real(ledger.db, 1)
    assert probe is not None
    os.close(probe)
    with pytest.raises(StopIteration) as finished:
        unit.send('resume')
    assert finished.value.value == {'status': 'progressed', 'done': 1} and sent == ['resume']
    # Ending the unit released it again.
    probe = real(ledger.db, 1)
    assert probe is not None
    os.close(probe)


def test_patient_lock_failure_skips_only_that_patient(runtime, monkeypatch):
    ledger, _, calls = runtime
    seed(ledger, mid=1, pid=1, body=IN_SCOPE)
    seed(ledger, mid=2, pid=2, body=OUT_OF_SCOPE)
    real = refresh._patient_lock

    def busy_first(db, pid):
        if pid == 1:
            return None
        return real(db, pid)

    monkeypatch.setattr(refresh, '_patient_lock', busy_first)
    result = step(runtime)
    assert result['status'] == 'progressed' and result.get('wait_patient_busy') == 1, result
    assert _bodies(calls) == [OUT_OF_SCOPE]


# --- roles inside the context engine --------------------------------------------------


def _cloud_seeded(runtime, cfg, monkeypatch):
    """Patient 1 is in the cloud scope, patient 2 only in the local one; the real mode check
    answers per patient."""
    ledger, _, _ = runtime
    monkeypatch.setattr(extract_llm, '_cloud_mode', extract_llm_provider._cloud_mode)
    cfg['semantic'] = {**cfg['semantic'], 'project_ids': None}
    seed(ledger, mid=1, pid=1, body=IN_SCOPE)
    seed(ledger, mid=2, pid=2, body=OUT_OF_SCOPE)


def test_role_bookmarks_are_independent(runtime, real_cloud, monkeypatch):
    ledger, clock, _ = runtime
    cfg, _ = real_cloud
    _cloud_seeded(runtime, cfg, monkeypatch)
    refresh.run_due(ledger, cfg, deadline=clock.mono + 600, admit=lambda: True, role='local')
    rotation = _rotation(ledger)
    assert 'patient_scan_local' in rotation and 'patient_scan' not in rotation
    local_mark = rotation['patient_scan_local']
    refresh.run_due(ledger, cfg, deadline=clock.mono + 600, admit=lambda: True, role='cloud')
    rotation = _rotation(ledger)
    assert rotation['patient_scan_local'] == local_mark and 'patient_scan' in rotation


def test_the_other_roles_bookmark_moving_before_the_claim_keeps_both(runtime, real_cloud, monkeypatch):
    """The claim's rotation check ignores both scan bookmarks: another role's scan between this
    selection and its claim neither rejects the claim nor is overwritten by it."""
    ledger, clock, _ = runtime
    cfg, _ = real_cloud
    _cloud_seeded(runtime, cfg, monkeypatch)
    other = {'version': 1, 'turn': 2, 'activity_after': None, 'fair_after': 0, 'fair_upper': None}
    real = readiness._readiness_for_write

    def readiness_for_write(*args, **kwargs):
        # Inside the claim's write transaction, after the selection: the local role's scan moved.
        content, meta = state._latest(ledger.db, state.PROGRESS, 0)
        state._replace(ledger, state.PROGRESS, 0, {**content, 'patient_scan_local': other}, meta)
        return real(*args, **kwargs)

    monkeypatch.setattr(readiness, '_readiness_for_write', readiness_for_write)
    result = refresh.run_due(ledger, cfg, deadline=clock.mono + 600, admit=lambda: True, role='cloud')
    assert result['status'] == 'progressed', result
    rotation = _rotation(ledger)
    assert rotation['patient_scan_local'] == other and rotation['patient_scan'] and rotation['units'] == 1


def test_local_role_never_sends_when_cloud_turns_on_mid_pass(runtime, real_cloud, monkeypatch):
    ledger, clock, calls = runtime
    cfg, posts = real_cloud
    seed(ledger, mid=1, pid=1, body=IN_SCOPE)
    modes = iter(['off'])
    monkeypatch.setattr(extract_llm, '_cloud_mode', lambda *args: next(modes, 'on'))
    follow_cloud_mode(monkeypatch, extract_llm)
    result = refresh.run_due(ledger, cfg, deadline=clock.mono + 600, admit=lambda: True, role='local')
    assert result == {**result, 'status': 'deferred', 'done': 0, 'reason': 'patient_context_role_changed'}
    assert not posts and not calls and not ledger.artifacts('cloud_usage_reserve')


@pytest.mark.parametrize('role,expected', [('local', [OUT_OF_SCOPE]), ('cloud', [])])
def test_each_role_skips_the_patients_of_the_other(runtime, real_cloud, monkeypatch, role, expected):
    ledger, clock, calls = runtime
    cfg, posts = real_cloud
    _cloud_seeded(runtime, cfg, monkeypatch)
    def run():
        outcome = refresh.run_due(ledger, cfg, deadline=clock.mono + 600, admit=lambda: True, role=role)
        clock.mono += 1
        clock.wall += 1
        return outcome

    served = 2 if role == 'local' else 1
    outcomes = [run() for _ in range(40) if document(ledger, pid=served) is None]
    outcomes += [run() for _ in range(4)]
    assert document(ledger, pid=served) is not None, outcomes
    assert any(outcome.get('wait_other_role') for outcome in outcomes), outcomes
    assert set(_bodies(calls)) == set(expected)
    if role == 'local':
        assert not posts and document(ledger, pid=1) is None
    else:
        assert posts and document(ledger, pid=2) is None


def test_local_role_exits_before_the_selection_lock_when_the_cloud_serves_every_patient(
        runtime, real_cloud, monkeypatch):
    ledger, clock, calls = runtime
    cfg, _ = real_cloud
    seed(ledger, mid=1, pid=1, body=IN_SCOPE)
    cfg['cloud_llm']['project_ids'] = None
    monkeypatch.setattr(refresh, '_selection_lock', lambda db: pytest.fail('selection lock taken'))
    result = refresh.run_due(ledger, cfg, deadline=clock.mono + 600, admit=lambda: True, role='local')
    assert result == {'status': 'deferred', 'done': 0, 'reason': 'patient_context_other_role'}
    assert not calls


# --- roles in the resident and its children ------------------------------------------


@pytest.fixture
def lanes(request, runtime, real_cloud, tmp_path, monkeypatch):
    """The real resident entry with its role probes rooted in the test directory."""
    ledger, clock, calls = runtime
    cfg, posts = real_cloud
    param = getattr(request, 'param', 'both')
    if param == 'both':
        _cloud_seeded(runtime, cfg, monkeypatch)
    elif param == 'many':
        # 70 patients in the cloud scope ahead of one outside it with three sources.
        monkeypatch.setattr(extract_llm, '_cloud_mode', extract_llm_provider._cloud_mode)
        cfg['semantic'] = {**cfg['semantic'], 'project_ids': None}
        cfg['cloud_llm']['project_ids'] = list(range(1, 71))
        for pid in range(1, 71):
            seed(ledger, mid=pid, pid=pid, body=IN_SCOPE)
        for index in range(3):
            seed(ledger, mid=1000 + index, pid=71, body=OUT_OF_SCOPE + f'記録{index}。')
    elif param == 'in_scope':
        seed(ledger, mid=1, pid=1, body=IN_SCOPE)
    monkeypatch.setattr(resident, 'HOME', str(tmp_path))
    monkeypatch.setattr(resident, 'disk_free_mb', lambda _: 100000)
    monkeypatch.setattr(resident, 'disk_floor_mb', lambda: 1)
    monkeypatch.setattr(resident, '_context_memory_available', lambda: True)
    monkeypatch.setattr(resident, 'circuit_open_s', lambda _: 0)
    monkeypatch.setattr(resident, 'time', SimpleNamespace(monotonic=lambda: clock.mono, time=lambda: clock.wall))
    monkeypatch.setattr(extract_llm_call_context, '_SLOT_OVERRIDE', local_llm.LANES[3])
    held = []

    def hold(name):
        fd = cloud_backlog.hold_role(tmp_path, name)
        assert fd is not None
        held.append(fd)

    initial = extract_llm.writer_lock.updater_state()

    def lane(*, cloud_worker=False):
        result = resident._background_context_refresh(ledger, initial, cloud_worker=cloud_worker)
        clock.mono += 1
        clock.wall += 1
        return result

    yield SimpleNamespace(ledger=ledger, clock=clock, calls=calls, posts=posts, cfg=cfg,
                          hold=hold, lane=lane)
    for fd in held:
        os.close(fd)


def test_lane3_with_cloud_on_processes_only_patients_outside_cloud_scope(lanes):
    lanes.hold(cloud_backlog.ROLE_CLOUD)   # a live context child
    outcomes = _until(lanes.lane, lambda: document(lanes.ledger, pid=2))
    assert document(lanes.ledger, pid=2) is not None, outcomes
    assert document(lanes.ledger, pid=1) is None
    assert set(_bodies(lanes.calls)) == {OUT_OF_SCOPE}
    assert not lanes.posts and not lanes.ledger.artifacts('cloud_usage_reserve')


@pytest.mark.parametrize('lanes', ['in_scope'], indirect=True)
def test_lane3_takes_over_cloud_patients_after_child_absence_grace(lanes):
    first = lanes.lane()
    assert first.get('done') == 0 and IN_SCOPE not in _bodies(lanes.calls), first
    lanes.clock.mono += resident.CHILD_ABSENT_GRACE_S - 10
    assert not lanes.lane().get('done') and not lanes.calls
    # The lane also keeps its one-minute pause after a pass that found only cloud patients.
    lanes.clock.mono += 10 + cloud_backlog._context_retry_delay({'reason': 'patient_context_other_role'})
    refusals = []

    def lane():
        outcome = lanes.lane()
        refusals.append(outcome.get('cloud_refusal'))
        return outcome

    outcomes = _until(lane, lambda: document(lanes.ledger, pid=1))
    assert 'cloud_worker_unavailable' in refusals, outcomes
    assert document(lanes.ledger, pid=1) is not None
    assert IN_SCOPE in _bodies(lanes.calls)
    assert not lanes.posts and not lanes.ledger.artifacts('cloud_usage_reserve')
    scans = [json.loads(row['meta']) for row in lanes.ledger.artifacts(state.SCAN)]
    assert scans and all(meta.get('local_fallback') is True for meta in scans)


def test_takeover_waits_for_a_unit_with_cloud_progress(runtime, real_cloud, monkeypatch):
    """Owner Q-C: an absent child does not discard paid work; the unit waits for the cloud."""
    ledger, clock, local_calls = runtime
    cfg, _ = real_cloud
    sent = _partial_cloud_scan(runtime, cfg, monkeypatch, adopted=1)
    reserved = len(ledger.artifacts('cloud_usage_reserve'))
    outcome = refresh.run_due(ledger, cfg, deadline=clock.mono + 600, admit=lambda: True, role='local_takeover')
    assert (outcome['status'], outcome['reason'], outcome['cloud_refusal']) == (
        'deferred', 'patient_context_cloud_deferred:cloud_worker_unavailable', 'cloud_worker_unavailable'), outcome
    assert not _local_sources(local_calls) and len(sent) == 1
    assert len(ledger.artifacts('cloud_usage_reserve')) == reserved
    assert not ledger.artifacts('cloud_usage_disposition')
    # A child is back: the cloud continues the same source from its saved cursor.
    assert step(runtime, cfg=cfg)['status'] == 'progressed'
    assert len(sent) == 2 and not _local_sources(local_calls)


def test_a_returning_child_ends_the_takeover_grace(lanes):
    lanes.lane()
    lanes.clock.mono += resident.CHILD_ABSENT_GRACE_S - 1
    lanes.hold(cloud_backlog.ROLE_CLOUD)
    lanes.lane()   # the child is back: the absence restarts from zero
    assert resident._CHILD_ABSENT not in lanes.ledger.__dict__


def test_child_skips_local_only_patients_while_a_local_worker_is_alive(lanes):
    lanes.hold(cloud_backlog.ROLE_LOCAL)   # lane 3 serves local patients
    outcomes = _until(lambda: lanes.lane(cloud_worker=True), lambda: document(lanes.ledger, pid=1))
    for _ in range(8):
        outcomes.append(lanes.lane(cloud_worker=True))
    assert document(lanes.ledger, pid=1) is not None, outcomes
    assert document(lanes.ledger, pid=2) is None
    assert OUT_OF_SCOPE not in _bodies(lanes.calls)
    assert lanes.posts


def test_child_serves_every_patient_without_a_local_worker(lanes):
    outcomes = _until(lambda: lanes.lane(cloud_worker=True),
                      lambda: document(lanes.ledger, pid=1) and document(lanes.ledger, pid=2))
    assert document(lanes.ledger, pid=1) is not None and document(lanes.ledger, pid=2) is not None, outcomes
    assert OUT_OF_SCOPE in _bodies(lanes.calls)


def test_role_probe_failure_semantics(lanes, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cloud_backlog, '_open_lock_file', lambda path: (_ for _ in ()).throw(OSError('synthetic')))
    assert cloud_backlog.role_alive(tmp_path, cloud_backlog.ROLE_CLOUD) is None
    assert cloud_backlog.hold_role(tmp_path, cloud_backlog.ROLE_LOCAL) is None
    # The lane treats an unobservable child as present: it never takes over.
    lanes.lane()
    lanes.clock.mono += resident.CHILD_ABSENT_GRACE_S + 1
    for _ in range(6):
        lanes.lane()
    assert IN_SCOPE not in _bodies(lanes.calls) and not lanes.posts
    assert capsys.readouterr().err.count('context_role_probe_unavailable') == 1   # once, not per pass
    # A child treats an unobservable local worker as absent: it serves every patient.
    _until(lambda: lanes.lane(cloud_worker=True), lambda: OUT_OF_SCOPE in _bodies(lanes.calls))
    assert OUT_OF_SCOPE in _bodies(lanes.calls)


@pytest.mark.parametrize('lanes', ['in_scope'], indirect=True)
def test_an_unobservable_child_never_starts_the_takeover_grace(lanes, monkeypatch):
    """Probes that cannot observe the child, a little less than the grace apart and for longer
    than the grace in all, never count as an absence."""
    monkeypatch.setattr(cloud_backlog, 'role_alive', lambda root, name: None)
    roles = []
    real = resident._context_role
    monkeypatch.setattr(resident, '_context_role', lambda *args, **kwargs: roles.append(real(*args, **kwargs)) or roles[-1])
    started = lanes.clock.mono
    while lanes.clock.mono - started <= resident.CHILD_ABSENT_GRACE_S + 100:
        lanes.lane()
        lanes.clock.mono += 99
    assert len(roles) >= 10 and set(roles) == {'local'}, roles
    assert resident._CHILD_ABSENT not in lanes.ledger.__dict__
    assert IN_SCOPE not in _bodies(lanes.calls) and not lanes.posts


@pytest.mark.parametrize('local', [None, False, True])
def test_a_child_serves_only_cloud_patients_while_a_local_lane_is_proven_alive(lanes, monkeypatch, local):
    """A child treats an unobservable local lane as absent and serves every patient."""
    probed = []
    monkeypatch.setattr(cloud_backlog, 'role_alive', lambda root, name: probed.append(name) or local)
    role = resident._context_role(lanes.ledger, cloud_worker=True, cloud_on=True)
    assert role == ('cloud' if local is True else 'any') and probed == [cloud_backlog.ROLE_LOCAL]


def test_a_role_never_probes_itself(tmp_path):
    fd = cloud_backlog.hold_role(tmp_path, cloud_backlog.ROLE_LOCAL)
    try:
        assert cloud_backlog.role_alive(tmp_path, cloud_backlog.ROLE_LOCAL) is True
        assert cloud_backlog.role_alive(tmp_path, cloud_backlog.ROLE_CLOUD) is False
        other = cloud_backlog.hold_role(tmp_path, cloud_backlog.ROLE_LOCAL)   # shared by every local lane
        assert other is not None
        os.close(other)
    finally:
        os.close(fd)
    assert cloud_backlog.role_alive(tmp_path, cloud_backlog.ROLE_LOCAL) is False
    assert oct((tmp_path / 'data' / 'cloud_locks').stat().st_mode & 0o777) == '0o700'


@pytest.mark.parametrize('lanes', ['many'], indirect=True)
def test_lane_reaches_its_patient_behind_many_cloud_patients(lanes):
    """A pass that ends on its budget among cloud patients is not paused like a pass that found
    only cloud patients: the lane continues from its bookmark at its normal pace."""
    lanes.hold(cloud_backlog.ROLE_CLOUD)
    started = lanes.clock.mono
    outcomes = _until(lanes.lane, lambda: document(lanes.ledger, pid=71), limit=40)
    assert document(lanes.ledger, pid=71) is not None, outcomes
    assert lanes.clock.mono - started < cloud_backlog._context_retry_delay(
        {'reason': 'patient_context_other_role'}), outcomes
    assert not lanes.posts and IN_SCOPE not in _bodies(lanes.calls)


@pytest.mark.parametrize('lanes', ['in_scope'], indirect=True)
def test_lane_pauses_after_a_pass_that_found_only_other_role_patients(lanes, monkeypatch):
    lanes.hold(cloud_backlog.ROLE_CLOUD)
    first = lanes.lane()
    assert (first['reason'], first['covered']) == ('patient_context_other_role', True), first
    entered = []
    real = refresh.run_due
    monkeypatch.setattr(refresh, 'run_due', lambda *args, **kwargs: entered.append(1) or real(*args, **kwargs))
    lanes.clock.mono += 50
    # Within the pause the lane neither scans nor takes the selection lock.
    assert lanes.lane() == {'status': 'deferred', 'done': 0, 'reason': 'patient_context_other_role'}
    assert not entered
    lanes.clock.mono += 10
    assert lanes.lane()['reason'] == 'patient_context_other_role' and entered == [1]


@pytest.mark.parametrize('reason,child,pause', [
    ('patient_context_cloud_deferred:daily_cap', 60, 60), ('patient_context_cloud_budget', 60, 60),
    ('patient_context_selection_busy', 20, 20), ('patient_context_other_role', 60, 60),
    ('patient_context_cloud_deferred:cloud_worker_unavailable', 60, 20),
    ('patient_context_cloud_deferred:cloud_capacity', 5, 0), ('patient_context_waiting', 5, 0)])
def test_lane_paces_its_passes_like_the_children(lanes, monkeypatch, reason, child, pause):
    """Owner Q12: a cloud wait pauses the lane's context passes for a minute like a child's, and
    another worker in the selection for 20 s; a full send slot or ordinary waiting does not. A
    takeover pass that found only patients waiting for a child waits like a busy selection
    (its unit holds back only its patient, which the selection passes over)."""
    assert cloud_backlog._context_retry_delay({'reason': reason}) == child
    entered = []

    def run_due(*args, **kwargs):
        entered.append(1)
        return {'status': 'deferred', 'done': 0, 'reason': reason}

    monkeypatch.setattr(refresh, 'run_due', run_due)
    lanes.lane()
    lanes.lane()
    assert len(entered) == (1 if pause else 2)
    if pause:
        lanes.clock.mono += pause - 3   # each pass takes a second: just before the pause ends
        lanes.lane()
        assert len(entered) == 1
        lanes.clock.mono += 1
    lanes.lane()
    assert len(entered) == (2 if pause else 3)


@pytest.mark.parametrize('lanes', ['in_scope'], indirect=True)
def test_takeover_grace_restarts_when_the_lane_did_not_probe(lanes, tmp_path):
    """Reproduction: two probes far apart that both saw no child do not show a continuous
    absence; a child may have served in between."""
    lanes.lane()   # no child seen: the absence starts
    child = cloud_backlog.hold_role(tmp_path, cloud_backlog.ROLE_CLOUD)
    lanes.clock.mono += resident.CHILD_ABSENT_GRACE_S + 100   # a child served; the lane made no pass
    os.close(child)
    outcome = lanes.lane()
    assert outcome['reason'] == 'patient_context_other_role', outcome
    assert IN_SCOPE not in _bodies(lanes.calls)
    # Continuous probes still take over after the grace.
    for _ in range(resident.CHILD_ABSENT_GRACE_S // 60 + 2):
        lanes.clock.mono += 59
        lanes.lane()
        if IN_SCOPE in _bodies(lanes.calls):
            break
    assert IN_SCOPE in _bodies(lanes.calls)


def _hold_probe(tmp_path, name):
    directory = tmp_path / 'data' / 'cloud_locks'
    directory.mkdir(parents=True, exist_ok=True)
    fd = os.open(directory / (name + '.probe'), os.O_RDWR | os.O_CREAT, 0o600)
    cloud_backlog.fcntl.flock(fd, cloud_backlog.fcntl.LOCK_EX | cloud_backlog.fcntl.LOCK_NB)
    return fd


def test_a_busy_role_mutex_makes_the_probe_and_the_hold_unavailable(tmp_path, monkeypatch):
    """Holding and probing a role happen inside its short mutex, so two probers never see each
    other as a live holder; a mutex that stays busy is unavailable, not an answer."""
    monkeypatch.setattr(cloud_backlog, '_ROLE_PROBE_TRIES', 3)
    probe = _hold_probe(tmp_path, cloud_backlog.ROLE_CLOUD)
    try:
        assert cloud_backlog.role_alive(tmp_path, cloud_backlog.ROLE_CLOUD) is None
        assert cloud_backlog.hold_role(tmp_path, cloud_backlog.ROLE_CLOUD) is None
        # The other role's mutex is separate.
        assert cloud_backlog.role_alive(tmp_path, cloud_backlog.ROLE_LOCAL) is False
    finally:
        os.close(probe)
    assert cloud_backlog.role_alive(tmp_path, cloud_backlog.ROLE_CLOUD) is False


def test_two_simultaneous_probes_never_see_each_other(tmp_path):
    import threading
    barrier = threading.Barrier(2)
    answers = []

    def probe():
        barrier.wait()
        for _ in range(200):
            answers.append(cloud_backlog.role_alive(tmp_path, cloud_backlog.ROLE_CLOUD))

    threads = [threading.Thread(target=probe) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(answers) == 400 and set(answers) <= {False, None} and False in answers


# --- per-patient completion -----------------------------------------------------------


@pytest.mark.parametrize('rebuilding', [True, False])
def test_rebuild_finishes_one_patient_before_the_next(runtime, rebuilding):
    ledger, _, calls = runtime
    bodies = {}
    for pid in (1, 2):
        for index in range(3):
            mid = pid * 10 + index
            body = f'本人は合成の居室{pid}-{index}で生活しています。'
            bodies[body] = pid
            seed(ledger, mid=mid, pid=pid, body=body)
        if rebuilding:
            # Local work replaced this patient's paid cloud scans: its context is hidden until
            # its overview is rebuilt.
            with ledger.db:
                state._replace(ledger, state.PROGRESS, pid, {'rebuild_summary': True}, {})
    for _ in range(80):
        result = step(runtime)
        if result['status'] == 'complete':
            break
    order = [bodies[body] for body in _bodies(calls)]
    assert sorted(order) == [1, 1, 1, 2, 2, 2]
    grouped = order in ([1, 1, 1, 2, 2, 2], [2, 2, 2, 1, 1, 1])
    # A rebuilding patient's sources are scanned together, so it is visible again before the
    # next starts; without a rebuild the fair turn is unchanged.
    assert grouped is rebuilding, order
    assert readiness.context_view_input(ledger.db, 1, cfg=CFG) and readiness.context_view_input(ledger.db, 2, cfg=CFG)
    assert all(state._latest(ledger.db, state.PROGRESS, pid)[0].get('rebuild_summary') is None for pid in (1, 2))


def test_preference_ends_after_its_unit_limit(runtime, monkeypatch):
    ledger, _, _ = runtime
    ledger.__dict__[refresh._PREFER] = (1, refresh.PREFER_LIMIT)
    monkeypatch.setattr(readiness, 'context_view_input', lambda *a, **k: None)
    with ledger.db:
        state._replace(ledger, state.PROGRESS, 1, {'rebuild_summary': True}, {})
    assert refresh._preferred_patient(ledger, CFG) is None and refresh._PREFER not in ledger.__dict__
    ledger.__dict__[refresh._PREFER] = (1, refresh.PREFER_LIMIT - 1)
    assert refresh._preferred_patient(ledger, CFG) == 1


def _prefer(ledger, pid, units=6):
    """This worker last progressed ``pid``, whose rebuild hides its context."""
    ledger.__dict__[refresh._PREFER] = (pid, units)
    content, meta = state._latest(ledger.db, state.PROGRESS, pid)
    with ledger.db:
        state._replace(ledger, state.PROGRESS, pid, {**content, 'rebuild_summary': True}, meta)


def test_a_waiting_preferred_patient_does_not_hold_other_patients(runtime, real_cloud, monkeypatch):
    """Reproduction: the preferred patient's unit waits for the day's cap, which ends the
    preference, so another patient's ready local work is served."""
    ledger, _, calls = runtime
    cfg, posts = real_cloud
    _cloud_seeded(runtime, cfg, monkeypatch)
    monkeypatch.setattr(cloud_policy, 'block_reason', lambda *args, **kwargs: 'daily_cap')
    _prefer(ledger, 1)
    outcomes = _until(lambda: step(runtime, cfg=cfg), lambda: document(ledger, pid=2), limit=12)
    assert document(ledger, pid=2) is not None, outcomes
    assert outcomes[0][:2] == ('deferred', 'patient_context_cloud_deferred:daily_cap'), outcomes
    assert not posts and IN_SCOPE not in _bodies(calls)


def test_a_waiting_preferred_patient_does_not_hold_the_takeover(runtime, real_cloud, monkeypatch):
    """Reproduction: under takeover the preferred patient's unit with cloud progress waits; a
    fresh patient is still taken over."""
    ledger, _, local_calls = runtime
    cfg, _ = real_cloud
    _partial_cloud_scan(runtime, cfg, monkeypatch, adopted=1)
    cfg['semantic'] = {**cfg['semantic'], 'project_ids': None}
    seed(ledger, mid=2, pid=2, body=OUT_OF_SCOPE)
    _prefer(ledger, 1)
    first = step(runtime, cfg=cfg, role='local_takeover')
    # The same pass waits on the preferred patient and moves on to the fresh one.
    assert (first['status'], first['done']) == ('progressed', 1), first
    assert OUT_OF_SCOPE in _bodies(local_calls) and not [body for body in _bodies(local_calls) if body in LONG]
    assert refresh._PREFER not in ledger.__dict__ or ledger.__dict__[refresh._PREFER][0] == 2
    assert 1 in ledger.__dict__[refresh._CHILD_WAIT]
    assert 'patient_scan_local' in _rotation(ledger)


def _many_other_role(runtime, cfg, monkeypatch, others):
    """``others`` patients outside the cloud scope, then one inside it with three sources (the
    scan reaches it last)."""
    ledger, _, _ = runtime
    monkeypatch.setattr(extract_llm, '_cloud_mode', extract_llm_provider._cloud_mode)
    cfg['semantic'] = {**cfg['semantic'], 'project_ids': None}
    own = others + 1
    cfg['cloud_llm']['project_ids'] = [own]
    posted_at = '2026-09-19T00:00:00+09:00'
    for pid in range(1, own):
        seed(ledger, mid=pid, pid=pid, body=f'本人は合成の自宅{pid}で生活しています。',
             posted_at=posted_at)
    for index in range(3):
        seed(ledger, mid=1000 + index, pid=own, body=IN_SCOPE + f'記録{index}。',
             posted_at=posted_at)
    return own


@pytest.mark.parametrize('others', [10, 70])
def test_other_role_patients_do_not_use_the_candidate_budget(runtime, real_cloud, monkeypatch, others):
    ledger, clock, _ = runtime
    cfg, _ = real_cloud
    _many_other_role(runtime, cfg, monkeypatch, others)
    first = refresh.run_due(ledger, cfg, deadline=clock.mono + 600, admit=lambda: True, role='cloud')
    if others == 10:
        # More other-role patients than the 8 candidates, and the pass still reaches its own.
        assert first['status'] == 'progressed' and first['wait_other_role'] == others, first
        return
    # The pass ended on its budget before the population was covered: that is not "only
    # other-role patients", so the caller keeps its normal pacing and continues next pass.
    assert 8 < first['wait_other_role'] <= refresh._ROLE_SKIP_LIMIT == 64 and not first['covered'], first
    assert (first['status'], first['reason']) == ('deferred', 'patient_context_scan_incomplete'), first
    outcomes = []
    for _ in range(6):
        outcomes.append(refresh.run_due(ledger, cfg, deadline=clock.mono + 600, admit=lambda: True, role='cloud'))
        if outcomes[-1]['status'] == 'progressed':
            break
    assert outcomes[-1]['status'] == 'progressed', outcomes


def test_skipped_patients_write_no_bookmark_and_admission_runs_outside_the_write_lock(
        runtime, real_cloud, monkeypatch):
    """Reproduction: every other-role patient wrote the bookmark twice, and each write ran the
    admission check (which may start external commands) under BEGIN IMMEDIATE."""
    ledger, clock, _ = runtime
    cfg, _ = real_cloud
    _many_other_role(runtime, cfg, monkeypatch, 70)
    real, writes, admitted = refresh._advance_patient_scan, [], []

    def counted(*args, **kwargs):
        writes.append(1)
        return real(*args, **kwargs)

    def admit():
        admitted.append(ledger.db.in_transaction)
        return True

    monkeypatch.setattr(refresh, '_advance_patient_scan', counted)
    first = refresh.run_due(ledger, cfg, deadline=clock.mono + 600, admit=admit, role='cloud')
    assert 8 < first['wait_other_role'] <= refresh._ROLE_SKIP_LIMIT, first
    # At most one write per candidate, one for the skips at the end and one for a cancelled
    # query, never one per skipped patient.
    assert len(writes) <= 10 and len(writes) < first['wait_other_role'], (len(writes), first)
    assert admitted and not any(admitted), admitted


def test_no_preference_while_the_rebuilding_patients_context_is_visible(runtime, monkeypatch):
    ledger, _, _ = runtime
    assert refresh.PREFER_LIMIT == 32
    ledger.__dict__[refresh._PREFER] = (1, 1)
    with ledger.db:
        state._replace(ledger, state.PROGRESS, 1, {'rebuild_summary': True}, {})
    # Hidden (no context yet): preferred.
    assert readiness.context_view_input(ledger.db, 1, cfg=CFG) is None
    assert refresh._preferred_patient(ledger, CFG) == 1
    # Visible: the fair turn decides, and the preference ends.
    monkeypatch.setattr(readiness, 'context_view_input', lambda *args, **kwargs: {'facts': []})
    assert refresh._preferred_patient(ledger, CFG) is None
    assert refresh._PREFER not in ledger.__dict__


def _stuck_patients(lanes, monkeypatch, stuck):
    """``stuck`` cloud patients whose source the cloud answered in part (cloud progress), then a
    fresh cloud patient with three sources; the cloud is never asked again afterwards."""
    ledger, clock = lanes.ledger, lanes.clock
    monkeypatch.setattr(plan, 'SCAN_CHUNK', 80)
    sent = _wire(monkeypatch, lambda index, payload, limit: None)
    fresh = stuck + 1
    lanes.cfg['semantic'] = {**lanes.cfg['semantic'], 'project_ids': None}
    for index in range(3):
        seed(ledger, mid=1000 + index, pid=fresh, body=IN_SCOPE + f'記録{index}。')
    for pid in range(1, fresh):
        seed(ledger, mid=pid, pid=pid, body=LONG)
        lanes.cfg['cloud_llm']['project_ids'] = [pid]   # this patient's first chunk only
        for _ in range(8):
            step((ledger, clock, lanes.calls), cfg=lanes.cfg, role='cloud')
            if ledger.db.execute('SELECT 1 FROM artifacts WHERE kind=? AND project_id=?',
                                 (state.SCAN, pid)).fetchone():
                break
    for pid in range(1, fresh):
        rows = ledger.db.execute('SELECT content,meta FROM artifacts WHERE kind=? AND project_id=?',
                                 (state.SCAN, pid)).fetchall()
        assert rows and all(json.loads(meta).get('provider') and not json.loads(content)['complete']
                            for content, meta in rows), [(json.loads(c), json.loads(m)) for c, m in rows]
    lanes.cfg['cloud_llm']['project_ids'] = list(range(1, fresh + 1))
    assert not lanes.calls and len(sent) >= stuck
    return fresh, sent


def _takeover_lane_seconds(lanes, done, limit=6000):
    """Simulated lane seconds until ``done()``, the lane already past the child-absence grace; a
    pass that did no work waits the lane loop's idle floor before the next."""
    clock = lanes.clock
    lanes.ledger.__dict__[resident._CHILD_ABSENT] = (clock.mono - resident.CHILD_ABSENT_GRACE_S - 1, clock.mono)
    started = clock.mono
    outcomes = []
    while not done() and clock.mono - started < limit:
        outcome = lanes.lane()
        outcomes.append((outcome.get('status'), outcome.get('reason')))
        if not outcome.get('done'):
            clock.mono += 5
            clock.wall += 5
    return clock.mono - started, outcomes


@pytest.mark.parametrize('lanes', ['none'], indirect=True)
@pytest.mark.parametrize('stuck', [1, 4, 10])
def test_takeover_serves_a_fresh_patient_behind_stuck_ones(lanes, monkeypatch, stuck):
    """Reproduction: a unit waiting for a child paused the whole lane for a minute, so fresh
    local work waited behind every stuck patient (1 stuck: 437 s, 4: 3223 s, 10: over 6000 s).
    The wait now holds back only its patient; the stuck ones are never redone locally or sent."""
    assert refresh.CHILD_WAIT_S == cloud_backlog._context_retry_delay({'reason': refresh.CHILD_WAIT_REASON})
    fresh, sent = _stuck_patients(lanes, monkeypatch, stuck)
    sent_before, reserved = len(sent), len(lanes.ledger.artifacts('cloud_usage_reserve'))
    seconds, outcomes = _takeover_lane_seconds(lanes, lambda: document(lanes.ledger, pid=fresh))
    assert document(lanes.ledger, pid=fresh) is not None, outcomes[-12:]
    assert seconds < 120, (seconds, outcomes)
    # The stuck patients keep waiting, checked again after each wait, and nothing else happens.
    waits = {}
    real = refresh._remember_patient

    def remember(ledger, pid, result):
        if result.get('reason') == refresh.CHILD_WAIT_REASON:
            waits[pid] = waits.get(pid, 0) + 1
        return real(ledger, pid, result)

    monkeypatch.setattr(refresh, '_remember_patient', remember)
    _takeover_lane_seconds(lanes, lambda: False, limit=300)
    assert set(waits) == set(range(1, fresh)) and all(2 <= count <= 7 for count in waits.values()), waits
    assert not [body for body in _bodies(lanes.calls) if body in LONG]
    assert len(sent) == sent_before and len(lanes.ledger.artifacts('cloud_usage_reserve')) == reserved
    assert not lanes.ledger.artifacts('cloud_usage_disposition')


def test_child_waits_expire_and_end_with_the_takeover(runtime, real_cloud):
    ledger, clock, _ = runtime
    refresh._remember_patient(ledger, 3, {'status': 'deferred', 'done': 0, 'reason': refresh.CHILD_WAIT_REASON})
    assert refresh._child_waits(ledger, 'local_takeover') == {3}
    clock.mono += refresh.CHILD_WAIT_S
    assert refresh._child_waits(ledger, 'local_takeover') == frozenset()
    refresh._remember_patient(ledger, 3, {'status': 'deferred', 'done': 0, 'reason': refresh.CHILD_WAIT_REASON})
    assert refresh._child_waits(ledger, 'local') == frozenset()   # a child is back
    assert refresh._CHILD_WAIT not in ledger.__dict__


def _write_transactions_per_minute(lanes, seconds=600):
    """BEGIN IMMEDIATE statements per simulated lane minute while the lane runs."""
    statements = []
    lanes.ledger.db.set_trace_callback(lambda sql: statements.append(sql) if sql.startswith('BEGIN IMMEDIATE') else None)
    try:
        elapsed, _ = _takeover_lane_seconds(lanes, lambda: False, limit=seconds)
    finally:
        lanes.ledger.db.set_trace_callback(None)
    return len(statements) * 60 / elapsed


@pytest.mark.parametrize('lanes', ['none'], indirect=True)
def test_a_lane_whose_patients_all_wait_for_a_child_pauses_briefly(lanes, monkeypatch):
    """Reproduction: with only stuck patients the lane walked every one of them each pass, two
    bookmark writes each, at the idle floor: 339 write transactions a minute for 10, now about 95."""
    assert resident._CHILD_WAIT_PAUSE_S == cloud_backlog._context_retry_delay(
        {'reason': 'patient_context_selection_busy'})
    fresh, _ = _stuck_patients(lanes, monkeypatch, 10)
    _takeover_lane_seconds(lanes, lambda: document(lanes.ledger, pid=fresh))
    _takeover_lane_seconds(lanes, lambda: False, limit=120)   # every stuck patient checked once
    rate = _write_transactions_per_minute(lanes)
    assert rate < 120, rate
    assert lanes.ledger.__dict__[resident._CONTEXT_PAUSE][1] == refresh.CHILD_WAIT_REASON


@pytest.mark.parametrize('lanes', ['none'], indirect=True)
def test_a_busy_selection_after_a_child_wait_keeps_its_pause(lanes, monkeypatch):
    """The pass reports what stopped its later unit: another worker in the selection keeps the
    lane's 20 s pause, and the child wait before it is not reported instead."""
    _stuck_patients(lanes, monkeypatch, 1)
    lanes.ledger.__dict__[resident._CHILD_ABSENT] = (
        lanes.clock.mono - resident.CHILD_ABSENT_GRACE_S - 1, lanes.clock.mono)
    real, calls = refresh._selection_lock, []

    def selection_lock(db):
        calls.append(1)
        return real(db) if len(calls) == 1 else (None, 'patient_context_selection_busy')

    monkeypatch.setattr(refresh, '_selection_lock', selection_lock)
    started = lanes.clock.mono
    outcome = lanes.lane()
    assert outcome['reason'] == 'patient_context_selection_busy' and len(calls) == 2, outcome
    assert lanes.ledger.__dict__[resident._CONTEXT_PAUSE] == (started + 20, 'patient_context_selection_busy')
    assert 1 in lanes.ledger.__dict__[refresh._CHILD_WAIT]


@pytest.mark.parametrize('role', ['any', 'cloud', 'local'])
def test_only_a_takeover_selects_again_after_a_child_wait(runtime, monkeypatch, role):
    ledger, clock, _ = runtime
    units = []

    def one_unit(*args, **kwargs):
        units.append(1)
        return (1, {'status': 'deferred', 'done': 0, 'reason': refresh.CHILD_WAIT_REASON})
        yield

    monkeypatch.setattr(refresh, '_run_one_unit', one_unit)
    refresh.run_due(ledger, CFG, deadline=clock.mono + 600, admit=lambda: True, role=role)
    assert units == [1]
    outcome = refresh.run_due(ledger, CFG, deadline=clock.mono + 600, admit=lambda: True, role='local_takeover')
    assert len(units) == 1 + refresh.CHILD_WAIT_UNITS
    # A pass that used up its units continues without the lane's pause.
    assert outcome['reason'] == 'patient_context_scan_incomplete'


@pytest.mark.parametrize('lanes', ['none'], indirect=True)
def test_passed_over_patients_do_not_use_the_candidate_budget(lanes, monkeypatch):
    """More waiting patients than the pass's 8 candidates: one pass still reaches the fresh one."""
    fresh, _ = _stuck_patients(lanes, monkeypatch, 12)
    waits = lanes.ledger.__dict__.setdefault(refresh._CHILD_WAIT, {})
    waits.update({pid: lanes.clock.mono + 3600 for pid in range(1, fresh)})
    outcome = refresh.run_due(lanes.ledger, lanes.cfg, deadline=lanes.clock.mono + 600,
                              admit=lambda: True, role='local_takeover')
    assert outcome['status'] == 'progressed' and outcome['wait_cloud_worker'] >= 9, outcome
    assert IN_SCOPE in ''.join(_bodies(lanes.calls))


def test_the_takeover_start_is_recorded_once_per_absence(monkeypatch, capsys):
    clock = [1000.0]
    alive = [False]
    monkeypatch.setattr(resident.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(cloud_backlog, 'role_alive', lambda root, name: alive[0])
    lane = SimpleNamespace()

    def probes(count):
        roles = []
        for _ in range(count):
            roles.append(resident._context_role(lane, cloud_worker=False, cloud_on=True))
            clock[0] += resident.CHILD_ABSENT_GRACE_S / 2 + 1
        return roles

    assert probes(5) == ['local', 'local', 'local_takeover', 'local_takeover', 'local_takeover']
    assert capsys.readouterr().err.count('"context_local_takeover"') == 1   # once, not per pass
    alive[0] = True
    assert probes(1) == ['local']
    alive[0] = False
    assert probes(4)[-1] == 'local_takeover'
    assert capsys.readouterr().err.count('"context_local_takeover"') == 1   # the next absence
