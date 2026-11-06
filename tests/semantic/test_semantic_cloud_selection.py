"""Synthetic cloud selection, paging and scope revalidation contracts."""
import semantic_due_selection
import semantic_job_processor
import hashlib
import json
import os
import select
import sqlite3
import subprocess
import sys
import time
from contextlib import contextmanager
import pytest
import cloud_policy
import extract_llm
import cloud_inference
import semantic_drain
import writer_lock
from semantic_testkit import _message, _patient
from semantic_cloud_testkit import cloud as cloud, continuous_snapshot as continuous_snapshot, rows as rows, snapshot as snapshot
# cloud_only 選択/ドレインの仕組みは退役 lane の休眠機構 — 実閉鎖policyは test_semantic_cloud で直接検証
from test_cloud_response_recovery import legacy_cloud_routes


@pytest.mark.parametrize("lane", ["semantic", "extract"])
@pytest.mark.parametrize("change", ["reply", "edit", "aba"])
def test_cloud_selection_requires_current_thread_receipt(cloud, lane, change):
    led, _, _, _ = cloud
    if change == "reply":
        led.save_messages([_message(2, parent=1, body="完全合成の新しい返信。")])
    else:
        led.save_messages([_message(body="完全合成の変更。")])
        if change == "aba":
            led.save_messages([_message()])

    def select():
        with legacy_cloud_routes():
            if lane == "semantic":
                return semantic_drain._select_cloud_due(led, "backlog", ("semantic",), False, 1)[0]
            return extract_llm._select_pending(led, 10, None, None, False,
                cloud_only=True, prioritize_patients=True)[0]

    assert not select()
    snapshot(led)
    assert 1 in [row["message_id"] for row in select()]


