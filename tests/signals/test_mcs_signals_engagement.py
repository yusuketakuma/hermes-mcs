"""Validate responder identity, engagement and self-reaction evidence."""
import self_identity
import json
import pytest
import mcs_signals
from ops_testkit import DAY, NOW, _msg
import message_observations
from signal_detector_testkit import (
    _extract_llm as _extract_llm,
    _extract_doc as _extract_doc,
    _states as _states,
    _ev as _ev,
    _req_item as _req_item,
    _capture as _capture,
    _stamp as _stamp,
    _ph_open as _ph_open,
    _ph_request as _ph_request,
    _side_tables as _side_tables,
    _open_types as _open_types,
    led as led,
    SELF_REACT_CFG as SELF_REACT_CFG,
)


@pytest.mark.parametrize("raw", ["{broken", "[" * 1500 + "0" + "]" * 1500,
                                 "[]", "null"], ids=["malformed", "deep", "array", "null"])
def test_corrupt_latest_profile_never_revives_prior_identity(led, raw):
    self_identity.record_self_profile(led.db, {
        "sender_id": 42, "organizations": ["synthetic-org"], "professions": ["看護師"]})
    led.artifact_add(self_identity.SELF_PROFILE_KIND, raw)
    before = led.db.total_changes
    assert self_identity.latest_self_profile(led.db) == {}
    assert self_identity.self_sender_id(led.db) is None
    assert not self_identity.is_self_message(led.db, 42)
    assert self_identity.self_sets({}, led.db) == ([], ["薬剤師"], [])
    explicit = {"self_organizations": ["synthetic-explicit"], "self_professions": []}
    assert self_identity.self_sets(explicit, led.db) == (["synthetic-explicit"], [], [])
    assert led.db.total_changes == before



@pytest.mark.parametrize("raw", ["{broken", "[" * 1500 + "0" + "]" * 1500,
                                 "[]", "null"], ids=["malformed", "deep", "array", "null"])
def test_corrupt_latest_roster_never_revives_prior_membership(led, raw):
    self_identity.record_station_staff(led.db, [{"staff_id": 42, "is_self": True}])
    led.artifact_add(self_identity.STATION_STAFF_KIND, raw)
    before = led.db.total_changes
    assert self_identity.latest_station_staff(led.db) == []
    assert self_identity.own_sender_ids(led.db) == frozenset()
    assert self_identity.is_own_station_message(led.db, 42) is None
    assert self_identity.self_sender_id(led.db) is None
    assert led.db.total_changes == before



def test_station_staff_roster_replaced_only_on_change(led):
    staff = [{"staff_id": 1, "name": "山田 花子", "station": "みどり薬局"}]
    with led.db:
        assert self_identity.record_station_staff(led.db, staff) is True
        assert self_identity.record_station_staff(led.db, staff) is False
    assert self_identity.latest_station_staff(led.db) == staff



def test_self_org_med_mention_excluded(led):
    """A med-change mention authored by the pharmacy itself is not a
    review candidate — our own reports need no follow-up ping to us.
    Without self_organizations configured nothing is suppressed."""
    _msg(led.db, 1, ts=NOW - 30 * DAY, org="みどり薬局")
    _extract_llm(led.db, 1, "h1", [{"name": "薬A", "action": "stop"}])
    res = _ev(led)
    assert res["open"] == 1            # unconfigured: org is just an org
    # configure self -> self-authored mentions drop out of the
    # evidence set entirely: new ones open nothing, and the
    # already-open one RESOLVES (evidence gone) — that is exactly how
    # the self-authored backlog cleans itself up, append-only.
    cfg = {"signals": {"self_organizations": ["みどり薬局"]}}
    _msg(led.db, 2, ts=NOW - 20 * DAY, chash="h2",
         org="みどり薬局")
    _extract_llm(led.db, 2, "h2", [{"name": "薬B", "action": "stop"}])
    _ev(led, cfg=cfg)
    states = _states(led.db)
    assert "med_change_no_followup:1:薬B" not in states
    assert states["med_change_no_followup:1:薬A"] == "resolved"



