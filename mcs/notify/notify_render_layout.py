"""Transport-neutral text budgets, display serialization and page layout."""
from __future__ import annotations

import re


PAGE_TEXT_BUDGET = 3200


def display_text(parts: dict) -> str:
    """カードの可視本文を順序どおり連結し、通知を発生させるメンションを除く。"""
    return render_text(parts, head="{}")


# the shared display model's text budget (adapters/common/spec.py
# MAX_TOTAL_TEXT — the tightest transport, Discord Components V2)
PARTS_TEXT_BUDGET = 4000


_FOLD_RE = re.compile(r"^・…他([0-9]+)件$")


def _text_cost(parts: dict) -> int:
    """Text size as the strictest consumer counts it: the larger of the
    visible text and adapters/common/spec.py's per-item accounting
    (_blocks_len/_footer_len — the container types are spec's closed
    set), so a folded card never fails the worker's ``text_budget`` check."""
    return max(_blocks_len(parts["containers"])
               + _footer_len(parts.get("footer") or []),
               len(display_text(parts)))


def fit_parts(parts: dict, limit: int = PARTS_TEXT_BUDGET) -> dict:
    """Fold list lines (``・`` bullets) of text containers marked
    ``"fold": True`` (one item per line) until the visible text fits
    ``limit``: the longest such list loses its last rows first and ends
    with ``・…他N件``. Unmarked containers — disclosures such as fetch
    gaps, aggregate lines — and the footer are never folded, so a
    content builder never sizes for a transport."""
    containers = [dict(c) for c in parts["containers"]]
    out = {**parts, "containers": containers}

    def bullets(c):
        return [i for i, ln in enumerate(c["text"].split("\n"))
                if ln.startswith("・") and not _FOLD_RE.match(ln)]

    while _text_cost(out) > limit:
        lists = [c for c in containers
                 if c["type"] == "text" and c.get("fold") and bullets(c)]
        if not lists:
            break
        c = max(lists, key=lambda c: len(bullets(c)))
        lines = c["text"].split("\n")
        folded = 0
        marks = [i for i, ln in enumerate(lines) if _FOLD_RE.match(ln)]
        if marks:  # may sit before a trailing note line
            folded = int(_FOLD_RE.match(lines[marks[-1]]).group(1))
            del lines[marks[-1]]
        last = bullets({"text": "\n".join(lines)})[-1]
        lines[last] = f"・…他{folded + 1}件"
        c["text"] = "\n".join(lines)
    return out


def presenter(transport: str):
    """The per-transport presentation module (present_slack /
    present_discord / present_lineworks) for a validated transport."""
    import importlib
    if transport not in ("slack", "discord", "lineworks"):
        raise ValueError("bad_transport")
    return importlib.import_module("present_" + transport)


def render_text(parts: dict, head: str = "【{}】", lit=str,
                footer: str = "{}") -> str:
    """Shared mechanics of a display model as one chat's text; each
    transport's present_* module picks the heading, literal escaping
    and footer marker."""
    lines = []
    for item in parts["containers"]:
        kind = item["type"]
        if kind == "meta":
            continue
        if parts.get("processing_display_version") in (1, 2, 3) and item.get("provider_scope") == "header":
            import processing_route
            item = {**item, "text": processing_route.compact_header(item)}
        if item.get("rule") and lines:
            lines.append(SECTION_RULE)
        if kind == "heading":
            lines.append(head.format(lit(item["text"])))
        elif kind == "field":
            lines.append(f"{lit(str(item['name']))}: {lit(str(item['value']))}")
        else:
            lines.append(f"引用: {lit(item['text'])}" if kind == "quote"
                         else lit(item["text"]))
    foot = [lit(ln) for item in parts.get("footer") or []
            if item["type"] == "text" for ln in item["text"].splitlines()]
    if foot and lines:
        lines.append(SECTION_RULE)
    lines.extend(footer.format(ln) for ln in foot)
    return MENTION_TOKEN_PATTERN.sub("メンバー", "\n".join(lines))