def test_cloud_selection_releases_run_lock_for_another_process(cloud, tmp_path, monkeypatch):
    led, cfg, _, calls = cloud
    lock_path = tmp_path / "synthetic-run.lock"
    lock_fd = writer_lock.acquire_run_lock(str(lock_path))
    assert lock_fd is not None
    script = """\
import fcntl, os, sys
sys.stdin.readline()
fd = os.open(sys.argv[1], os.O_WRONLY)
try:
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    print('acquired', flush=True)
finally:
    os.close(fd)
"""
    proc = subprocess.Popen([sys.executable, "-c", script, str(lock_path)],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    original = cloud_policy.history_target_ready
    observed = []

    def probe(db, pid, mid):
        if not observed:
            proc.stdin.write("go\n")
            proc.stdin.flush()
            ready, _, _ = select.select([proc.stdout], [], [], 10)
            assert ready, "synthetic lock probe did not finish"
            observed.append(proc.stdout.readline().strip())
        return original(db, pid, mid)

    monkeypatch.setattr(cloud_policy, "history_target_ready", probe)
    monkeypatch.setattr(semantic_drain, "_process_job", lambda *args, **kwargs: "deferred")
    try:
        with legacy_cloud_routes():
            semantic_drain.run_due(led, cfg, {"errors": []}, time.monotonic() + 1200,
                                   lane="backlog", max_jobs=1, cloud_only=True, run_lock_fd=lock_fd)
        assert observed == ["acquired"]
        assert proc.wait(timeout=10) == 0
        assert writer_lock.acquire_run_lock(str(lock_path)) is None
        assert not calls and not rows(led, "cloud_usage_reserve")
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
        proc.stdin.close()
        proc.stdout.close()
        proc.stderr.close()
        os.close(lock_fd)


def test_cloud_selection_refill_reuses_original_deadline(cloud, tmp_path, monkeypatch):
    led, cfg, _, calls = cloud
    for mid in range(2, 12):
        led.save_messages([_message(mid, body="完全合成の過去記録。")])
        led.semantic_seed(1, [mid], {"source": "history_import"})
    snapshot(led)
    lock_fd = writer_lock.acquire_run_lock(str(tmp_path / "synthetic-run.lock"))
    assert lock_fd is not None
    deadline = time.monotonic() + 1200
    original = semantic_drain._select_cloud_due
    selections, attempted = [], []

    def select_recorded(*args, **kwargs):
        if kwargs.get("isolated"):
            selections.append((kwargs.get("deadline"), kwargs.get("isolated")))
        return original(*args, **kwargs)

    @contextmanager
    def held_prefix(_ledger, job_id):
        attempted.append(job_id)
        yield len(attempted) > 9

    monkeypatch.setattr(semantic_drain, "_select_cloud_due", select_recorded)
    monkeypatch.setattr(semantic_due_selection, '_select_cloud_due', semantic_drain._select_cloud_due)
    monkeypatch.setattr(semantic_drain, "_job_lock", held_prefix)
    monkeypatch.setattr(semantic_drain, "_process_job", lambda *args, **kwargs: "deferred")
    try:
        with legacy_cloud_routes():
            semantic_drain.run_due(led, cfg, {"errors": []}, deadline,
                                   lane="backlog", max_jobs=1, cloud_only=True, run_lock_fd=lock_fd)
        assert len(attempted) >= 10 and len(selections) >= 2
        assert all(item == (deadline, True) for item in selections)
        assert not calls and not rows(led, "cloud_usage_reserve")
    finally:
        os.close(lock_fd)


def test_cloud_selection_refill_deadline_does_not_count_remaining_jobs(cloud, tmp_path, monkeypatch):
    led, cfg, _, calls = cloud
    for mid in range(2, 12):
        led.save_messages([_message(mid, body="完全合成の過去記録。")])
        led.semantic_seed(1, [mid], {"source": "history_import"})
    snapshot(led)
    lock_fd = writer_lock.acquire_run_lock(str(tmp_path / "synthetic-run.lock"))
    assert lock_fd is not None
    original = semantic_drain._select_cloud_due
    selections, attempted, statements = [], [], []
    deadline = time.monotonic() + 1200

    def select_until_refill(*args, **kwargs):
        if kwargs.get("isolated"):
            selections.append(kwargs.get("deadline"))
            if len(selections) == 2:
                raise semantic_drain._CloudSelectionIncomplete("deadline")
        return original(*args, **kwargs)

    @contextmanager
    def held_prefix(_ledger, job_id):
        attempted.append(job_id)
        yield False

    monkeypatch.setattr(semantic_drain, "_select_cloud_due", select_until_refill)
    monkeypatch.setattr(semantic_due_selection, '_select_cloud_due', semantic_drain._select_cloud_due)
    monkeypatch.setattr(semantic_drain, "_job_lock", held_prefix)
    monkeypatch.setattr(semantic_drain, "_process_job_inner",
                        lambda *args, **kwargs: pytest.fail("incomplete refill started work"))
    monkeypatch.setattr(semantic_job_processor, '_process_job_inner', semantic_drain._process_job_inner)
    led.db.set_trace_callback(statements.append)
    try:
        with legacy_cloud_routes():
            out = semantic_drain.run_due(led, cfg, {"errors": []}, deadline,
                                         lane="backlog", max_jobs=1, cloud_only=True, run_lock_fd=lock_fd)
        assert len(attempted) == 9 and selections == [deadline, deadline]
        assert out.get("cloud_selection_incomplete") is True and out["left"] is None
        assert out["cloud_selection_reason"] == "deadline"
        assert not any("SELECT COUNT(*) FROM fetch_jobs WHERE kind IN (" in sql for sql in statements)
        assert not calls and not rows(led, "cloud_usage_reserve") and not rows(led, "semantic_usage")
    finally:
        led.db.set_trace_callback(None)
        os.close(lock_fd)


@pytest.mark.parametrize("change", ["job", "source", "receipt", "v2_source", "v2_revision",
                                     "v2_attachment_add", "v2_attachment_withdraw"])
def test_cloud_selection_rechecks_changes_after_unlock(cloud, tmp_path, monkeypatch, change):
    import semantic_store
    led, cfg, job, calls = cloud
    if change == "v2_attachment_withdraw":
        with led.db:
            led.db.execute("INSERT INTO attachments(message_id,file_id,name,state) VALUES(1,?,?,?)",
                           ("synthetic-file", "synthetic.pdf", "pending"))
    if change.startswith("v2_"):
        continuous_snapshot(led)
        original_scope = cloud_policy.history_thread_snapshot(led.db, 1, 1)
        original_bundle = semantic_store.thread_bundle(led.db, 1, 1, [1])["source_fingerprint"]
        assert original_scope and cloud_policy.history_thread_allowed(led.db, 1, 1)
        assert cloud_policy.history_target_ready(led.db, 1, 1)
    lock_path = tmp_path / "synthetic-run.lock"
    lock_fd = writer_lock.acquire_run_lock(str(lock_path))
    assert lock_fd is not None
    original = semantic_drain._select_cloud_due
    db_path = led.db.execute("PRAGMA database_list").fetchone()[2]
    changed = []

    def select_and_change(*args, **kwargs):
        batch = original(*args, **kwargs)
        if kwargs.get("isolated") and not changed and batch[0]:
            with sqlite3.connect(db_path) as other:
                if change == "job":
                    payload = {**json.loads(job["payload"]), "synthetic_revision": 1}
                    other.execute("UPDATE fetch_jobs SET payload=? WHERE job_id=?",
                                  (json.dumps(payload), job["job_id"]))
                elif change in ("source", "v2_source"):
                    body = "完全合成の変更後本文。"
                    html = f"<p>{body}</p>"
                    other.execute("UPDATE messages SET body_text=?,body_html=?,content_hash=? "
                                  "WHERE project_id=1 AND message_id=1",
                                  (body, html, hashlib.sha256(html.encode()).hexdigest()))
                elif change == "v2_revision":
                    other.execute("INSERT INTO message_revisions(message_id,seq,content_hash,prev_content_hash,"
                                  "body_state,observed_at) SELECT m.message_id,"
                                  "(SELECT COALESCE(MAX(seq),1)+1 FROM message_revisions WHERE message_id=m.message_id),"
                                  "m.content_hash,m.content_hash,m.body_state,? FROM messages m "
                                  "WHERE m.project_id=1 AND m.message_id=1", (time.time() - 1200,))
                elif change == "v2_attachment_add":
                    other.execute("INSERT INTO attachments(message_id,file_id,name,state) VALUES(1,?,?,?)",
                                  ("synthetic-file", "synthetic.pdf", "pending"))
                elif change == "v2_attachment_withdraw":
                    other.execute("UPDATE attachments SET state='withdrawn' WHERE message_id=1 AND file_id=?",
                                  ("synthetic-file",))
                else:
                    other.execute("INSERT INTO artifacts(kind,content,model,meta,created_at) VALUES(?,?,?,?,?)",
                                  ("cloud_history_scope", '{"version":1,"sources":{}}', "synthetic", "{}", time.time()))
            changed.append(True)
        return batch

    monkeypatch.setattr(semantic_drain, "_select_cloud_due", select_and_change)
    monkeypatch.setattr(semantic_due_selection, '_select_cloud_due', semantic_drain._select_cloud_due)
    monkeypatch.setattr(semantic_drain, "_process_job_inner",
                        lambda *args, **kwargs: pytest.fail("stale selection started work"))
    monkeypatch.setattr(semantic_job_processor, '_process_job_inner', semantic_drain._process_job_inner)
    try:
        with legacy_cloud_routes():
            out = semantic_drain.run_due(led, cfg, {"errors": []}, time.monotonic() + 1200,
                                         lane="backlog", max_jobs=1, cloud_only=True, run_lock_fd=lock_fd)
        assert changed
        assert out.get("cloud_selection_changed") is True
        if change.startswith("v2_"):
            assert cloud_policy.history_thread_allowed(led.db, 1, 1)
            assert cloud_policy.history_target_ready(led.db, 1, 1)
            if change.startswith("v2_attachment_"):
                assert cloud_policy.history_thread_snapshot(led.db, 1, 1) == original_scope
                assert semantic_store.thread_bundle(led.db, 1, 1, [1])["source_fingerprint"] != original_bundle
            else:
                assert cloud_policy.history_thread_snapshot(led.db, 1, 1) != original_scope
        assert not calls and not rows(led, "cloud_usage_reserve") and not rows(led, "semantic_usage")
        current = led.db.execute("SELECT state,attempts FROM fetch_jobs WHERE job_id=?", (job["job_id"],)).fetchone()
        assert tuple(current) == ("pending", job["attempts"])
    finally:
        os.close(lock_fd)


def test_cloud_selection_resumes_pages_and_preserves_sql_order(cloud, monkeypatch):
    from types import SimpleNamespace
    led, cfg, _, calls = cloud
    cfg['cloud_llm']['project_ids'] = None
    for pid in range(2, 7):
        _patient(led, pid)
        led.save_messages([_message(pid, pid=pid)])
        led.semantic_seed(pid, [pid], {'source': 'history_import'})
    with led.db:
        for pid, ts in [(1, 100), (2, 300), (3, 300), (4, None), (5, 400), (6, 500)]:
            led.db.execute('UPDATE messages SET posted_at_ts=? WHERE project_id=?', (ts, pid))
        led.db.execute('UPDATE patients SET is_archived=1 WHERE project_id=6')
        led.db.execute('DELETE FROM patients WHERE project_id=5')
    snapshot(led)
    monkeypatch.setattr(semantic_drain, '_CLOUD_SELECTION_PAGE', 2)
    monkeypatch.setattr(semantic_due_selection, '_CLOUD_SELECTION_PAGE', semantic_drain._CLOUD_SELECTION_PAGE)
    clock = [0.0]
    monkeypatch.setattr(semantic_drain, 'time', SimpleNamespace(time=time.time, monotonic=lambda: clock[0]))
    monkeypatch.setattr(semantic_due_selection, 'time', semantic_drain.time)
    monkeypatch.setattr(cloud_inference, 'time', semantic_drain.time)
    cursors = []
    for _ in range(3):
        clock[0] = 0.0
        led.db.set_trace_callback(lambda sql: clock.__setitem__(0, 1.0)
            if sql.startswith('SELECT j.job_id,j.kind') else None)
        try:
            with pytest.raises(semantic_drain._CloudSelectionIncomplete, match='deadline'):
                with legacy_cloud_routes():
                    semantic_drain._select_cloud_due(led, 'backlog', ('semantic',), False, 10, deadline=1.0)
        finally:
            led.db.set_trace_callback(None)
        cursors.append(semantic_due_selection._cloud_selection_progress['cursor'])
        with led.db:
            led.db.execute("UPDATE patients SET station_name='synthetic' WHERE project_id=1")
    assert cursors == sorted(set(cursors)) and len(cursors) == 3
    clock[0] = 0.0
    with legacy_cloud_routes():
        result = semantic_drain._select_cloud_due(led, 'backlog', ('semantic',), False, 10, deadline=10.0)[0]
    assert [row['project_id'] for row in result] == [5, 2, 3, 1, 4, 6]
    assert not calls and not led.db.in_transaction


def test_cloud_selection_rechecks_scope_and_deleted_job_after_incomplete(cloud, monkeypatch):
    from types import SimpleNamespace
    led, cfg, job, calls = cloud
    for mid in range(2, 5):
        led.save_messages([_message(mid)])
        led.semantic_seed(1, [mid], {'source': 'history_import'})
    snapshot(led)
    monkeypatch.setattr(semantic_drain, '_CLOUD_SELECTION_PAGE', 1)
    monkeypatch.setattr(semantic_due_selection, '_CLOUD_SELECTION_PAGE', semantic_drain._CLOUD_SELECTION_PAGE)
    clock = [0.0]
    monkeypatch.setattr(semantic_drain, 'time', SimpleNamespace(time=time.time, monotonic=lambda: clock[0]))
    monkeypatch.setattr(semantic_due_selection, 'time', semantic_drain.time)
    monkeypatch.setattr(cloud_inference, 'time', semantic_drain.time)
    led.db.set_trace_callback(lambda sql: clock.__setitem__(0, 1.0)
        if sql.startswith('SELECT j.job_id,j.kind') else None)
    try:
        with pytest.raises(semantic_drain._CloudSelectionIncomplete, match='deadline'):
            with legacy_cloud_routes():
                semantic_drain._select_cloud_due(led, 'backlog', ('semantic',), False, 10, deadline=1.0)
    finally:
        led.db.set_trace_callback(None)
    with led.db:
        led.db.execute('DELETE FROM fetch_jobs WHERE job_id=?', (job['job_id'],))
        led.db.execute("UPDATE artifacts SET content=? WHERE artifact_id=(SELECT MAX(artifact_id) "
                       "FROM artifacts WHERE kind='cloud_history_scope')",
                       ('{"version":1,"sources":{}}',))
    clock[0] = 0.0
    with legacy_cloud_routes():
        result = semantic_drain._select_cloud_due(led, 'backlog', ('semantic',), False, 10, deadline=10.0)[0]
    assert result == [] and not calls and not led.db.in_transaction


def test_cloud_selection_refill_advances_job_ids_without_reselecting_excluded(cloud):
    led, _, _, calls = cloud
    for mid in range(2, 16):
        led.save_messages([_message(mid)])
        led.semantic_seed(1, [mid], {'source': 'history_import'})
    snapshot(led)
    with legacy_cloud_routes():
        first = semantic_drain._select_cloud_due(led, 'backlog', ('semantic',), False, 1)[0]
    first_ids = [row['job_id'] for row in first]
    with legacy_cloud_routes():
        second = semantic_drain._select_cloud_due(led, 'backlog', ('semantic',), False, 1,
                                                  excluded=first_ids)[0]
    second_ids = [row['job_id'] for row in second]
    assert len(first_ids) == 9 and second_ids
    assert first_ids == sorted(first_ids) and second_ids == sorted(second_ids)
    assert max(first_ids) < min(second_ids)
    assert not calls


def test_cloud_selection_revalidates_partial_batch_after_deadline(cloud, monkeypatch):
    from types import SimpleNamespace
    led, _, job, calls = cloud
    led.save_messages([_message(2)])
    led.semantic_seed(1, [2], {'source': 'history_import'})
    snapshot(led)
    clock = [0.0]
    monkeypatch.setattr(semantic_drain, 'time', SimpleNamespace(time=time.time, monotonic=lambda: clock[0]))
    monkeypatch.setattr(semantic_due_selection, 'time', semantic_drain.time)
    monkeypatch.setattr(cloud_inference, 'time', semantic_drain.time)
    ready = cloud_policy.history_target_ready
    checked = []

    def expire_on_second(db, pid, mid):
        checked.append(mid)
        if len(checked) == 2:
            clock[0] = 1.0
        return ready(db, pid, mid)

    monkeypatch.setattr(cloud_policy, 'history_target_ready', expire_on_second)
    with pytest.raises(semantic_drain._CloudSelectionIncomplete, match='deadline'):
        with legacy_cloud_routes():
            semantic_drain._select_cloud_due(led, 'backlog', ('semantic',), False, 1, deadline=1.0)
    assert checked == [1, 2] and not led.db.in_transaction
    monkeypatch.setattr(cloud_policy, 'history_target_ready', ready)
    clock[0] = 0.0
    with legacy_cloud_routes():
        selected = semantic_drain._select_cloud_due(led, 'backlog', ('semantic',), False, 1, deadline=10.0)[0]
    assert [row['job_id'] for row in selected] == [job['job_id']]
    with legacy_cloud_routes():
        later = semantic_drain._select_cloud_due(led, 'backlog', ('semantic',), False, 1,
                                                excluded=[job['job_id']], deadline=10.0)[0]
    assert [row['job_id'] for row in later] == [job['job_id'] + 1]
    assert not calls


def test_cloud_selection_invalidates_progress_when_config_changes(cloud, monkeypatch):
    from types import SimpleNamespace
    led, cfg, _, calls = cloud
    led.save_messages([_message(2)])
    led.semantic_seed(1, [2], {'source': 'history_import'})
    snapshot(led)
    monkeypatch.setattr(semantic_drain, '_CLOUD_SELECTION_PAGE', 1)
    monkeypatch.setattr(semantic_due_selection, '_CLOUD_SELECTION_PAGE', semantic_drain._CLOUD_SELECTION_PAGE)
    clock = [0.0]
    monkeypatch.setattr(semantic_drain, 'time', SimpleNamespace(time=time.time, monotonic=lambda: clock[0]))
    monkeypatch.setattr(semantic_due_selection, 'time', semantic_drain.time)
    monkeypatch.setattr(cloud_inference, 'time', semantic_drain.time)
    led.db.set_trace_callback(lambda sql: clock.__setitem__(0, 1.0)
        if sql.startswith('SELECT j.job_id,j.kind') else None)
    try:
        with pytest.raises(semantic_drain._CloudSelectionIncomplete, match='deadline'):
            with legacy_cloud_routes():
                semantic_drain._select_cloud_due(led, 'backlog', ('semantic',), False, 1, deadline=1.0)
    finally:
        led.db.set_trace_callback(None)
    old = semantic_due_selection._cloud_selection_progress['binding']
    cfg['cloud_llm']['project_ids'] = []
    clock[0] = 0.0
    with legacy_cloud_routes():
        selected = semantic_drain._select_cloud_due(led, 'backlog', ('semantic',), False, 1, deadline=10.0)[0]
    assert selected == [] and semantic_due_selection._cloud_selection_progress is None
    assert old is not None and not calls


def test_cloud_selection_memory_failure_is_incomplete(cloud, monkeypatch):
    led, _, _, calls = cloud

    def out_of_memory(*args, **kwargs):
        raise MemoryError('synthetic')

    monkeypatch.setattr(semantic_drain.mcs_util, 'payload_hash', out_of_memory)
    with pytest.raises(semantic_drain._CloudSelectionIncomplete, match='memory'):
        with legacy_cloud_routes():
            semantic_drain._select_cloud_due(led, 'backlog', ('semantic',), False, 1)
    assert semantic_due_selection._cloud_selection_progress is None
    assert not calls and not led.db.in_transaction


def test_cloud_selection_incomplete_invalid_suffix_advances(cloud, monkeypatch):
    from collections import Counter
    from types import SimpleNamespace
    led, _, _, calls = cloud
    for mid in range(2, 13):
        led.save_messages([_message(mid)])
        led.semantic_seed(1, [mid], {'source': 'history_import'})
    snapshot(led)
    clock = [0.0]
    monkeypatch.setattr(semantic_drain, 'time', SimpleNamespace(time=time.time, monotonic=lambda: clock[0]))
    monkeypatch.setattr(semantic_due_selection, 'time', semantic_drain.time)
    monkeypatch.setattr(cloud_inference, 'time', semantic_drain.time)
    ready = cloud_policy.history_target_ready
    observed = Counter()

    def sparse(db, pid, mid):
        observed[mid] += 1
        if 2 <= mid <= 11:
            clock[0] += .4
            return False
        return ready(db, pid, mid)

    monkeypatch.setattr(cloud_policy, 'history_target_ready', sparse)
    delivered = []
    incomplete = 0
    for _ in range(15):
        clock[0] = 0.0
        try:
            with legacy_cloud_routes():
                batch = semantic_drain._select_cloud_due(led, 'backlog', ('semantic',), False, 1, deadline=1.0)[0]
        except semantic_drain._CloudSelectionIncomplete:
            incomplete += 1
            continue
        if batch:
            mid = batch[0]['message_id']
            delivered.append(mid)
            with led.db:
                led.db.execute("UPDATE fetch_jobs SET state='done' WHERE job_id=?", (batch[0]['job_id'],))
        if delivered == [1, 12]:
            break
    assert incomplete >= 2 and delivered == [1, 12]
    assert all(observed[mid] == 1 for mid in range(2, 12))
    assert not calls and not led.db.in_transaction


def test_cloud_selection_highwater_defers_new_job_to_next_cycle(cloud, monkeypatch):
    from types import SimpleNamespace
    led, _, _, calls = cloud
    led.save_messages([_message(2)])
    led.semantic_seed(1, [2], {'source': 'history_import'})
    continuous_snapshot(led)
    with led.db:
        led.db.execute('UPDATE messages SET first_seen=?', (time.time() - 601,))
        led.db.execute('UPDATE message_revisions SET observed_at=?', (time.time() - 601,))
    monkeypatch.setattr(semantic_drain, '_CLOUD_SELECTION_PAGE', 1)
    monkeypatch.setattr(semantic_due_selection, '_CLOUD_SELECTION_PAGE', semantic_drain._CLOUD_SELECTION_PAGE)
    clock = [0.0]
    monkeypatch.setattr(semantic_drain, 'time', SimpleNamespace(time=time.time, monotonic=lambda: clock[0]))
    monkeypatch.setattr(semantic_due_selection, 'time', semantic_drain.time)
    monkeypatch.setattr(cloud_inference, 'time', semantic_drain.time)
    led.db.set_trace_callback(lambda sql: clock.__setitem__(0, 1.0)
        if sql.startswith('SELECT j.job_id,j.kind') else None)
    try:
        with pytest.raises(semantic_drain._CloudSelectionIncomplete, match='deadline'):
            with legacy_cloud_routes():
                semantic_drain._select_cloud_due(led, 'backlog', ('semantic',), False, 10, deadline=1.0)
    finally:
        led.db.set_trace_callback(None)
    led.save_messages([_message(3)])
    led.semantic_seed(1, [3], {'source': 'history_import'})
    with led.db:
        led.db.execute('UPDATE messages SET first_seen=? WHERE message_id=3', (time.time() - 601,))
        led.db.execute('UPDATE message_revisions SET observed_at=? WHERE message_id=3', (time.time() - 601,))
    clock[0] = 0.0
    with legacy_cloud_routes():
        first = semantic_drain._select_cloud_due(led, 'backlog', ('semantic',), False, 10, deadline=10.0)[0]
    assert [r['message_id'] for r in first] == [1, 2]
    with led.db:
        led.db.execute("UPDATE fetch_jobs SET state='done' WHERE message_id IN (1,2)")
    with legacy_cloud_routes():
        assert not semantic_drain._select_cloud_due(led, 'backlog', ('semantic',), False, 10, deadline=10.0)[0]
    with legacy_cloud_routes():
        later = semantic_drain._select_cloud_due(led, 'backlog', ('semantic',), False, 10, deadline=10.0)[0]
    assert [r['message_id'] for r in later] == [3]
    assert not calls


def test_cloud_selection_invalidates_replaced_database_inode(cloud, tmp_path, monkeypatch):
    from types import SimpleNamespace
    led, _, _, calls = cloud
    led.save_messages([_message(2)])
    led.semantic_seed(1, [2], {'source': 'history_import'})
    snapshot(led)
    monkeypatch.setattr(semantic_drain, '_CLOUD_SELECTION_PAGE', 1)
    monkeypatch.setattr(semantic_due_selection, '_CLOUD_SELECTION_PAGE', semantic_drain._CLOUD_SELECTION_PAGE)
    clock = [0.0]
    monkeypatch.setattr(semantic_drain, 'time', SimpleNamespace(time=time.time, monotonic=lambda: clock[0]))
    monkeypatch.setattr(semantic_due_selection, 'time', semantic_drain.time)
    monkeypatch.setattr(cloud_inference, 'time', semantic_drain.time)

    def interrupt_after_page():
        clock[0] = 0.0
        led.db.set_trace_callback(lambda sql: clock.__setitem__(0, 1.0)
            if sql.startswith('SELECT j.job_id,j.kind') else None)
        try:
            with pytest.raises(semantic_drain._CloudSelectionIncomplete, match='deadline'):
                with legacy_cloud_routes():
                    semantic_drain._select_cloud_due(led, 'backlog', ('semantic',), False, 1, deadline=1.0)
        finally:
            led.db.set_trace_callback(None)

    interrupt_after_page()
    old = semantic_due_selection._cloud_selection_progress['binding']
    path = led.db.execute('PRAGMA database_list').fetchone()[2]
    replacement = tmp_path / 'synthetic-replacement.db'
    dest = sqlite3.connect(replacement)
    try:
        led.db.backup(dest)
    finally:
        dest.close()
    led.close()
    os.replace(replacement, path)
    led.db = sqlite3.connect(path)
    led.db.row_factory = sqlite3.Row
    interrupt_after_page()
    progress = semantic_due_selection._cloud_selection_progress
    assert progress['cursor'] == 1
    assert progress['binding'][0][0] == old[0][0]
    assert progress['binding'][0][1:] != old[0][1:]
    assert progress['binding'][1:] == old[1:]
    assert not calls and not led.db.in_transaction