def test_discharge_notice_self_authored_excluded(led):
    _msg(led.db, 1, ts=NOW - 5 * DAY, body="退院報告",
         org="みどり薬局")
    _extract_doc(led.db, 1, "h1", events=["discharge"])
    cfg = {"signals": {"self_organizations": ["みどり薬局"]}}
    _ev(led, cfg=cfg)
    assert not [s for s in mcs_signals.current_open(led.db)["items"]
                if s["type"] in ("discharge_notice",
                                 "transition_reconciliation")]



def test_self_org_request_responder(led):
    """A post by the configured self organization counts as a
    responder for pharmacist-directed requests."""
    _msg(led.db, 1, ts=NOW - 4 * DAY)
    _extract_doc(led.db, 1, "h1",
                 requests=[_req_item("薬剤師", "残薬調整の確認")])
    _msg(led.db, 2, ts=NOW - 3 * DAY, chash="h2", prof="その他",
         org="みどり薬局")
    cfg = {"signals": {"self_organizations": ["みどり薬局"]}}
    _ev(led, cfg=cfg)
    assert not [s for s in mcs_signals.current_open(led.db)["items"]
                if s["type"] == "pharmacist_request_unanswered"]



def test_self_profile_artifact_supplies_defaults(led):
    """Without config, the fetched MCS self profile (recorded as an
    artifact by run_check) supplies self organizations/professions."""
    self_identity.record_self_profile(led.db, {
        "sender_id": 42, "name": "山田 薬剤",
        "professions": ["薬剤師"], "organizations": ["みどり薬局"]})
    _msg(led.db, 1, ts=NOW - 30 * DAY, org="みどり薬局")
    _extract_llm(led.db, 1, "h1", [{"name": "薬A", "action": "stop"}])
    _msg(led.db, 2, ts=NOW - 20 * DAY, chash="h2")   # other org
    _extract_llm(led.db, 2, "h2", [{"name": "薬B", "action": "stop"}])
    _ev(led)
    states = _states(led.db)
    assert "med_change_no_followup:1:薬A" not in states
    assert states["med_change_no_followup:1:薬B"] == "open"



def test_config_overrides_self_profile_artifact(led):
    """An explicit signals.self_organizations wins over the fetched
    profile — manual override beats derivation."""
    self_identity.record_self_profile(led.db, {
        "sender_id": 42, "name": "山田 薬剤",
        "professions": [], "organizations": ["みどり薬局"]})
    _msg(led.db, 1, ts=NOW - 30 * DAY, org="みどり薬局")
    _extract_llm(led.db, 1, "h1", [{"name": "薬A", "action": "stop"}])
    cfg = {"signals": {"self_organizations": ["そら薬局"]}}
    _ev(led, cfg=cfg)
    # みどり薬局 is NOT self under the override -> signal opens
    assert _states(led.db)["med_change_no_followup:1:薬A"] == "open"



def test_record_self_profile_dedupes(led):
    """Identical profiles are not re-appended — append-only stays
    clean across ticks."""
    prof = {"sender_id": 42, "name": "山田 薬剤",
            "professions": ["薬剤師"], "organizations": ["みどり薬局"]}
    assert self_identity.record_self_profile(led.db, prof) is True
    assert self_identity.record_self_profile(led.db, prof) is False
    changed = dict(prof, organizations=["みどり薬局", "そら薬局"])
    assert self_identity.record_self_profile(led.db, changed) is True
    n = led.db.execute("SELECT COUNT(*) FROM artifacts "
                       "WHERE kind='self_profile_v1'").fetchone()[0]
    assert n == 2
    latest = self_identity.latest_self_profile(led.db)
    assert latest["organizations"] == ["みどり薬局", "そら薬局"]
    assert latest["name"] == "山田 薬剤"