def parts_text(parts: dict, dialect: str = "plain") -> str:
    """The display model as one chat's text in ``dialect``: a transport
    (``discord``/``slack``/``lineworks``) uses its present_* module;
    ``plain`` (CLI, relayed notices) is the LINE WORKS-style text."""
    if dialect in ("discord", "slack", "lineworks"):
        return presenter(dialect).parts_text(parts)
    return render_text(parts)


def _blocks_len(blocks) -> int:
    """Rendered length of container blocks under the same accounting the
    Components-V2 validator applies (text +4, field name+value +6)."""
    n = 0
    for b in blocks:
        if b["type"] in ("heading", "text"):
            n += len(b.get("text") or "") + 4
        elif b["type"] == "quote":
            n += sum(len(line) + 3 for line in (b.get("text") or "").splitlines())
        elif b["type"] == "field":
            n += len(b.get("name") or "") + len(b.get("value") or "") + 6
    return n


def _cap_card_text(text, cap=PAGE_TEXT_BUDGET) -> str:
    """Full text on the card, bounded only by the physical per-page
    ceiling — an explicit marker points at 本文表示/原本 for the tail
    that cannot physically fit."""
    cap = max(cap, 80)
    if len(text) <= cap:
        return text
    # cut at a line boundary when one lies in the second half, so a drug
    # or request row is dropped whole rather than left half-read (one row
    # per item); a single long line still cuts mid-text
    end = text.rfind("\n", cap // 2, cap - 1)
    if end == -1:
        end = cap - 1
    return text[:end] + "…\n（省略 — 本文表示または原本を参照）"


def _fit_item(blocks, budget=PAGE_TEXT_BUDGET) -> list:
    """Bound one pageable item (a signal's block set) to the per-page
    budget: the largest text/quote block shrinks first — structured
    fields yield only as a last resort, since an item left over budget
    would make the whole spec fail validation and the card render
    nothing at all."""
    for _ in range(len(blocks)):
        over = _blocks_len(blocks) - budget
        if over <= 0:
            return blocks
        target = max((b for b in blocks
                      if b["type"] in ("heading", "text", "quote") and not b.get("provider_states")),
                     key=lambda b: len(b.get("text") or ""),
                     default=None)
        if target is None or len(target["text"]) <= 120:
            break
        keep = len(target["text"]) - over - 40
        target["text"] = _cap_card_text(target["text"], keep)
    for b in blocks:
        over = _blocks_len(blocks) - budget
        if over <= 0:
            break
        if b["type"] == "field" and len(b.get("value") or "") > 120:
            keep = len(b["value"]) - over - 40
            b["value"] = _cap_card_text(b["value"], keep)
    return blocks


def _pack_pages(lengths, max_count, budget=PAGE_TEXT_BUDGET) -> list:
    """Group item indices into pages whose summed rendered length fits
    the budget (and count cap) — an item alone over budget still takes
    its own page; its emitted text is already capped."""
    pages, cur, used = [], [], 0
    for i, n in enumerate(lengths):
        if cur and (used + n > budget or len(cur) >= max_count):
            pages.append(cur)
            cur, used = [], 0
        cur.append(i)
        used += n
    if cur:
        pages.append(cur)
    return pages or [[]]


def _footer_len(footer) -> int:
    """Count every footer line, including the transport's subtext prefix."""
    return sum(len(line) + 4 for item in footer if item["type"] == "text"
               for line in item["text"].splitlines())


_PAGE_RESERVE = 20


def _add_page_marker(containers, page, pages) -> None:
    """Page position on the card's context line (② under the heading)."""
    mark = f"{page + 1}/{pages}ページ"
    if len(containers) > 1 and containers[1]["type"] == "text" \
            and not containers[1].get("rule") \
            and not containers[1]["text"].startswith("🆕"):
        containers[1] = {**containers[1],
                         "text": containers[1]["text"] + " · " + mark}
    else:
        containers.insert(1, {"type": "text", "text": mark})


SECTION_RULE = "─" * 12


MENTION_TOKEN_PATTERN = re.compile(r"<@([^>\n]+)>")
