"""Durable, whole-source fact extraction helpers.

This module is intentionally separate from the semantic worker.  It persists
one completed chunk before requesting the next one, so a retry can reuse the
completed prefix without treating a partial extraction as complete.  The
source text is used only to build model input and verify codepoint spans; no
network client is created here.
"""
from __future__ import annotations

import bisect
import json
import re
import time
from contextlib import suppress

import semantic_facts as sf
from semantic_policy import KIND_MANIFEST


KIND_CHUNK = "semantic_extraction_chunk"
SCHEMA_VERSION = "semantic-extraction/v1"
MANIFEST_VERSION = "semantic-source-manifest/v1"
_sha256 = sf._sha256


def _source_fingerprint(member: dict, supplied: str | None) -> str:
    if supplied is not None:
        return str(supplied)
    identity = {
        "project_id": member.get("project_id"),
        "message_id": member.get("message_id"),
        "revision": member.get("revision", ""),
        "body": member.get("body_original", ""),
    }
    return _sha256(json.dumps(identity, ensure_ascii=False,
                               sort_keys=True, separators=(",", ":")))


def _chunk_specs(source: str, chunk_size: int, chunker) -> list[dict]:
    if type(chunk_size) is not int or chunk_size <= 0:
        raise ValueError("chunk_size_invalid")
    if not source:
        return []
    pieces = chunker(source, chunk_size)
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
                      "end": end, "hash": _sha256(piece)})
        cursor = end
    if cursor != len(source):
        raise ValueError("chunk_coverage_incomplete")
    return specs


_HEADING_RE = re.compile(
    r"^\s*(?:【[^】\n]{1,60}】|#{1,6}\s+|\S[^\n]{0,40}[:：]\s*$)")
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
    if _HEADING_RE.match(line):
        return "heading"
    return "clause"


