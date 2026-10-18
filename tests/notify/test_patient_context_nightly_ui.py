"""Private patient navigation and timestamp-only checkpoints over synthetic fixtures."""
import json

import pytest

import notify_cards
import notify_views
from notify_testkit import CFG, NOW, ORIGIN, _delivered_card, _token_for, led, pinned_clock

__all__ = ["led", "pinned_clock"]
pytestmark = pytest.mark.usefixtures("pinned_clock")
ENABLED = {**CFG, "extract": {"patient_context_nightly": {"enabled": True, "publication": True}}}


def click(led, token, *, actor="discord:1001", now=NOW, cfg=ENABLED):
    return notify_cards.apply_notification(led, {
        "command_id": f"{token}:{now}:{actor}", "actor": actor, "token": token,
        "origin": dict(ORIGIN, message_id="m-9")}, cfg, now=now)


def open_section(led, spec, *, now=NOW, cfg=ENABLED, entry=0):
    """The card's 🧾 shows the existing summary; its entry buttons open the private views."""
    summary = click(led, _token_for(spec, "summary"), now=now, cfg=cfg)
    assert summary["action"] == "summary" and summary["body"]
    return click(led, summary["navigation"][entry]["token"], now=now, cfg=cfg)


def test_explicitly_disabled_summary_unchanged(led):
    spec = _delivered_card(led)
    cfg = {**CFG, "extract": {"patient_context_nightly": {"enabled": False}}}
    result = click(led, _token_for(spec, "summary"), cfg=cfg)
    assert result["action"] == "summary"
    assert "navigation" not in result
    assert not led.db.execute("SELECT 1 FROM artifacts WHERE kind='patient_context_view_checkpoint_v1'").fetchone()


def test_all_sections_reachable_and_actor_bound(led):
    spec = _delivered_card(led)
    token = click(led, _token_for(spec, "summary"))["navigation"][0]["token"]
    sections = list(notify_views.CONTEXT_SECTIONS)
    assert len(sections) == 8
    for index, section in enumerate(sections):
        result = click(led, token, now=NOW + index)
        assert result["action"] == "list"
        assert notify_views.CONTEXT_SECTIONS[section] in result["list"]["title"]
        assert len(result["navigation"]) <= 5
        token = result["navigation"][1]["token"]
        assert click(led, token, actor="discord:2002", now=NOW + index)["error"] == "actor_mismatch"
    assert notify_views.CONTEXT_SECTIONS[sections[0]] in click(
        led, token, now=NOW + len(sections))["list"]["title"]
    row = led.db.execute("SELECT content,meta FROM artifacts WHERE kind='patient_context_view_checkpoint_v1'").fetchone()
    assert json.loads(row["content"])["at"] == NOW and set(json.loads(row["content"])) <= {"at", "since", "command_id"}
    assert "discord:1001" not in row["meta"]
    receipts = led.db.execute("SELECT receipt_json FROM command_receipts").fetchall()
    assert receipts and all("navigation" not in json.loads(row[0]) and "list" not in json.loads(row[0])
                            for row in receipts)


def test_previous_time_preserved_in_navigation_and_per_actor(led):
    spec = _delivered_card(led)
    token = _token_for(spec, "summary")
    click(led, token, now=NOW)
    second = click(led, token, now=NOW + 10)
    changes_token = second["navigation"][1]["token"]
    params = json.loads(led.db.execute("SELECT params FROM notification_action_tokens WHERE token=?", (changes_token,)).fetchone()[0])
    assert params["since"] == NOW
    changes = click(led, changes_token, now=NOW + 20)
    assert "初回" not in str(changes["list"]["head"])
    other = click(led, token, actor="discord:2002", now=NOW + 30)
    params = json.loads(led.db.execute("SELECT params FROM notification_action_tokens WHERE token=?", (other["navigation"][1]["token"],)).fetchone()[0])
    assert params["since"] == 0


