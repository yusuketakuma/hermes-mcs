"""Assemble source-bound semantic facts, evidence, obligations and progress without I/O."""
from __future__ import annotations

import bisect
import copy
import json
import re

import clinical_chunking
import mcs_util
import semantic_facts as sf


MANIFEST_VERSION = "semantic-source-manifest/v1"


def source_fingerprint(member: dict, supplied: str | None) -> str:
    if supplied is not None:
        return str(supplied)
    identity = {
        "project_id": member.get("project_id"),
        "message_id": member.get("message_id"),
        "revision": member.get("revision", ""),
        "body": member.get("body_original", ""),
    }
    return sf.text_sha256(json.dumps(identity, ensure_ascii=False,
                               sort_keys=True, separators=(",", ":")))


def chunk_specs(source: str, chunk_size: int, pieces) -> list[dict]:
    if type(chunk_size) is not int or chunk_size <= 0:
        raise ValueError("chunk_size_invalid")
    if not source:
        return []
    if not isinstance(pieces, list) or any(not isinstance(p, str) or not p
                                           for p in pieces):
        raise ValueError("chunker_invalid")
    specs = []
    cursor = 0
    for index, piece in enumerate(pieces):
        start = source.find(piece, cursor)
        if start != cursor:
            raise ValueError("chunk_coverage_incomplete")
        end = start + len(piece)
        specs.append({"index": index, "text": piece, "start": start,
                      "end": end, "hash": sf.text_sha256(piece)})
        cursor = end
    if cursor != len(source):
        raise ValueError("chunk_coverage_incomplete")
    return specs


_HEADING_RE = re.compile(
    r"^\s*(?:【[^】\n]{1,60}】|#{1,6}\s+|\S[^\n]{0,40}[:：]\s*$|"
    r"(?:本人|患者本人|家族|母|父|妻|夫|過去|現在|予定)\s*$)")


_LIST_ITEM_RE = re.compile(
    r"^\s*(?:・|[-*‐–—●◦▪]|(?:\d{1,3}|[A-Za-z])[.)、．]|"
    r"[（(]\s*\d{1,3}\s*[)）])")


_TABLE_CELL_RE = re.compile(r"\|\s*\S|\S\s*\|")


_SENTENCE_BREAK = re.compile(r"[。．!！?？；;]\s*|\n")


def _line_kind(line: str) -> str:
    if _TABLE_CELL_RE.search(line):
        return "table_row"
    if _LIST_ITEM_RE.match(line):
        return "list_item"
    if clinical_chunking.is_heading(line) or _HEADING_RE.match(line):
        return "heading"
    return "clause"


