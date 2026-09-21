#!/usr/bin/env python3
"""Phase J open-loop candidates (spec §17) — advisory items derived
from extracted facts, plus Jev relation checks between open items and
new arrivals. Promotion to a formal request is NEVER automatic — it
stays behind mcs_requests' human_confirmed contract (INV-11/19).
"""
import json
import time

import semantic_jev as jev
from mcs_requests import payload_hash
from semantic_model import KIND_LOOP, KIND_LOOP_EVENT


def update_loops(ledger, project_id: int, bundle: dict,
                 facts_by_target: dict, jev_client, scfg: dict,
                 deadline: float) -> tuple[int, bool]:
    """Returns (candidates_created, complete). complete=False means the
    pair budget/deadline/a Jev error cut the relation pass short — the
    caller must reschedule so the remainder is carried forward (§17.2).
    Candidate creation is automatic; promotion to a formal request is
    NOT — that stays behind mcs_requests' human_confirmed contract
    (INV-11/19, AT-051)."""
    created = 0
    for target_id, facts in facts_by_target.items():
        member = next((m for m in bundle["members"]
                       if m["message_id"] == target_id), None)
        if member is None:
            continue
        for f in facts:
            if f["kind"] not in ("explicit_request", "pending_item",
                                 "schedule") or f["polarity"] == "negated":
                continue
            cand_fp = payload_hash({"p": project_id, "m": target_id,
                                    "s": f["statement"]})
            dup = False
            for r in ledger.db.execute(
                    "SELECT meta FROM artifacts WHERE kind=? "
                    "AND message_id=?", (KIND_LOOP, target_id)):
                try:
                    if json.loads(r["meta"] or "{}").get(
                            "candidate_fp") == cand_fp:
                        dup = True
                        break
                except (json.JSONDecodeError, TypeError):
                    continue
            if dup:
                continue
            ledger.artifact_add(
                KIND_LOOP,
                json.dumps({"loop_id": f"loop_{cand_fp[:12]}",
                            "project_id": project_id,
                            "kind": f["kind"],
                            "description": f["statement"],
                            "origin": {"message_id": target_id,
                                       "revision": member["revision"],
                                       "evidence_refs":
                                       f["evidence_refs"]},
                            "assignee_text": None,
                            "due_text": f.get("time_text"),
                            "state": "PROPOSED",
                            "history": [{
                                "state": "PROPOSED",
                                "at": int(time.time()),
                                "trigger_message_id": target_id}]},
                           ensure_ascii=False),
                project_id=project_id, message_id=target_id,
                model=jev.JEV_MODEL,
                meta={"fingerprint": bundle["source_fingerprint"],
                      "candidate_fp": cand_fp,
                      "registry": jev.REGISTRY_VERSION})
            created += 1
    if jev_client is None:
        return created, True
    # relate new arrivals to open candidates. EVERY open candidate is
    # eligible (no LIMIT — the spec forbids dropping the remainder,
    # §17.2); evaluated (candidate, target) pairs are deduped via their
    # recorded loop_event so re-runs only evaluate new pairs, and a
    # per-job pair budget carries the remainder to the next run of the
    # same job instead of truncating it.
    seen = set()
    for r in ledger.db.execute(
            "SELECT content FROM artifacts WHERE kind=? AND project_id=?",
            (KIND_LOOP_EVENT, project_id)):
        try:
            ev = json.loads(r["content"])
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(ev, dict):
            # loop_artifact_id identifies the candidate — two candidates
            # can share an origin message, so the bare origin id is not
            # a safe dedup key (legacy rows fall back to it)
            seen.add((ev.get("loop_artifact_id")
                      or ev.get("loop_origin_id"),
                      ev.get("trigger_message_id")))
    open_loops = ledger.db.execute("""
      SELECT artifact_id, message_id, content FROM artifacts
      WHERE kind=? AND project_id=? ORDER BY artifact_id DESC
    """, (KIND_LOOP, project_id)).fetchall()
    targets = [m for m in bundle["members"] if m["role"] == "target"]
    pairs = 0
    for row in open_loops:
        try:
            cand = json.loads(row["content"])
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(cand, dict) or cand.get("state") not in \
                ("PROPOSED", "RESOLUTION_CANDIDATE"):
            continue
        for m in targets:
            if row["message_id"] == m["message_id"]:
                continue  # a candidate never relates to its own origin
            key = (row["artifact_id"], m["message_id"])
            if key in seen:
                continue
            if pairs >= 40 or time.monotonic() > deadline:
                return created, False   # remainder -> next run
            qid = f"rel_{row['message_id']}_{m['message_id']}"
            q = jev.choice_question(
                "state.context[0] is an open follow-up item recorded "
                "earlier. state.target is a newer message. Classify "
                "their relation — 'acknowledged'/'will confirm' is "
                "receipt, not resolution (AT-037/038).",
                jev.LOOP_RELATION_OPTIONS)
            try:
                out = jev_client.evaluate(
                    {"target": {"id": qid, "text": m["body_original"]},
                     "context": [{"id": "loop", "role": "open_item",
                                  "text": cand.get("description", "")}]},
                    {qid: q}, deadline)
            except jev.JevError:
                return created, False
            pairs += 1
            seen.add(key)
            ledger.artifact_add(
                KIND_LOOP_EVENT,
                json.dumps({"loop_origin_id": row["message_id"],
                            "loop_artifact_id": row["artifact_id"],
                            "trigger_message_id": m["message_id"],
                            "relation": out["answers"][qid]["choice"],
                            "confidence": out["answers"][qid]
                            .get("confidence"),
                            "candidate_state": cand.get("state")},
                           ensure_ascii=False),
                project_id=project_id, message_id=m["message_id"],
                model=jev.JEV_MODEL,
                meta={"fingerprint": bundle["source_fingerprint"],
                      "registry": jev.REGISTRY_VERSION,
                      "jev_requests": 1})
    return created, True
