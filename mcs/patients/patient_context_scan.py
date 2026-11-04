"""Transform one patient-context scan with caller-owned answers and discovery time."""
import patient_context_plan as context_plan


def facts_signature(facts):
    """Bind meaning and relations apart from discovery bookkeeping."""
    return context_plan.source_digest([{k: v for k, v in fact.items()
        if k not in ("found_at", "found_night_id", "discovery")} for fact in facts])


def scan_source(source, saved, night_id, *, answer_for, clock, metrics=None, source_repair=None):
    """Validate one answer through the shared scan and protected-fact rules."""
    chunks, cursor, saved, items, covered = context_plan.scan_plan(source, saved)
    chunk = chunks[cursor]
    split = saved.get("split", 0)
    # passed bounded repairs on this source: a health/status count, never content
    repairs = saved.get("repairs", 0) if type(saved.get("repairs")) is int else 0
    prior_repairs = repairs
    original = (context_plan.source_facts(source, source["items"], night_id=night_id, discovery="new_report", found_at=clock())
                if source_repair is None else source_repair["protected"])
    if source_repair is not None:
        items = []  # No unverified seed from the rejected original candidate.
    if not covered:
        payload = {"instruction": context_plan.SCAN_INSTRUCTION,
            **context_plan.scan_schema(), "already_found": items, "source": chunk}
        if source_repair is not None:
            payload["source_quality_findings"] = source_repair["codes"]
        def asked(body):
            """The answer, or the finer re-plan when it stopped at the answer budget."""
            try:
                return answer_for(body), None
            except ValueError as exc:
                finer = str(exc) == "nightly_length_stop" and context_plan.split_scan(source, saved, cursor)
                if not finer:
                    raise
                # nothing was accepted from this chunk: resume on the finer plan, keeping facts
                return None, {**finer, "complete": False, "facts": saved.get("facts", []), "audited": False,
                              **({"repairs": repairs} if repairs else {})}
        answer, replan = asked(payload)
        if replan:
            return replan
        try:
            checked = context_plan.scan_items(answer, chunk)
        except ValueError as exc:
            if str(exc) == "nightly_scan_capacity":
                finer = context_plan.split_scan(source, saved, cursor)
                if finer:
                    return {**finer, "complete": False, "facts": [], "audited": False, "repairs": repairs}
                raise ValueError("nightly_unsplittable_assertion") from exc
            if str(exc) != "nightly_unsupported_items" or source_repair is not None:
                raise
            # one bounded repair: name the refused items by index and fixed reason only;
            # it must pass whole and keep every item that already passed on its own
            kept = context_plan.grounded_items(answer, chunk)
            if metrics is not None:
                metrics["rejected"] = len(context_plan.rejected_items(answer, chunk))
            answer, replan = asked({**payload, "instruction": payload["instruction"]
                                    + "前回の回答のrejectedの項目は、原文と完全一致しないか、属性が引用の中にありません。"
                                    "原文に完全一致する引用だけでJSON全体を返し直す。裏付けできない項目は含めない。",
                                    "rejected": context_plan.rejected_items(answer, chunk)})
            if replan:
                return replan
            checked = context_plan.scan_items(answer, chunk)
            if any(item not in checked for item in kept):
                raise ValueError("nightly_unsupported_items")
            repairs += 1
        items += checked
    if metrics is not None:
        metrics.setdefault("rejected", 0)
        metrics["repairs"] = repairs - prior_repairs
    offset = sum(map(len, chunks[:cursor]))
    facts = {f["id"]: f for f in original if offset <= f["evidence_span"][0] < offset + len(chunk)}
    known = {f["id"] for f in original}
    for fact in context_plan.source_facts(source, items, night_id=night_id, discovery="backfill_found", offset=offset, chunk=chunk, found_at=clock()):
        if source_repair is not None and fact["id"] in source_repair.get("historical_by_id", {}):
            # A Karte moving into history changes display placement, not the
            # meaning of an existing support question or its original answer.
            fact.pop("historical", None)
            fact.update(source_repair["historical_by_id"][fact["id"]])
        if fact["id"] in known:
            fact["discovery"] = "new_report"
        if source_repair is not None and fact["id"] in facts:
            if facts_signature([facts[fact["id"]]]) != facts_signature([fact]):
                raise ValueError("nightly_source_repair_protected")
            continue  # Retain the original supported fact, including its bookkeeping.
        facts[fact["id"]] = fact
    return {"chunk_span": [offset, offset + len(chunk)], "cursor": cursor + 1, "chunks": len(chunks), "complete": cursor + 1 == len(chunks),
            "facts": list(facts.values()), "audited": False, **({"split": split} if split else {}),
            **({"repairs": repairs} if repairs else {})}