def _atomize(source: str, source_fp: str,
             max_atom: int = 3000, *, boundaries=()) -> list[dict]:
    """Partition *source* into ordered atoms covering every codepoint once.

    Atoms are clause/list-item/table-row/heading units; whitespace-only
    regions attach to the following atom (or the last atom at end of
    input) so the union of ranges is the whole source with no gaps and
    no overlap.  Atoms larger than ``max_atom`` split at a sentence
    boundary when possible, otherwise at the size limit.  A heading is a
    dependency of every following atom until the next heading.
    """
    pieces = re.split(r"(?<=\n)", source)
    spans = []          # (start, end, kind)
    cursor = 0
    pending = 0         # start of whitespace-only run awaiting an owner
    have_content = False
    for piece in pieces:
        start, end = cursor, cursor + len(piece)
        cursor = end
        if not piece.strip():
            if not have_content:
                # a leading whitespace run always begins at offset 0 —
                # keep its earliest start, not each piece's (FIX-SE3:
                # consecutive blank pieces orphaned the run head)
                pending = min(pending, start)
            continue
        # A whitespace run (including one leading the source) belongs to
        # the following atom so every atom keeps a contiguous range —
        # pending already holds the run start, or 0 when there is none
        # (FIX-SE3: a leading \n previously orphaned [0, content_start)).
        atom_start = pending
        spans.append([atom_start, end, _line_kind(piece)])
        have_content = True
        pending = end
    if have_content and pending < len(source):
        spans[-1][1] = len(source)
    elif not spans and source:
        spans.append([0, len(source), "clause"])

    # Split oversized atoms at sentence boundaries, hard-split as last
    # resort — mirrors _chunks' coverage rule at atom granularity.
    refined = []
    for start, end, kind in spans:
        while end - start > max_atom:
            window = source[start:start + max_atom]
            cut = -1
            for match in _SENTENCE_BREAK.finditer(window):
                cut = match.end()
            if cut <= 0:
                cut = max_atom
            refined.append((start, start + cut, kind))
            start += cut
        refined.append((start, end, kind))

    planned = []
    for start, end, kind in refined:
        cuts = [start, *boundaries[bisect.bisect_right(boundaries, start):
                                   bisect.bisect_left(boundaries, end)], end]
        planned.extend((left, right, kind) for left, right in zip(cuts, cuts[1:]))
    refined = planned

    atoms = []
    heading_index = None
    for index, (start, end, kind) in enumerate(refined):
        atom = {
            "atom_id": sf.atom_id(source_fp, index),
            "kind": kind, "start": start, "end": end,
            "text_hash": sf.text_sha256(source[start:end]),
            "section_path": [],
            "dependency_atom_ids": [],
            "importance": "unknown",
        }
        if kind == "heading":
            heading_index = index
        elif heading_index is not None:
            heading = atoms[heading_index]
            atom["section_path"] = [source[heading["start"]:
                                           heading["end"]].strip()]
            atom["dependency_atom_ids"] = [heading["atom_id"]]
        atoms.append(atom)
    # Fragments of the same original line retain its shared subject/time
    # introduction and trailing action as references, never additional owners.
    ends = [span[1] for span in spans]
    groups = {}
    for atom in atoms:
        groups.setdefault(bisect.bisect_right(ends, atom["start"]), []).append(atom)
    for group in groups.values():
        if len(group) < 2:
            continue
        for atom in group:
            for endpoint in (group[0]["atom_id"], group[-1]["atom_id"]):
                if endpoint != atom["atom_id"] and endpoint not in atom["dependency_atom_ids"]:
                    atom["dependency_atom_ids"].append(endpoint)
    return atoms


def build_manifest(source: str, source_fp: str,
                   chunk_size: int = 3000) -> dict:
    """Build the atom/chunk ownership manifest for one source message.

    Every atom belongs to exactly one core chunk; neighbour atoms are
    exposed as bounded context only and never own evidence.  Core atom
    ranges therefore cover the source exactly once.
    """
    if type(chunk_size) is not int or chunk_size <= 0:
        raise ValueError("chunk_size_invalid")
    pieces = clinical_chunking.plan_chunks(source, chunk_size) if source else []
    specs = chunk_specs(source, chunk_size, pieces)
    if any(spec["end"] - spec["start"] > chunk_size for spec in specs):
        raise ValueError("chunk_size_exceeded")
    atoms = _atomize(source, source_fp, chunk_size,
                     boundaries=[spec["end"] for spec in specs[:-1]]) if source else []
    by_id = {a["atom_id"]: a for a in atoms}
    positions = {a["atom_id"]: index for index, a in enumerate(atoms)}
    chunks = []
    spec_index = 0
    for atom in atoms:
        while atom["start"] >= specs[spec_index]["end"]:
            spec_index += 1
        if len(chunks) <= spec_index:
            chunks.append([])
        chunks[spec_index].append(atom["atom_id"])
    chunk_rows = []
    for index, core_ids in enumerate(chunks):
        first = by_id[core_ids[0]]
        last = by_id[core_ids[-1]]
        first_i = positions[first["atom_id"]]
        last_i = positions[last["atom_id"]]
        context = []
        if first_i > 0:
            context.append(atoms[first_i - 1]["atom_id"])
        if last_i + 1 < len(atoms):
            context.append(atoms[last_i + 1]["atom_id"])
        deps = sorted({dep for aid in core_ids
                       for dep in by_id[aid]["dependency_atom_ids"]
                       if dep not in core_ids})
        chunk_rows.append({
            "chunk_id": sf.chunk_id(source_fp, index),
            "core_atom_ids": core_ids,
            "context_atom_ids": context,
            "dependency_atom_ids": deps,
            "status": "pending",
            "index": index, "start": first["start"], "end": last["end"],
            "text": source[first["start"]:last["end"]],
            "hash": sf.text_sha256(source[first["start"]:last["end"]]),
        })
    manifest = {
        "version": MANIFEST_VERSION,
        "source_fingerprint": source_fp,
        "atoms": atoms,
        "chunks": chunk_rows,
    }
    # Contract self-check: core ranges cover the source exactly once.
    expected = 0
    for atom in atoms:
        if atom["start"] != expected or atom["end"] <= atom["start"]:
            raise ValueError("atom_coverage_incomplete")
        expected = atom["end"]
    if expected != len(source):
        raise ValueError("atom_coverage_incomplete")
    return manifest


