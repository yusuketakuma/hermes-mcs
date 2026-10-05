import copy

import semantic_blind as blind


def test_baseline_context_matches_production_selection_and_labels(tmp_path):
    import semantic
    from semantic_testkit import _seeded
    db = _seeded(tmp_path)
    try:
        bundle = semantic.thread_bundle(db, 1, 1)
    finally:
        db.close()
    root, reply = bundle["members"][:2]
    root.update(posted_at="2026-10-01T10:00:00+09:00",
                sender={"id": 42, "type": "staff", "profession": "看護師"})
    reply.update(posted_at="2026-10-01T12:00:00+09:00", body_original="target body")
    def member(mid, posted_at, body):
        m = copy.deepcopy(reply)
        m.update(message_id=mid, posted_at=posted_at, body_original=body,
                 sender={"id": 7, "type": "family", "profession": ""})
        return m
    bundle["members"] += [member(3, "", "no time"),
                          member(4, "2026-10-01T11:00:00+09:00", "earlier"),
                          member(5, "2026-10-01T12:00:00+09:00", "same second later id"),
                          member(6, "2026-10-01T13:00:00+09:00", "later")]
    bundle["source_fingerprint"] = semantic.bundle_fingerprint(bundle["members"])
    meta = {"fingerprint": bundle["source_fingerprint"],
            "target_revision": reply["revision"], "policy_fingerprint": "policy",
            "publication_mode": "shadow"}
    candidate = {"message_id": 2, "project_id": 1, "meta": dict(meta, stage="pre_audit"),
                 "content": {"claims": [{"text": "draft"}], "limitations": [],
                             "target_message_id": 2, "input_bundle_id": bundle["bundle_id"]}}
    final = copy.deepcopy(candidate)
    final["meta"] = dict(meta, audit_status="PASS")
    seen = []
    def baseline(body, context=None, **_):
        seen.append(context)
        return {"summary": "baseline"}
    blind.fixed_bundle_outputs(bundle, 2, candidate, final, baseline)
    assert seen[0].splitlines() == ["[看護師] " + root["body_original"], "[family] earlier"]