def test_role_cycle_and_search_reuse_existing_modal(led):
    spec = _delivered_card(led)
    result = open_section(led, spec)
    role = click(led, result["navigation"][2]["token"], now=NOW + 1)
    assert "表示順: 看護" in role["list"]["head"]
    changes = click(led, role["navigation"][1]["token"], now=NOW + 2)
    search = click(led, changes["navigation"][2]["token"], now=NOW + 3)
    assert search["action"] == "search" and search["modal"]


def test_all_ten_profession_orders_cycle_back_to_standard(led):
    spec = _delivered_card(led)
    result = open_section(led, spec)
    roles = list(notify_views.CONTEXT_ROLES)
    assert len(roles) == 11
    for index, role in enumerate(roles):
        assert f"表示順: {notify_views.CONTEXT_ROLES[role]}" in result["list"]["head"]
        button = next(b for b in result["navigation"] if b["label"].startswith("表示順:"))
        result = click(led, button["token"], now=NOW + index + 1)
    assert "表示順: 標準" in result["list"]["head"]


def test_shadow_does_not_expose_navigation(led):
    spec = _delivered_card(led)
    cfg = {**ENABLED, "extract": {"patient_context_nightly": {"enabled": True, "publication": False}}}
    result = click(led, _token_for(spec, "summary"), cfg=cfg)
    assert result["action"] == "summary"
    assert "navigation" not in result


def test_pagination_preserves_quotes_and_sources(led, monkeypatch):
    import patient_context_view
    spec = _delivered_card(led)
    original = patient_context_view.evidence
    def evidence(db, pid, **kw):
        ev = original(db, pid, **kw)
        ev["items"] = [{"category": "living", "text": f"合成の生活情報{i}",
                        "quote": f"家族の予定{i}です。確定していません。",
                        "message_id": i, "posted_at_ts": NOW, "sender": "合成職員",
                        "source": "chat"} for i in range(10)]
        return ev
    monkeypatch.setattr(patient_context_view, "evidence", evidence)
    result = open_section(led, spec)
    assert len(result["list"]["items"]) == 8
    assert "確定していません。" in result["list"]["items"][0]["text"]
    assert "合成職員" in result["list"]["items"][0]["text"]
    page = next(b for b in result["navigation"] if b["label"] == "次の8件")
    second = click(led, page["token"], now=NOW + 1)
    # the two overflow records, then the poster list that closes the background section
    assert len(second["list"]["items"]) == 3
    assert "#9）" in second["list"]["items"][1]["text"]
    assert second["list"]["items"][2]["group"] == "関係者・事業者（投稿者から取得）"


@pytest.mark.parametrize("change", [{"section": []}, {"role": {}}, {"page": -1}, {"page": True}, {"since": float("inf")}])
def test_invalid_navigation_rejected(led, change):
    spec = _delivered_card(led)
    result = click(led, _token_for(spec, "summary"))
    token = result["navigation"][1]["token"]
    row = led.db.execute("SELECT params FROM notification_action_tokens WHERE token=?", (token,)).fetchone()
    params = {**json.loads(row[0]), **change}
    led.db.execute("UPDATE notification_action_tokens SET params=? WHERE token=?", (json.dumps(params), token))
    led.db.commit()
    assert click(led, token, now=NOW + 1)["error"] == "action_not_applicable"


def test_replayed_summary_does_not_advance_checkpoint(led):
    spec = _delivered_card(led)
    token = _token_for(spec, "summary")
    req = {"command_id": "same-view", "actor": "discord:1001", "token": token,
           "origin": dict(ORIGIN, message_id="m-9")}
    first = notify_cards.apply_notification(led, req, ENABLED, now=NOW)
    second = notify_cards.apply_notification(led, req, ENABLED, now=NOW + 30)
    assert first["context_since"] == second["context_since"] == 0
    row = led.db.execute("SELECT content FROM artifacts WHERE kind='patient_context_view_checkpoint_v1'").fetchone()
    assert json.loads(row[0])["at"] == NOW


