"""display_text と _text_cost が統合前の実装と同じ値を返すことを完全合成の表示モデルで確かめる。"""
import re

import notify_render


def _old_display_text(parts):
    lines = []
    for item in parts["containers"]:
        kind = item["type"]
        if kind == "meta":
            continue
        if item.get("rule") and lines:
            lines.append(notify_render.SECTION_RULE)
        value = (f"{item['name']}: {item['value']}"
                 if kind == "field" else item["text"])
        lines.append(f"引用: {value}" if kind == "quote" else value)
    footer = [item["text"] for item in parts.get("footer") or []
              if item["type"] == "text"]
    if footer and lines:
        lines.append(notify_render.SECTION_RULE)
    lines.extend(footer)
    return re.sub(r"<@[^>\n]+>", "メンバー", "\n".join(lines))


def _old_text_cost(parts):
    cost = 0
    for c in parts["containers"]:
        if c["type"] == "quote":
            cost += sum(len(ln) + 3 for ln in c["text"].splitlines())
        elif c["type"] == "field":
            cost += len(c["name"]) + len(c["value"]) + 6
        elif c["type"] != "meta":
            cost += len(c["text"]) + 4
    cost += sum(len(ln) + 4 for c in parts.get("footer") or []
                if c["type"] == "text" for ln in c["text"].splitlines())
    return max(cost, len(_old_display_text(parts)))


CASES = [
    {"containers": [], "footer": []},
    {"containers": [{"type": "heading", "text": "合成見出し"},
                    {"type": "meta", "text": "corr"},
                    {"type": "text", "text": "一行目\n二行目 <@U1> 宛"},
                    {"type": "quote", "text": "引用一\n引用二", "rule": True},
                    {"type": "field", "name": "項目", "value": "値"},
                    {"type": "text", "text": "・a\n・b", "rule": True, "fold": True}],
     "footer": [{"type": "text", "text": "注記一\n注記二"}, {"type": "meta", "text": "x"}]},
    {"containers": [{"type": "text", "text": "本文のみ"}],
     "footer": [{"type": "text", "text": "フッター"}]},
    {"containers": [{"type": "meta", "text": "m"}],
     "footer": [{"type": "text", "text": "フッターのみ"}]},
]


def test_display_text_and_cost_match_the_previous_implementation():
    for parts in CASES:
        assert notify_render.display_text(parts) == _old_display_text(parts)
        assert notify_render._text_cost(parts) == _old_text_cost(parts)