@pytest.mark.parametrize(("value", "expected"), [
    (42, 42), ("42", 42), (" 0042 ", 42), (2**63 - 1, 2**63 - 1),
    (None, None), (True, None), (42.0, None), (0, None), (-1, None),
    (2**63, None), ("9223372036854775808", None), ("", None),
    ("4.2", None), ("４２", None), ({"id": 42}, None),
])
def test_normalize_sender_id(value, expected):
    assert self_identity.normalize_sender_id(value) == expected



def test_station_self_id_repairs_profile_and_does_not_match_names(led):
    prof = {"sender_id": None, "name": "SYNTH-A",
            "professions": [], "organizations": ["SYNTH-薬局"]}
    self_identity.record_self_profile(led.db, prof)
    assert self_identity.is_own_station_message(led.db, 42) is None
    staff = [{"staff_id": "42", "name": "SYNTH-A", "is_self": True},
             {"staff_id": 43, "name": "SYNTH-A", "is_self": False},
             {"staff_id": 42, "name": "SYNTH-改名", "is_self": True}]
    assert self_identity.record_station_staff(led.db, staff) is True
    assert self_identity.self_sender_id(led.db) == 42
    assert self_identity.own_sender_ids(led.db) == frozenset({42, 43})
    assert self_identity.is_own_station_message(led.db, "0042") is True
    assert self_identity.is_own_station_message(led.db, 43) is True
    assert self_identity.is_own_station_message(led.db, 44) is False
    assert self_identity.is_own_station_message(led.db, None) is None
    assert self_identity.is_own_station_message(led.db, True) is None
    assert self_identity.is_self_message(led.db, "0042") is True
    assert self_identity.is_self_message(led.db, 43) is False
    assert self_identity.is_self_message(led.db, None) is False
    current = self_identity.latest_self_profile(led.db)
    assert current["sender_id"] == 42
    assert all(current[k] == prof[k] for k in ("name", "professions", "organizations"))
    assert self_identity.self_sets({}, led.db) == (["SYNTH-薬局"], [], [])
    # A subsequent /users/self response missing its ID cannot erase it.
    assert self_identity.record_self_profile(led.db, prof) is False
    assert self_identity.record_station_staff(led.db, staff) is False
    # Repair also runs if an old missing-ID profile follows an unchanged roster.
    led.artifact_add(self_identity.SELF_PROFILE_KIND, json.dumps(prof))
    assert self_identity.record_station_staff(led.db, staff) is True
    assert self_identity.latest_self_profile(led.db)["sender_id"] == 42



@pytest.mark.parametrize("self_ids", [[42, 43], [42, None], [True], [None]])
def test_ambiguous_station_self_id_is_unknown(led, self_ids):
    self_identity.record_self_profile(led.db, {"sender_id": 42})
    self_identity.record_station_staff(led.db, [
        {"staff_id": sid, "is_self": True} for sid in self_ids])
    assert self_identity.self_sender_id(led.db) is None
    assert self_identity.is_self_message(led.db, 42) is False



def test_station_self_id_works_without_profile_and_falls_back_when_absent(led):
    assert self_identity.self_sender_id(led.db) is None
    self_identity.record_station_staff(led.db, [{"staff_id": 42, "is_self": True}])
    assert self_identity.self_sender_id(led.db) == 42
    assert self_identity.latest_self_profile(led.db) == {}
    self_identity.record_self_profile(led.db, {"sender_id": "44"})
    assert self_identity.self_sender_id(led.db) == 42  # roster takes precedence
    self_identity.record_station_staff(led.db, [{"staff_id": 42, "is_self": False}])
    assert self_identity.self_sender_id(led.db) == 44
    self_identity.record_station_staff(led.db, [{"name": "SYNTH-A"}])
    assert self_identity.is_own_station_message(led.db, 44) is None



def test_deleted_responder_does_not_resolve_unanswered_request(led):
    _msg(led.db, 1, ts=NOW - 4 * DAY)
    _extract_doc(led.db, 1, "h1", requests=[_req_item("薬剤師", "残薬確認")])
    _msg(led.db, 2, ts=NOW - 3 * DAY, chash="h2", prof="薬剤師")
    led.db.execute("UPDATE messages SET body_state='deleted',body_text='' "
                   "WHERE message_id=2")
    _ev(led)
    assert _states(led.db).get("pharmacist_request_unanswered:1:1") == "open"



