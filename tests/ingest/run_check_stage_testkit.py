"""Shared synthetic stage fixtures and adapters without collected test functions."""
import extract_llm_resident
import collection_stages
import health_report
import mcs_contract
import json
import time
from types import SimpleNamespace
import pytest
import extract_llm
import mcs_adapter
import run_check
from ingest_testkit import _ledger, _message, _unread_patient, _att


@pytest.fixture(autouse=True)
def _no_disk_guard(monkeypatch):
    # the attachment disk guard reads the real volume — disable it so
    # these stage tests never depend on the host's free space
    monkeypatch.setenv("MCS_DISK_GUARD_MB", "0")


def _stub_llm_lane(monkeypatch, busy=False):
    calls = []
    monkeypatch.setattr(extract_llm, "pinned_slot_busy", lambda deadline: busy)
    monkeypatch.setattr(extract_llm, "run_pending", lambda ledger, **kw: (
        calls.append(kw) or {"done": 0, "failed": 0, "left": 0, "pids": []}))
    monkeypatch.setattr(extract_llm_resident, 'run_pending', extract_llm.run_pending)
    return calls


def _health_unread_at(tmp_path, monkeypatch, unread_at):
    path = tmp_path / "health.json"
    path.write_text(json.dumps({"overall": "failed", "unread_at": unread_at}))
    monkeypatch.setattr(health_report, 'HEALTH_FILE', str(path))


def _expire_once():
    """Stage stub: raw SessionExpired on the first call, clean on the
    post-recovery replay."""
    state = {"n": 0}

    def stage(*a, **k):
        state["n"] += 1
        if state["n"] == 1:
            raise mcs_contract.SessionExpired(status=403)
        return None
    return stage


def _always_expired(*a, **k):
    raise mcs_contract.SessionExpired(status=403)


def _point_run_check_at(tmp_path, monkeypatch):
    data = tmp_path / "data"
    for name, value in {"HOME": tmp_path, "DB": data / "ledger.db",
                        "ATTACH_DIR": data / "attachments",
                        "LOCKFILE": data / "run.lock",
                        "HEALTH_FILE": data / "health.json"}.items():
        monkeypatch.setattr(
            collection_stages if name in {"ATTACH_DIR", "with_relogin"} else (health_report if name == "HEALTH_FILE" else run_check),
            name, str(value))
    data.mkdir(exist_ok=True)
    return data


def _pinned_history_adapter(count=32, created_at="2026-09-19T01:00:00+09:00"):
    """Synthetic timeline longer than BACKFILL_MAX_PAGES, newer than the
    stored head — the bounded backfill cannot certify it."""
    items = [{"id": mid, "comment": "synthetic post",
              "created_at": created_at}
             for mid in range(1, count + 1)]

    class Adapter(mcs_adapter.MCSAdapter):
        def _get(self, path, params=None, **kwargs):
            start = (params["page"] - 1) * params["per_page"]
            return {"messages": items[start:start + params["per_page"]],
                    "paginate": {"has_next": start + params["per_page"] < len(items)}}
    return Adapter()


def _lagging_patient(tmp_path):
    db = _ledger(tmp_path)
    db.ensure_patient(1)
    db.save_messages([_message(mid=100)])
    db.set_history_floor(1, 0)
    db.set_coverage(1, db.high_watermark(1))
    return db


def _pending_attachments(tmp_path, n=2):
    db = _ledger(tmp_path)
    message = _message()
    message.attachments = [_att(f"file{i}") for i in range(n)]
    db.save_messages([message])
    return db


# ---- in-tick semantic run guards (2026-09-30 starvation incident) ----

def _sem_args():
    return SimpleNamespace(jobs_only=False)


def _sem_setup(monkeypatch, tmp_path, busy=False):
    """busy: False, True, or a list consumed one probe at a time."""
    monkeypatch.setattr(health_report, 'prev_health', lambda: {})
    samples = list(busy) if isinstance(busy, list) else None
    monkeypatch.setattr(extract_llm, "pinned_slot_busy",
                        lambda deadline: samples.pop(0) if samples else bool(busy) if samples is None else False)
    monkeypatch.setattr(run_check.time, "sleep", lambda s: None)


# ---- F-5: unread-capped patients are never acknowledged early ----

def _capped_run(db, adapter):
    result = {"errors": [], "incomplete": [], "messages": 0,
              "new_messages": 0, "marked_read": []}
    collection_stages.stage_unread(adapter, db, result, time.monotonic() + 30, db.begin_run(None), mark_read=True)
    return result


def _certified_capped_db(tmp_path):
    db = _ledger(tmp_path)
    db.upsert_patient_info(_unread_patient(1))
    db.save_messages([_message(mid=5, unread=True,
                               posted_at="2026-09-01T00:00:00+09:00")], project_id=1)
    db.set_history_floor(1, 0)
    db.set_coverage(1, db.high_watermark(1))
    return db


# ---------- stage: 連携サマリー ----------

class _MemoAdapter:
    def __init__(self, fail=(), empty=(), gone=()):
        self.calls = []
        self.fail = set(fail)      # 5xx: retryable
        self.gone = set(gone)      # 404: not retryable
        self.empty = set(empty)
        self.marked = []

    def fetch_memo_summary(self, karte_id):
        self.calls.append(karte_id)
        if karte_id in self.fail:
            raise mcs_contract.MCSError("http_error", status=500,
                                       retryable=True)
        if karte_id in self.gone:
            raise mcs_contract.MCSError("http_error", status=404)
        if karte_id in self.empty:
            return None
        return {"comment": f"合成 {karte_id}", "updated_at": "", "is_editable": True,
                "user": {"profession": "", "name": ""}}

    def mark_patient_read(self, *a, **k):
        self.marked.append(a)


def _karte_db(tmp_path, n, artifact=False):
    db = _ledger(tmp_path)
    for pid in range(1, n + 1):
        p = _unread_patient(pid)
        p.karte_id = pid * 10
        db.upsert_patient_info(p)
        if artifact:
            db.karte_summary_store(pid, pid * 10, None)
    db.db.execute("UPDATE patients SET last_seen=project_id")
    db.db.commit()
    return db


def _run_stage(adapter, db, targets=(), jobs_only=False, deadline=None):
    for pid in targets:                     # what the save paths do
        db.karte_summary_mark_due(pid)
    result = {"errors": [], "incomplete": []}
    collection_stages.stage_karte_summary(
        adapter, db, result, deadline or time.monotonic() + 300,
        jobs_only=jobs_only)
    return result
