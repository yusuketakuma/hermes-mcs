"""Private patient navigation and timestamp-only checkpoints over synthetic fixtures."""
import json

import pytest

import notify_actions
import notify_views
import patient_context_status
from notify_testkit import CFG, NOW, ORIGIN, _delivered_card, _token_for, led, pinned_clock
from semantic_testkit import _message

__all__ = ["led", "pinned_clock"]
pytestmark = pytest.mark.usefixtures("pinned_clock")
ENABLED = {**CFG, "extract": {"patient_context": {"enabled": True, "publication": True}}}


def click(led, token, *, actor="discord:1001", now=NOW, cfg=ENABLED):
    return notify_actions.apply_notification(led, {
        "command_id": f"{token}:{now}:{actor}", "actor": actor, "token": token,
        "origin": dict(ORIGIN, message_id="m-9")}, cfg, now=now)


def open_section(led, spec, *, now=NOW, cfg=ENABLED, entry=0):
    """The card's 🧾 shows the existing summary; its entry buttons open the private views."""
    summary = click(led, _token_for(spec, "summary"), now=now, cfg=cfg)
    assert summary["action"] == "summary" and summary["body"]
    return click(led, summary["navigation"][entry]["token"], now=now, cfg=cfg)


def test_explicitly_disabled_summary_unchanged(led):
    spec = _delivered_card(led)
    cfg = {**CFG, "extract": {"patient_context": {"enabled": False}}}
    result = click(led, _token_for(spec, "summary"), cfg=cfg)
    assert result["action"] == "summary"
    assert "navigation" not in result
    assert not led.db.execute("SELECT 1 FROM artifacts WHERE kind='patient_context_view_checkpoint_v1'").fetchone()


def test_all_sections_reachable_and_actor_bound(led):
    spec = _delivered_card(led)
    token = click(led, _token_for(spec, "summary"))["navigation"][0]["token"]
    sections = [section for section in notify_views.CONTEXT_SECTIONS if section != "measurements"]
    assert len(sections) == 10
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
    assert json.loads(row["content"])["at"] == NOW and set(json.loads(row["content"])) <= {"at", "since", "command_id", "first_view"}
    assert "discord:1001" not in row["meta"]
    receipts = led.db.execute("SELECT receipt_json FROM command_receipts").fetchall()
    assert receipts and all("navigation" not in json.loads(row[0]) and "list" not in json.loads(row[0])
                            for row in receipts)


@pytest.mark.parametrize("state,expected", [
    ("unacquired", "原文の取得完了を未確認"),
    ("acquisition_incomplete", "履歴の取得は未完了"),
    ("dependency_gap", "変更前の根拠が不足しています"),
    ("summary_pending", "患者コンテキストの更新待ち"),
    ("audit_pending", "原文の根拠照合待ち"),
    ("quality_unknown", "要約の照合結果を確認できません"),
    ("quality_held", "要約の照合を保留しています"),
])
def test_context_waiting_reason_survives_each_transport_without_pending_messages(led, monkeypatch, state, expected):
    import patient_context_view
    from adapters.common.text import list_messages
    from adapters.discord.views import context_texts
    from adapters.slack.presentation import context_list_blocks

    _delivered_card(led)
    monkeypatch.setattr(patient_context_view, "evidence", lambda *a, **k: {
        "project_id": 1, "items": [], "summary": [], "context_facts": [], "as_of": None,
        "coverage": {"context": None, "inference": {"complete": False, "status": state,
                                                      "sources": {}}}})
    view = notify_views.patient_context_list(led.db, 1, "background", cfg=ENABLED, since=0, now=NOW)
    assert expected in view["notes"]
    assert "未解析 0件 / 保留 0件 / 解析失敗 0件 / 本文不完全 0件" in "\n".join(view["notes"])
    result = {"outcome": "applied", "action": "list", "projects": [1], "list": view}
    def allowed(pid):
        return pid == 1
    assert expected in json.dumps(context_list_blocks(view, allowed), ensure_ascii=False)
    assert expected in "\n".join(context_texts(result, allowed))
    assert expected in "\n".join(list_messages(result, allowed, plain=True))


