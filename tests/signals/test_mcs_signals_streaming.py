"""Pin that the window-scanning detectors stream one statement without
changing output, ordering or read snapshot (synthetic data only)."""
import gc
import json

import pytest

import mcs_signals
import signal_policy
from inference_version import CURRENT_VERSION, VERSION_KEY
from ops_testkit import DAY, NOW, _msg
from signal_detector_testkit import led as led  # noqa: F401

N = 300
ROOMS = 7
# 292/299 and 6/26 are the candidates at positions 255/256 of the request
# and adherence detectors (see the boundary test, which re-derives them).
REGISTERED = ({1, 300, 292, 299, 6, 26} | set(range(11, N + 1, 11)))
MED_ROOMS = 30
MED_MENTIONS = 11                    # > 10: mention_count and mids[-10:] differ
STREAMED = ("_med_followup", "_pharmacist_request",
            "_rx_request_visibility", "_adherence_concern")
ENGAGEMENT_BATCH = ("_pharmacist_request", "_rx_request_visibility",
                    "_adherence_concern")


def _pid(mid):
    return 1 + mid % ROOMS


def _copies(mid):
    return 1 + mid % 2               # odd mids: duplicate current artifacts (ties)


def _add_artifact(db, mid, doc):
    db.execute(
        "INSERT INTO artifacts(kind,message_id,content,meta) "
        "VALUES ('extract_llm',?,?,?)",
        (mid, json.dumps(doc),
         json.dumps({"hash": f"h{mid}", VERSION_KEY: CURRENT_VERSION})))


def _seed(db):
    """300 posts across 7 rooms (crossing the 256-item engagement batch)
    plus 30 rooms with long med-change histories."""
    for mid in range(1, N + 1):
        pid = _pid(mid)
        # mid%5==0: meds-only; mid%5==1: phrase-only; others: both sources
        body = "特記事項なし。" if mid % 5 == 0 else "飲み忘れがあります。"
        _msg(db, mid, pid=pid, ts=NOW - (5 + mid % 4) * DAY,
             chash=f"h{mid}", body=body)
        meds = [{"name": f"合成薬{mid % 5}", "action": "start"}]
        if mid % 5 != 1:
            meds.append({"name": "合成薬X", "action": "stop", "negated": True})
        doc = {"meds": meds,
               "requests": [{"to": "薬剤師", "action": f"処方確認{mid % 3}"},
                            {"to": "医師", "action": "残薬の処方調整"},
                            {"to": "調剤薬局", "action": "一包化"}]}
        for _ in range(_copies(mid)):
            _add_artifact(db, mid, doc)
        if mid in REGISTERED:
            db.execute(
                "INSERT INTO requests(project_id,source_message_id,source_hash,"
                "title,status,revision,created_at,updated_at) "
                "VALUES (?,?,?,'synthetic','open',1,?,?)",
                (pid, mid, f"h{mid}", NOW, NOW))
    for r in range(MED_ROOMS):
        pid = 100 + r
        for k in range(MED_MENTIONS):
            mid = 1000 + r * MED_MENTIONS + k
            _msg(db, mid, pid=pid, ts=NOW - (8 + 8 * k) * DAY, chash=f"h{mid}")
            _add_artifact(db, mid, {"meds": [
                {"name": f"合成薬{k % 2}", "action": "start"},
                {"name": "合成薬Z", "action": "stop"}]})
    db.commit()


@pytest.fixture
def seeded(led):  # noqa: F811
    _seed(led.db)
    return led, signal_policy.resolved_thresholds(led.db)


def _expected(name, th):
    out = []
    posts = sorted((m for m in range(1, N + 1) if m not in REGISTERED),
                   key=lambda m: (_pid(m), m))
    if name == "_med_followup":
        for r in range(MED_ROOMS):
            base = 1000 + r * MED_MENTIONS
            for med, ks in (("合成薬0", range(0, MED_MENTIONS, 2)),
                            ("合成薬1", range(1, MED_MENTIONS, 2)),
                            ("合成薬Z", range(MED_MENTIONS))):
                mids = [base + k for k in ks]
                # evidence follows posting time (oldest first); the fixture
                # posts at NOW-(8+8k)d so larger k is older -> mid descending
                out.append((f"med_change_no_followup:{100 + r}:{med}", {
                    "message_ids": mids[::-1][-10:], "mention_count": len(mids)}))
        return out
    if name == "_pharmacist_request":
        for m in posts:
            acts = [a for _ in range(_copies(m))
                    for a in (f"処方確認{m % 3}", "一包化")]
            out.append((f"pharmacist_request_unanswered:{_pid(m)}:{m}", {
                "message_ids": [m], "request_actions": acts}))
        return out
    if name == "_rx_request_visibility":
        for m in posts:
            out.append((f"rx_request_visibility:{_pid(m)}:{m}", {
                "message_ids": [m],
                "request_actions": ["残薬の処方調整"] * _copies(m)}))
        return out
    # adherence: meds-source keys first (sorted), then phrase-only keys
    for m in [m for m in posts if m % 5 != 1]:
        mentions = ["合成薬X"] + (["飲み忘れ"] if m % 5 != 0 else [])
        out.append((f"adherence_concern:{_pid(m)}:{m}", {
            "message_ids": [m], "mentions": mentions}))
    # the body scan is unordered SQL (planner dependent): the identity test
    # compares this tail as a set
    for m in (m for m in posts if m % 5 == 1):
        out.append((f"adherence_concern:{_pid(m)}:{m}", {
            "message_ids": [m], "mentions": ["飲み忘れ"]}))
    return out