@pytest.mark.parametrize("later", [3601, 2 * 86400])
def test_resend_of_a_later_reopening_keeps_that_openings_baseline(led, later):
    # NA2: the same 🧾 command reopened after the live window, then resent 30s later,
    # must show the same 前回から baseline and must not advance the checkpoint again
    spec = _delivered_card(led)
    req = {"command_id": "same-view", "actor": "discord:1001", "token": _token_for(spec, "summary"),
           "origin": dict(ORIGIN, message_id="m-9")}
    first = notify_cards.apply_notification(led, req, ENABLED, now=NOW)
    reopened = notify_cards.apply_notification(led, req, ENABLED, now=NOW + later)
    resent = notify_cards.apply_notification(led, req, ENABLED, now=NOW + later + 30)
    assert first["context_since"] == 0 and reopened["context_since"] == NOW
    assert resent["context_since"] == reopened["context_since"]
    row = led.db.execute("SELECT content FROM artifacts WHERE kind='patient_context_view_checkpoint_v1'").fetchone()
    assert json.loads(row[0])["at"] == NOW + later


def test_nightly_summary_uses_fact_id_not_same_text_source(led, monkeypatch):
    import patient_context_view
    spec = _delivered_card(led)
    original = patient_context_view.evidence
    def evidence(db, pid, **kw):
        ev = original(db, pid, **kw)
        common = {"category": "living", "text": "合成の同じ引用", "quote": "合成の同じ引用",
                  "posted_at_ts": NOW, "source": "chat"}
        ev["nightly_facts"] = [{**common, "id": "source1", "message_id": 100, "sender": "合成職員A"},
                               {**common, "id": "source2", "message_id": 101, "sender": "合成職員B"}]
        ev["summary"] = [{"text": "合成の同じ引用", "fact_ids": ["source2"]}]
        return ev
    monkeypatch.setattr(patient_context_view, "evidence", evidence)
    result = open_section(led, spec)
    row = result["list"]["items"][0]
    assert row["group"] == "夜間に整理した記録"
    assert "#101）" in row["text"] and "合成職員B" in row["text"]
    assert "#100）" not in row["text"]


def test_omitted_options_enable_private_summary_navigation(led):
    spec = _delivered_card(led)
    result = click(led, _token_for(spec, "summary"), cfg=CFG)
    assert result["action"] == "summary" and result["body"]
    assert result["navigation"]


def test_first_click_keeps_existing_summary_text(led):
    spec = _delivered_card(led)
    result = click(led, _token_for(spec, "summary"))
    pid = led.db.execute("SELECT project_id FROM notification_cards").fetchone()[0]
    title, body = notify_views.patient_summary_text(led.db, pid, cfg=ENABLED)
    assert (result["title"], result["body"]) == (title, body)
    assert [b["label"] for b in result["navigation"]] == [
        notify_views.CONTEXT_SECTIONS[k] for k in (
            "background", "changes", "previsit", "replies", "discrepancies")]


def test_broken_nightly_view_falls_back_to_existing_summary(led, monkeypatch):
    import patient_context_view
    spec = _delivered_card(led)
    entry = click(led, _token_for(spec, "summary"))["navigation"][0]["token"]
    def broken(*a, **k):
        raise RuntimeError("synthetic nightly failure")
    monkeypatch.setattr(patient_context_view, "evidence", broken)
    result = click(led, entry, now=NOW + 1)
    assert result["action"] == "summary" and result["body"]
    assert result["body"].startswith("患者の記録まとめを読み込めなかったため")
    assert "synthetic nightly failure" not in result["body"]


def test_later_click_by_same_user_on_same_card_advances_checkpoint(led):
    from adapters.common import envelopes
    spec = _delivered_card(led)
    token = _token_for(spec, "summary")
    actor = "discord:1001"
    # adapters derive the command id from token and actor only, so repeat clicks share it
    req = {"command_id": f"{token}:{envelopes.actor_hash(actor)}", "actor": actor, "token": token,
           "origin": dict(ORIGIN, message_id="m-9")}
    first = notify_cards.apply_notification(led, req, ENABLED, now=NOW)
    later = notify_cards.apply_notification(led, req, ENABLED, now=NOW + 2 * 86400)
    assert first["context_since"] == 0 and later["context_since"] == NOW
    row = led.db.execute("SELECT content FROM artifacts WHERE kind='patient_context_view_checkpoint_v1'").fetchone()
    assert json.loads(row[0])["at"] == NOW + 2 * 86400
