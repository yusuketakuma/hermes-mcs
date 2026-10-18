"""Direct contracts of the per-transport presentation modules
(present_slack / present_discord / present_lineworks): rendering
dialects, card behaviour flags, links, owner labels and the LINE WORKS
1000-character split. Synthetic display models only."""
import pytest

import present_discord
import present_lineworks
import present_slack
from notify_render import SECTION_RULE

PARTS = {"containers": [{"type": "heading", "text": "見出し"},
                        {"type": "meta", "text": "非表示"},
                        {"type": "field", "name": "担当", "value": 3},
                        {"type": "quote", "text": "引用本文", "rule": True},
                        {"type": "text", "text": "本文 <@U1> 宛"}],
         "footer": [{"type": "text", "text": "注記1\n注記2"},
                    {"type": "button", "text": "無視"}]}


# --- rendering dialects -------------------------------------------------

def test_slack_text_bolds_heading_and_keeps_body_unformatted():
    assert present_slack.parts_text(PARTS).split("\n") == [
        "*見出し*", "担当: 3", SECTION_RULE, "引用: 引用本文",
        "本文 メンバー 宛", SECTION_RULE, "注記1", "注記2"]


def test_discord_text_uses_markdown_heading_and_subtext_footer():
    assert present_discord.parts_text(PARTS).split("\n") == [
        "## 見出し", "担当: 3", SECTION_RULE, "引用: 引用本文",
        "本文 メンバー 宛", SECTION_RULE, "-# 注記1", "-# 注記2"]


def test_lineworks_text_uses_bracket_heading_without_markup():
    assert present_lineworks.parts_text(PARTS).split("\n") == [
        "【見出し】", "担当: 3", SECTION_RULE, "引用: 引用本文",
        "本文 メンバー 宛", SECTION_RULE, "注記1", "注記2"]


def test_discord_staff_text_cannot_render_as_markup():
    parts = {"containers": [{"type": "heading", "text": "*強調* [x](y)"},
                            {"type": "text", "text": "# 見出し\n> 引用\n1. 番号"}]}
    out = present_discord.parts_text(parts)
    assert out.split("\n") == ["## ＊強調＊ ［x］(y)", "＃ 見出し",
                               "＞ 引用", "1． 番号"]


@pytest.mark.parametrize("text", [
    "*a* _b_ ~c~ |d| `e` [f] <g \\h",
    "  - 箇条\n+ 追加\n> 引用\n# 題\n12. 項目",
    "",
])
def test_discord_literal_keeps_length(text):
    assert len(present_discord.literal(text)) == len(text)


def test_discord_literal_keeps_mentions_and_urls_verbatim():
    text = "<@123> と <@&45> 参照 https://example.invalid/a_b*c 終わり*"
    out = present_discord.literal(text)
    assert out == "<@123> と <@&45> 参照 https://example.invalid/a_b*c 終わり＊"


def test_discord_literal_only_escapes_line_leading_markers():
    assert present_discord.literal("a-b > c # d") == "a-b > c # d"
    assert present_discord.literal("\t- x") == "\t－ x"


# --- card behaviour flags -----------------------------------------------

@pytest.mark.parametrize("present,expected", [
    (present_slack, dict(EDITABLE=True, ALWAYS_THREAD=False, MORE_BUTTON=False,
                         DRUG_ROW_NEEDS_THREAD=True, SOURCE_THREAD=True,
                         THREAD_DRUG_ACTIONS=False, CAPTION="header",
                         THREAD_HEAD_PATIENT=False, POST_ACTIONS=True,
                         ACCENT_URGENT=False)),
    (present_discord, dict(EDITABLE=True, ALWAYS_THREAD=False, MORE_BUTTON=False,
                           DRUG_ROW_NEEDS_THREAD=False, SOURCE_THREAD=True,
                           THREAD_DRUG_ACTIONS=True, CAPTION="header",
                           THREAD_HEAD_PATIENT=False, POST_ACTIONS=True,
                           ACCENT_URGENT=True)),
    (present_lineworks, dict(EDITABLE=False, ALWAYS_THREAD=True, MORE_BUTTON=True,
                             DRUG_ROW_NEEDS_THREAD=False, SOURCE_THREAD=False,
                             THREAD_DRUG_ACTIONS=False, CAPTION="preview",
                             THREAD_HEAD_PATIENT=True, POST_ACTIONS=False,
                             ACCENT_URGENT=False)),
], ids=["slack", "discord", "lineworks"])
def test_card_behaviour_flags(present, expected):
    assert {name: getattr(present, name) for name in expected} == expected


# --- face / links / owner labels ----------------------------------------

@pytest.mark.parametrize("present", [present_slack, present_discord])
@pytest.mark.parametrize("op", ["send", "update"])
def test_editable_transports_pass_face_through(present, op):
    containers = [{"type": "text", "text": "🆕 変更"}]
    footer = [{"type": "text", "text": "<@U1>"}]
    assert present.face(containers, footer, op, {}) == (containers, footer)


def test_lineworks_face_names_members_and_keeps_other_footer_items():
    cfg = {"notify": {"lineworks": {"user_names": {"u1": "  合成 太郎  "}}}}
    footer = [{"type": "text", "text": "担当 <@u1> / <@u2>"},
              {"type": "button", "text": "<@u1>"}]
    containers = [{"type": "heading", "text": "見出し"}]
    out_containers, out_footer = present_lineworks.face(
        containers, footer, "send", cfg)
    assert out_containers == containers
    assert out_footer == [{"type": "text", "text": "担当 合成 太郎 / メンバー"},
                          {"type": "button", "text": "<@u1>"}]
    assert footer[0]["text"] == "担当 <@u1> / <@u2>"       # input untouched


