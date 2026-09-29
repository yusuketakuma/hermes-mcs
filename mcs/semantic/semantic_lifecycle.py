"""Derive observed verified/rendered/delivered fact-ID sets for evaluation records from a read-only ledger snapshot.

Each stage is read from its own durable record and never imputed from
another stage (``semantic_evaluation`` scores an absent list as a
missing observation, so an unprovable stage is left out):

- verified: fact IDs of the current ``semantic_facts_v2`` document of the
  summary's generation (message, source fingerprint) when the newest fact
  audit for the summary's policy is a completed PASS on that document's
  hash (``semantic_v4.fact_audit_verdict``, the drain's own reuse rule),
  with complete coverage. Only facts marked ``verified`` count.
- rendered: fact IDs bound (``、ID:<id>、証拠:``) in the summary artifact's
  stored ``mandatory_pages``, only when ``verify_mandatory_pages`` finds
  the pages complete.
- delivered: rendered facts whose page line reached the channel in the
  generation's ``semantic_notice`` outbox intent. The frozen notice text
  is re-chunked with the send path's own chunker, which also returns each
  part's body (``semantic_chunk_parts``); a fact counts only when
  every chunk its line spans is inside the accepted-chunk receipt
  (``progress.next``; an ``accepted`` row must record every chunk). No
  matching intent, or an unparseable/contradictory receipt, leaves the
  stage absent.

The snapshot is opened ``mode=ro`` inside one read transaction; nothing
is written to the ledger and no source text is emitted.
"""
from __future__ import annotations

import os
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

# flat-import bootstrap: put mcs/ root on sys.path, then _mcs_path
# registers every first-level subdir as an import root
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
import _mcs_path  # noqa: F401

from mcs_util import loads_dict
from semantic_evaluation import LIFECYCLE_STAGES, EvaluationError, _dict
from semantic_policy import KIND_FACT_AUDIT, KIND_FACTS_V2, KIND_SUMMARY


def _verified(db, mid: int, fp: str, policy: str) -> tuple[list | None, str]:
    """Current v2 doc (newest for the generation) bound to a PASS fact
    audit — selected and judged exactly as the drain and the
    re-projection gate do (``semantic_store._current`` +
    ``semantic_v4.fact_audit_verdict``), so ``verified`` is observed only
    when the publication gate would accept the document as it stands."""
    from types import SimpleNamespace

    from semantic_store import _current
    from semantic_v4 import _doc_hash, fact_audit_verdict
    ledger = SimpleNamespace(db=db)       # _current reads only .db
    current = _current(ledger, KIND_FACTS_V2, mid, fp)
    if current is None:
        return None, "facts_doc_missing"
    doc = current["content"]
    try:
        doc_hash = _doc_hash(doc)
    except (KeyError, TypeError, ValueError):
        return None, "facts_doc_invalid"
    status = fact_audit_verdict(
        _current(ledger, KIND_FACT_AUDIT, mid, fp, policy), doc_hash)
    if status is None:
        return None, "fact_audit_missing"
    if status != "PASS":
        return None, "fact_audit_not_pass"
    coverage = doc.get("coverage")
    if not isinstance(coverage, dict) or coverage.get("status") != "complete":
        return None, "coverage_incomplete"
    facts = doc.get("facts")
    if not isinstance(facts, list):
        return None, "facts_doc_invalid"
    ids = []
    for fact in facts:
        if not isinstance(fact, dict) or fact.get("validation_status") != "verified":
            continue
        fid = fact.get("fact_id")
        if not isinstance(fid, str) or not fid.strip() or fid in ids:
            return None, "facts_doc_invalid"
        ids.append(fid)
    return ids, "pass_audit"