@pytest.mark.parametrize("name", STREAMED)
def test_streamed_detectors_identical_output(seeded, name):
    led, th = seeded
    found = list(getattr(mcs_signals, name)(led.db, NOW, th, {}))
    want = _expected(name, th)
    if name == "_adherence_concern":
        head = sum(1 for k, _ in want
                   if int(k.rsplit(":", 1)[1]) % 5 != 1)
        assert [k for k, _ in found[:head]] == [k for k, _ in want[:head]]
        found = found[:head] + sorted(found[head:])
        want = want[:head] + sorted(want[head:])
    assert [k for k, _ in found] == [k for k, _ in want]
    for (key, sig), (_, ev) in zip(found, want):
        got = sig["evidence"]
        assert {f: got[f] for f in ev} == ev, key


@pytest.mark.parametrize("name", ENGAGEMENT_BATCH)
def test_engagement_batch_boundary_suppression(seeded, monkeypatch, name):
    """The candidates at positions 255/256 (last of batch 1, first of batch 2)
    are registered requests: both stay suppressed and neighbours are not."""
    led, th = seeded
    order = []
    real = mcs_signals._engagement_checker

    def spy(db, candidates, profs, orgs):
        candidates = list(candidates)
        order.extend(row[1] for row in candidates)
        return real(db, candidates, profs, orgs)

    monkeypatch.setattr(mcs_signals, "_engagement_checker", spy)
    mids = {int(k.rsplit(":", 1)[1])
            for k, _ in getattr(mcs_signals, name)(led.db, NOW, th, {})}
    assert len(order) > 257
    last_of_first, first_of_second = order[255], order[256]
    assert {last_of_first, first_of_second} <= REGISTERED
    assert not mids & {last_of_first, first_of_second}
    for neighbour in (order[254], order[257]):
        assert neighbour in REGISTERED or neighbour in mids
    assert len(mids) == N - len(REGISTERED)


class _Rows(tuple):
    """Counts live row objects; supports row["col"] and keys()."""
    live = peak = 0

    def __new__(cls, cursor, values):
        row = super().__new__(cls, values)
        row._names = [d[0] for d in cursor.description]
        cls.live += 1
        cls.peak = max(cls.peak, cls.live)
        return row

    def __getitem__(self, key):
        if isinstance(key, str):
            key = self._names.index(key)
        return tuple.__getitem__(self, key)

    def keys(self):
        return list(self._names)

    def __del__(self):
        type(self).live -= 1


@pytest.mark.parametrize("name", STREAMED)
def test_streamed_detectors_hold_bounded_result_rows(seeded, name):
    """Peak memory: a window's result rows are consumed one at a time, not
    all held as Row objects (same pattern as the symptom_after_med test)."""
    led, th = seeded
    gc.collect()
    _Rows.live = _Rows.peak = 0
    old = led.db.row_factory
    led.db.row_factory = _Rows
    try:
        found = list(getattr(mcs_signals, name)(led.db, NOW, th, {}))
    finally:
        led.db.row_factory = old
    gc.collect()
    assert len(found) == len(_expected(name, th))
    assert _Rows.peak <= 8, _Rows.peak


@pytest.mark.parametrize("name", STREAMED)
def test_streamed_detectors_never_interleave_statements(seeded, name):
    """No other statement runs between the first and last row of one
    cursor: the single-statement read snapshot is the same as fetchall."""
    led, th = seeded
    events, keep = [], []
    old = led.db.row_factory

    def factory(cursor, row):
        keep.append(cursor)          # a live cursor never shares an id
        events.append(("row", id(cursor)))
        return old(cursor, row) if old else row

    led.db.row_factory = factory
    led.db.set_trace_callback(lambda sql: events.append(("stmt", sql)))
    try:
        list(getattr(mcs_signals, name)(led.db, NOW, th, {}))
    finally:
        led.db.set_trace_callback(None)
        led.db.row_factory = old
    span = {}
    for i, (kind, ref) in enumerate(events):
        if kind == "row":
            first, _ = span.get(ref, (i, i))
            span[ref] = (first, i)
    assert span
    for first, last in span.values():
        inside = [ref[:80] for kind, ref in events[first:last + 1]
                  if kind == "stmt"]
        assert not inside, inside