def manifest_specs(manifest: dict) -> list[dict]:
    """Adapt manifest chunks to the durable spec shape used below."""
    return [{"index": chunk["index"], "text": chunk["text"],
             "start": chunk["start"], "end": chunk["end"],
             "hash": chunk["hash"], "chunk_id": chunk["chunk_id"],
             "core_atom_ids": chunk["core_atom_ids"],
             "context_atom_ids": chunk["context_atom_ids"],
             "dependency_atom_ids": chunk["dependency_atom_ids"]}
            for chunk in manifest["chunks"]]


def evidence_atom(manifest: dict, start: int, end: int) -> str | None:
    """Return the atom that owns ``[start, end)``, else ``None``.

    Ownership follows the atom containing ``start``; an evidence span may
    legitimately cross into the next atom inside the same core chunk.
    """
    atoms = manifest.get("atoms") if manifest else None
    if not atoms:
        return None
    starts = [a["start"] for a in atoms]
    pos = bisect.bisect_right(starts, start) - 1
    if pos < 0 or pos >= len(atoms):
        return None
    atom = atoms[pos]
    if start < atom["start"] or start >= atom["end"]:
        return None
    return atom["atom_id"]


def manifest_content(manifest, body_len):
    return {
        "version": MANIFEST_VERSION,
        "source_fingerprint": manifest["source_fingerprint"],
        "body_codepoints": body_len,
        "atoms": manifest["atoms"],
        "chunks": [{k: c[k] for k in
                    ("chunk_id", "core_atom_ids", "context_atom_ids",
                     "dependency_atom_ids", "status", "index", "start",
                    "end", "hash")} for c in manifest["chunks"]],
    }


def collect_facts(facts: list, evidence: dict | None = None) -> tuple:
    """Return owned clean facts and evidence without changing the input records."""
    facts = copy.deepcopy(facts)
    evidence = copy.deepcopy(evidence) if evidence is not None else {}
    clean = []
    for fact in facts:
        ev = fact.pop("_evidence", None)
        fact.pop("_chunk_id", None)
        if ev is not None:
            evidence[ev["evidence_id"]] = ev
        clean.append(fact)
    return clean, evidence


def category_counts(clean_facts: list) -> dict:
    return {category: sum(1 for f in clean_facts
                          if sf.KIND_CATEGORY[f["kind"]] == category)
            for category in sf.MANDATORY_CATEGORIES}


def chunk_progress(specs, completed, reused, failed, failure_reason,
                    failed_dropped, source_fp) -> dict:
    """Resumable-chunk accounting tail shared by the v1/v2 result
    schemas — the durable chunk ledger's view of what ran."""
    pending = [index for index in range(len(specs))
               if index not in completed]
    return {"dropped": 0,
            "chunks_total": len(specs),
            "chunks_completed": len(completed),
            "completed_chunks": completed,
            "reused_chunks": reused,
            "failed_chunks": failed,
            "pending_chunks": pending,
            "failure_reason": failure_reason,
            "dropped_in_failed_chunk": failed_dropped,
            "source_fingerprint": source_fp}


CATEGORY_VERDICTS = ("none", "one", "multiple", "ambiguous")