def test_lineworks_update_is_marked_as_reposted():
    containers = [{"type": "heading", "text": "見出し"},
                  {"type": "text", "text": "🆕 新しい投稿"}]
    out, _ = present_lineworks.face(containers, [], "update", {})
    assert out[1]["text"] == "🆕 新しい投稿 · 🔄 更新版"
    assert containers[1]["text"] == "🆕 新しい投稿"
    out, _ = present_lineworks.face(
        [{"type": "heading", "text": "見出し"},
         {"type": "text", "text": "本文", "rule": True}], [], "update", None)
    assert [c["text"] for c in out] == ["見出し", "🔄 更新版", "本文"]


def test_lineworks_face_tolerates_missing_or_malformed_config():
    footer = [{"type": "text", "text": "<@u1>"}]
    for cfg in (None, {}, {"notify": "x"}, {"notify": {"lineworks": {"user_names": []}}}):
        assert present_lineworks.face([], footer, "send", cfg)[1] == [
            {"type": "text", "text": "メンバー"}]


def test_card_links():
    card = {"message_id": "m1", "channel_id": "c1", "guild_id": "g1"}
    assert present_discord.card_link(card) == \
        "https://discord.com/channels/g1/c1/m1"
    assert present_discord.card_link(dict(card, guild_id=None)) == \
        "https://discord.com/channels/@me/c1/m1"
    assert present_discord.card_link(dict(card, message_id=None)) is None
    assert present_discord.card_link(dict(card, channel_id="")) is None
    assert present_slack.card_link(card) is None
    assert present_lineworks.card_link(card) is None


def test_owner_labels():
    names = {"u1": "合成 花子"}
    # Slack private answers are escaped plain text: a name, never a literal <@id>
    assert present_slack.owner_label("<@u1> と <@u9>", names) == "合成 花子 と メンバー"
    assert present_discord.owner_label("<@u1>", names) == "<@u1>"
    assert present_lineworks.owner_label("<@u1> と <@u9>", names) == \
        "合成 花子 と メンバー"
    assert present_lineworks.owner_label("<@u1>", None) == "メンバー"


# --- LINE WORKS card split / overflow -----------------------------------

def test_lineworks_card_split_boundaries():
    limit, more = present_lineworks.CARD_LIMIT, present_lineworks.MORE
    exact = "あ" * limit
    assert present_lineworks.card_split(exact) == (exact, "")
    room = limit - len(more)
    text = "a" * room + "\n" + "b" * 10      # break exactly at the room edge
    head, rest = present_lineworks.card_split(text)
    assert head == "a" * room + more and rest == "b" * 10
    assert len(head) == limit


def test_lineworks_card_split_hard_cuts_a_single_overlong_line():
    room = present_lineworks.CARD_LIMIT - len(present_lineworks.MORE)
    text = "x" * (present_lineworks.CARD_LIMIT + 50)
    head, rest = present_lineworks.card_split(text)
    assert head == "x" * room + present_lineworks.MORE
    assert rest == text[room:]
    # a break only at position 0 is not usable either
    head, rest = present_lineworks.card_split("\n" + text)
    assert head.endswith(present_lineworks.MORE) and len(head) == \
        present_lineworks.CARD_LIMIT


def test_lineworks_card_split_never_loses_text():
    lines = [f"行{i:03d} " + "本文" * 20 for i in range(60)]
    text = "\n".join(lines)
    head, rest = present_lineworks.card_split(text)
    assert len(head) <= present_lineworks.CARD_LIMIT
    assert head.removesuffix(present_lineworks.MORE) + "\n" + rest == text


def test_lineworks_overflow_names_chunks_of_the_remainder():
    body = "\n".join(f"行{i:03d} " + "本文" * 20 for i in range(60))
    parts = {"containers": [{"type": "text", "text": body}], "footer": []}
    seen = []

    def split_chunks(rest):
        seen.append(rest)
        return [rest[:100], rest[100:]]

    out = present_lineworks.overflow(parts, split_chunks)
    rest = present_lineworks.card_split(body)[1]
    assert seen == [rest]
    assert out == [("display#1", rest[:100]), ("display#2", rest[100:])]


def test_lineworks_overflow_is_empty_for_a_short_card():
    parts = {"containers": [{"type": "text", "text": "短い"}], "footer": []}
    assert present_lineworks.overflow(parts, lambda rest: [rest] if rest else []) == []


def test_lineworks_card_split_keeps_whole_posts():
    from notify_render import SECTION_RULE
    post = "09-24 08:4x 合成 職員（看護師）\n・" + "合成要約。" * 60
    text = f"\n{SECTION_RULE}\n".join([post] * 4)
    head, rest = present_lineworks.card_split(text)
    assert head.endswith(present_lineworks.MORE) and len(head) <= present_lineworks.CARD_LIMIT
    assert head[:-len(present_lineworks.MORE)].count("合成 職員") == \
        head[:-len(present_lineworks.MORE)].count("・")       # no post cut in half
    assert rest.startswith("09-24") and SECTION_RULE not in head[-30:]
