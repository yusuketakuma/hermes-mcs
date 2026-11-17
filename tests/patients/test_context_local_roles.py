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
import patient_context_refresh as refresh
import patient_context_refresh_readiness as readiness
import patient_context_refresh_state as state
from test_context_cloud_update_identity import cloud, real_cloud, runtime
from test_patient_context_refresh import CFG, document, seed, step
from test_patient_context_cloud_stop_paths import _local_sources, _partial_cloud_scan

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
    else:
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


@pytest.mark.parametrize('reason,pause', [
    ('patient_context_cloud_deferred:cloud_worker_unavailable', 60), ('patient_context_cloud_budget', 60),
    ('patient_context_cloud_deferred:cloud_capacity', 0), ('patient_context_waiting', 0)])
def test_lane_paces_its_passes_like_the_children(lanes, monkeypatch, reason, pause):
    """Owner Q12: a cloud wait (an absent child holds a unit with cloud progress) pauses the
    lane's context passes for a minute; a full send slot or ordinary waiting does not."""
    assert cloud_backlog._context_retry_delay({'reason': reason}) == (pause or 5)
    entered = []

    def run_due(*args, **kwargs):
        entered.append(1)
        return {'status': 'deferred', 'done': 0, 'reason': reason}

    monkeypatch.setattr(refresh, 'run_due', run_due)
    lanes.lane()
    lanes.lane()
    assert len(entered) == (1 if pause else 2)
    lanes.clock.mono += pause
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
    outcomes = _until(lambda: step(runtime, cfg=cfg, role='local_takeover'),
                      lambda: OUT_OF_SCOPE in _bodies(local_calls), limit=6)
    assert OUT_OF_SCOPE in _bodies(local_calls), outcomes
    assert outcomes[0][1] == 'patient_context_cloud_deferred:cloud_worker_unavailable', outcomes
    assert 'patient_scan_local' in _rotation(ledger)


def _many_other_role(runtime, cfg, monkeypatch, others):
    """``others`` patients outside the cloud scope, then one inside it with three sources (the
    scan reaches it last)."""
    ledger, _, _ = runtime
    monkeypatch.setattr(extract_llm, '_cloud_mode', extract_llm_provider._cloud_mode)
    cfg['semantic'] = {**cfg['semantic'], 'project_ids': None}
    own = others + 1
    cfg['cloud_llm']['project_ids'] = [own]
    for pid in range(1, own):
        seed(ledger, mid=pid, pid=pid, body=f'本人は合成の自宅{pid}で生活しています。')
    for index in range(3):
        seed(ledger, mid=1000 + index, pid=own, body=IN_SCOPE + f'記録{index}。')
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