def _enum_or_unknown(value, options) -> str:
    return value if isinstance(value, str) and value in options \
        else sf.UNKNOWN


def _str_or_unknown(value) -> str:
    return value.strip() if isinstance(value, str) and value.strip() \
        else sf.UNKNOWN


def _member_actor(member: dict) -> str:
    sender = member.get("sender")
    sender_id = sender.get("id") if isinstance(sender, dict) else None
    if sender_id is None:
        return sf.UNKNOWN
    try:
        return sf.subject_identity(str(member.get("project_id")),
                                   sender_id=sender_id)
    except sf.ContractError:
        return sf.UNKNOWN


def resolve_evidence(quote, piece, piece_start, source):
    """Locate ``quote`` inside the owning chunk, then map to absolute
    codepoints.  A quote that only exists elsewhere in the source does
    not count — ownership prevents duplicate-quote shadowing."""
    if not isinstance(quote, str) or not quote:
        return None
    local_span = mcs_util.locate_quote_span(piece, quote)
    if local_span:
        return (piece_start + local_span[0], piece_start + local_span[1])
    return None


def normalise_facts_v2(items: list, member: dict, source: str,
                        *, spec: dict, manifest: dict,
                        atom_owner: dict, obligations: dict,
                        provenance: str = "local_llm",
                        drop_reasons: dict | None = None,
                        omitted: dict | None = None) -> tuple[list, int, dict, dict | None, dict | None]:
    """Model items -> contract facts for one chunk.

    ``kind``/``statement`` are identity: malformed values drop the item
    and fail the chunk.  Quality enums degrade to ``unknown`` rather than
    destroying the fact.  ``obligations`` maps (chunk_id, category) to
    the obligation row being accumulated so fact links land on it.
    ``drop_reasons`` (optional) counts each drop by a fixed code;
    ``omitted`` (optional) counts optional fields left out, never drops.
    Return facts, dropped count and owned copies of obligations/counters; inputs remain unchanged.
    """
    obligations = copy.deepcopy(obligations)
    drop_reasons = copy.deepcopy(drop_reasons)
    omitted = copy.deepcopy(omitted)
    facts = []
    dropped = 0

    def drop(code: str) -> int:
        if drop_reasons is not None:
            drop_reasons[code] = drop_reasons.get(code, 0) + 1
        return 1

    project_id = str(member.get("project_id"))
    message_id = member["message_id"]
    revision = member.get("revision", "")
    actor = _member_actor(member)
    chunk_id = spec["chunk_id"]
    piece = spec["text"]
    piece_start = spec["start"]
    for item in items:
        if not isinstance(item, dict):
            dropped += drop("item_not_object")
            continue
        statement = item.get("statement")
        kind = item.get("kind")
        if not isinstance(statement, str) or not statement.strip():
            dropped += drop("statement_invalid")
            continue
        if kind not in sf.FACT_KINDS:
            dropped += drop("kind_invalid")
            continue
        statement = statement.strip()
        role = item.get("subject_role")
        name = item.get("subject_name")
        role = role if isinstance(role, str) and role else None
        name = name if isinstance(name, str) and name else None
        subject = sf.subject_identity(project_id, role=role, name=name)
        span = resolve_evidence(item.get("evidence_quote"), piece,
                                 piece_start, source)
        evidence_ids = []
        ev_rec = None
        if span is not None:
            quote = source[span[0]:span[1]]
            atom_ref = evidence_atom(manifest, span[0], span[1])
            if atom_ref is not None:
                eid = sf.evidence_id(message_id, revision, span[0],
                                     span[1], quote)
                evidence_ids = [eid]
                ev_rec = {"evidence_id": eid, "message_id": str(message_id),
                          "revision": revision, "start": span[0],
                          "end": span[1], "quote": quote,
                          "atom_id": atom_ref}
        category = sf.KIND_CATEGORY[kind]
        obligation_ids = []
        if ev_rec is not None:
            owner = atom_owner.get(ev_rec["atom_id"], chunk_id)
        else:
            owner = chunk_id
        oid = sf.obligation_id(owner, category, "deterministic")
        if oid in obligations:
            obligation_ids.append(oid)
        fact = {
            "fact_id": sf.fact_id(project_id, message_id, revision, kind,
                                  subject, statement, evidence_ids),
            "kind": kind,
            "subject": subject,
            "actor": actor,
            "statement": statement,
            "polarity": _enum_or_unknown(item.get("polarity"),
                                         sf.POLARITIES),
            "epistemic": _enum_or_unknown(item.get("epistemic"),
                                          sf.EPISTEMICS),
            "workflow_status": _enum_or_unknown(
                item.get("workflow_status"), sf.WORKFLOW_STATUSES),
            "event_time": _str_or_unknown(item.get("event_time")),
            "valid_time": _str_or_unknown(item.get("valid_time")),
            "evidence_ids": evidence_ids,
            "obligation_ids": obligation_ids,
            "importance": _enum_or_unknown(item.get("importance"),
                                           sf.IMPORTANCE_TIERS),
            "quantity": _str_or_unknown(item.get("quantity")),
            "provenance": provenance,
            "validation_status": "verified" if evidence_ids
                                 else "unverified",
            "_chunk_id": owner,
            "_evidence": ev_rec,
        }
        if kind == "medication_event":
            fact["action"] = _enum_or_unknown(item.get("action"),
                                              sf.MED_ACTIONS)
        fact.update(sf.request_details(item, [ev_rec["quote"]] if ev_rec else []))
        if kind == "care_event" and isinstance(item.get("care_detail"), dict):
            try:
                fact["care_detail"] = sf.validate_care_detail_fact(
                    item["care_detail"])
            except sf.ContractError:
                # Optional enrichment: a malformed detail is omitted, never
                # attached raw — projection re-binds it to the quote anyway.
                if omitted is not None:
                    omitted["care_detail_invalid"] = \
                        omitted.get("care_detail_invalid", 0) + 1
        if "patient_context" in item and item["patient_context"] is None:
            # an explicit JSON null claims nothing: the same as leaving the
            # optional field out (the prompt asks to omit it), not a defect
            if omitted is not None:
                omitted["context_null"] = omitted.get("context_null", 0) + 1
        elif "patient_context" in item:
            raw = item["patient_context"]
            if isinstance(raw, dict) and not set(raw) - {"category", "text", "details"} and ev_rec:
                candidate = {**raw, "evidence": ev_rec["quote"], "subject": sf.context_subject(subject)}
                try:
                    fact["patient_context"] = sf.validate_patient_context(
                        {"subject": subject, "patient_context": candidate}, [ev_rec["quote"]])
                except sf.ContractError:
                    # Optional claims that failed grounding still block chunk publication.
                    dropped += drop("context_ungrounded")
            else:
                dropped += drop("context_no_evidence" if isinstance(raw, dict) and not ev_rec
                                else "context_shape")
        # A repair may return a kept fact again; one fact links once.
        if obligation_ids and fact["fact_id"] not in obligations[oid]["fact_ids"]:
            obligations[oid]["fact_ids"].append(fact["fact_id"])
        facts.append(fact)
    return facts, dropped, obligations, drop_reasons, omitted


