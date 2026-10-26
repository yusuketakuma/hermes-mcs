"""Plan bounded patient-context source scans and validate verbatim selections without I/O."""
import json
from itertools import accumulate

from clinical_chunking import plan_chunks
from patient_context import CATEGORIES, context_items

MAX_FACTS = 256
MAX_SUMMARY = 12
MAX_SOURCE_FACTS = 128  # Per accepted chunk, never a cumulative source limit.
MAX_PROMPT_CHARS = 18000
# A scan answer quotes every sentence it keeps twice (text and evidence), so a
# background-rich prose chunk can outgrow the fixed answer budget; such a chunk
# is re-planned finer, a bounded number of times, instead of retried unchanged.
SCAN_CHUNK = 3000
MAX_SCAN_SPLIT = 3
MAX_SCAN_ITEMS = 1024  # Bounded answer validation, independent of storage-shard size.


def select_facts(facts):
    """Select category-fair reports without changing per-source storage."""
    # Selection is bounded, storage remains per source. Round robin categories
    # prevents one busy topic from hiding every older background category.
    ordered = sorted(facts.values(), key=lambda f: (f.get("posted_at_ts") or 0, f["id"]), reverse=True)
    buckets = []
    for bucket in ("current", "conflict", "plan", "unknown", "history"):
        groups = [[f for f in ordered if f["category"] == c and f.get("view_bucket", "unknown") == bucket]
                  for c in sorted(CATEGORIES)]
        buckets.append([group[i] for i in range(max(map(len, groups), default=0)) for group in groups if i < len(group)])
    selected = [group[i] for i in range(max(map(len, buckets), default=0)) for group in buckets if i < len(group)]
    return selected[:MAX_FACTS], len(selected)


def summary_reports(facts):
    """Deduplicate source reports within the unchanged prompt budget."""
    reports, used = [], 0
    seen = set()
    for fact in facts:
        key = fact["id"]
        if key in seen:
            continue
        seen.add(key)
        item = {k: fact[k] for k in ("id", "project_id", "category", "posted_at", "subject")}
        item.update(text=summary_text(fact), details=fact.get("details", []),
                    reported_subject=fact.get("reported_subject"),
                    evidence_span=fact.get("evidence_span"), reference_context=fact.get("reference_context"),
                    view_bucket=fact.get("view_bucket", "unknown"), temporal=fact.get("temporal"),
                    relations=fact.get("relations", []))
        size = len(json.dumps(item, ensure_ascii=False))
        if used + size <= MAX_PROMPT_CHARS - 2000:
            reports.append(item)
            used += size
    if not reports:
        raise ValueError("nightly_summary_capacity")
    return reports


def summary_text(fact):
    """Select bounded verbatim context, retaining the atomic assertion separately."""
    return fact.get("quote") or fact["text"]


def summary_selection(answer, reports):
    """Accept unique known IDs and retain their exact source text."""
    ids = answer.get("fact_ids")
    by_id = {f["id"]: f for f in reports}
    if (set(answer) != {"fact_ids"} or not isinstance(ids, list) or not 1 <= len(ids) <= MAX_SUMMARY
            or any(not isinstance(i, str) or i not in by_id for i in ids) or len(set(ids)) != len(ids)):
        raise ValueError("nightly_unsupported_summary")
    return [{"text": by_id[i]["text"], "fact_ids": [i]} for i in ids]


def scan_plan(source, saved):
    """Choose one chunk; quoted text coverage alone never proves fact coverage."""
    split = saved.get("split", 0)
    if type(split) is not int or not 0 <= split <= MAX_SCAN_SPLIT:
        split, saved = 0, {}
    chunks = plan_chunks(source["body_text"], SCAN_CHUNK >> split)
    cursor = saved.get("cursor", 0)
    if type(cursor) is not int or not 0 <= cursor < len(chunks):
        # a reset restarts the whole plan: a finer split must not outlive its cursor
        cursor, saved = 0, {}
        chunks = plan_chunks(source["body_text"], SCAN_CHUNK)
    chunk = chunks[cursor]
    items = context_items({"patient_context": source["items"]}, chunk)
    return chunks, cursor, saved, items, False


