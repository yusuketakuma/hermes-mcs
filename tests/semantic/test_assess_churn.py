"""Synthetic regressions: a primary Jev assessment is asked again only when its request changes."""
import json
import time

import pytest

import mcs_contract
import semantic_drain
import semantic_jev as jev
import semantic_policy
import semantic_store
from semantic_testkit import BODY, _FakeJev, _cfg, _ledger, _message, _patient

POSTED = ("2099-01-01T09:00:00+09:00", "2099-01-01T09:05:00+09:00", "2099-01-01T09:10:00+09:00")
CFG = _cfg("shadow", summary_mode="off", loop_mode="off")


def _thread(tmp_path, *, attachment=False):
    """Root 1 with reply 2, both assessed once; returns the ledger and the requests spent."""
    db = _ledger(tmp_path)
    patient = _patient(db)
    root = _message(1, posted_at=POSTED[0])
    if attachment:
        root.attachments = [mcs_contract.Attachment("f1", "synthetic.pdf", "https://example.invalid/f1")]
    root.replies = [_message(2, parent=1, body="合成の返信です。", posted_at=POSTED[1])]
    root.reply_count = 1
    patient.messages = [root]
    db.save_patient(patient, notify={"source": "unread"}, semantic=True)
    return db, _drain(db)


def _drain(db):
    client = _FakeJev()
    semantic_drain.run_due(db, CFG, {"errors": []}, time.monotonic() + 300, jev_client=client,
                           llm_fn=lambda _: pytest.fail("must not call LLM"))
    assert db.job_state("semantic", 1, 1) == "done"
    return client.requests_made


def _fingerprint(db):
    return semantic_store.thread_bundle(db.db, 1, 1)["source_fingerprint"]


def _current(db, mid):
    scfg = semantic_policy.runtime_config(CFG)[0]
    policy = semantic_policy.policy_fingerprint(scfg)
    fp = _fingerprint(db)
    row, previous = semantic_store._primary_assessment_history(db.db, 1, mid, fp, policy)
    assert row is not None and row["meta"]["technical_status"] == "complete"
    assert semantic_store._valid_primary_assessment(
        row["content"], row["meta"], row["model"], mid, fp, policy, scfg)
    assert semantic_store._valid_primary_history(
        row["content"], row["meta"], previous, mid, fp, policy, scfg)
    return row


def test_attachment_download_keeps_thread_assessments(tmp_path):
    db, first = _thread(tmp_path, attachment=True)
    try:
        assert first > 0
        before = _fingerprint(db)
        answers = {mid: _current(db, mid)["content"]["verdicts"] for mid in (1, 2)}
        attachment_id = db.db.execute("SELECT attachment_id FROM attachments").fetchone()[0]
        db.attachment_saved(attachment_id, str(tmp_path / "synthetic.pdf"), 10, "a" * 64, semantic=True)
        assert _fingerprint(db) != before and db.job_state("semantic", 1, 1) == "pending"
        assert _drain(db) == 0
        for mid in (1, 2):
            row = _current(db, mid)
            assert row["content"]["verdicts"] == answers[mid]
            assert row["meta"]["jev_requests"] == 0 and row["meta"]["carried_fingerprint"] == before
    finally:
        db.close()


def test_reply_count_alone_keeps_thread_assessments(tmp_path):
    db, _ = _thread(tmp_path)
    try:
        before = _fingerprint(db)
        root = _message(1, unread=False, posted_at=POSTED[0])
        root.reply_count = 2          # the reply itself is not stored yet
        db.save_messages([root], project_id=1, notify={"source": "history"}, semantic=True)
        assert _fingerprint(db) != before and db.job_state("semantic", 1, 1) == "pending"
        assert _drain(db) == 0
        assert _current(db, 1)["meta"]["carried_fingerprint"] == before
    finally:
        db.close()


def test_new_reply_and_edit_still_ask_again(tmp_path):
    db, first = _thread(tmp_path)
    try:
        reply = _message(3, parent=1, body="合成の追加返信です。", posted_at=POSTED[2])
        db.save_messages([reply], project_id=1, notify={"source": "history"}, semantic=True)
        # the new reply is context of every earlier post: all three requests differ
        assert _drain(db) == first // 2 * 3
        assert "carried_fingerprint" not in _current(db, 1)["meta"]
        db.save_messages([_message(3, parent=1, body="合成の追加返信（編集）です。", unread=False,
                                   posted_at=POSTED[2])],
                         project_id=1, notify={"source": "history"}, semantic=True)
        assert _drain(db) == first // 2 * 3
    finally:
        db.close()


