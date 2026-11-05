"""Synthetic extract llm testkit contracts with isolated shared fixtures."""
import extract_llm_runner
import json
import urllib.error
import urllib.request
import extract_llm
from inference_version import CURRENT_VERSION, VERSION_KEY
from extract_testkit import _hash, _ledger, _message


def _request_from_opener(opener):
    """Keep scripted HTTP fixtures behind the bounded transport's request seam."""
    def request(endpoint, method, body, timeout, deadline=None):
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode()
        req = urllib.request.Request(endpoint, data=data, method=method)
        try:
            with opener.open(req, timeout=timeout) as response:
                return 200, {}, response.read()
        except urllib.error.HTTPError as error:
            return error.code, dict(error.headers or {}), b""
    return request


def _v1_artifact(db, mid, content, chash):
    """A legacy row: hash-current but no extract_version."""
    db.artifact_add("extract_llm", json.dumps(content),
                    project_id=1, message_id=mid,
                    meta={"hash": chash})


# ---------- lazy-replace version migration ----------

def _run(db, monkeypatch, result):
    monkeypatch.setattr(extract_llm, "llm_extract", lambda body, **_: result)
    monkeypatch.setattr(extract_llm_runner, 'llm_extract', extract_llm.llm_extract)
    return extract_llm.run_pending(db, limit=10, budget_s=30)


# ---------- consumer filters ----------

def _llm_artifact(db, mid, content, chash):
    db.artifact_add(
        "extract_llm", json.dumps(content), project_id=1,
        message_id=mid, meta={VERSION_KEY: CURRENT_VERSION, "hash": chash,
                              "extract_version": extract_llm.EXTRACT_VERSION})


# --- #21 step 3: 患者連携サマリー reference-only block -----------------

def _karte(db, comment, project_id=1, updated_at="2026-09-18T10:00:00+09:00"):
    return db.karte_summary_store(project_id, 10, None if comment is None else {
        "comment": comment, "updated_at": updated_at,
        "is_editable": True, "user": {"profession": "看護師", "name": "合成"}})


# --- Phase 3: response_format probe + chunked coverage --------------

def _http_error(code):
    import urllib.error
    return urllib.error.HTTPError("http://x", code, "e", {}, None)


class _Opener:
    """Scripted opener: responds per request's response_format."""

    def __init__(self, script):
        self.script = script          # {"schema": "ok"|code, ...}
        self.calls = []

    def open(self, req, timeout=None):
        import io
        data = json.loads(req.data.decode())
        self.calls.append(data)
        rf = data.get("response_format") or {}
        mode = ("schema" if rf.get("type") == "json_schema"
                else "object" if rf.get("type") == "json_object"
                else "plain")
        verdict = self.script.get(mode, "ok")
        if verdict != "ok":
            raise _http_error(verdict)
        probe = rf.get("json_schema") or {}
        content = ('{"probe":"schema"}'
                   if probe.get("name") == "mcs_format_probe"
                   else '{"ok":true}')
        return io.BytesIO(json.dumps(
            {"choices": [{"message": {"content": content}, "finish_reason": "stop"}]}
        ).encode())


# ---------- batched multi-target extraction ----------

def _batch_ledger(tmp_path, n=4, ensure=True):
    db = _ledger(tmp_path)
    if ensure:
        db.ensure_patient(1)
    db.save_messages([_message(mid=i, body=f"本文{i} カロナール服用")
                      for i in range(1, n + 1)])
    return db


def _seed_human_report(db, body="合成の本文 BT37.5 咳あり", content=None):
    """A current artifact plus a ⚠ report pinned to it."""
    db.save_messages([_message(body=body)])
    aid = db.artifact_add(
        "extract_llm", json.dumps(content or {
            "summary": "旧", "symptoms": [{"text": "咳"}],
            "vitals": {"bt": 37.5}}),
        project_id=1, message_id=1,
        meta={VERSION_KEY: CURRENT_VERSION, "hash": _hash(db),
              "extract_version": extract_llm.EXTRACT_VERSION})
    fid = db.artifact_add(
        "extract_feedback_v1",
        json.dumps({"message_id": 1, "artifact_id": aid, "field": "meds",
                    "note": "合成"}),
        project_id=1, message_id=1, meta={"source_artifact_id": aid})
    return aid, fid