def _inference_notes(led, monkeypatch, inference):
    import patient_context_view
    monkeypatch.setattr(patient_context_view, "evidence", lambda *a, **k: {
        "project_id": 1, "items": [], "summary": [], "context_facts": [], "as_of": None,
        "coverage": {"context": None, "inference": {"complete": False, "sources": {}, **inference}}})
    return notify_views.patient_context_list(led.db, 1, "background", cfg=ENABLED, since=0, now=NOW)["notes"]


@pytest.mark.parametrize("state", patient_context_status.COVERAGE_STATES)
def test_every_coverage_state_is_worded_in_the_status_line(led, monkeypatch, state):
    _delivered_card(led)
    notes = _inference_notes(led, monkeypatch, {"status": state})
    assert patient_context_status.STATUS_JA[state] in notes
    assert patient_context_status.UNKNOWN_STATUS_JA not in notes


@pytest.mark.parametrize("state", ["synthetic_future_state", None, 7])
def test_unrecognised_coverage_state_is_shown_not_dropped(led, monkeypatch, state):
    _delivered_card(led)
    notes = _inference_notes(led, monkeypatch, {"status": state})
    assert patient_context_status.UNKNOWN_STATUS_JA in notes


@pytest.mark.parametrize("state", ["held", "failed"])
def test_held_and_failed_wording_makes_no_retry_promise(state):
    text = patient_context_status.STATUS_JA[state]
    assert "自動" not in text and "再試行" not in text and "再開" not in text


def test_dependency_gap_breakdown_only_with_integer_coverage(led, monkeypatch):
    _delivered_card(led)
    notes = _inference_notes(led, monkeypatch, {"status": "dependency_gap", "dependencies": {
        "complete": False, "gaps": 3, "coverage": {"terminal": 1, "repairable": 2, "reasons": {}}}})
    line = next(note for note in notes if note.startswith("根拠不足の内訳"))
    assert "復元できない記録 1件" in line and "原本の再点検待ち 2件" in line
    assert "再点検だけでは解消しません" in line
    repairable_only = _inference_notes(led, monkeypatch, {"status": "dependency_gap", "dependencies": {
        "complete": False, "coverage": {"terminal": 0, "repairable": 2, "reasons": {}}}})
    assert not any("解消しません" in note for note in repairable_only)
    for dependencies in ({"complete": False}, {"complete": False, "coverage": {"terminal": "1", "repairable": 0}},
                         {"complete": False, "coverage": {"terminal": True, "repairable": 0}}):
        notes = _inference_notes(led, monkeypatch, {"status": "dependency_gap", "dependencies": dependencies})
        assert "変更前の根拠が不足しています" in notes
        assert not any(note.startswith("根拠不足の内訳") for note in notes)     # absent is omitted, not 0


def test_previous_time_preserved_in_navigation_and_per_actor(led):
    spec = _delivered_card(led)
    token = _token_for(spec, "summary")
    click(led, token, now=NOW)
    second = click(led, token, now=NOW + 10)
    changes_token = second["navigation"][1]["token"]
    params = json.loads(led.db.execute("SELECT params FROM notification_action_tokens WHERE token=?", (changes_token,)).fetchone()[0])
    assert params["since"] == NOW
    assert params["first_view"] is False
    changes = click(led, changes_token, now=NOW + 20)
    assert "初回" not in str(changes["list"]["head"])
    other = click(led, token, actor="discord:2002", now=NOW + 30)
    params = json.loads(led.db.execute("SELECT params FROM notification_action_tokens WHERE token=?", (other["navigation"][1]["token"],)).fetchone()[0])
    assert params["since"] == NOW + 30 - FIRST
    assert params["first_view"] is True


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
    cfg = {**ENABLED, "extract": {"patient_context": {"enabled": True, "publication": False}}}
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


