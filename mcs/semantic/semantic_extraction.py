"""Durable, whole-source fact extraction helpers.

This module is intentionally separate from the semantic worker.  It persists
one completed chunk before requesting the next one, so a retry can reuse the
completed prefix without treating a partial extraction as complete.  The
source text is used only to build model input and verify codepoint spans; no
network client is created here.
"""
from __future__ import annotations

from inference_version import CURRENT_VERSION, VERSION_KEY, is_current
import clinical_values
from mcs_queries import iter_artifacts
import mcs_util
import semantic_llm
import semantic_transport

import copy
import json
import re
import time

import semantic_facts as sf
import semantic_assembly as assembly
from mcs_util import loads_dict
from semantic_policy import KIND_MANIFEST
import clinical_chunking


KIND_CHUNK = "semantic_extraction_chunk"
SCHEMA_VERSION = "semantic-extraction/v1"


def _require_idle_ledger(ledger):
    """The checkpoint writer commits; never commit a caller's transaction."""
    if ledger is not None and ledger.db.in_transaction:
        raise RuntimeError("semantic_extraction_requires_idle_connection")


def _chunk_generation(model: str, prompt_template: str, *, chunk_size: int = 3000) -> str:
    """Return the model + prompt identity a cached chunk must match."""
    return f"inference:{CURRENT_VERSION}|{model}|{sf.text_sha256(prompt_template)[:16]}|plan:{clinical_chunking.PLAN_VERSION}|size:{chunk_size}"


def v2_generation() -> str:
    """Identity of the default v2 extraction (model + prompt + chunk plan);
    a change means an earlier attempt says nothing about a new one."""
    import semantic_llm
    return _chunk_generation(semantic_transport.llm_model(), semantic_llm._FACT_V2_PROMPT)