def v2_chunk_ok(content: dict) -> bool:
    if not isinstance(content.get("category_presence"), dict):
        return False
    try:
        for fact in content["facts"]:
            sf.validate_fact(fact)
            if fact.get("provenance") != "local_llm":
                return False
    except sf.ContractError:
        return False
    return True


def v2_obligations(manifest: dict) -> dict:
    """One open deterministic obligation per (chunk, category)."""
    obligations = {}
    for chunk in manifest["chunks"]:
        for category in sf.MANDATORY_CATEGORIES:
            oid = sf.obligation_id(chunk["chunk_id"], category,
                                   "deterministic")
            obligations[oid] = {
                "obligation_id": oid,
                "owner_id": chunk["chunk_id"],
                "category": category,
                "source": "deterministic",
                "importance": "unknown",
                "status": "open",
                "fact_ids": [],
            }
    return obligations


def _hint_target(candidates, hint: dict):
    """The same-wording fact an extract_v1 hint corroborates, or None.

    Only a fact whose evidence span contains the hint's own span: the same
    wording elsewhere (another chunk, or a past vs current mention in one
    chunk) is a different statement."""
    hint_ev = hint["_evidence"]
    return next((f for f in candidates
                 if hint_ev is not None and f["_evidence"] is not None
                 and f["_evidence"]["start"] <= hint_ev["start"]
                 and hint_ev["end"] <= f["_evidence"]["end"]), None)