def test_skip_row_names_its_request_so_a_carry_stops_at_it(tmp_path):
    db, _ = _thread(tmp_path)
    try:
        reply = _message(3, parent=1, body="合成の追加返信です。", posted_at=POSTED[2])
        db.save_messages([reply], project_id=1, notify={"source": "history"}, semantic=True)
        semantic_drain.run_due(db, CFG, {"errors": []}, time.monotonic() + 300, jev_client=None,
                               llm_fn=lambda _: pytest.fail("must not call LLM"))
        scfg = semantic_policy.runtime_config(CFG)[0]
        policy = semantic_policy.policy_fingerprint(scfg)
        bundle = semantic_store.thread_bundle(db.db, 1, 1, [1, 2, 3])
        jev_input = semantic_store.assessment_input(bundle, 1, scfg)
        def rows():
            return semantic_store._assessment_history_rows(db.db, 1, [1]).get(1, ())

        newest = json.loads(rows()[0]["meta"])
        assert newest["technical_status"] == "skipped" and newest["jev_input"] == jev_input
        # An older complete answer to the same request under another thread
        # fingerprint must not be reached past the newer skip.
        answered = next(r for r in rows() if json.loads(r["meta"])["technical_status"] == "complete")
        meta = {**json.loads(answered["meta"]), "fingerprint": "synthetic-older", "jev_input": jev_input}
        db.artifact_add(semantic_drain.KIND_ASSESS, answered["content"], project_id=1, message_id=1,
                        model=jev.JEV_MODEL, meta=meta)
        skip_id, older_id = (r[0] for r in db.db.execute(
            "SELECT artifact_id FROM artifacts WHERE kind=? AND message_id=1 "
            "ORDER BY artifact_id DESC LIMIT 2", (semantic_drain.KIND_ASSESS,)).fetchall()[::-1])
        with db.db:
            for src, dst in ((skip_id, -1), (older_id, skip_id), (-1, older_id)):
                db.db.execute("UPDATE artifacts SET artifact_id=? WHERE artifact_id=?", (dst, src))
        assert json.loads(rows()[1]["meta"])["fingerprint"] == "synthetic-older"
        assert semantic_store._carried_primary_assessment(rows(), 1, policy, scfg, jev_input) is None
    finally:
        db.close()


@pytest.mark.parametrize("damage", ["invalidated", "stale", "retry_wait", "policy", "registry"])
def test_carry_never_bypasses_the_newest_row_of_the_input(tmp_path, damage):
    db, _ = _thread(tmp_path)
    try:
        scfg = semantic_policy.runtime_config(CFG)[0]
        policy = semantic_policy.policy_fingerprint(scfg)
        bundle = semantic_store.thread_bundle(db.db, 1, 1, [1, 2])
        jev_input = semantic_store.assessment_input(bundle, 1, scfg)
        row = _current(db, 1)
        assert row["meta"]["jev_input"] == jev_input

        def carried():
            rows = semantic_store._assessment_history_rows(db.db, 1, [1]).get(1, ())
            return semantic_store._carried_primary_assessment(rows, 1, policy, scfg, jev_input)

        assert carried()["content"] == row["content"]
        meta = dict(row["meta"])
        if damage in ("invalidated", "stale"):
            meta[damage] = True
        elif damage == "retry_wait":
            meta["technical_status"] = "retry_wait"
        elif damage == "registry":
            meta["registry"] = "synthetic-other-registry"
        if damage == "policy":
            policy = "synthetic-other-policy"
        else:
            db.artifact_add(semantic_drain.KIND_ASSESS, json.dumps(row["content"]), project_id=1,
                            message_id=1, model=jev.JEV_MODEL, meta=meta)
        assert carried() is None
    finally:
        db.close()


def test_assessment_input_names_only_the_request(tmp_path):
    db, _ = _thread(tmp_path)
    try:
        scfg = semantic_policy.runtime_config(CFG)[0]

        def identity(mid=1, cfg=scfg):
            return semantic_store.assessment_input(semantic_store.thread_bundle(db.db, 1, 1, [1, 2]), mid, cfg)

        base = identity()
        db.db.execute("UPDATE messages SET reply_count=9,sender_name='合成の別名',organization='合成事業所' "
                      "WHERE message_id=1")
        assert identity() == base
        assert identity(2) != base
        assert identity(cfg=semantic_policy.runtime_config(
            _cfg("shadow", summary_mode="off", loop_mode="off", extra_propositions=["E01"]))[0]) != base
        db.db.execute("UPDATE messages SET body_text=? WHERE message_id=2", (BODY + "（編集）",))
        assert identity() != base
    finally:
        db.close()
