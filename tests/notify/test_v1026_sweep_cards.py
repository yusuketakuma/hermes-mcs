"""v1.0.26 sweep: Slack owner labels stay distinct; thread titles never borrow the card date."""
import pytest

import notify_cards
import notify_views
import present_slack
from notify_testkit import NOW, _delivered_card, _patient, led, pinned_clock  # noqa: F401

__all__ = ["led", "pinned_clock"]


# cards-B2: LINE WORKS user_names never hold Slack ids, so two owners must not collapse to メンバー.
def test_slack_owner_labels_keep_owners_distinct():
    names = {"someone@example.com": "合成 太郎"}
    assert present_slack.owner_label("<@U0ABC>", names) == "U0ABC"
    assert present_slack.owner_label("<@U0XYZ>", names) == "U0XYZ"


@pytest.mark.usefixtures("pinned_clock")
def test_slack_unacked_owner_shows_the_id_without_mention_syntax(led):
    _delivered_card(led)
    led.db.execute("UPDATE notification_cards SET transport='slack'")
    led.db.execute("INSERT INTO notification_triage"
                   "(card_id,owner,state,last_actor,updated_at) VALUES"
                   "(1,'slack:T0SYN:U0ABC','assigned','a',?)", (NOW,))
    led.db.commit()
    text = notify_views.unacked_view(
        led.db, "slack", NOW, [1],
        member_names={"someone@example.com": "合成 太郎"})["items"][0]["text"]
    assert "（担当中: U0ABC）" in text and "<@" not in text


# cards-B3: a thread card whose root date is unknown shows no date, not the card's creation day.
def test_thread_card_with_unknown_root_date_has_no_date(led):
    _patient(led, 1)
    card = {"kind": "thread", "project_id": 1, "root_message_id": 999,
            "created_at": 1_790_000_000.0}
    assert notify_cards._thread_name(led.db, card) == "💬 患者A"
    led.db.execute("INSERT INTO messages(message_id,project_id,sender_name,posted_at,"
                   "posted_at_ts,body_text,content_hash,body_state) VALUES"
                   "(999,1,'職員','壊れた日時',1,'本文',?,'full')", (f"{999:064x}",))
    assert notify_cards._thread_name(led.db, card) == "💬 患者A"
    # signal cards keep the creation-day fallback
    assert notify_cards._thread_name(led.db, {**card, "kind": "signal"}).endswith("— 09-21")