def test_intervention_history_reaches_older_posts_without_crossing_patient_scope(led):
    spec = _delivered_card(led)
    older = "看護師が2026年11月3日に本人を訪問する予定です。"
    posted_at = '2026-09-19T00:00:00+09:00'
    led.save_messages([_message(1000, body=older, posted_at=posted_at)] +
                      [_message(mid, body=(f"看護師が2026年11月{mid - 1189}日に本人を訪問する予定です。"
                                           if mid >= 1190 else "完全合成の一般連絡です。"),
                               posted_at=posted_at)
                       for mid in range(1001, 1202)],
                      project_id=1)
    led.ensure_patient(2)
    led.save_messages([_message(2000, pid=2, body="医師が2026年11月3日に本人を往診する予定です。",
                                posted_at=posted_at)], project_id=2)
    previsit = open_section(led, spec, entry=2)
    start = next(button for button in previsit["navigation"] if button["label"] == "介入の履歴を見る")
    history = click(led, start["token"], now=NOW + 1)
    assert "ページ間の変更は未照合" in str(history["list"]["head"])
    step = 2
    while any(button["label"] == "次の8件" for button in history["navigation"]):
        assert len(history["navigation"]) <= 5
        assert not any(button["label"] == "さらに古い投稿を確認" for button in history["navigation"])
        next_page = next(button for button in history["navigation"] if button["label"] == "次の8件")
        history = click(led, next_page["token"], now=NOW + step)
        step += 1
    button = next(button for button in history["navigation"] if button["label"] == "さらに古い投稿を確認")
    assert len(history["navigation"]) <= 5
    assert click(led, button["token"], actor="discord:2002", now=NOW + 2)["error"] == "actor_mismatch"
    older_page = click(led, button["token"], now=NOW + step)
    assert older in str(older_page["list"]["items"])
    assert "医師が2026年11月3日に本人を往診する予定です。" not in str(older_page["list"]["items"])
    assert "履歴の取得完了は未確認" in str(older_page["list"]["head"])


def test_intervention_cursor_from_another_patient_is_rejected(led):
    spec = _delivered_card(led)
    previsit = open_section(led, spec, entry=2)
    token = next(button["token"] for button in previsit["navigation"] if button["label"] == "介入の履歴を見る")
    row = led.db.execute("SELECT params FROM notification_action_tokens WHERE token=?", (token,)).fetchone()
    params = json.loads(row[0])
    params["intervention_cursor"] = {"version": 1, "project_id": 2, "message_id": 1,
                                     "offset": 0, "source_fp": None, "consistency": "page"}
    led.db.execute("UPDATE notification_action_tokens SET params=? WHERE token=?", (json.dumps(params), token))
    led.db.commit()
    assert click(led, token, now=NOW + 1)["error"] == "intervention_cursor_changed"


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
    first = notify_actions.apply_notification(led, req, ENABLED, now=NOW)
    second = notify_actions.apply_notification(led, req, ENABLED, now=NOW + 30)
    assert first["context_since"] == second["context_since"] == NOW - FIRST
    row = led.db.execute("SELECT content FROM artifacts WHERE kind='patient_context_view_checkpoint_v1'").fetchone()
    assert json.loads(row[0])["at"] == NOW


@pytest.mark.parametrize("later", [3601, 2 * 86400])
def test_resend_of_a_later_reopening_keeps_that_openings_baseline(led, later):
    # NA2: the same 🧾 command reopened after the live window, then resent 30s later,
    # must show the same 前回から baseline and must not advance the checkpoint again
    spec = _delivered_card(led)
    req = {"command_id": "same-view", "actor": "discord:1001", "token": _token_for(spec, "summary"),
           "origin": dict(ORIGIN, message_id="m-9")}
    first = notify_actions.apply_notification(led, req, ENABLED, now=NOW)
    reopened = notify_actions.apply_notification(led, req, ENABLED, now=NOW + later)
    resent = notify_actions.apply_notification(led, req, ENABLED, now=NOW + later + 30)
    assert first["context_since"] == NOW - FIRST and reopened["context_since"] == NOW
    assert resent["context_since"] == reopened["context_since"]
    row = led.db.execute("SELECT content FROM artifacts WHERE kind='patient_context_view_checkpoint_v1'").fetchone()
    assert json.loads(row[0])["at"] == NOW + later