def split_scan(source, saved, cursor):
    """The finer plan after a length stop, resumed where the stopped chunk began.

    None once the finest plan also stopped: that stays an ordinary failure."""
    split = saved.get("split", 0)
    body = source["body_text"]
    coarse = plan_chunks(body, SCAN_CHUNK >> split)
    offset = sum(map(len, coarse[:cursor]))
    for level in range(split + 1, MAX_SCAN_SPLIT + 1):
        finer = plan_chunks(body, SCAN_CHUNK >> level)
        starts = list(accumulate(map(len, finer), initial=0))[:len(finer)]
        # the chunks partition the body, so some finer chunk starts at or before it
        resume = max(i for i, start in enumerate(starts) if start <= offset)
        # a re-plan that leaves the stopped text whole would only repeat the stop
        if len(finer[resume]) < len(coarse[cursor]):
            return {"cursor": resume, "chunks": len(finer), "split": level}
    return None


def _checked_item(item, chunk):
    """(grounded item, None) or (None, fixed refusal reason) for one scan item."""
    valid = context_items({"patient_context": [item]}, chunk)
    if not valid:
        return None, "not_verbatim_or_invalid"
    if isinstance(item, dict) and item.get("details", []) != valid[0].get("details", []):
        return None, "details_not_in_quote"
    return valid[0], None


def rejected_items(answer, chunk):
    """Fixed reasons for each refused scan item, by index only: never its text."""
    out = []
    for index, item in enumerate(answer.get("patient_context") or []):
        reason = _checked_item(item, chunk)[1]
        if reason:
            out.append({"index": index, "reason": reason})
    return out


def grounded_items(answer, chunk):
    """The items of a refused answer that pass the same checks on their own."""
    return [valid for valid in (_checked_item(item, chunk)[0]
                                for item in answer.get("patient_context") or []) if valid]


def scan_items(answer, chunk):
    """Reject malformed, unsupported or changed grounded attributes."""
    raw = answer.get("patient_context")
    if isinstance(raw, list) and len(raw) > MAX_SCAN_ITEMS:
        raise ValueError("nightly_scan_capacity")
    if (set(answer) != {"patient_context"} or not isinstance(raw, list)
            or any(not isinstance(item, dict) or set(item) - {
                "category", "text", "evidence", "subject", "details"} for item in raw)):
        raise ValueError("nightly_invalid_items")
    checked = context_items(answer, chunk)
    if len(checked) != len(raw) or any(
            item.get("details", []) != valid.get("details", []) for item, valid in zip(raw, checked)):
        raise ValueError("nightly_unsupported_items")
    return checked


def candidate_key(snapshot, progress, pid, lane):
    """Prioritize initial publication, then the least recently attempted patient."""
    return (bool(snapshot.get("scope", {}).get("is_archived")),
            not snapshot["initial"] if lane == "A" else False, progress.get("last_attempt", 0), pid)


def lane_rotation(best, rotation):
    """Choose the next available lane with the unchanged six/three/one schedule."""
    sequence = "AAAAAABBBC"
    index = rotation.get("cursor", 0) % len(sequence)
    lane = next((sequence[(index + i) % len(sequence)] for i in range(len(sequence))
                 if sequence[(index + i) % len(sequence)] in best), None)
    return lane, index, (index + 1) % len(sequence)


def ranges_complete(ranges, size):
    """Accepted chunk intervals must cover every source codepoint without a gap."""
    intervals = []
    for span in ranges:
        if (not isinstance(span, list) or len(span) != 2 or any(type(n) is not int for n in span)
                or not 0 <= span[0] < span[1] <= size):
            return False
        intervals.append(span)
    end = 0
    for left, right in sorted(intervals):
        if left > end:
            return False
        end = max(end, right)
    return size > 0 and end == size


def assertion_parts(text, details):
    """Partition a source quote without cutting a grounded attribute value."""
    from mcs_util import locate_quote_span
    protected = []
    for detail in details:
        value = detail.get("value")
        span = locate_quote_span(text, value) if isinstance(value, str) else None
        if span is not None:
            if span[1] - span[0] > SCAN_CHUNK:
                raise ValueError("nightly_unsplittable_assertion")
            protected.append(span)
    parts, start = [], 0
    for planned in plan_chunks(text, SCAN_CHUNK):
        end = start + len(planned)
        # Planner boundaries may move left to retain an indivisible value.
        while any(left < end < right for left, right in protected):
            end = min(left for left, right in protected if left < end < right)
        if end > start:
            parts.append(text[start:end])
            start = end
    # Repartition any tail introduced by moving a boundary, maintaining the
    # same source coordinates. Overlapping indivisible values can make a
    # single assertion unsplittable; that is an explicit hold, never a drop.
    if start < len(text):
        while start < len(text):
            end = min(len(text), start + SCAN_CHUNK)
            while any(left < end < right for left, right in protected):
                end = min(left for left, right in protected if left < end < right)
            if end <= start:
                raise ValueError("nightly_unsplittable_assertion")
            parts.append(text[start:end])
            start = end
    return parts