def _rendered(summary: dict) -> tuple[list | None, dict, str]:
    """IDs bound in the stored mandatory pages, plus fid -> page line."""
    from semantic_render import fact_binding, verify_mandatory_pages
    pages, fact_ids = summary.get("mandatory_pages"), summary.get("mandatory_fact_ids")
    if not isinstance(pages, list) or not isinstance(fact_ids, list):
        return None, {}, "pages_absent"
    if any(not isinstance(p, dict) or not isinstance(p.get("text"), str)
           or not isinstance(p.get("fact_ids"), list) for p in pages) \
            or any(not isinstance(f, str) or not f.strip() for f in fact_ids):
        return None, {}, "pages_invalid"
    if not verify_mandatory_pages({"fact_ids": fact_ids, "pages": pages})["complete"]:
        return None, {}, "pages_incomplete"
    ids, lines = [], {}
    for page in pages:
        page_lines = page["text"].split("\n")
        for fid in page["fact_ids"]:
            bound = [line for line in page_lines if fact_binding(fid) in line]
            if len(bound) != 1:
                return None, {}, "pages_invalid"
            ids.append(fid)
            lines[fid] = bound[0]
    return ids, lines, "pages_complete"


def _accepted_prefix(row, count: int) -> tuple[int | None, str | None]:
    """Chunks proven accepted by the outbox receipt; None = unprovable."""
    raw = row["progress"]
    if not raw:
        return (None, None) if row["state"] == "accepted" else (0, None)
    progress = loads_dict(raw)
    if progress is None:
        return None, None
    nxt, sent = progress.get("next", 0), progress.get("sent", [])
    if (type(nxt) is not int or not 0 <= nxt <= count
            or not isinstance(sent, list) or len(sent) != nxt
            or sent != [str(i) for i in range(1, nxt + 1)]):
        return None, None
    if row["state"] == "accepted" and nxt != count:
        return None, None
    return nxt, progress.get("fingerprint")


def _delivered(db, pid: int, mid: int, fp: str, policy: str, revision,
               rendered: list | None, lines: dict,
               cfg: dict[str, str] | None = None) -> tuple[list | None, str]:
    import notify_flush
    from notify_flush import _delivery_fingerprint, _target
    from semantic_send_gate import semantic_chunk_parts
    if rendered is None:
        # nothing observed as rendered: a delivered fact ID could only be
        # read from the notice text itself, which is not the rendered stage
        return None, "rendered_unobserved"
    rows = []
    for row in db.execute(
            "SELECT event_id,state,payload,progress FROM notify_outbox "
            "WHERE kind='semantic_notice' AND project_id=? ORDER BY event_id",
            (pid,)):
        payload = loads_dict(row["payload"])
        if payload is None:
            return None, "notice_payload_invalid"
        if (payload.get("degraded") or payload.get("target_message_id") != mid
                or payload.get("fingerprint") != fp
                or payload.get("policy_fingerprint") != policy):
            continue
        if payload.get("target_revision") != revision \
                or not isinstance(payload.get("text"), str):
            return None, "notice_binding_invalid"
        rows.append((row, payload["text"]))
    if not rows:
        return None, "notice_missing"
    delivered = set()
    for row, text in rows:
        try:
            # the send path's chunker at its current width
            # (notify_flush._semantic_chunks), with each part's body
            chunks, bodies = semantic_chunk_parts(text, notify_flush._MAX_LEN)
        except ValueError:
            return None, "notice_unchunkable"
        prefix, fingerprint = _accepted_prefix(row, len(chunks))
        if prefix is None:
            return None, "notice_receipt_unprovable"
        partial = row["state"] != "accepted" or prefix < len(chunks)
        if partial:
            target = _target(cfg or {}, "semantic_notice")
            if (not isinstance(fingerprint, str) or target is None
                    or fingerprint != _delivery_fingerprint(
                        target, chunks, [])):
                return None, "notice_receipt_unprovable"
        joined = "".join(bodies)
        bounds, offset = [], 0
        for body in bodies:
            bounds.append((offset, offset + len(body)))
            offset += len(body)
        for fid in rendered:
            needle = "・" + lines[fid]
            at = joined.find(needle)
            if at < 0 or joined.find(needle, at + 1) >= 0:
                continue          # not (unambiguously) in this notice
            end = at + len(needle)
            spanned = [i for i, (lo, hi) in enumerate(bounds)
                       if lo < end and hi > at]
            if spanned and max(spanned) < prefix:
                delivered.add(fid)
    return [fid for fid in rendered if fid in delivered], "notice_receipt"


