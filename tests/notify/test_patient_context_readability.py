"""Patient-context views: rows survive the real scope filter and read compactly (all synthetic)."""
from datetime import datetime

import pytest

from adapters.common.text import list_messages
from notify_views import CONTEXT_PAGE_SIZE, CONTEXT_SECTIONS, patient_context_list
from patient_context import extract_context
from test_patient_context_v3_views import DAY, T0, ctx  # noqa: F401 - shared synthetic fixture
from test_read_model import _artifact, _hash_of
from semantic_testkit import _message

__all__ = ["ctx"]   # the shared fixture is used by name below
NOW = T0 + 30 * DAY


def _view(ctx, section, **kw):
    return patient_context_list(ctx.db.db, 1, section, cfg={}, since=T0, now=NOW, **kw)


def _shown(view, allowed):
    return list_messages({"outcome": "applied", "action": "list", "list": view}, allowed, False, plain=True)


@pytest.mark.parametrize("section", list(CONTEXT_SECTIONS))
def test_every_section_keeps_its_rows_under_the_real_scope_filter(ctx, section):
    # 1.0.25 regression: rows had no project_id, so project_allowed dropped every one
    view = _view(ctx, section)
    assert all(row["project_id"] == 1 for row in view["items"])
    in_scope = "\n".join(_shown(view, lambda pid: pid == 1))
    assert all(row["text"] in in_scope for row in view["items"])
    if view["items"]:
        out = "\n".join(_shown(view, lambda pid: pid == 2))
        assert not any(row["text"] in out for row in view["items"]) and view["empty"] in out


def test_background_reads_in_fixed_order_without_repeating_the_heading(ctx):
    view = _view(ctx, "background")
    groups = list(dict.fromkeys(row["group"] for row in view["items"]))
    assert groups.index("主病名") < groups.index("服薬管理")       # 病名 first, whatever was posted last
    diagnoses = [q for row in view["items"] if row["group"] == "主病名" for q in row["quotes"]]
    assert "架空病B" in diagnoses and not any(q.startswith("病名") for q in diagnoses)


def test_one_post_in_one_group_shares_a_single_source_line(ctx):
    view = _view(ctx, "background")
    pairs = [(row["group"], row["source"]) for row in view["items"]]
    assert all(a != b for a, b in zip(pairs, pairs[1:]))
    for row in view["items"]:
        # the short source rides at the end of the last quote line, never on a line of its own
        assert row["text"].splitlines()[-1].endswith(f"（{row['source']}）")
        assert len(row["text"].splitlines()) == max(1, len(row["quotes"]))
        assert "T" not in row["source"]                           # no raw ISO timestamps anywhere


def test_the_source_date_links_to_the_patients_mcs_room(ctx):
    from adapters.common.text import list_messages as render
    from mcs_contract import project_url
    rows = [r for r in _view(ctx, "timeline")["items"] if r.get("url")]
    assert rows and all(r["url"] == project_url(1) for r in rows)
    discord = "\n".join(render({"outcome": "applied", "action": "list", "list": _view(ctx, "timeline")},
                               lambda pid: pid == 1, True))
    assert f"]({'<'}{project_url(1)}{'>'})" in discord                   # masked link, no embed preview
    plain = "\n".join(_shown(_view(ctx, "timeline"), lambda pid: pid == 1))
    assert project_url(1) not in plain                                   # LINE WORKS / plain keep it short


def test_long_views_name_their_page_and_participants_head_the_view(ctx):
    extra = {mid: f"病名: 架空病{mid}" for mid in range(20, 32)}
    ctx.db.save_messages([_message(mid, body=body) for mid, body in extra.items()], project_id=1)
    for mid in extra:
        body = ctx.db.db.execute("SELECT body_text FROM messages WHERE message_id=?", (mid,)).fetchone()[0]
        _artifact(ctx.db, "extract_v1", mid, {"patient_context": extract_context(body)}, {"hash": _hash_of(ctx.db, mid)})
    first, second = _view(ctx, "background"), _view(ctx, "background", page=1)
    assert first["pages"] > 1 and first["title"].endswith(f"（1/{first['pages']}ページ）")
    assert second["title"].endswith(f"（2/{first['pages']}ページ）")
    assert len(first["items"]) == CONTEXT_PAGE_SIZE
    assert any(h.startswith("関係者: ") for h in first["head"]) and "表示順: 標準" in "\n".join(first["head"])


def test_dates_drop_the_year_only_within_the_current_year(ctx):
    rows = _view(ctx, "timeline")["items"]
    same_year = datetime.fromtimestamp(T0 + DAY).year == datetime.fromtimestamp(NOW).year
    assert any(("/" in r["source"].split(" ")[0]) for r in rows)
    assert all(r["source"][:4].isdigit() != same_year for r in rows if r["source"][:1].isdigit())