def _atomize(source: str, source_fp: str,
             max_atom: int = 3000) -> list[dict]:
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

    atoms = []
    heading_index = None
    for index, (start, end, kind) in enumerate(refined):
        atom = {
            "atom_id": sf.atom_id(source_fp, index),
            "kind": kind, "start": start, "end": end,
            "text_hash": _sha256(source[start:end]),
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
    atoms = _atomize(source, source_fp, chunk_size) if source else []
    by_id = {a["atom_id"]: a for a in atoms}
    chunks = []
    core = []
    size = 0
    for atom in atoms:
        width = atom["end"] - atom["start"]
        if core and size + width > chunk_size:
            chunks.append(core)
            core, size = [], 0
        core.append(atom["atom_id"])
        size += width
    if core:
        chunks.append(core)
    chunk_rows = []
    for index, core_ids in enumerate(chunks):
        first = by_id[core_ids[0]]
        last = by_id[core_ids[-1]]
        first_i = atoms.index(first)
        last_i = atoms.index(last)
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
            "hash": _sha256(source[first["start"]:last["end"]]),
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


def _manifest_specs(manifest: dict) -> list[dict]:
    """Adapt manifest chunks to the durable spec shape used below."""
    return [{"index": chunk["index"], "text": chunk["text"],
             "start": chunk["start"], "end": chunk["end"],
             "hash": chunk["hash"], "chunk_id": chunk["chunk_id"],
             "core_atom_ids": chunk["core_atom_ids"],
             "context_atom_ids": chunk["context_atom_ids"],
             "dependency_atom_ids": chunk["dependency_atom_ids"]}
            for chunk in manifest["chunks"]]


def _evidence_atom(manifest: dict, start: int, end: int) -> str | None:
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


def _persist_manifest(ledger, project_id, message_id, model, source_fp,
                      manifest, body_len):
    if ledger is None:
        return
    existing = ledger.artifacts(KIND_MANIFEST, project_id=project_id,
                                message_id=message_id)
    for row in existing or []:
        with suppress(TypeError, json.JSONDecodeError):
            meta = json.loads(row["meta"] or "{}")
            if meta.get("source_fingerprint") == source_fp \
                    and meta.get("version") == MANIFEST_VERSION:
                return
    doc = {
        "version": MANIFEST_VERSION,
        "source_fingerprint": source_fp,
        "body_codepoints": body_len,
        "atoms": manifest["atoms"],
        "chunks": [{k: c[k] for k in
                    ("chunk_id", "core_atom_ids", "context_atom_ids",
                     "dependency_atom_ids", "status", "index", "start",
                     "end", "hash")} for c in manifest["chunks"]],
    }
    ledger.artifact_add(
        KIND_MANIFEST,
        json.dumps(doc, ensure_ascii=False, allow_nan=False),
        project_id=project_id, message_id=message_id, model=model,
        meta={"version": MANIFEST_VERSION,
              "source_fingerprint": source_fp,
              "body_codepoints": body_len,
              "atoms": len(manifest["atoms"]),
              "chunks": len(manifest["chunks"])})


def _json_object(response, semantic):
    try:
        parsed = semantic._json_block(response or "")
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None


def _member_prelude(member, llm_fn, project_id, source_fingerprint):
    """Shared input validation for the extraction entry points."""
    if not isinstance(member, dict) or not callable(llm_fn):
        raise ValueError("extraction_input_invalid")
    source = member.get("body_original")
    message_id = member.get("message_id")
    if not isinstance(source, str) or message_id is None:
        raise ValueError("member_source_invalid")
    project_id = member.get("project_id") if project_id is None else project_id
    revision = member.get("revision", "")
    if not isinstance(revision, str):
        revision = str(revision)
    return (source, message_id, project_id, revision,
            _source_fingerprint(member, source_fingerprint),
            _sha256(source))


def _chunk_llm(llm_fn, prompt: str, deadline):
    """One chunk extraction call -> (parsed_doc, reason).  reason is
    "model" or "deadline" on failure; RuntimeGuardError propagates so
    the runtime boundary still aborts the job."""
    import semantic
    from semantic_runtime import RuntimeGuardError
    try:
        response = llm_fn(prompt)
    except RuntimeGuardError:
        raise
    except Exception:
        return None, "model"
    if deadline is not None and time.monotonic() > deadline:
        return None, "deadline"
    return _json_object(response, semantic), None


def _preflight_verdict(jev_client, text: str, deadline, chunk_id):
    """jev_preflight adjudication for one chunk; "failed" on transport
    errors, RuntimeGuardError propagates."""
    from semantic_runtime import RuntimeGuardError
    try:
        return jev_preflight(jev_client, text, deadline, chunk_id=chunk_id)
    except RuntimeGuardError:
        raise
    except Exception:
        return "failed"


def _collect_facts(facts: list, evidence: dict) -> list:
    """Pop per-fact internals (_evidence/_chunk_id) into ``evidence``
    keyed by evidence_id, and return the cleaned fact list."""
    clean = []
    for fact in facts:
        ev = fact.pop("_evidence", None)
        fact.pop("_chunk_id", None)
        if ev is not None:
            evidence[ev["evidence_id"]] = ev
        clean.append(fact)
    return clean


def _category_counts(clean_facts: list) -> dict:
    return {category: sum(1 for f in clean_facts
                          if sf.KIND_CATEGORY[f["kind"]] == category)
            for category in sf.MANDATORY_CATEGORIES}


def _cached_chunks(ledger, project_id, message_id, source_fp, body_hash,
                   revision, specs, source: str, *,
                   kind: str = KIND_CHUNK,
                   schema: str = SCHEMA_VERSION,
                   extra=None) -> dict[int, dict]:
    if ledger is None:
        return {}
    cached = {}
    rows = ledger.artifacts(kind, project_id=project_id,
                            message_id=message_id)
    wanted = {spec["index"]: spec for spec in specs}
    for row in rows:
        try:
            meta = json.loads(row["meta"] or "{}")
            index = int(meta["chunk_index"])
            spec = wanted[index]
            if (meta.get("schema") != schema
                    or meta.get("status") != "complete"
                    or meta.get("source_fingerprint") != source_fp
                    or meta.get("source_hash") != body_hash
                    or meta.get("revision", "") != revision
                    or meta.get("chunks_total") != len(specs)
                    or meta.get("chunk_hash") != spec["hash"]
                    or meta.get("start_codepoint") != spec["start"]
                    or meta.get("end_codepoint") != spec["end"]):
                continue
            content = json.loads(row["content"])
            if (content.get("chunk_index") != index
                    or content.get("start_codepoint") != spec["start"]
                    or content.get("end_codepoint") != spec["end"]
                    or not isinstance(content.get("facts"), list)
                    or type(content.get("dropped", 0)) is not int
                    or content.get("dropped", 0) < 0):
                continue
            if any(not isinstance(fact, dict) for fact in content["facts"]):
                continue
            for fact in content["facts"]:
                evidence = fact.get("_evidence") if isinstance(fact, dict) else None
                if evidence:
                    # v1 rows use start/end_codepoint, v2 rows use
                    # start/end — accept either spelling or every
                    # evidence-bearing v2 chunk misses the cache and
                    # re-extracts on each restart (FIX-SE2)
                    start = evidence.get("start_codepoint",
                                       evidence.get("start"))
                    end = evidence.get("end_codepoint",
                                       evidence.get("end"))
                    quote = evidence.get("quote")
                    if (type(start) is not int or type(end) is not int
                            or not isinstance(quote, str)
                            or not 0 <= start < end <= len(source)
                            or source[start:end] != quote):
                        raise ValueError("cached_evidence_invalid")
            if extra is not None and not extra(content):
                continue
            cached[index] = {"facts": content["facts"],
                             "dropped": content.get("dropped", 0),
                             "extra": {k: content[k] for k in content
                                       if k not in {"schema", "chunk_index",
                                                    "start_codepoint",
                                                    "end_codepoint",
                                                    "facts", "dropped"}}}
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
    return cached


def _normalise_facts(items: list, member: dict, source: str,
                     fact_offset: int, semantic, *,
                     piece: str | None = None, piece_start: int = 0,
                     manifest: dict | None = None) -> tuple[list, int]:
    facts = []
    dropped = 0
    message_id = member["message_id"]
    revision = member.get("revision", "")
    for item in items:
        if not isinstance(item, dict):
            dropped += 1
            continue
        statement = item.get("statement")
        if not isinstance(statement, str) or not statement.strip():
            dropped += 1
            continue
        statement = statement.strip()
        quote = item.get("evidence_quote")
        span = None
        atom_ref = None
        if isinstance(quote, str):
            if piece is not None:
                # Owning-chunk resolution first: a duplicate quote in
                # another chunk cannot shadow this chunk's match.
                local = piece.find(quote)
                if local >= 0:
                    span = (piece_start + local,
                            piece_start + local + len(quote))
                else:
                    local_span = semantic._locate_quote(piece, quote)
                    if local_span:
                        span = (piece_start + local_span[0],
                                piece_start + local_span[1])
            if span is None:
                span = semantic._locate_quote(source, quote)
        if span:
            # _locate_quote may have matched after whitespace
            # normalization — store the verbatim original so
            # body[s:e] == quote holds exactly for the audit.
            quote = source[span[0]:span[1]]
            atom_ref = _evidence_atom(manifest, span[0], span[1]) \
                if manifest is not None else None
        index = fact_offset + len(facts)
        evidence_id = f"ev_{message_id}_{index}"
        status = item.get("status")
        polarity = item.get("polarity")
        time_text = item.get("time_text")
        fact = {
            "fact_id": f"fact_{message_id}_{index}",
            "kind": item.get("kind") if item.get("kind") in semantic.FACT_KINDS
                    else "other",
            # Deliberately do not apply semantic.py's historical 200-char cap.
            "statement": statement,
            "subject_ref": f"m{message_id}",
            "drug_ref": item.get("drug_ref")
                        if isinstance(item.get("drug_ref"), str)
                        and span and item["drug_ref"] in quote else None,
            "status": status if status in semantic.FACT_STATUSES else "not_stated",
            "polarity": polarity if polarity in semantic.POLARITIES else "uncertain",
            "occurred_at": semantic._iso_date(time_text),
            "time_text": time_text if isinstance(time_text, str) else None,
            "quantity": item.get("quantity")
                        if isinstance(item.get("quantity"), str) else None,
            "evidence_refs": [evidence_id] if span else [],
            "validation_status": "candidate" if span else "unverified",
            "_evidence": {
                "evidence_id": evidence_id,
                "message_id": message_id,
                "revision_id": revision,
                "start_codepoint": span[0],
                "end_codepoint": span[1],
                "quote": quote,
                "atom_id": atom_ref,
            } if span else None,
        }
        facts.append(fact)
    return facts, dropped


def _chunk_progress(specs, completed, reused, failed, failure_reason,
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


def _persist_chunk(ledger, project_id, message_id, model, source_fp,
                   body_hash, revision, spec, facts, dropped, chunk_count,
                   *, kind: str = KIND_CHUNK, schema: str = SCHEMA_VERSION,
                   extra_content=None):
    if ledger is None:
        return
    content = {
        "schema": schema,
        "chunk_index": spec["index"],
        "start_codepoint": spec["start"],
        "end_codepoint": spec["end"],
        "facts": facts,
        "dropped": dropped,
    }
    if extra_content:
        content.update(extra_content)
    meta = {
        "schema": schema,
        "status": "complete",
        "source_fingerprint": source_fp,
        "source_hash": body_hash,
        "revision": revision,
        "chunk_index": spec["index"],
        "chunks_total": chunk_count,
        "start_codepoint": spec["start"],
        "end_codepoint": spec["end"],
        "chunk_hash": spec["hash"],
        "dropped": dropped,
    }
    ledger.artifact_add(
        kind, json.dumps(content, ensure_ascii=False, allow_nan=False),
        project_id=project_id, message_id=message_id, model=model, meta=meta)


def extract_facts_resumable(llm_fn, member: dict,
                            deadline: float | None = None, *,
                            ledger=None, source_fingerprint: str | None = None,
                            project_id=None, chunk_size: int = 3000,
                            chunker=None) -> dict:
    """Extract all source chunks with durable prefix reuse.

    ``failure_reason`` is ``"deadline"`` or ``"model"`` when incomplete,
    matching the optional reason returned by ``semantic.extract_facts``. A
    completed chunk is persisted before the next model call.  The returned
    facts are partial only when ``complete`` is false, and callers must keep
    them out of a terminal/pass artifact until the pending chunks succeed.
    """
    import semantic

    source, message_id, project_id, revision, source_fp, body_hash = \
        _member_prelude(member, llm_fn, project_id, source_fingerprint)
    manifest = build_manifest(source, source_fp, chunk_size)
    if chunker is None:
        # Canonical path: atom-owned chunks cover the source exactly once.
        specs = _manifest_specs(manifest)
    else:
        specs = _chunk_specs(source, chunk_size, chunker)
    _persist_manifest(ledger, project_id, message_id, semantic.LLM_MODEL,
                      source_fp, manifest, len(source))
    cached = _cached_chunks(ledger, project_id, message_id, source_fp,
                            body_hash, revision, specs, source)

    facts = []
    completed = []
    reused = []
    failed = []
    failed_dropped = 0
    failure_reason = None
    for spec in specs:
        index = spec["index"]
        cached_chunk = cached.get(index)
        if cached_chunk is not None:
            facts.extend(cached_chunk["facts"])
            completed.append(index)
            reused.append(index)
            continue
        if deadline is not None and time.monotonic() > deadline:
            failed.append(index)
            failure_reason = "deadline"
            break
        parsed, reason = _chunk_llm(
            llm_fn, semantic._FACT_PROMPT % spec["text"], deadline)
        if reason is not None:
            failed.append(index)
            failure_reason = reason
            break
        items = parsed.get("facts") if parsed is not None else None
        if not isinstance(items, list):
            failed.append(index)
            failure_reason = "model"
            break
        chunk_facts, chunk_dropped = _normalise_facts(
            items, member, source, len(facts), semantic,
            piece=spec["text"], piece_start=spec["start"],
            manifest=manifest)
        if chunk_dropped:
            failed.append(index)
            failed_dropped = chunk_dropped
            failure_reason = "model"
            break
        # This is deliberately before the next loop iteration/model call.
        _persist_chunk(ledger, project_id, message_id, semantic.LLM_MODEL,
                       source_fp,
                       body_hash, revision, spec, chunk_facts, chunk_dropped,
                       len(specs))
        facts.extend(chunk_facts)
        completed.append(index)

    complete = len(completed) == len(specs) and not failed
    return {
        "facts": facts,
        "complete": complete,
        **_chunk_progress(specs, completed, reused, failed,
                          failure_reason, failed_dropped, source_fp),
    }


# ---------------------------------------------------------------------
# Canonical semantic-facts/v2 extraction.
#
# The v2 path produces a contract-validated ``semantic-facts/v2``
# document: every atom belongs to exactly one core chunk, every mandatory
# pharmacist-rubric category carries an extraction obligation per chunk,
# and a category only closes as ``explicit_no_fact`` when the model
# adjudicated it absent without a deterministic signal contradicting it.
# Dropped items, unfinished chunks, or unverified facts keep the document
# ``incomplete`` — they never silently become coverage.
# ---------------------------------------------------------------------

KIND_CHUNK_V2 = "semantic_extraction_chunk_v2"
SCHEMA_VERSION_V2 = "semantic-extraction/v2"
CATEGORY_VERDICTS = ("none", "one", "multiple", "ambiguous")

# extract_v1 fields that signal a mandatory category inside one text
# span.  Used to catch a model verdict of "none" that deterministic
# evidence contradicts.
_V1_CATEGORY_FIELDS = {
    "medication": ("medications", "rx_actions", "med_periods"),
    "adherence_administration": ("adherence_flags",),
    "symptom_state": ("symptoms",),
    "vital_lab": ("vitals",),
    "request_pending": ("requests",),
    "care_event": ("events", "visit_date", "next_planned"),
}


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


def _resolve_evidence(quote, piece, piece_start, source, semantic):
    """Locate ``quote`` inside the owning chunk, then map to absolute
    codepoints.  A quote that only exists elsewhere in the source does
    not count — ownership prevents duplicate-quote shadowing."""
    if not isinstance(quote, str) or not quote:
        return None
    local = piece.find(quote)
    if local >= 0:
        return (piece_start + local, piece_start + local + len(quote))
    local_span = semantic._locate_quote(piece, quote)
    if local_span:
        return (piece_start + local_span[0], piece_start + local_span[1])
    return None


def _normalise_facts_v2(items: list, member: dict, source: str, semantic,
                        *, spec: dict, manifest: dict,
                        atom_owner: dict, obligations: dict) -> tuple[list, int]:
    """Model items -> contract facts for one chunk.

    ``kind``/``statement`` are identity: malformed values drop the item
    and fail the chunk.  Quality enums degrade to ``unknown`` rather than
    destroying the fact.  ``obligations`` maps (chunk_id, category) to
    the obligation row being accumulated so fact links land on it.
    """
    facts = []
    dropped = 0
    project_id = str(member.get("project_id"))
    message_id = member["message_id"]
    revision = member.get("revision", "")
    actor = _member_actor(member)
    chunk_id = spec["chunk_id"]
    piece = spec["text"]
    piece_start = spec["start"]
    for item in items:
        if not isinstance(item, dict):
            dropped += 1
            continue
        statement = item.get("statement")
        kind = item.get("kind")
        if not isinstance(statement, str) or not statement.strip() \
                or kind not in sf.FACT_KINDS:
            dropped += 1
            continue
        statement = statement.strip()
        role = item.get("subject_role")
        name = item.get("subject_name")
        role = role if isinstance(role, str) and role else None
        name = name if isinstance(name, str) and name else None
        if role == "patient" and name is None:
            subject = sf.subject_identity(project_id, role="patient")
        elif role is None and name is None:
            subject = sf.UNKNOWN
        else:
            subject = sf.subject_identity(project_id, role=role, name=name)
        span = _resolve_evidence(item.get("evidence_quote"), piece,
                                 piece_start, source, semantic)
        evidence_ids = []
        ev_rec = None
        if span is not None:
            quote = source[span[0]:span[1]]
            atom_ref = _evidence_atom(manifest, span[0], span[1])
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
            "provenance": item.get("_provenance", "local_llm"),
            "validation_status": "verified" if evidence_ids
                                 else "unverified",
            "_chunk_id": owner,
            "_evidence": ev_rec,
        }
        if kind == "medication_event":
            fact["action"] = _enum_or_unknown(item.get("action"),
                                              sf.MED_ACTIONS)
        if obligation_ids:
            obligations[oid]["fact_ids"].append(fact["fact_id"])
        facts.append(fact)
    return facts, dropped


def _v1_hint_items(source: str, posted_at: str) -> list:
    """Deterministic extract_v1 signals converted to candidate items.

    They enter the same normalisation path as model output with
    provenance ``extract_v1`` — union, never replacement.
    """
    import extract
    hints = extract.extract_message(source, posted_at or "")
    if not isinstance(hints, dict):
        return []
    items = []

    def add(kind, statement, quote):
        if isinstance(statement, str) and statement.strip():
            items.append({"kind": kind, "statement": statement.strip(),
                          "evidence_quote": quote
                          if isinstance(quote, str) else None,
                          "_provenance": "extract_v1"})

    for med in hints.get("medications") or []:
        if isinstance(med, dict):
            add("medication_exposure",
                " ".join(p for p in (str(med.get("name") or ""),
                                     str(med.get("dose") or "")) if p),
                med.get("name"))
    for name in hints.get("symptoms") or []:
        add("symptom_state", str(name), name)
    vit = hints.get("vitals")
    if isinstance(vit, dict):
        for key, value in vit.items():
            add("vital_lab", f"{key} {value}", str(value))
    for flag in hints.get("adherence_flags") or []:
        add("adherence_administration", str(flag), flag)
    for req in hints.get("requests") or []:
        if isinstance(req, dict):
            add("request_pending", str(req.get("kind") or "request"),
                req.get("ctx"))
    return items


def _v1_category_signals(source: str, posted_at: str) -> set:
    """Mandatory categories with a deterministic extract_v1 signal."""
    import extract
    hints = extract.extract_message(source, posted_at or "")
    if not isinstance(hints, dict):
        return set()
    return {category for category, fields in _V1_CATEGORY_FIELDS.items()
            if any(hints.get(field) for field in fields)}


def _v2_chunk_ok(content: dict) -> bool:
    return isinstance(content.get("category_presence"), dict)


_PREFLIGHT_CHOICES = ("present", "absent", "uncertain")


def jev_preflight(jev_client, chunk_text: str, deadline,
                  *, chunk_id: str) -> dict | None:
    """Ask Jev which mandatory categories are present in one chunk.

    Returns ``{category: present|absent|uncertain}`` — a presence map,
    never extracted content.  ``None`` only when no client is supplied;
    transport/protocol failures propagate so the caller can mark the
    preflight failed rather than skip it silently.
    """
    if jev_client is None:
        return None
    import semantic_jev as jev
    questions = {}
    for category in sf.MANDATORY_CATEGORIES:
        questions[f"has_{category}"] = jev.choice_question(
            f"Does the source text at state.target.text contain any "
            f"{category} information? Choose present only when the "
            f"category is explicitly represented, absent when clearly "
            f"not, uncertain when it cannot be decided.",
            {"present": "the category is represented in the text",
             "absent": "the category is not represented",
             "uncertain": "presence cannot be decided"})
    state = {"target": {"id": chunk_id, "text": chunk_text}}
    response = jev_client.evaluate(state, questions, deadline)
    answers = response.get("answers") if isinstance(response, dict) \
        else None
    if not isinstance(answers, dict):
        raise ValueError("jev_preflight_invalid")
    verdicts = {}
    for category in sf.MANDATORY_CATEGORIES:
        answer = answers.get(f"has_{category}")
        choice = answer.get("choice") if isinstance(answer, dict) else None
        verdicts[category] = choice if choice in _PREFLIGHT_CHOICES \
            else "uncertain"
    return verdicts


def extract_facts_v2(llm_fn, member: dict,
                     deadline: float | None = None, *,
                     ledger=None, source_fingerprint: str | None = None,
                     project_id=None, chunk_size: int = 3000,
                     jev_client=None, retry_coverage: bool = False) -> dict:
    """Extract a contract-validated ``semantic-facts/v2`` document.

    Returns ``{"doc", "complete", "coverage", ...}``.  ``doc`` is always
    contract-valid; ``coverage.status`` is ``complete`` only when every
    chunk finished, nothing was dropped, no fact stayed unverified, and
    no obligation remains open/ambiguous/failed.  Completed chunks are
    persisted before the next model call so a restart reuses the prefix.
    """
    import semantic
    import semantic_llm

    source, message_id, project_id, revision, source_fp, body_hash = \
        _member_prelude(member, llm_fn, project_id, source_fingerprint)
    posted_at = member.get("posted_at") or ""
    manifest = build_manifest(source, source_fp, chunk_size)
    specs = _manifest_specs(manifest)
    _persist_manifest(ledger, project_id, message_id, semantic.LLM_MODEL,
                      source_fp, manifest, len(source))
    cache_schema = SCHEMA_VERSION_V2 + ("/coverage-retry" if retry_coverage else "")
    cached = _cached_chunks(ledger, project_id, message_id, source_fp,
                            body_hash, revision, specs, source,
                            kind=KIND_CHUNK_V2, schema=cache_schema,
                            extra=_v2_chunk_ok)

    atom_owner = {}
    for chunk in manifest["chunks"]:
        for aid in chunk["core_atom_ids"]:
            atom_owner[aid] = chunk["chunk_id"]

    obligations = _v2_obligations(manifest)

    facts = []
    presence = {}
    jev_verdicts = {}
    chunk_status = {}
    completed = []
    reused = []
    failed = []
    failed_dropped = 0
    failure_reason = None
    model = semantic.LLM_MODEL

    for spec in specs:
        index = spec["index"]
        cid = spec["chunk_id"]
        cached_chunk = cached.get(index)
        if cached_chunk is not None:
            facts.extend(cached_chunk["facts"])
            presence[cid] = cached_chunk["extra"]["category_presence"]
            stored_pf = cached_chunk["extra"].get("jev_preflight")
            chunk_status[cid] = "complete"
            for fact in cached_chunk["facts"]:
                for oid in fact.get("obligation_ids", []):
                    if oid in obligations:
                        obligations[oid]["fact_ids"].append(
                            fact["fact_id"])
            completed.append(index)
            reused.append(index)
            if isinstance(stored_pf, dict):
                jev_verdicts[cid] = stored_pf
            elif jev_client is not None:
                # Cached chunks extracted before preflight existed still
                # get adjudicated — the call is cheap and honest.
                jev_verdicts[cid] = _preflight_verdict(
                    jev_client, spec["text"], deadline, cid)
            continue
        if deadline is not None and time.monotonic() > deadline:
            failed.append(index)
            failure_reason = "deadline"
            break
        if jev_client is not None:
            jev_verdicts[cid] = _preflight_verdict(
                jev_client, spec["text"], deadline, cid)
        parsed, reason = _chunk_llm(
            llm_fn, semantic_llm._FACT_V2_PROMPT % spec["text"], deadline)
        if reason is not None:
            failed.append(index)
            failure_reason = reason
            break
        items = parsed.get("facts") if parsed is not None else None
        verdicts = parsed.get("category_presence") \
            if parsed is not None else None
        if not isinstance(items, list) or not isinstance(verdicts, dict):
            failed.append(index)
            failure_reason = "model"
            break
        clean_verdicts = {category: verdicts.get(category)
                          for category in sf.MANDATORY_CATEGORIES
                          if verdicts.get(category) in CATEGORY_VERDICTS}
        link_marks = {oid: len(ob["fact_ids"])
                      for oid, ob in obligations.items()
                      if ob["owner_id"] == cid}
        chunk_facts, chunk_dropped = _normalise_facts_v2(
            items, member, source, semantic, spec=spec,
            manifest=manifest, atom_owner=atom_owner,
            obligations=obligations)
        if chunk_dropped:
            # A failed chunk must not leave fact links on obligations for
            # facts that never enter the document.
            for oid, mark in link_marks.items():
                del obligations[oid]["fact_ids"][mark:]
            failed.append(index)
            failed_dropped = chunk_dropped
            failure_reason = "model"
            break
        pf = jev_verdicts.get(cid)
        extra = {"category_presence": clean_verdicts}
        if isinstance(pf, dict):
            extra["jev_preflight"] = pf
        _persist_chunk(ledger, project_id, message_id, model, source_fp,
                       body_hash, revision, spec, chunk_facts,
                       chunk_dropped, len(specs), kind=KIND_CHUNK_V2,
                       schema=cache_schema, extra_content=extra)
        facts.extend(chunk_facts)
        presence[cid] = clean_verdicts
        chunk_status[cid] = "complete"
        completed.append(index)

    # Chunks never reached keep "pending"; failed ones are "failed".
    for chunk in manifest["chunks"]:
        cid = chunk["chunk_id"]
        if cid not in chunk_status:
            chunk_status[cid] = "failed" \
                if chunk["index"] in failed else "pending"

    # Union deterministic extract_v1 hints (whole-message candidates).
    hint_facts, _ = _normalise_facts_v2(
        _v1_hint_items(source, posted_at), member, source, semantic,
        spec={"chunk_id": manifest["chunks"][0]["chunk_id"]
              if manifest["chunks"] else "chk_unknown",
              "text": source, "start": 0},
        manifest=manifest, atom_owner=atom_owner,
        obligations=obligations)
    seen = {}
    for fact in facts:
        seen[(fact["kind"], sf._normalize_name(fact["statement"]))] = fact
    for fact in hint_facts:
        key = (fact["kind"], sf._normalize_name(fact["statement"]))
        existing = seen.get(key)
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
        else:
            seen[key] = fact
            facts.append(fact)

    facts = _merge_dupe_facts(facts)

    # Close obligations from verdicts, signals, and linked facts.
    signals = {chunk["chunk_id"]: _v1_category_signals(chunk["text"],
                                                     posted_at)
               for chunk in manifest["chunks"]}
    open_ids = _close_obligations(manifest, obligations, presence,
                                  signals, chunk_status)
    if jev_client is not None:
        open_ids.extend(_jev_obligations(manifest, obligations,
                                       jev_verdicts, presence, signals))

    evidence = {}
    clean_facts = _collect_facts(facts, evidence)

    limitations = []
    if failed:
        limitations.append("chunks_incomplete")
    if any(f["validation_status"] == "unverified" for f in clean_facts):
        limitations.append("unverified_facts")
    complete = len(completed) == len(specs) and not failed
    status = "complete" \
        if complete and not open_ids and not limitations else "incomplete"
    coverage = {
        "category_counts": _category_counts(clean_facts),
        "open_obligation_ids": open_ids,
        "limitations": limitations,
        "status": status,
    }

    # Intra-document relation reconciliation (T6): every pair of kept
    # facts is classified — ordered transitions supersede, unordered
    # conflicts contradict, never silent latest-wins.
    from semantic_relations import reconcile_facts
    relations = reconcile_facts([], clean_facts)["relations"]

    member_quality = "full" if member.get("body_state") == "full" \
        else "partial"
    doc = {
        "version": sf.CONTRACT_VERSION,
        "source": {
            "message_id": str(message_id),
            "revision": revision,
            "content_hash": body_hash,
            "body_codepoints": len(source),
            "content_quality": member_quality,
            "attachments_complete": bool(
                member.get("attachments_complete", True)),
            "source_fingerprint": source_fp,
        },
        "atoms": manifest["atoms"],
        "chunks": [{
            "chunk_id": c["chunk_id"],
            "core_atom_ids": c["core_atom_ids"],
            "context_atom_ids": c["context_atom_ids"],
            "dependency_atom_ids": c["dependency_atom_ids"],
            "status": chunk_status[c["chunk_id"]],
        } for c in manifest["chunks"]],
        "obligations": list(obligations.values()),
        "evidence": list(evidence.values()),
        "facts": clean_facts,
        "relations": relations,
        "coverage": coverage,
    }
    sf.validate_facts_doc(doc)
    return {
        "doc": doc,
        "facts": clean_facts,
        "obligations": list(obligations.values()),
        "coverage": coverage,
        "manifest": manifest,
        "complete": complete and status == "complete",
        "extraction_complete": complete,
        **_chunk_progress(specs, completed, reused, failed,
                          failure_reason, failed_dropped, source_fp),
    }


def _v2_obligations(manifest: dict) -> dict:
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


def _merge_dupe_facts(facts: list) -> list:
    """Two chunks can report an identical fact (same kind/subject/
    statement/evidence -> same stable ID).  Chunk IDs are deliberately
    excluded from fact identity, so merge the duplicates and union the
    obligation links rather than emitting a duplicate ID."""
    merged_facts = []
    by_fact_id = {}
    for fact in facts:
        existing = by_fact_id.get(fact["fact_id"])
        if existing is None:
            by_fact_id[fact["fact_id"]] = fact
            merged_facts.append(fact)
            continue
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
    return merged_facts


def _close_obligations(manifest: dict, obligations: dict,
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


def _jev_obligations(manifest: dict, obligations: dict,
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


# ---------------------------------------------------------------------
# Targeted fact repair (T10)
#
# A post-audit finding names the rejected fact_ids.  Only the chunks
# owning those facts are re-prompted — one bounded dispatch budget per
# generation, enforced by the caller persisting the repair receipt.
# Repaired output merges back through the same stable fact_id contract;
# obligations are re-linked from fact references and never silently
# upgraded: an obligation that loses all facts drops covered -> open.
# ---------------------------------------------------------------------


def repair_facts_v2(llm_fn, member: dict, doc: dict,
                    rejected: dict, deadline: float | None = None,
                    *, chunk_size: int = 3000) -> dict:
    """Re-extract only the chunks owning ``rejected`` facts and merge the
    repaired items into a re-validated document.

    ``rejected`` maps fact_id -> human-readable reason.  Returns
    ``{"doc", "repaired", "repaired_fact_ids", "owner_chunk_ids"}``;
    ``repaired`` is False when nothing could be dispatched (deadline or
    unusable input) — the caller then keeps the prior doc and holds the
    generation rather than degrading it.
    """
    if not isinstance(doc, dict) or not isinstance(member, dict) \
            or not rejected or not callable(llm_fn):
        return {"doc": doc, "repaired": False,
                "repaired_fact_ids": [], "owner_chunk_ids": []}
    import semantic
    import semantic_llm

    source = member.get("body_original")
    if not isinstance(source, str):
        return {"doc": doc, "repaired": False,
                "repaired_fact_ids": [], "owner_chunk_ids": []}
    source_fp = doc["source"]["source_fingerprint"]
    manifest = build_manifest(source, source_fp, chunk_size)
    specs = _manifest_specs(manifest)
    doc_chunk_ids = {c["chunk_id"] for c in doc.get("chunks", [])}
    if {c["chunk_id"] for c in manifest["chunks"]} != doc_chunk_ids:
        # The stored doc does not match a fresh atomization of the
        # source — repair cannot locate owners safely.
        return {"doc": doc, "repaired": False,
                "repaired_fact_ids": [], "owner_chunk_ids": []}

    atom_owner = {}
    for chunk in manifest["chunks"]:
        for aid in chunk["core_atom_ids"]:
            atom_owner[aid] = chunk["chunk_id"]

    obligations = {ob["obligation_id"]: dict(ob)
                   for ob in doc.get("obligations", [])
                   if isinstance(ob, dict) and ob.get("obligation_id")}
    facts_by_id = {f["fact_id"]: f for f in doc.get("facts", [])
                   if isinstance(f, dict) and f.get("fact_id")}
    rejected_ids = [fid for fid in rejected if fid in facts_by_id]
    if not rejected_ids:
        return {"doc": doc, "repaired": False,
                "repaired_fact_ids": [], "owner_chunk_ids": []}

    # rejected fact -> owning chunks via obligation links; evidence
    # quotes inside a chunk's text are a fallback locator.
    owner_ids = set()
    for fid in rejected_ids:
        fact = facts_by_id[fid]
        for oid in fact.get("obligation_ids", []):
            ob = obligations.get(oid)
            if ob is not None and ob.get("owner_id") in doc_chunk_ids:
                owner_ids.add(ob["owner_id"])
        if not fact.get("obligation_ids"):
            for spec in specs:
                for ref in fact.get("evidence_ids", []):
                    ev = next((e for e in doc.get("evidence", [])
                               if e.get("evidence_id") == ref), None)
                    if ev is not None and ev["quote"] in spec["text"]:
                        owner_ids.add(spec["chunk_id"])
    if not owner_ids:
        return {"doc": doc, "repaired": False,
                "repaired_fact_ids": [], "owner_chunk_ids": []}

    repaired_items = {}
    dispatched = []
    for spec in specs:
        cid = spec["chunk_id"]
        if cid not in owner_ids:
            continue
        if deadline is not None and time.monotonic() > deadline:
            break
        feedback = "\n".join(
            f"- {facts_by_id[fid]['statement']}"
            f" [{fid}]: {rejected[fid]}"
            for fid in rejected_ids
            if any(obligations.get(oid, {}).get("owner_id") == cid
                   for oid in facts_by_id[fid].get("obligation_ids", []))
            or not facts_by_id[fid].get("obligation_ids"))
        base = semantic_llm._FACT_V2_PROMPT % spec["text"]
        base = base.rsplit("JSON:", 1)[0]
        prompt = base + semantic_llm._FACT_V2_REPAIR_SUFFIX % feedback
        parsed, _reason = _chunk_llm(llm_fn, prompt, None)
        if parsed is None:
            continue
        items = parsed.get("facts")
        if not isinstance(items, list):
            continue
        repaired_items[cid] = items
        dispatched.append(cid)
    if not dispatched:
        return {"doc": doc, "repaired": False,
                "repaired_fact_ids": [], "owner_chunk_ids": []}

    # Normalise repaired items per owning chunk, then merge by fact_id.
    facts = [f for f in doc["facts"]
             if f.get("fact_id") not in rejected_ids]
    by_fact_id = {f["fact_id"]: f for f in facts}
    for ob in obligations.values():
        ob["fact_ids"] = [fid for fid in ob.get("fact_ids", [])
                          if fid in by_fact_id]
    repaired_fact_ids = []
    for spec in specs:
        cid = spec["chunk_id"]
        items = repaired_items.get(cid)
        if items is None:
            continue
        link_marks = {oid: len(ob["fact_ids"])
                      for oid, ob in obligations.items()
                      if ob["owner_id"] == cid}
        new_facts, dropped_n = _normalise_facts_v2(
            items, member, source, semantic, spec=spec,
            manifest=manifest, atom_owner=atom_owner,
            obligations=obligations)
        if dropped_n:
            # Same fail-the-chunk semantics as extraction: a repair
            # chunk that produced malformed items contributes nothing —
            # links roll back and partial facts never merge (FIX-SE4).
            for oid, mark in link_marks.items():
                del obligations[oid]["fact_ids"][mark:]
            continue
        for fact in new_facts:
            existing = by_fact_id.get(fact["fact_id"])
            if existing is None:
                by_fact_id[fact["fact_id"]] = fact
                facts.append(fact)
                repaired_fact_ids.append(fact["fact_id"])
                continue
            for oid in fact["obligation_ids"]:
                if oid not in existing["obligation_ids"]:
                    existing["obligation_ids"].append(oid)
            if fact["validation_status"] == "verified":
                existing["validation_status"] = "verified"
            if fact.get("_evidence") and not existing.get("_evidence"):
                existing["_evidence"] = fact["_evidence"]

    # Evidence surviving from the original doc stays keyed by id;
    # repaired facts contribute their fresh _evidence entries.
    evidence = {e["evidence_id"]: e for e in doc.get("evidence", [])
                if isinstance(e, dict) and e.get("evidence_id")}
    clean_facts = _collect_facts(facts, evidence)

    # Re-derive statuses conservatively: linked facts cover; a covered
    # obligation that lost all links falls back to open, never to
    # explicit_no_fact.  Obligations owned by a failed/pending chunk
    # keep their non-terminal status — a stray link must not upgrade a
    # chunk whose adjudication never completed (FIX-SE5).
    chunk_status_by_id = {c["chunk_id"]: c.get("status")
                          for c in doc.get("chunks", [])
                          if isinstance(c, dict)}
    open_ids = []
    for ob in obligations.values():
        owner_status = chunk_status_by_id.get(ob["owner_id"])
        if owner_status == "failed":
            ob["status"] = "failed"
        elif owner_status == "pending":
            ob["status"] = "open"
        elif ob["fact_ids"]:
            ob["status"] = "covered"
        elif ob["status"] == "covered":
            ob["status"] = "open"
        if ob["status"] in ("open", "ambiguous", "failed"):
            open_ids.append(ob["obligation_id"])

    limitations = []
    if any(f["validation_status"] == "unverified"
           for f in clean_facts):
        limitations.append("unverified_facts")
    coverage = {
        "category_counts": _category_counts(clean_facts),
        "open_obligation_ids": open_ids,
        "limitations": limitations,
        "status": "complete" if not open_ids and not limitations
        else "incomplete",
    }
    new_doc = dict(doc)
    new_doc["obligations"] = list(obligations.values())
    new_doc["evidence"] = list(evidence.values())
    new_doc["facts"] = clean_facts
    new_doc["coverage"] = coverage
    # Relations referencing removed facts dangle — recompute over the
    # surviving set so every endpoint resolves.
    from semantic_relations import reconcile_facts
    new_doc["relations"] = reconcile_facts([], clean_facts)["relations"]
    sf.validate_facts_doc(new_doc)
    return {"doc": new_doc, "repaired": True,
            "repaired_fact_ids": repaired_fact_ids,
            "owner_chunk_ids": dispatched}