def fact_lifecycle(db, final_id: int, artifact_ids: dict | None = None,
                   cfg: dict[str, str] | None = None) -> dict:
    """Observed stage sets for one semantic_summary artifact.

    Returns ``{"fact_ids": {stage: [...]}, "observations": {stage: code}}``;
    a stage missing from ``fact_ids`` was not provable from the ledger.
    ``db`` must be a sqlite3 connection with ``Row`` factory."""
    if type(final_id) is not int or final_id <= 0:
        raise EvaluationError("artifact_id_invalid")
    row = db.execute("SELECT * FROM artifacts WHERE artifact_id=? AND kind=?",
                     (final_id, KIND_SUMMARY)).fetchone()
    if row is None:
        raise EvaluationError("artifact_missing")
    summary, meta = loads_dict(row["content"]), loads_dict(row["meta"])
    pid, mid = row["project_id"], row["message_id"]
    if (summary is None or meta is None or type(pid) is not int
            or type(mid) is not int or not isinstance(meta.get("fingerprint"), str)
            or not isinstance(meta.get("policy_fingerprint"), str)):
        raise EvaluationError("artifact_meta_invalid")
    fp, policy = meta["fingerprint"], meta["policy_fingerprint"]
    for field, kind in (("bundle_id", "semantic_bundle"),
                        ("candidate_id", "semantic_candidate")):
        aid = (artifact_ids or {}).get(field)
        if aid is None:
            continue
        other = db.execute("SELECT * FROM artifacts WHERE artifact_id=? AND kind=?",
                           (aid, kind)).fetchone() if type(aid) is int else None
        other_meta = loads_dict(other["meta"]) if other is not None else None
        other_content = loads_dict(other["content"]) if other is not None else None
        bound = (other_content or {}).get("source_fingerprint") \
            if kind == "semantic_bundle" else (other_meta or {}).get("fingerprint")
        if other is None or other["project_id"] != pid or bound != fp or (
                kind == "semantic_candidate" and other["message_id"] != mid):
            raise EvaluationError("artifact_source_mismatch")
    out, observations = {}, {}
    verified, observations["verified"] = _verified(db, mid, fp, policy)
    rendered, lines, observations["rendered"] = _rendered(summary)
    delivered, observations["delivered"] = _delivered(
        db, pid, mid, fp, policy, meta.get("target_revision"), rendered, lines,
        cfg)
    for stage, ids in zip(LIFECYCLE_STAGES, (verified, rendered, delivered),
                          strict=True):
        if ids is not None:
            out[stage] = list(ids)
    return {"fact_ids": out, "observations": observations}


def attach_lifecycle(path: str, records: list[dict],
                     cfg: dict[str, str] | None = None) -> list[dict]:
    """Copy each candidate record with its observed stage sets attached.

    Records name the summary artifact via ``artifact_ids.final_id`` (the
    same IDs semantic_blind snapshot selections use). Existing stage lists
    are refused rather than overwritten. All records are read in one
    read-only snapshot transaction."""
    out = []
    with closing(sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro",
                                 uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        for record in records:
            record = _dict(record, "evaluation_record")
            ids = _dict(record.get("artifact_ids"), "artifact_ids")
            if not set(ids) <= {"bundle_id", "candidate_id", "final_id"} or any(
                    type(value) is not int or value <= 0 for value in ids.values()):
                raise EvaluationError("artifact_ids_invalid")
            candidate = _dict(record.get("candidate"), "candidate")
            if any(f"{stage}_fact_ids" in candidate for stage in LIFECYCLE_STAGES) \
                    or "fact_lifecycle_observations" in record:
                raise EvaluationError("lifecycle_already_present")
            result = fact_lifecycle(db, ids.get("final_id"), ids, cfg)
            candidate = dict(candidate)
            for stage, fact_ids in result["fact_ids"].items():
                candidate[f"{stage}_fact_ids"] = fact_ids
            out.append({**record, "candidate": candidate,
                        "fact_lifecycle_observations": result["observations"]})
    return out