def test_self_sets_explicit_empty_is_not_unset(led):
    """signals.self_organizations=[] means 'no self org' — distinct
    from unset, which would fall back to the fetched profile."""
    self_identity.record_self_profile(
        led.db, {"sender_id": 1, "name": "n",
                 "professions": ["薬剤師"], "organizations": ["orgX"]})
    orgs, profs, _ = self_identity.self_sets(
        {"self_organizations": []}, led.db)
    assert orgs == [] and profs == ["薬剤師"]



def test_self_sets_empty_profile_professions_authoritative(led):
    """A fetched profile with no specialist categories means 'no
    listed profession' — the 薬剤師 default only applies when NO
    profile exists."""
    self_identity.record_self_profile(
        led.db, {"sender_id": 1, "name": "n",
                 "professions": [], "organizations": ["orgX"]})
    orgs, profs, _ = self_identity.self_sets({}, led.db)
    assert orgs == ["orgX"] and profs == []



@pytest.mark.parametrize("cfg", [
    None, {"signals": {}}, {"signals": {"self_reaction_response": False}},
    {"signals": {"self_reaction_response": "true"}}])
def test_self_reaction_off_matches_existing(led, cfg):
    _ph_request(led.db)
    _capture(led.db, 1, [_stamp("accepted", True)])
    _ev(led, cfg)
    assert len(_ph_open(led.db)) == 1



@pytest.mark.parametrize("kind", ["accepted", "completed"])
def test_self_reaction_resolves_and_reopens(led, kind):
    _ph_request(led.db)
    _ev(led, SELF_REACT_CFG)
    [sig] = _ph_open(led.db)
    before = _side_tables(led.db)
    _capture(led.db, 1, [_stamp(kind, True)])
    res = _ev(led, SELF_REACT_CFG)
    assert res["resolved"] == 1 and not _ph_open(led.db)
    # stamp withdrawn and re-observed -> the candidate returns
    _capture(led.db, 1, [_stamp(kind, False)])
    _ev(led, SELF_REACT_CFG)
    [again] = _ph_open(led.db)
    assert again["evidence"] == sig["evidence"]
    assert _side_tables(led.db) == before
    # no extra supersede rows: open, resolved, open
    assert led.db.execute(
        "SELECT COUNT(*) FROM artifacts WHERE kind='signal_v1'"
    ).fetchone()[0] == 3



@pytest.mark.parametrize("reactions", [
    None,                                        # never fetched
    [],                                          # observed, no stamps
    [_stamp("accepted", False, count=3)],        # someone else's 承知
    [_stamp("viewed", True), _stamp("thanked", True), _stamp("good", True)],
    [{"type": "accepted", "count": 1}],          # invalid: no self flag
])
def test_self_reaction_non_counting(led, reactions):
    _ph_request(led.db)
    _capture(led.db, 1, reactions)
    _ev(led, SELF_REACT_CFG)
    assert len(_ph_open(led.db)) == 1



def test_self_reaction_only_on_request_post(led):
    _ph_request(led.db)
    _msg(led.db, 2, ts=NOW - 3 * DAY, chash="h2")
    _capture(led.db, 2, [_stamp("accepted", True)])
    _ev(led, SELF_REACT_CFG)
    assert len(_ph_open(led.db)) == 1