def merge_dupe_facts(facts: list) -> list:
    """Two chunks can report an identical fact (same kind/subject/
    statement/evidence -> same stable ID).  Chunk IDs are deliberately
    excluded from fact identity, so merge the duplicates and union the
    obligation links rather than emitting a duplicate ID."""
    facts = copy.deepcopy(facts)
    merged_facts = []
    by_fact_id = {}
    # Keys whose known values already disagreed stay "unknown" for good,
    # so a later duplicate cannot revive one side of the conflict.
    conflicted = {}
    for fact in facts:
        existing = by_fact_id.get(fact["fact_id"])
        if existing is None:
            by_fact_id[fact["fact_id"]] = fact
            merged_facts.append(fact)
            continue
        if fact["kind"] == "request_pending":
            seen = conflicted.setdefault(fact["fact_id"], set())
            for key in sf.REQUEST_DETAIL_FIELDS:
                # "unknown" means not determined, not a conflicting value.
                if key in seen or fact.get(key, sf.UNKNOWN) == sf.UNKNOWN:
                    continue
                if existing.get(key, sf.UNKNOWN) == sf.UNKNOWN:
                    existing[key] = fact[key]
                elif existing[key] != fact[key]:
                    existing[key] = sf.UNKNOWN
                    seen.add(key)
        for oid in fact["obligation_ids"]:
            if oid not in existing["obligation_ids"]:
                existing["obligation_ids"].append(oid)
        for prov in fact["provenance"].split("+"):
            if prov not in existing["provenance"].split("+"):
                existing["provenance"] += "+" + prov
        if fact["validation_status"] == "verified":
            existing["validation_status"] = "verified"
        if fact.get("_evidence") and not existing.get("_evidence"):
            existing["_evidence"] = fact["_evidence"]
        detail_seen = conflicted.setdefault(fact["fact_id"], set())
        new_detail = fact.get("care_detail")
        if "care_detail" not in detail_seen and new_detail is not None:
            prior_detail = existing.get("care_detail")
            if prior_detail is None:
                existing["care_detail"] = new_detail
            elif prior_detail != new_detail:
                # Conflicting optional details on an identical fact: keep
                # neither rather than guessing which chunk was right.
                existing.pop("care_detail", None)
                detail_seen.add("care_detail")
        context = fact.get("patient_context")
        if context and not existing.get("patient_context"):
            existing["patient_context"] = context
        elif context:
            prior = existing["patient_context"]
            if all(prior.get(key) == context.get(key) for key in ("category", "text", "evidence", "subject")):
                for detail in context.get("details") or []:
                    if detail not in prior.setdefault("details", []):
                        prior["details"].append(detail)
    return merged_facts


def close_obligations(manifest: dict, obligations: dict,
                       presence: dict, signals: dict,
                       chunk_status: dict) -> list:
    """Close deterministic obligations from verdicts, signals, and
    linked facts — returns the still-open ids."""
    open_ids = []
    for chunk in manifest["chunks"]:
        cid = chunk["chunk_id"]
        for category in sf.MANDATORY_CATEGORIES:
            oid = sf.obligation_id(cid, category, "deterministic")
            ob = obligations[oid]
            verdict = presence.get(cid, {}).get(category)
            has_signal = category in signals[cid]
            if chunk_status[cid] == "failed":
                ob["status"] = "failed"
            elif chunk_status[cid] == "pending":
                ob["status"] = "open"
            elif verdict in ("one", "multiple"):
                ob["status"] = "covered" if ob["fact_ids"] else "open"
            elif verdict == "none":
                ob["status"] = "ambiguous" \
                    if ob["fact_ids"] or has_signal \
                    else "explicit_no_fact"
            elif verdict == "ambiguous":
                ob["status"] = "ambiguous"
            else:
                ob["status"] = "open"
            if ob["status"] in ("open", "ambiguous", "failed"):
                open_ids.append(oid)
    return open_ids


