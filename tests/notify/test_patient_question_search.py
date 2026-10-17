"""Patient search answers bounded questions with grounded posts, never a guessed answer (synthetic)."""
import pytest

import notify_views
from notify_testkit import _msg, _patient, led as led  # noqa: F401


@pytest.fixture
def posts(led):
    _patient(led)
    _msg(led, 101, body="残薬は7日分、本人管理。", prof="薬剤師")
    _msg(led, 102, body="食事量は半分程度に減少。", prof="看護師")
    _msg(led, 103, body="残薬なし、カレンダーで管理。", prof="薬剤師")
    _msg(led, 104, body="食事量は全量摂取。", prof="看護師")
    return led


@pytest.mark.parametrize("question, term", [
    ("最後に残薬に触れた投稿は？", "残薬"),
    ("最後に残薬について報告されたのはいつ？", "残薬"),
    ("食事量の変化は？", "食事量"),
    ("残薬に関する記録", "残薬"),
    ("直近の食事量は?", "食事量"),
    ("最後の残薬はいつ？", "残薬"),
])
def test_question_is_mapped_to_a_grounded_term_search(posts, question, term):
    view = notify_views.patient_search_view(posts.db, 1, question)
    assert f"「{term}」" in view["head"][0]
    assert view["items"] and all(term in i["text"] for i in view["items"])
    newest = view["items"][0]["text"]
    assert "投稿#10" in newest and "09-24" in newest  # source post, date and quote per row


def test_latest_question_puts_the_newest_matching_post_first(posts):
    view = notify_views.patient_search_view(posts.db, 1, "最後に残薬について報告されたのはいつ？")
    assert "投稿#103" in view["items"][0]["text"] and "残薬なし" in view["items"][0]["text"]


def test_unreadable_question_falls_back_to_the_literal_search_without_guessing(posts):
    view = notify_views.patient_search_view(posts.db, 1, "どうですか？")
    assert view["items"] == []
    assert any("検索語" in line for line in view["head"])


def test_plain_terms_keep_the_existing_rule(posts):
    view = notify_views.patient_search_view(posts.db, 1, "残薬 管理")
    assert [i["text"].count("残薬") for i in view["items"]] == [1, 1]
    assert view["notes"]  # the acquisition range line stays