def test_failed_refresh_never_uses_previous_stamp_as_response(led):
    from types import SimpleNamespace
    from message_observations import get_message_metadata
    _ph_request(led.db)
    _capture(led.db, 1, [_stamp("accepted", True)])
    # An invalid recapture retains the observation for history, but must
    # not turn it into a current response or publish a failed refresh.
    led.db.execute("UPDATE message_metadata SET last_error='reactions_invalid'")
    assert get_message_metadata(led.db, 1)["reactions_status"] == "invalid"
    _ev(led, SELF_REACT_CFG)
    assert len(_ph_open(led.db)) == 1
    led.db.execute("UPDATE message_metadata SET last_error=NULL")
    _ev(led, SELF_REACT_CFG)
    assert not _ph_open(led.db)
    before = tuple(led.db.execute("SELECT * FROM message_metadata").fetchone())
    led.save_metadata_shadow(SimpleNamespace(
        message_id=1, project_id=1, metadata={}, metadata_errors=["reactions_invalid"]),
        publish=True)
    assert tuple(led.db.execute(
        "SELECT * FROM message_metadata WHERE source='capture'").fetchone()) == before
    _ev(led, SELF_REACT_CFG)
    assert len(_ph_open(led.db)) == 1



def test_stamp_resolved_request_stays_watched_until_cancellation(led):
    from types import SimpleNamespace
    _ph_request(led.db)
    _ev(led, SELF_REACT_CFG)
    assert len(_ph_open(led.db)) == 1
    _capture(led.db, 1, [_stamp("accepted", True)])
    _ev(led, SELF_REACT_CFG)
    assert not _ph_open(led.db)
    assert 1 in {r["message_id"] for r in message_observations.metadata_watch_targets(led.db, now=NOW)}
    # Simulate the actual refresh publication, not a direct replacement
    # of the capture row used by the existing lifecycle test.
    led.save_metadata_shadow(SimpleNamespace(
        message_id=1, project_id=1, metadata={"reactions": []}, metadata_errors=[]),
        publish=True)
    _ev(led, SELF_REACT_CFG)
    assert len(_ph_open(led.db)) == 1



def test_self_reaction_detector_error_never_resolves(led, monkeypatch):
    import message_observations
    _ph_request(led.db)
    _ev(led, SELF_REACT_CFG)

    def boom(*a, **k):
        raise RuntimeError("x")
    monkeypatch.setattr(message_observations, "get_message_metadata", boom)
    res = _ev(led, SELF_REACT_CFG)
    assert "pharmacist_request_unanswered:RuntimeError" in res["errors"]
    assert res["resolved"] == 0 and len(_ph_open(led.db)) == 1



def test_multi_profession_reply_counts_as_responder(led):
    _msg(led.db, 1, ts=NOW - 4 * DAY)
    _extract_doc(led.db, 1, "h1",
                 requests=[{"to": "薬剤師", "action": "残薬確認"}])
    _msg(led.db, 2, ts=NOW - 3 * DAY, chash="h2",
         prof="薬剤師, ケアマネジャー")
    _ev(led)
    assert "pharmacist_request_unanswered" not in _open_types(led.db)



def test_multi_station_reply_counts_as_responder(led):
    _msg(led.db, 1, ts=NOW - 4 * DAY)
    _extract_doc(led.db, 1, "h1",
                 requests=[{"to": "薬剤師", "action": "残薬確認"}])
    _msg(led.db, 2, ts=NOW - 3 * DAY, chash="h2",
         org="SYN第二薬局, SYN薬局")
    _ev(led, {"signals": {"self_organizations": ["SYN薬局"],
                          "self_professions": []}})
    assert "pharmacist_request_unanswered" not in _open_types(led.db)



def test_multi_station_self_post_excluded(led):
    _msg(led.db, 1, ts=NOW - DAY, body="飲み忘れが多い",
         org="SYN薬局, SYN第二薬局")
    _ev(led, {"signals": {"self_organizations": ["SYN薬局"]}})
    assert "adherence_concern" not in _open_types(led.db)
    # a station merely containing the name as a substring is not ours
    _msg(led.db, 2, pid=2, ts=NOW - DAY, chash="h2", body="飲み忘れが多い",
         org="SYN薬局東")
    _ev(led, {"signals": {"self_organizations": ["SYN薬局"]}})
    assert {s["project_id"] for s in mcs_signals.current_open(led.db)["items"]
            if s["type"] == "adherence_concern"} == {2}