def test_context_refresh_summary_uses_fact_id_not_same_text_source(led, monkeypatch):
    import patient_context_view
    spec = _delivered_card(led)
    original = patient_context_view.evidence
    def evidence(db, pid, **kw):
        ev = original(db, pid, **kw)
        common = {"category": "living", "text": "合成の同じ引用", "quote": "合成の同じ引用",
                  "posted_at_ts": NOW, "source": "chat"}
        ev["context_facts"] = [{**common, "id": "source1", "message_id": 100, "sender": "合成職員A"},
                               {**common, "id": "source2", "message_id": 101, "sender": "合成職員B"}]
        ev["summary"] = [{"text": "合成の同じ引用", "fact_ids": ["source2"]}]
        return ev
    monkeypatch.setattr(patient_context_view, "evidence", evidence)
    result = open_section(led, spec)
    row = result["list"]["items"][0]
    assert row["group"] == "患者コンテキスト"
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


def test_broken_context_refresh_view_falls_back_to_existing_summary(led, monkeypatch):
    import patient_context_view
    spec = _delivered_card(led)
    entry = click(led, _token_for(spec, "summary"))["navigation"][0]["token"]
    def broken(*a, **k):
        raise RuntimeError("synthetic context refresh failure")
    monkeypatch.setattr(patient_context_view, "evidence", broken)
    result = click(led, entry, now=NOW + 1)
    assert result["action"] == "summary" and result["body"]
    assert result["body"].startswith("患者の記録まとめを読み込めなかったため")
    assert "synthetic context refresh failure" not in result["body"]


def _fallback_events(capsys):
    events = []
    for line in capsys.readouterr().err.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("event") == "patient_context_view_fallback":
            events.append(event)
    return events


def test_context_fallback_logs_only_the_exception_class(led, monkeypatch, capsys):
    import patient_context_view
    spec = _delivered_card(led)
    entry = click(led, _token_for(spec, "summary"))["navigation"][0]["token"]
    def broken(*a, **k):
        raise RuntimeError("合成患者 架空太郎 本文")
    monkeypatch.setattr(patient_context_view, "evidence", broken)
    capsys.readouterr()
    result = click(led, entry, now=NOW + 1)
    assert result["body"].startswith("患者の記録まとめを読み込めなかったため")
    events = _fallback_events(capsys)
    assert len(events) == 1
    assert events[0] == {"event": "patient_context_view_fallback",
                         "error": "RuntimeError", "section": "background"}
    # the message text must not appear anywhere (stderr was drained above; re-run to check)
    click(led, entry, now=NOW + 2)
    assert "架空太郎" not in capsys.readouterr().err


@pytest.mark.parametrize("known", [True, False])
def test_context_fallback_logs_registered_value_error_code(led, monkeypatch, capsys, known):
    import patient_context_view
    from error_codes import KNOWN_ERROR_CODES
    from mcs_util import safe_error_code
    spec = _delivered_card(led)
    entry = click(led, _token_for(spec, "summary"))["navigation"][0]["token"]
    raised = ValueError(sorted(KNOWN_ERROR_CODES)[0] if known else "合成の本文")
    def broken(*a, **k):
        raise raised
    monkeypatch.setattr(patient_context_view, "evidence", broken)
    capsys.readouterr()
    click(led, entry, now=NOW + 1)
    events = _fallback_events(capsys)
    assert len(events) == 1 and events[0]["error"] == safe_error_code(raised)
    assert (events[0]["error"] == "ValueError") is (not known)


def test_context_fallback_survives_a_broken_stderr(led, monkeypatch):
    import patient_context_view
    spec = _delivered_card(led)
    entry = click(led, _token_for(spec, "summary"))["navigation"][0]["token"]
    def broken(*a, **k):
        raise RuntimeError("synthetic")
    class DeadStderr:
        def write(self, *_):
            raise BrokenPipeError("synthetic")
        def flush(self):
            raise BrokenPipeError("synthetic")
    monkeypatch.setattr(patient_context_view, "evidence", broken)
    monkeypatch.setattr("sys.stderr", DeadStderr())
    result = click(led, entry, now=NOW + 1)
    assert result["body"].startswith("患者の記録まとめを読み込めなかったため")