def jev_obligations(manifest: dict, obligations: dict,
                   jev_verdicts: dict, presence: dict,
                   signals: dict) -> list:
    """Jev preflight obligations — a second adjudication source per
    (chunk, category).  They exist only where a verdict was actually
    produced; a "present" verdict without facts, or an "absent" verdict
    contradicted by model/deterministic signals, never closes."""
    open_ids = []
    det_by_owner = {(ob["owner_id"], ob["category"]): ob
                    for ob in obligations.values()}
    for chunk in manifest["chunks"]:
        cid = chunk["chunk_id"]
        verdicts = jev_verdicts.get(cid)
        if verdicts is None:
            continue
        for category in sf.MANDATORY_CATEGORIES:
            oid = sf.obligation_id(cid, category, "jev_pre")
            det = det_by_owner[(cid, category)]
            ob = {"obligation_id": oid, "owner_id": cid,
                  "category": category, "source": "jev_pre",
                  "importance": "unknown", "status": "open",
                  "fact_ids": list(det["fact_ids"])}
            if verdicts == "failed":
                ob["status"] = "failed"
            else:
                verdict = verdicts.get(category)
                det_v = presence.get(cid, {}).get(category)
                has_signal = category in signals[cid]
                if verdict == "present":
                    ob["status"] = "covered" if ob["fact_ids"] \
                        else "open"
                elif verdict == "absent":
                    ob["status"] = "explicit_no_fact" \
                        if (not ob["fact_ids"] and det_v == "none"
                            and not has_signal) else "ambiguous"
                else:
                    ob["status"] = "ambiguous"
            if ob["status"] in ("open", "ambiguous", "failed"):
                open_ids.append(oid)
            obligations[oid] = ob
    return open_ids


def assemble_facts(member, source, manifest, facts, obligations, presence,
                   jev_verdicts, chunk_status, hint_items, signals, *, use_jev_preflight):
    """Return owned fact/evidence/obligation data from completed chunks and supplied rule decisions."""
    facts = copy.deepcopy(facts)
    atom_owner = {aid: chunk["chunk_id"] for chunk in manifest["chunks"]
                  for aid in chunk["core_atom_ids"]}
    # Union deterministic extract_v1 hints (whole-message candidates).
    hint_facts, _, obligations, _, _ = normalise_facts_v2(
        hint_items, member, source,
        spec={"chunk_id": manifest["chunks"][0]["chunk_id"]
              if manifest["chunks"] else "chk_unknown",
              "text": source, "start": 0},
        manifest=manifest, atom_owner=atom_owner,
        obligations=obligations, provenance="extract_v1")
    seen = {}
    for fact in facts:
        seen.setdefault((fact["kind"], sf.normalize_name(fact["statement"])),
                        []).append(fact)
    for fact in hint_facts:
        key = (fact["kind"], sf.normalize_name(fact["statement"]))
        existing = _hint_target(seen.get(key, ()), fact)
        if existing is not None:
            if "extract_v1" not in existing["provenance"]:
                existing["provenance"] += "+extract_v1"
            # The dropped hint's fact_id already sits in obligation
            # fact_ids — retarget those links to the surviving fact and
            # union the obligation ids, or the doc validator fails on
            # doc_obligation_fact_unknown (FIX-SE1).
            for oid in fact["obligation_ids"]:
                ob = obligations.get(oid)
                if ob is not None:
                    ob["fact_ids"] = list(dict.fromkeys(
                        existing["fact_id"] if fid == fact["fact_id"]
                        else fid for fid in ob["fact_ids"]))
                if oid not in existing["obligation_ids"]:
                    existing["obligation_ids"].append(oid)
        elif fact["validation_status"] == "unverified":
            # A hint quote is a bare token (name or normalised value) that
            # may be ambiguous or absent verbatim; an unverifiable
            # deterministic candidate is dropped, not reported as an
            # unverified fact — category signals still hold coverage.
            for oid in fact["obligation_ids"]:
                ob = obligations.get(oid)
                if ob is not None:
                    ob["fact_ids"] = [fid for fid in ob["fact_ids"]
                                      if fid != fact["fact_id"]]
        else:
            seen.setdefault(key, []).append(fact)
            facts.append(fact)

    facts = merge_dupe_facts(facts)

    # Close obligations from verdicts, signals, and linked facts.
    open_ids = close_obligations(manifest, obligations, presence,
                                  signals, chunk_status)
    if use_jev_preflight:
        open_ids.extend(jev_obligations(manifest, obligations,
                                       jev_verdicts, presence, signals))

    clean_facts, evidence = collect_facts(facts)

    return {"facts": clean_facts, "evidence": list(evidence.values()),
            "obligations": list(obligations.values()), "open_ids": open_ids,
            "chunk_status": dict(chunk_status)}