def _chunk_prompt(template, spec, manifest, source, chunk_size):
    """Expose bounded source neighbours as reference only; evidence stays in the core piece."""
    prompt = template % spec["text"]
    references = set(spec.get("context_atom_ids") or []) | set(spec.get("dependency_atom_ids") or [])
    fragments = [source[atom["start"]:atom["end"]] for atom in manifest["atoms"]
                 if atom["atom_id"] in references]
    budget = max(chunk_size, 256)
    per_fragment = max(1, (budget - max(0, len(fragments) - 1)) // max(1, len(fragments)))
    context = "\n".join(fragment if len(fragment) <= per_fragment else
                        fragment[:max(0, (per_fragment - 1) // 2)] + "…" +
                        (fragment[-(per_fragment // 2):] if per_fragment > 1 else "")
                        for fragment in fragments)[:budget]
    if context:
        prompt = prompt.rsplit("JSON:", 1)[0] + (
            "\n参考文脈（対象本文の前後・見出し。対象や同薬の解釈用だけ。ここだけの事実は抽出せず、"
            "evidence_quoteと属性引用は上の対象本文<<< >>>内だけから取る）:\n"
            + context + "\nJSON:")
    return prompt


def _persist_manifest(ledger, project_id, message_id, model, source_fp,
                      manifest, body_len):
    if ledger is None:
        return
    doc = assembly.manifest_content(manifest, body_len)
    content = json.dumps(doc, ensure_ascii=False, allow_nan=False)
    meta = {VERSION_KEY: CURRENT_VERSION, "version": assembly.MANIFEST_VERSION,
            "source_fingerprint": source_fp, "body_codepoints": body_len,
            "atoms": len(manifest["atoms"]), "chunks": len(manifest["chunks"])}
    def store(add):
        latest = ledger.db.execute(
            "SELECT content,meta,model FROM artifacts WHERE kind=? AND project_id IS ? "
            "AND message_id IS ? ORDER BY artifact_id DESC LIMIT 1",
            (KIND_MANIFEST, project_id, message_id)).fetchone()
        if (latest is not None and latest["content"] == content
                and latest["meta"] == json.dumps(meta, ensure_ascii=False)
                and latest["model"] == model):
            return False
        add(KIND_MANIFEST, content, project_id=project_id, message_id=message_id,
            model=model, meta=meta)
        return True

    add_tx = getattr(ledger, "artifact_add_tx", None)
    if callable(add_tx):
        if ledger.db.in_transaction:
            # The caller owns both the write and any eventual rollback.
            changed = store(add_tx)
        else:
            with ledger.db:
                ledger.db.execute("BEGIN IMMEDIATE")
                changed = store(add_tx)
    else:
        # Small test ledgers own their transaction and only expose artifact_add.
        changed = store(ledger.artifact_add)
    if not changed or ledger.db.in_transaction:
        return
    from extraction_refresh import refresh_extraction_cards
    from semantic_store import refresh_cards_allowed
    if refresh_cards_allowed(getattr(ledger, 'db', None), project_id, message_id):
        refresh_extraction_cards(ledger, project_id, message_id)


def _json_object(response):
    try:
        parsed = mcs_util.json_object(response or "")
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None


def _member_prelude(member, llm_fn, project_id, source_fingerprint):
    """Shared input validation for the extraction entry points."""
    if not isinstance(member, dict) or not callable(llm_fn):
        raise ValueError("extraction_input_invalid")
    return _member_source(member, project_id, source_fingerprint)


def _member_source(member, project_id, source_fingerprint):
    """Validate source identity independently of any execution capability."""
    if not isinstance(member, dict):
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
            assembly.source_fingerprint(member, source_fingerprint),
            sf.text_sha256(source))


def _chunk_llm(llm_fn, prompt: str, deadline):
    """One chunk extraction call -> (parsed_doc, reason).  reason is
    "model" or "deadline" on failure; RuntimeGuardError propagates so
    the runtime boundary still aborts the job."""
    from semantic_runtime import RuntimeGuardError
    try:
        response = llm_fn(prompt)
    except RuntimeGuardError:
        raise
    except Exception:
        return None, "model"
    if deadline is not None and time.monotonic() > deadline:
        return None, "deadline"
    return _json_object(response), None


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


def _cached_chunks(db, project_id, message_id, source_fp, body_hash,
                   revision, specs, source: str, *,
                   kind: str = KIND_CHUNK,
                   schema: str = SCHEMA_VERSION,
                   extra=None, generation: str | None = None) -> dict[int, dict]:
    if db is None:
        return {}
    cached = {}
    rows = iter_artifacts(db, kind, project_id=project_id, message_id=message_id)
    wanted = {spec["index"]: spec for spec in specs}
    for row in rows:
        try:
            meta = json.loads(row["meta"] or "{}")
            if not isinstance(meta, dict) or type(meta.get("chunk_index")) is not int:
                continue
            index = meta["chunk_index"]
            spec = wanted[index]
            if (not is_current(meta) or meta.get("schema") != schema
                    or meta.get("generation") != generation
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
            if (not isinstance(content, dict)
                    or content.get("chunk_index") != index
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
                if evidence is not None:
                    if not isinstance(evidence, dict):
                        raise ValueError("cached_evidence_invalid")
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
                            or not spec["start"] <= start < end <= spec["end"]
                            or source[start:end] != quote
                            or spec["text"].find(quote) != start - spec["start"]
                            or spec["text"].rfind(quote) != start - spec["start"]):
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
        except (KeyError, TypeError, ValueError, RecursionError):
            continue
    return cached


def _normalise_facts(items: list, member: dict, source: str,
                     fact_offset: int, *,
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
                span = assembly.resolve_evidence(quote, piece, piece_start,
                                         source)
            else:
                span = mcs_util.locate_quote_span(source, quote)
        if span:
            # _locate_quote may have matched after whitespace
            # normalization — store the verbatim original so
            # body[s:e] == quote holds exactly for the audit.
            quote = source[span[0]:span[1]]
            atom_ref = assembly.evidence_atom(manifest, span[0], span[1]) \
                if manifest is not None else None
        index = fact_offset + len(facts)
        evidence_id = f"ev_{message_id}_{index}"
        status = item.get("status")
        polarity = item.get("polarity")
        time_text = item.get("time_text")
        fact = {
            "fact_id": f"fact_{message_id}_{index}",
            "kind": item.get("kind") if item.get("kind") in semantic_llm.FACT_KINDS
                    else "other",
            # Deliberately do not apply semantic.py's historical 200-char cap.
            "statement": statement,
            "subject_ref": f"m{message_id}",
            "drug_ref": item.get("drug_ref")
                        if isinstance(item.get("drug_ref"), str)
                        and span and item["drug_ref"] in quote else None,
            "status": status if status in semantic_llm.FACT_STATUSES else "not_stated",
            "polarity": polarity if polarity in semantic_llm.POLARITIES else "uncertain",
            "occurred_at": clinical_values.absolute_date(time_text),
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


def progress_for_message(db, row, *, model, policy_fingerprint, chunk_size=3000):
    """Count only current-plan checkpoints; completion requires the existing audited publication gate."""
    import semantic_llm
    from semantic_store import thread_bundle
    from semantic_v4 import reproject_doc
    from semantic_projection import PROJECTION_VERSION, project_v2_doc_legacy

    source = row.get("body_text")
    if (not isinstance(source, str) or not source or row.get("body_state") not in (None, "full")
            or not isinstance(model, str) or not model or not isinstance(policy_fingerprint, str)):
        return None
    pid, mid = row["project_id"], row["message_id"]
    bundle = thread_bundle(db, pid, row.get("parent_id") or mid, local_model=model)
    if bundle is None:
        return None
    fp = bundle["source_fingerprint"]
    manifest = assembly.build_manifest(source, fp, chunk_size)
    specs = assembly.manifest_specs(manifest)
    expected = assembly.manifest_content(manifest, len(source))
    persisted = db.execute("SELECT content,model FROM artifacts WHERE kind=? AND project_id=? "
                           "AND message_id=? ORDER BY artifact_id DESC LIMIT 1",
                           (KIND_MANIFEST, pid, mid)).fetchone()
    if persisted is None:
        return None
    out = {"state": "attention", "completed": 0, "total": len(specs), "backend": "semantic"}
    if persisted["model"] != model or loads_dict(persisted["content"]) != expected:
        return out
    publication_blocked = False
    for publication in db.execute(
            "SELECT content,meta,model FROM artifacts WHERE kind IN ('semantic_facts_v4','canonical_projection') "
            "AND project_id=? AND message_id=? ORDER BY artifact_id DESC", (pid, mid)):
        meta = loads_dict(publication["meta"])
        if not meta and publication["model"] == model:
            publication_blocked = True
            break
        if (not meta or not is_current(meta) or publication["model"] != model or meta.get("hash") != row["content_hash"]
                or meta.get("fingerprint") != fp or meta.get("policy_fingerprint") != policy_fingerprint):
            continue
        publication_blocked = True
        if meta.get("invalidated") or meta.get("projection_version") != PROJECTION_VERSION:
            break
        doc, _ = reproject_doc(db, mid, meta)
        try:
            clean = sf.validate_facts_doc(doc)
        except sf.ContractError:
            break
        keys = ("chunk_id", "core_atom_ids", "context_atom_ids", "dependency_atom_ids")
        if (clean["source"]["source_fingerprint"] == fp and clean["source"]["content_hash"] == sf.text_sha256(source)
                and clean["source"]["revision"] == row["content_hash"]
                and clean["source"]["message_id"] == str(mid)
                and clean["source"]["body_codepoints"] == len(source)
                and clean["atoms"] == manifest["atoms"]
                and [{key: chunk[key] for key in keys} for chunk in clean["chunks"]]
                == [{key: chunk[key] for key in keys} for chunk in manifest["chunks"]]
                and all(chunk["status"] in ("complete", "done") for chunk in clean["chunks"])
                and all(source[e["start"]:e["end"]] == e["quote"] for e in clean["evidence"])
                and loads_dict(publication["content"]) == project_v2_doc_legacy(clean)):
            return {**out, "state": "complete", "completed": len(specs)}
        break
    generation = _chunk_generation(model, semantic_llm._FACT_V2_PROMPT, chunk_size=chunk_size)
    cached = _cached_chunks(db, pid, mid, fp, sf.text_sha256(source), row["content_hash"], specs, source,
                            kind=KIND_CHUNK_V2, schema=SCHEMA_VERSION_V2,
                            extra=assembly.v2_chunk_ok, generation=generation)
    completed = len(cached)
    return {**out, "completed": completed,
            "state": "processing" if not publication_blocked and completed < len(specs) else "attention"}


def _persist_chunk(ledger, project_id, message_id, model, source_fp,
                   body_hash, revision, spec, facts, dropped, chunk_count,
                   *, kind: str = KIND_CHUNK, schema: str = SCHEMA_VERSION,
                   extra_content=None, generation: str | None = None):
    if ledger is None:
        return
    _require_idle_ledger(ledger)
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
        VERSION_KEY: CURRENT_VERSION,
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
        "generation": generation,
    }
    ledger.artifact_add(
        kind, json.dumps(content, ensure_ascii=False, allow_nan=False),
        project_id=project_id, message_id=message_id, model=model, meta=meta)
    from extraction_refresh import refresh_extraction_cards
    from semantic_store import refresh_cards_allowed
    if refresh_cards_allowed(getattr(ledger, 'db', None), project_id, message_id):
        refresh_extraction_cards(ledger, project_id, message_id)


def extract_facts_resumable(llm_fn, member: dict,
                            deadline: float | None = None, *,
                            ledger=None, source_fingerprint: str | None = None,
                            project_id=None, chunk_size: int = 3000,
                            chunker=None) -> dict:
    """Extract all source chunks with durable prefix reuse.

    ``failure_reason`` is ``"deadline"`` or ``"model"`` when incomplete,
    matching the optional reason returned by ``extract_facts``. A
    completed chunk is persisted before the next model call.  The returned
    facts are partial only when ``complete`` is false, and callers must keep
    them out of a terminal/pass artifact until the pending chunks succeed.
    """

    _require_idle_ledger(ledger)
    source, message_id, project_id, revision, source_fp, body_hash = \
        _member_prelude(member, llm_fn, project_id, source_fingerprint)
    manifest = assembly.build_manifest(source, source_fp, chunk_size)
    if chunker is None:
        # Canonical path: atom-owned chunks cover the source exactly once.
        specs = assembly.manifest_specs(manifest)
    else:
        specs = assembly.chunk_specs(source, chunk_size, chunker(source, chunk_size) if source else [])
    # one model identity for the manifest, the cache generation and every chunk
    model = semantic_transport.llm_model()
    _persist_manifest(ledger, project_id, message_id, model,
                      source_fp, manifest, len(source))
    generation = _chunk_generation(model, semantic_llm._FACT_PROMPT, chunk_size=chunk_size)
    cached = _cached_chunks((ledger.db if ledger is not None else None), project_id, message_id, source_fp,
                            body_hash, revision, specs, source,
                            generation=generation)

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
            llm_fn, _chunk_prompt(semantic_llm._FACT_PROMPT, spec, manifest, source, chunk_size), deadline)
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
            items, member, source, len(facts),
            piece=spec["text"], piece_start=spec["start"],
            manifest=manifest)
        if chunk_dropped:
            failed.append(index)
            failed_dropped = chunk_dropped
            failure_reason = "model"
            break
        # This is deliberately before the next loop iteration/model call.
        _persist_chunk(ledger, project_id, message_id, model,
                       source_fp,
                       body_hash, revision, spec, chunk_facts, chunk_dropped,
                       len(specs), generation=generation)
        facts.extend(chunk_facts)
        completed.append(index)

    complete = len(completed) == len(specs) and not failed
    return {
        "facts": facts,
        "complete": complete,
        **assembly.chunk_progress(specs, completed, reused, failed,
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
# Build of the stored semantic_facts_v2 document's hint merge.  A stored
# document without it that carries a merged extract_v1 fact predates the
# evidence-span rule and is rebuilt from its cached chunks (hint-span/1).
FACTS_V2_BUILD = "hint-span/1"

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


def _symptom_names(source, names):
    """Keep symptom reports; an explicit PRN instruction alone is not a symptom report."""
    result = []
    for name in names:
        if not isinstance(name, str) or not name:
            continue
        for match in re.finditer(re.escape(name), source):
            tail = re.split(r"[。！？!?;；\n]", source[match.end():], maxsplit=1)[0]
            contingency = re.match(
                r"\s*(?:(?:の)?(?:時(?!間|刻|期|点|々)|とき(?!どき))(?:に|には|は|のみ|だけ)?|"
                r"が(?:出た|ある|生じた)(?:場合|とき|時)(?:に|は)?)", tail)
            medicine = re.search(r"使用|服用|内服|服薬|頓服|投与|飲(?:む|ん|み)", tail)
            reported = re.search(
                r"(?:使用|服用|内服|頓服|投与)(?:した|しました|済)|飲んだ|飲みました|"
                r"現在|持続|続いて|悪化|強い|ない|ありません|不明", tail)
            if contingency and medicine and not reported:
                continue
            result.append(name)
            break
    return result


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
                          if isinstance(quote, str) else None})

    for med in hints.get("medications") or []:
        if isinstance(med, dict):
            add("medication_exposure",
                " ".join(p for p in (str(med.get("name") or ""),
                                     str(med.get("dose") or "")) if p),
                med.get("name"))
    for name in _symptom_names(source, hints.get("symptoms") or []):
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
    signals = {category for category, fields in _V1_CATEGORY_FIELDS.items()
            if any(hints.get(field) for field in fields)}
    if not _symptom_names(source, hints.get("symptoms") or []):
        signals.discard("symptom_state")
    return signals


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
    response = jev.evaluate_once_more(jev_client, state, questions, deadline)
    answers = response.get("answers") if isinstance(response, dict) \
        else None
    if not isinstance(answers, dict):
        raise ValueError("jev_preflight_invalid")
    verdicts = {}
    for category in sf.MANDATORY_CATEGORIES:
        answer = answers.get(f"has_{category}")
        choice = answer.get("choice") if isinstance(answer, dict) else None
        confidence = answer.get("confidence") if isinstance(answer, dict) else None
        valid_confidence = type(confidence) in (int, float) and 0 <= confidence <= 1
        verdicts[category] = choice if valid_confidence and choice in _PREFLIGHT_CHOICES \
            else "uncertain"
    return verdicts


def extract_facts_v2(llm_fn, member: dict,
                     deadline: float | None = None, *,
                     ledger=None, source_fingerprint: str | None = None,
                     project_id=None, chunk_size: int = 3000,
                     jev_client=None, retry_coverage: bool = False,
                     request_following: bool = False) -> dict:
    """Extract a contract-validated ``semantic-facts/v2`` document.

    Returns ``{"doc", "complete", "coverage", ...}``.  ``doc`` is always
    contract-valid; ``coverage.status`` is ``complete`` only when every
    chunk finished, nothing was dropped, no fact stayed unverified, and
    no obligation remains open/ambiguous/failed.  Completed chunks are
    persisted before the next model call so a restart reuses the prefix.
    """
    _require_idle_ledger(ledger)
    import semantic_llm

    if type(request_following) is not bool:
        raise ValueError("request_following must be boolean")
    prompt_template = (semantic_llm._FACT_V2_REQUEST_FOLLOWING_PROMPT
                       if request_following else semantic_llm._FACT_V2_PROMPT)
    source, message_id, project_id, revision, source_fp, body_hash = \
        _member_prelude(member, llm_fn, project_id, source_fingerprint)
    posted_at = member.get("posted_at") or ""
    manifest = assembly.build_manifest(source, source_fp, chunk_size)
    specs = assembly.manifest_specs(manifest)
    model = semantic_transport.llm_model()
    _persist_manifest(ledger, project_id, message_id, model,
                      source_fp, manifest, len(source))
    cache_schema = SCHEMA_VERSION_V2 + ("/coverage-retry" if retry_coverage else "")
    generation = _chunk_generation(model, prompt_template, chunk_size=chunk_size)
    cached = _cached_chunks((ledger.db if ledger is not None else None), project_id, message_id, source_fp,
                            body_hash, revision, specs, source,
                            kind=KIND_CHUNK_V2, schema=cache_schema,
                            extra=assembly.v2_chunk_ok, generation=generation)

    atom_owner = {}
    for chunk in manifest["chunks"]:
        for aid in chunk["core_atom_ids"]:
            atom_owner[aid] = chunk["chunk_id"]

    obligations = assembly.v2_obligations(manifest)

    facts = []
    presence = {}
    jev_verdicts = {}
    chunk_status = {}
    completed = []
    reused = []
    failed = []
    failed_dropped = 0
    failure_reason = None
    drop_reasons: dict = {}
    omitted: dict = {}

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
            llm_fn, _chunk_prompt(prompt_template, spec, manifest, source, chunk_size), deadline)
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
                          if verdicts.get(category) in assembly.CATEGORY_VERDICTS}
        link_marks = {oid: len(ob["fact_ids"])
                      for oid, ob in obligations.items()
                      if ob["owner_id"] == cid}
        chunk_facts, chunk_dropped, chunk_obligations, drop_reasons, omitted = assembly.normalise_facts_v2(
            items, member, source, spec=spec,
            manifest=manifest, atom_owner=atom_owner,
            obligations={oid: obligations[oid] for oid in link_marks}, drop_reasons=drop_reasons,
            omitted=omitted)
        obligations.update(chunk_obligations)
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
                       schema=cache_schema, extra_content=extra,
                       generation=generation)
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

    hint_items = _v1_hint_items(source, posted_at)
    signals = {chunk["chunk_id"]: _v1_category_signals(chunk["text"], posted_at)
               for chunk in manifest["chunks"]}
    assembled = assembly.assemble_facts(member, source, manifest, facts, obligations,
                                        presence, jev_verdicts, chunk_status, hint_items, signals,
                                        use_jev_preflight=jev_client is not None)
    from semantic_relations import reconcile_facts
    reconciled = reconcile_facts([], assembled["facts"], deadline=deadline)
    progress = assembly.chunk_progress(specs, completed, reused, failed,
                                       failure_reason, failed_dropped, source_fp)
    return assembly.assemble_document(assembly.source_binding(member, source_fp), manifest,
                                      assembled, reconciled, progress, drop_reasons=drop_reasons,
                                      null_contexts_omitted=omitted.get("context_null", 0),
                                      generation=generation if request_following else None)


def rebuild_facts_v2_cached(member: dict, db, source_fingerprint: str,
                            project_id, stored_doc: dict, stored_meta: dict):
    """Assemble stored chunks without model/preflight dispatch, writes or transaction ownership."""
    if not is_current(stored_meta):
        return None, 'inference_version_changed'
    source, message_id, project_id, revision, source_fp, body_hash = \
        _member_source(member, project_id, source_fingerprint)
    manifest = assembly.build_manifest(source, source_fp, 3000)
    expected = assembly.manifest_content(manifest, len(source))
    for row in iter_artifacts(db, KIND_MANIFEST, project_id, message_id, descending=True):
        meta = loads_dict(row["meta"])
        if (meta is not None and is_current(meta) and meta.get("source_fingerprint") == source_fp
                and meta.get("version") == assembly.MANIFEST_VERSION):
            if loads_dict(row["content"]) != expected:
                return None, "manifest_changed"
            break
    else:
        return None, "manifest_changed"
    specs = assembly.manifest_specs(manifest)
    retry = bool(stored_meta.get("coverage_retry"))
    cached = _cached_chunks(db, project_id, message_id, source_fp, body_hash, revision,
                            specs, source, kind=KIND_CHUNK_V2,
                            schema=SCHEMA_VERSION_V2 + ("/coverage-retry" if retry else ""),
                            extra=assembly.v2_chunk_ok,
                            generation=v2_generation())
    if any(spec["index"] not in cached for spec in specs):
        return None, "chunk_cache_miss"
    jev_used = any(o.get("source") == "jev_pre" for o in stored_doc.get("obligations") or [])
    if jev_used and any(not isinstance(cached[spec["index"]]["extra"].get("jev_preflight"), dict)
                        for spec in specs):
        return None, "jev_preflight_uncached"
    obligations = assembly.v2_obligations(manifest)
    facts, presence, preflight, status, completed = [], {}, {}, {}, []
    for spec in specs:
        chunk = cached[spec["index"]]
        cid = spec["chunk_id"]
        facts.extend(chunk["facts"])
        presence[cid] = chunk["extra"]["category_presence"]
        if isinstance(chunk["extra"].get("jev_preflight"), dict):
            preflight[cid] = chunk["extra"]["jev_preflight"]
        status[cid] = "complete"
        completed.append(spec["index"])
        for fact in chunk["facts"]:
            for oid in fact.get("obligation_ids", []):
                if oid in obligations:
                    obligations[oid]["fact_ids"].append(fact["fact_id"])
    posted_at = member.get("posted_at") or ""
    hints = _v1_hint_items(source, posted_at)
    signals = {chunk["chunk_id"]: _v1_category_signals(chunk["text"], posted_at)
               for chunk in manifest["chunks"]}
    assembled = assembly.assemble_facts(member, source, manifest, facts, obligations,
                                        presence, preflight, status, hints, signals,
                                        use_jev_preflight=jev_used)
    from semantic_relations import reconcile_facts
    reconciled = reconcile_facts([], assembled["facts"])
    progress = assembly.chunk_progress(specs, completed, list(completed), [], None, 0, source_fp)
    result = assembly.assemble_document(assembly.source_binding(member, source_fp), manifest,
                                        assembled, reconciled, progress)
    if not result["extraction_complete"]:
        return None, "chunk_cache_miss"
    return result["doc"], None


def repair_facts_v2(llm_fn, member: dict, doc: dict,
                    rejected: dict, deadline: float | None = None,
                     *, chunk_size: int = 3000,
                     request_following: bool = False,
                     coverage_missing: bool = False) -> dict:
    """Re-extract rejected owner chunks, or all chunks when source coverage is missing.

    Merge repaired items into a re-validated document.

    ``rejected`` maps fact_id -> human-readable reason.  Returns
    ``{"doc", "repaired", "repaired_fact_ids", "owner_chunk_ids"}``;
    ``repaired`` is False when nothing could be dispatched (deadline or
    unusable input) — the caller then keeps the prior doc and holds the
    generation rather than degrading it.
    """
    import semantic_llm

    if type(request_following) is not bool:
        raise ValueError("request_following must be boolean")
    if type(coverage_missing) is not bool:
        raise ValueError("coverage_missing must be boolean")
    generation_detail = {}
    if request_following:
        generation_detail["generation"] = _chunk_generation(
            semantic_transport.llm_model(), semantic_llm._FACT_V2_REQUEST_FOLLOWING_PROMPT, chunk_size=chunk_size)
    # Only one return runs per call, so the early exits share this dict.
    unchanged = {"doc": doc, "repaired": False, "repaired_fact_ids": [],
                 "owner_chunk_ids": [], **generation_detail}
    if not isinstance(doc, dict) or not isinstance(member, dict) \
            or not (rejected or coverage_missing) or not callable(llm_fn):
        return unchanged
    prompt_template = (semantic_llm._FACT_V2_REQUEST_FOLLOWING_PROMPT
                       if request_following else semantic_llm._FACT_V2_PROMPT)

    source = member.get("body_original")
    source_meta = doc.get("source")
    if (not isinstance(source, str) or not isinstance(source_meta, dict)
            or source_meta.get("content_hash") != sf.text_sha256(source)
            or source_meta.get("message_id") != str(member.get("message_id"))
            or source_meta.get("revision") != str(member.get("revision", ""))):
        return unchanged
    source_fp = doc["source"]["source_fingerprint"]
    manifest = assembly.build_manifest(source, source_fp, chunk_size)
    specs = assembly.manifest_specs(manifest)
    doc_chunk_ids = {c["chunk_id"] for c in doc.get("chunks", [])}
    if ({c["chunk_id"] for c in manifest["chunks"]} != doc_chunk_ids
            or manifest["atoms"] != doc.get("atoms")):
        # The stored doc does not match a fresh atomization of the
        # source — repair cannot locate owners safely.
        return unchanged

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
    if not rejected_ids and not coverage_missing:
        return unchanged

    # rejected fact -> owning chunks via obligation links; evidence
    # quotes inside a chunk's text are a fallback locator.
    owner_ids = set(doc_chunk_ids) if coverage_missing else set()
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
        return unchanged

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
        if coverage_missing:
            feedback += "\n- 原文の重要な記載に取りこぼしがあります。既存の事実を保ち、同じ原文から不足を再確認してください。"
        base = _chunk_prompt(prompt_template, spec, manifest, source, chunk_size)
        base = base.rsplit("JSON:", 1)[0]
        prompt = base + semantic_llm._FACT_V2_REPAIR_SUFFIX % feedback
        parsed, _reason = _chunk_llm(llm_fn, prompt, deadline)
        if parsed is None:
            continue
        items = parsed.get("facts")
        if not isinstance(items, list):
            continue
        repaired_items[cid] = items
        dispatched.append(cid)
    if not dispatched:
        return unchanged

    # Normalise repaired items per owning chunk, then merge by fact_id
    # once, with the same rules as extraction's duplicate merge.  The
    # merge edits facts in place, so it works on copies: an unrepaired
    # return must hand back the stored doc untouched.
    facts = copy.deepcopy([f for f in doc["facts"]
                           if f.get("fact_id") not in rejected_ids])
    kept_ids = {f["fact_id"] for f in facts}
    for ob in obligations.values():
        ob["fact_ids"] = [fid for fid in ob.get("fact_ids", [])
                          if fid in kept_ids]
    collected = []
    for spec in specs:
        cid = spec["chunk_id"]
        items = repaired_items.get(cid)
        if items is None:
            continue
        link_marks = {oid: len(ob["fact_ids"])
                      for oid, ob in obligations.items()
                      if ob["owner_id"] == cid}
        new_facts, dropped_n, chunk_obligations, _, _ = assembly.normalise_facts_v2(
            items, member, source, spec=spec,
            manifest=manifest, atom_owner=atom_owner,
            obligations={oid: obligations[oid] for oid in link_marks})
        obligations.update(chunk_obligations)
        if dropped_n:
            # Same fail-the-chunk semantics as extraction: a repair
            # chunk that produced malformed items contributes nothing —
            # links roll back and partial facts never merge (FIX-SE4).
            for oid, mark in link_marks.items():
                del obligations[oid]["fact_ids"][mark:]
            continue
        collected.extend(new_facts)
    # One merge over every chunk: a detail that conflicted in one chunk
    # cannot be revived by a later one.
    facts = assembly.merge_dupe_facts(facts + collected)
    repaired_fact_ids = list(dict.fromkeys(
        f["fact_id"] for f in collected if f["fact_id"] not in kept_ids))

    # A covered jev_pre obligation snapshots its deterministic
    # counterpart's links at extraction; re-sync so repaired facts
    # keep a "present" verdict covered instead of reopening it.
    det_by_owner = {(ob["owner_id"], ob["category"]): ob
                    for ob in obligations.values()
                    if ob.get("source") == "deterministic"}
    for ob in obligations.values():
        det = det_by_owner.get((ob["owner_id"], ob["category"]))
        if (ob.get("source") == "jev_pre" and ob["status"] == "covered"
                and det is not None):
            ob["fact_ids"] = list(det["fact_ids"])

    # Evidence surviving from the original doc stays keyed by id;
    # repaired facts contribute their fresh _evidence entries.
    evidence = {e["evidence_id"]: e for e in doc.get("evidence", [])
                if isinstance(e, dict) and e.get("evidence_id")}
    clean_facts, evidence = assembly.collect_facts(facts, evidence)

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
        elif ob["fact_ids"] and ob["status"] == "explicit_no_fact":
            # an adjudicated absence now contradicted by a repaired fact is
            # ambiguous, exactly as at extraction — never covered
            ob["status"] = "ambiguous"
        elif ob["fact_ids"] and ob["status"] not in ("ambiguous", "failed"):
            # ambiguous/failed were held open by verdicts or jev_pre;
            # links alone never upgrade them.
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
        "category_counts": assembly.category_counts(clean_facts),
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
    # surviving set so every endpoint resolves.  A scan cut by the
    # deadline must not publish a partial set: keep the prior doc.
    from semantic_relations import reconcile_facts
    reconciled = reconcile_facts([], clean_facts, deadline=deadline)
    if reconciled["pairs_skipped"]:
        return {**unchanged, "owner_chunk_ids": dispatched,
                "relation_pairs_skipped": reconciled["pairs_skipped"]}
    new_doc["relations"] = reconciled["relations"]
    sf.validate_facts_doc(new_doc)
    return {"doc": new_doc, "repaired": True,
            "repaired_fact_ids": repaired_fact_ids,
             "owner_chunk_ids": dispatched, **generation_detail}


def extract_facts(llm_fn, member: dict,
                  deadline: float | None = None,
                  return_reason: bool = False, *, ledger=None,
                  source_fingerprint=None, project_id=None) -> tuple:
    """Whole-source extraction; durable chunks when called by the worker."""
    result = extract_facts_resumable(
        llm_fn, member, deadline, ledger=ledger,
        source_fingerprint=source_fingerprint, project_id=project_id)
    values = (result["facts"], result["complete"], result["dropped"])
    return (*values, result["failure_reason"]) if return_reason else values
