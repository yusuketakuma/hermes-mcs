"""Share synthetic ledger fixtures and signal source builders without collecting tests."""
import json
import mcs_signals
from semantic_projection import PROJECTION_VERSION
from inference_version import CURRENT_VERSION, VERSION_KEY
from ops_testkit import DAY, NOW, _msg
from common_testkit import ledger_fixture
from views_testkit import _extract as _views_extract


led = ledger_fixture()


def _extract_llm(db, mid, chash, meds, events=None):
    """Seed a current extraction without changing the shared view fixture."""
    _views_extract(db, mid, chash, meds, events=events)
    db.execute("UPDATE artifacts SET meta=json_set(meta, ?, ?) "
               "WHERE artifact_id=last_insert_rowid()",
               (f"$.{VERSION_KEY}", CURRENT_VERSION))


def _extract_doc(db, mid, chash, **fields):
    """extract_llm artifact with an arbitrary v2 document — requests,
    symptoms, urgency, events, meds."""
    db.execute(
        "INSERT INTO artifacts(kind,message_id,content,meta) "
        "VALUES ('extract_llm',?,?,?)",
        (mid, json.dumps(fields),
         json.dumps({"hash": chash, VERSION_KEY: CURRENT_VERSION})))


def _req(db, status, due=None, src_mid=1, created=NOW):
    db.execute(
        "INSERT INTO requests(project_id,source_message_id,source_hash,"
        "title,status,due_date,revision,created_at,updated_at) "
        "VALUES (1,?,?,?,?,?,1,?,?)",
        (src_mid, "h" * 64, "t", status, due, created, created))


def _states(db):
    """latest state per signal key (append-only model)"""
    out = {}
    for content, meta in db.execute(
            "SELECT content, meta FROM artifacts WHERE kind='signal_v1' "
            "ORDER BY artifact_id"):
        meta_d, content_d = json.loads(meta), json.loads(content)
        if isinstance(meta_d, dict) and isinstance(content_d, dict):
            out[meta_d["key"]] = content_d["state"]
    return out


def _ev(lg, cfg=None, now=NOW):
    return mcs_signals.evaluate(lg, cfg or {}, now=now)


def _projection(db, mid, chash, content, pid=1):
    db.execute(
        "INSERT INTO artifacts(kind,project_id,message_id,content,meta) "
        "VALUES ('canonical_projection',?,?,?,?)",
        (pid, mid, json.dumps(content),
         json.dumps({"hash": chash, "projection_version": PROJECTION_VERSION,
                     VERSION_KEY: CURRENT_VERSION})))


def _sig_ev(led):
    row = led.db.execute(
        "SELECT kind, payload FROM notify_outbox").fetchone()
    return {"kind": row["kind"], "payload": row["payload"]}


def _dismiss(led, key, pid=1, reason="原記録を確認済み"):
    import mcs_requests
    from uuid import uuid4
    req = {"cmd": "ops.signal_dismiss", "version": 1,
           "command_id": str(uuid4()), "actor": "tester",
           "human_confirmed": True, "project_id": pid,
           "signal_key": key, "reason": reason}
    return mcs_requests.apply_command(led, req)


def _policy(led, policy, reason="閾値承認"):
    import mcs_requests
    from uuid import uuid4
    req = {"cmd": "ops.signal_policy", "version": 1,
           "command_id": str(uuid4()), "actor": "tester",
           "human_confirmed": True, "project_id": 1,
           "policy": policy, "reason": reason}
    return mcs_requests.apply_command(led, req)


def _set_meds(db, meds, mid=1):
    """Rewrite the med list of one extract_llm artifact — the detector
    toggle used to resolve/reopen med signals between evaluations."""
    db.execute(
        "UPDATE artifacts SET content=? WHERE kind='extract_llm' "
        "AND message_id=?", (json.dumps({"meds": meds}), mid))


def _outbox_payloads(led):
    return [json.loads(r[0]) for r in led.db.execute(
        "SELECT payload FROM notify_outbox ORDER BY event_id")]


def _req_item(to, action, unverified=None, kind=None):
    r = {"to": to, "action": action}
    if unverified is not None:
        r["unverified"] = unverified
    if kind is not None:
        r["kind"] = kind
    return r


_MISSING = object()


SELF_REACT_CFG = {"signals": {"self_reaction_response": True}}


def _capture(db, mid, reactions, observed=NOW - DAY):
    """capture metadata row for mid; reactions=None leaves it unfetched."""
    content = {} if reactions is None else {
        "reactions": {"value": reactions, "observed_at": observed}}
    db.execute(
        "INSERT INTO message_metadata(message_id,source,content,checked_at,"
        "last_error) VALUES(?,?,?,?,NULL) ON CONFLICT(message_id,source) "
        "DO UPDATE SET content=excluded.content",
        (mid, "capture", json.dumps(content), observed))


def _stamp(type_, self_reacted, count=1):
    return {"type": type_, "count": count, "self_reacted": self_reacted}


def _ph_open(db):
    return [s for s in mcs_signals.current_open(db)["items"]
            if s["type"] == "pharmacist_request_unanswered"]


def _ph_request(db):
    _msg(db, 1, ts=NOW - 4 * DAY)
    _extract_doc(db, 1, "h1",
                 requests=[_req_item("薬剤師", "残薬調整の確認")])


def _side_tables(db):
    return {t: [tuple(r) for r in db.execute(f"SELECT * FROM {t} ORDER BY 1")]
            for t in ("requests", "notification_triage",
                      "notification_acknowledgements")}


def _extract_llm_meds(db, mid, chash, meds):
    db.execute(
        "INSERT INTO artifacts(kind,message_id,content,meta) "
        "VALUES ('extract_llm',?,?,?)",
        (mid, json.dumps({"meds": meds}),
         json.dumps({"hash": chash, VERSION_KEY: CURRENT_VERSION})))


def _open_types(db):
    return {s["type"] for s in mcs_signals.current_open(db)["items"]}