def source_binding(member, source_fp):
    """Construct the canonical document source identity from the supplied member."""
    source = member["body_original"]
    revision = member.get("revision", "")
    return {"message_id": str(member["message_id"]), "revision": revision if isinstance(revision, str) else str(revision),
            "content_hash": sf.text_sha256(source), "body_codepoints": len(source),
            "content_quality": "full" if member.get("body_state") == "full" else "partial",
            "attachments_complete": bool(member.get("attachments_complete", True)),
            "source_fingerprint": source_fp}


def assemble_document(binding, manifest, assembled, reconciled, progress, *,
                      drop_reasons=None, null_contexts_omitted=0, generation=None):
    """Finalize a contract-valid document; skipped comparisons and open obligations remain incomplete."""
    progress = dict(progress)
    clean_facts = assembled["facts"]
    open_ids = assembled["open_ids"]
    chunk_status = assembled["chunk_status"]
    failed = progress["failed_chunks"]
    relations, pairs_skipped = reconciled["relations"], reconciled["pairs_skipped"]
    if pairs_skipped and progress["failure_reason"] is None:
        progress["failure_reason"] = "deadline"
    limitations = []
    if failed:
        limitations.append("chunks_incomplete")
    if pairs_skipped:
        limitations.append("relations_incomplete")
    if any(f["validation_status"] == "unverified" for f in clean_facts):
        limitations.append("unverified_facts")
    complete = progress["chunks_completed"] == len(manifest["chunks"]) and not failed \
        and not pairs_skipped
    status = "complete" \
        if complete and not open_ids and not limitations else "incomplete"
    coverage = {
        "category_counts": category_counts(clean_facts),
        "open_obligation_ids": open_ids,
        "limitations": limitations,
        "status": status,
    }

    doc = {
        "version": sf.CONTRACT_VERSION,
        "source": dict(binding),
        "atoms": manifest["atoms"],
        "chunks": [{
            "chunk_id": c["chunk_id"],
            "core_atom_ids": c["core_atom_ids"],
            "context_atom_ids": c["context_atom_ids"],
            "dependency_atom_ids": c["dependency_atom_ids"],
            "status": chunk_status[c["chunk_id"]],
        } for c in manifest["chunks"]],
        "obligations": assembled["obligations"],
        "evidence": assembled["evidence"],
        "facts": clean_facts,
        "relations": relations,
        "coverage": coverage,
    }
    sf.validate_facts_doc(doc)
    return {
        "doc": doc,
        "facts": clean_facts,
        "obligations": assembled["obligations"],
        "coverage": coverage,
        "manifest": manifest,
        "complete": complete and status == "complete",
        "extraction_complete": complete,
        **progress,
        "dropped_reasons": dict(drop_reasons or {}),     # the failed chunk's, by code
        "relation_pairs_skipped": pairs_skipped,
        "null_contexts_omitted": null_contexts_omitted,
        **({"generation": generation} if generation is not None else {}),
    }
