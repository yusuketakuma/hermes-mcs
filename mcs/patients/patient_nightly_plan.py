"""Plan bounded nightly source scans and validate verbatim selections without I/O."""
import json
from clinical_chunking import plan_chunks
from patient_context import CATEGORIES, context_items

MAX_FACTS = 256
MAX_SUMMARY = 12
MAX_SOURCE_FACTS = 128
MAX_PROMPT_CHARS = 18000


def select_facts(facts):
    """Select category-fair reports without changing per-source storage."""
    # Selection is bounded, storage remains per source. Round robin categories
    # prevents one busy topic from hiding every older background category.
    ordered = sorted(facts.values(), key=lambda f: (f.get("posted_at_ts") or 0, f["id"]), reverse=True)
    groups = [[f for f in ordered if f["category"] == c] for c in sorted(CATEGORIES)]
    selected = [group[i] for i in range(max(map(len, groups), default=0)) for group in groups if i < len(group)]
    return selected[:MAX_FACTS], len(selected)


def summary_reports(facts):
    """Deduplicate source reports within the unchanged prompt budget."""
    reports, used = [], 0
    seen = set()
    for fact in facts:
        key = (fact["source"], fact["project_id"], fact["message_id"], fact["text"])
        if key in seen:
            continue
        seen.add(key)
        item = {k: fact[k] for k in ("id", "project_id", "category", "text", "posted_at", "subject")}
        size = len(json.dumps(item, ensure_ascii=False))
        if used + size <= MAX_PROMPT_CHARS - 2000:
            reports.append(item)
            used += size
    if not reports:
        raise ValueError("nightly_summary_capacity")
    return reports


def summary_selection(answer, reports):
    """Accept unique known IDs and retain their exact source text."""
    ids = answer.get("fact_ids")
    by_id = {f["id"]: f for f in reports}
    if (set(answer) != {"fact_ids"} or not isinstance(ids, list) or not 1 <= len(ids) <= MAX_SUMMARY
            or any(not isinstance(i, str) or i not in by_id for i in ids) or len(set(ids)) != len(ids)):
        raise ValueError("nightly_unsupported_summary")
    return [{"text": by_id[i]["text"], "fact_ids": [i]} for i in ids]


def scan_plan(source, saved):
    """Choose one chunk and determine whether its complete quote already covers it."""
    chunks = plan_chunks(source["body_text"])
    cursor = saved.get("cursor", 0)
    if type(cursor) is not int or not 0 <= cursor < len(chunks):
        cursor, saved = 0, {}
    chunk = chunks[cursor]
    items = context_items({"patient_context": source["items"]}, chunk)
    # A fully quoted labelled unit needs no second inference. Unknown prose
    # is still visited once; absence of a category is never a fabricated value.
    uncovered = list(chunk)
    for item in items:
        quote = item["evidence"]
        start = chunk.find(quote)
        if start >= 0:
            uncovered[start:start + len(quote)] = " " * len(quote)
    covered = bool(chunk.strip()) and not "".join(uncovered).strip()
    return chunks, cursor, saved, items, covered


def scan_items(answer, chunk):
    """Reject malformed, unsupported or changed grounded attributes."""
    raw = answer.get("patient_context")
    if (set(answer) != {"patient_context"} or not isinstance(raw, list) or len(raw) > 32
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
    return (not snapshot["initial"] if lane == "A" else False, progress.get("last_attempt", 0), pid)


def lane_rotation(best, rotation):
    """Choose the next available lane with the unchanged six/three/one schedule."""
    sequence = "AAAAAABBBC"
    index = rotation.get("cursor", 0) % len(sequence)
    lane = next((sequence[(index + i) % len(sequence)] for i in range(len(sequence))
                 if sequence[(index + i) % len(sequence)] in best), None)
    return lane, index, (index + 1) % len(sequence)