def test_later_click_by_same_user_on_same_card_advances_checkpoint(led):
    from adapters.common import envelopes
    spec = _delivered_card(led)
    token = _token_for(spec, "summary")
    actor = "discord:1001"
    # adapters derive the command id from token and actor only, so repeat clicks share it
    req = {"command_id": f"{token}:{envelopes.actor_hash(actor)}", "actor": actor, "token": token,
           "origin": dict(ORIGIN, message_id="m-9")}
    first = notify_actions.apply_notification(led, req, ENABLED, now=NOW)
    later = notify_actions.apply_notification(led, req, ENABLED, now=NOW + 2 * 86400)
    assert first["context_since"] == NOW - FIRST and later["context_since"] == NOW
    row = led.db.execute("SELECT content FROM artifacts WHERE kind='patient_context_view_checkpoint_v1'").fetchone()
    assert json.loads(row[0])["at"] == NOW + 2 * 86400


FIRST = 7 * 86400


def test_first_view_compares_against_seven_days_and_says_so(led):
    spec = _delivered_card(led)
    first = click(led, _token_for(spec, "summary"), now=NOW)
    assert first["context_since"] == NOW - FIRST and first["context_first_view"] is True
    changes_token = first["navigation"][1]["token"]
    changes = click(led, changes_token, now=NOW + 5)
    head = str(changes["list"]["head"])
    assert "初回（直近7日）" in head and "前回の表示依頼" not in head
    assert changes["context_since"] == NOW - FIRST and changes["context_first_view"] is True


def _token_params(led, token):
    return json.loads(led.db.execute("SELECT params FROM notification_action_tokens WHERE token=?", (token,)).fetchone()[0])


def test_first_view_survives_in_section_arrows(led):
    spec = _delivered_card(led)
    page = click(led, _token_for(spec, "summary"), now=NOW)["navigation"][0]  # background
    result = click(led, page["token"], now=NOW + 1)
    for step in range(12):
        assert result["context_first_view"] is True and result["context_since"] == NOW - FIRST
        assert all(_token_params(led, b["token"]).get("first_view") is True
                   for b in result["navigation"] if "section" in _token_params(led, b["token"]))
        if notify_views.CONTEXT_SECTIONS["changes"] in result["list"]["title"]:
            break
        arrow = next(b for b in result["navigation"] if b["label"].startswith("→ "))
        result = click(led, arrow["token"], now=NOW + 2 + step)
    assert "初回（直近7日）" in str(result["list"]["head"])


def test_first_view_survives_receipt_replay_and_saved_checkpoint_replay(led):
    spec = _delivered_card(led)
    req = {"command_id": "same-view", "actor": "discord:1001", "token": _token_for(spec, "summary"),
           "origin": dict(ORIGIN, message_id="m-9")}
    first = notify_actions.apply_notification(led, req, ENABLED, now=NOW)
    replayed = notify_actions.apply_notification(led, req, ENABLED, now=NOW + 30)
    assert replayed["context_first_view"] is True and replayed["context_since"] == first["context_since"]
    assert _token_params(led, replayed["navigation"][1]["token"])["first_view"] is True
    # without the receipt, the saved checkpoint of the same opening answers the resend
    led.db.execute("DELETE FROM command_receipts WHERE command_id='same-view'")
    led.db.commit()
    resent = notify_actions.apply_notification(led, req, ENABLED, now=NOW + 60)
    assert resent["context_first_view"] is True and resent["context_since"] == first["context_since"]
    saved = json.loads(led.db.execute("SELECT content FROM artifacts WHERE kind='patient_context_view_checkpoint_v1'").fetchone()[0])
    assert saved["at"] == NOW and saved["first_view"] is True


def test_token_issued_before_the_baseline_change_reads_as_first_view(led):
    # since=0 and no first_view flag: a token minted by the earlier release
    spec = _delivered_card(led)
    token = click(led, _token_for(spec, "summary"), now=NOW)["navigation"][1]["token"]
    params = _token_params(led, token)
    params.pop("first_view")
    params["since"] = 0
    led.db.execute("UPDATE notification_action_tokens SET params=? WHERE token=?", (json.dumps(params), token))
    led.db.commit()
    result = click(led, token, now=NOW + 5)
    assert result["context_first_view"] is True and result["context_since"] == NOW + 5 - FIRST
    head = str(result["list"]["head"])
    assert "初回（直近7日）" in head and "1970" not in head

