"""Fully synthetic real-Ledger interaction latency and privacy integration."""
import json
from datetime import datetime, timezone

import pytest

from ledger import Ledger, publish_snapshot
from mcs_adapter import Message
import export_schema
import mcs_stats
import mcs_view

NOW = 1791072000
POLICY = {"min_pairs": 2, "min_actors": 2, "min_projects": 2}


@pytest.fixture
def store(tmp_path):
    lg = Ledger(str(tmp_path / "synthetic-latency.db"))
    yield lg
    lg.close()


def post(store, mid, *, pid=1, parent=None, ts: int | None = NOW, actor=None,
         profession="医師", replies=0, state="full"):
    """Save a synthetic message through the real ingestion storage API."""
    store.ensure_patient(pid)
    store.save_messages([Message(
        message_id=mid, project_id=pid, parent_id=parent,
        sender_id=mid if actor is None else actor,
        sender_name="NAME_CANARY", sender_type="user", profession=profession,
        organization="FACILITY_CANARY",
        posted_at=datetime.fromtimestamp(ts, timezone.utc).isoformat() if ts else "",
        body_html="BODY_CANARY", body_state=state, is_unread=False,
        reply_count=replies)], project_id=pid)


def pair(store, mid, *, pid=1, delay=60, start="医師", end="看護師", replies=1):
    post(store, mid, pid=pid, profession=start, replies=replies)
    post(store, mid + 1, pid=pid, parent=mid, ts=NOW + delay, profession=end)


def measure(store, **args):
    return mcs_stats.run_stats(
        store.db, NOW + 1000, {"stat": "interaction_latency", **args}
    )["stats"]["interaction_latency"]


def roster(store, rows, *, at=NOW - 1, complete=True, pid=1, **extra):
    store.artifact_add("project_metadata_v1", json.dumps({
        "contract": "project-metadata/1", "dataset": "care_team",
        "scope": "project", "entity_id": pid, "item_id": None,
        "attempted_at": at, "complete": complete, "rows": rows,
        "reason": None if complete else "fetch_error", "http_status": None,
        "names_retained": False, **extra}), project_id=pid)


def test_explicit_selection_measures_first_reply_without_writes(store):
    # Given: two distinct roots/actors/rooms and a later reply.
    pair(store, 10, delay=60)
    pair(store, 20, pid=2, delay=180)
    post(store, 12, parent=10, ts=NOW + 300)
    store.db.execute("PRAGMA query_only=ON")
    before = store.db.total_changes
    # When
    result = measure(store, interaction_privacy_policy=POLICY)
    # Then
    assert result["role_cells"] == [{
        "from_roles": ["doctor"], "to_roles": ["nurse"],
        "n": 2, "median_s": 120, "p90_s": 180}]
    assert result["coverage"]["valid_time_pairs"] == 2
    assert store.db.total_changes == before
    assert not any(s in json.dumps(result) for s in
                   ("NAME_CANARY", "FACILITY_CANARY", "BODY_CANARY", "sender_id", "root_id"))
    assert all("interaction_latency" not in names for names in mcs_stats.PRESETS.values())


def test_latency_not_exportable_fails_closed(store):
    # Given: a measured internal-only latency stat.
    pair(store, 10)
    pair(store, 20, pid=2)
    value = measure(store, interaction_privacy_policy=POLICY)
    # When/Then: the aggregate export boundary rejects it by name.
    with pytest.raises(ValueError, match="stat_not_exportable"):
        export_schema.project_record(
            {"type": "stat", "name": "interaction_latency", "value": value})


@pytest.mark.parametrize("policy,project,reason", [
    (None, None, "owner_policy_required"),
    ({"min_pairs": 3, "min_actors": 2, "min_projects": 2}, None, "small_cell"),
    (POLICY, 1, "small_cell"),
    ({"min_pairs": 2, "min_actors": 3, "min_projects": 2}, None, "small_cell"),
])
def test_absent_policy_and_small_cells_hide_even_role_labels(store, policy, project, reason):
    # Given
    pair(store, 10)
    pair(store, 20, pid=2)
    # When
    result = measure(store, interaction_privacy_policy=policy, project=project)
    # Then
    assert result["role_cells"] == []
    assert result["role_cells_reason"] == reason
    assert "doctor" not in json.dumps(result)
    assert "median_s" not in json.dumps(result)


@pytest.mark.parametrize("start,end,expected", [
    ("医師", "医師", (["doctor"], ["doctor"])),
    ("看護師, 医師", "薬剤師", (["doctor", "nurse"], ["pharmacist"])),
    ("BAD_FREE_TEXT_CANARY", "看護師", None),
    ("医師, UNKNOWN_CANARY", "看護師", None),
])
def test_role_sets_are_not_duplicated_and_unmapped_values_stay_unknown(store, start, end, expected):
    # Given
    pair(store, 10, start=start, end=end)
    pair(store, 20, pid=2, start=start, end=end)
    # When
    result = measure(store, interaction_privacy_policy=POLICY)
    # Then
    if expected is None:
        assert result["coverage"]["unknown_role_pairs"] == 2
        assert result["role_cells"] == []
    else:
        cell = result["role_cells"][0]
        assert (cell["from_roles"], cell["to_roles"]) == expected
        assert cell["n"] == 2
    assert "CANARY" not in json.dumps(result)


def test_explicit_profession_map_uses_only_fixed_output_categories(store):
    # Given
    pair(store, 10, start="看護職", end="薬剤職")
    pair(store, 20, pid=2, start="看護職", end="薬剤職")
    # When
    result = measure(store, profession_map={"看護職": "nurse", "薬剤職": "pharmacist"},
                     interaction_privacy_policy=POLICY)
    # Then
    assert result["role_cells"][0]["from_roles"] == ["nurse"]
    assert result["role_cells"][0]["to_roles"] == ["pharmacist"]


@pytest.mark.parametrize("at,complete,duplicate,expected", [
    (NOW - 1, True, False, 0),
    (NOW + 1, True, False, 1),
    (NOW - 90000, True, False, 1),
    (NOW - 1, False, False, 1),
    (NOW - 1, True, True, 1),
])
def test_care_team_fallback_requires_exact_fresh_unambiguous_prior_actor_evidence(
        store, at, complete, duplicate, expected):
    # Given: the same roster evidence in two projects so a resolved cell is releasable.
    for pid, mid in ((1, 10), (2, 20)):
        pair(store, mid, pid=pid, start="", end="")
        rows = [{"id": mid, "type": "user", "professions": ["医師"]},
                {"id": mid + 1, "type": "user", "professions": ["看護師"]}]
        if duplicate:
            rows.append({"id": mid, "type": "user", "professions": ["薬剤師"]})
        roster(store, rows, at=at, complete=complete, pid=pid)
    # When
    result = measure(store, interaction_privacy_policy=POLICY)
    # Then
    assert result["coverage"]["unknown_role_pairs"] == 2 * expected
    assert len(result["role_cells"]) == 1 - expected
    assert result["role_sources"]["care_team"] == 2 * (
        2 if not expected else 1 if duplicate or at == NOW + 1 else 0)


def test_roster_actor_type_conflict_stays_unknown(store):
    # Given
    pair(store, 10, start="", end="")
    roster(store, [{"id": 10, "type": "other", "professions": ["医師"]},
                   {"id": 11, "type": "user", "professions": ["看護師"]}])
    # When
    result = measure(store, interaction_privacy_policy=POLICY)
    # Then
    assert result["coverage"]["unknown_role_pairs"] == 1


def test_missing_actor_never_resolves_by_name_or_facility(store):
    # Given
    pair(store, 10)
    store.db.execute("UPDATE messages SET sender_id=NULL WHERE message_id=10")
    # When
    result = measure(store, interaction_privacy_policy=POLICY)
    # Then
    assert result["coverage"]["unknown_role_pairs"] == 1


def test_time_and_source_gaps_never_become_zero_latency_or_unanswered(store):
    # Given
    pair(store, 10, delay=-1)
    pair(store, 20)
    store.db.execute("UPDATE messages SET posted_at_ts=NULL WHERE message_id=21")
    pair(store, 30, replies=2)
    post(store, 40)
    post(store, 50, replies=1)
    post(store, 60, ts=None)
    pair(store, 70)
    store.db.execute("UPDATE messages SET posted_at_ts='bad' WHERE message_id=71")
    # When
    result = measure(store, interaction_privacy_policy=POLICY)
    # Then
    assert result["coverage"] == {
        "roots": 7, "unplaced_root_time": 1, "incomplete_source": 2,
        "no_observed_reply": 1, "invalid_reply_time": 2,
        "negative_latency": 1, "valid_time_pairs": 0, "unknown_role_pairs": 0}
    assert result["role_cells"] == []
    assert result["valid_time_share"]["value"] == 0


def test_empty_denominator_is_null(store):
    # Given: an empty Ledger.
    # When
    result = measure(store)
    # Then
    assert result["valid_time_share"]["value"] is None
    assert result["valid_time_share"]["reason"] == "denominator_zero"


@pytest.mark.parametrize("policy", [{}, POLICY | {"min_pairs": 1}, POLICY | {"min_actors": True}])
def test_invalid_owner_policy_is_rejected(store, policy):
    # Given / When / Then
    with pytest.raises(ValueError, match="bad_interaction_privacy_policy"):
        measure(store, interaction_privacy_policy=policy)


def test_published_snapshot_view_accepts_explicit_policy_and_cli_defaults_closed(
        store, tmp_path, capsys):
    # Given
    pair(store, 10, delay=0)
    pair(store, 20, pid=2, delay=10)
    snapshot = publish_snapshot(str(tmp_path / "synthetic-latency.db"),
                                str(tmp_path / "snapshot"))
    view = mcs_view.View(snapshot)
    try:
        # When: the existing public View API and CLI consume the same snapshot.
        result = view.stats({"stat": "interaction_latency",
                             "interaction_privacy_policy": POLICY})
        exit_code = mcs_view.main(["--snapshot", str(snapshot), "stats",
                                   "--stat", "interaction_latency", "--project", "1"])
        cli = json.loads(capsys.readouterr().out)
        # Then: zero is valid only for a measured simultaneous posting pair.
        cell = result["stats"]["interaction_latency"]["role_cells"][0]
        assert (cell["median_s"], cell["p90_s"]) == (5, 10)
        assert exit_code == 0
        stat = cli["stats"]["interaction_latency"]
        assert stat["role_cells"] == []
        assert stat["role_cells_reason"] == "owner_policy_required"
        assert "project_id" not in stat["scope"]
        assert stat["scope"]["project_filter_applied"]
    finally:
        view.close()


@pytest.mark.parametrize("extra,released", [
    ([(30, "薬剤師", "家族")], False),
    ([(30, "薬剤師", "家族"), (40, "家族", "医師")], False),
])
def test_suppressed_cell_count_cannot_be_derived_from_coverage(store, extra, released):
    # Given: a releasable doctor-to-nurse cell (n=2) plus single-pair cells.
    pair(store, 10)
    pair(store, 20, pid=2)
    for mid, start, end in extra:
        pair(store, mid, start=start, end=end)
    # When
    result = measure(store, interaction_privacy_policy=POLICY)
    # Then: valid - released n (= unknown + suppressed) is never below min_pairs.
    assert result["role_cells_reason"] == "small_cell"
    assert result["coverage"]["unknown_role_pairs"] is None
    assert result["role_sources"] is None
    residual = (result["coverage"]["valid_time_pairs"]
                - sum(c["n"] for c in result["role_cells"]))
    assert residual >= POLICY["min_pairs"]
    assert bool(result["role_cells"]) is released


@pytest.mark.parametrize("shared", ["root_actor", "reply_actor", "project"])
def test_suppressed_residual_keeps_actor_and_project_thresholds(store, shared):
    pair(store, 10)
    pair(store, 20, pid=2)
    for i, (start, end) in enumerate((("薬剤師", "家族"), ("家族", "医師"))):
        mid = 30 + i * 10
        pid = 3 if shared == "project" else 3 + i
        post(store, mid, pid=pid, profession=start, replies=1,
             actor=900 if shared == "root_actor" else mid)
        post(store, mid + 1, pid=pid, parent=mid, ts=NOW + 60,
             profession=end, actor=901 if shared == "reply_actor" else mid + 1)
    result = measure(store, interaction_privacy_policy=POLICY)
    assert result["coverage"]["valid_time_pairs"] == 4
    assert result["role_cells"] == []
    assert result["role_sources"] is None


def test_suppressed_residual_with_all_thresholds_keeps_releasable_cell(store):
    pair(store, 10)
    pair(store, 20, pid=2)
    pair(store, 30, pid=3, start="薬剤師", end="家族")
    pair(store, 40, pid=4, start="家族", end="医師")
    result = measure(store, interaction_privacy_policy=POLICY)
    assert len(result["role_cells"]) == 1
    assert result["role_cells"][0]["n"] == 2


@pytest.mark.parametrize("profession", ["医師", "UNMAPPED_CANARY"])
def test_absent_policy_never_signals_role_resolution(store, profession):
    # Given: resolvable or unresolvable roles under an absent owner policy.
    pair(store, 10, start=profession)
    # When
    result = measure(store)
    # Then: null either way, so null-vs-value cannot reveal a resolved pair.
    assert result["coverage"]["unknown_role_pairs"] is None
    assert result["role_sources"] is None


def test_absent_policy_never_reads_roster_or_retains_role_groups(store, monkeypatch):
    pair(store, 10, start="", end="")
    pair(store, 20, pid=2, start="", end="")
    monkeypatch.setattr(mcs_stats, "get_project_metadata",
                        lambda *_args, **_kwargs: pytest.fail("non-public role resolution"))
    result = measure(store)
    assert result["coverage"]["valid_time_pairs"] == 2
    assert result["coverage"]["unknown_role_pairs"] is None
    assert result["role_sources"] is None and result["role_cells"] == []
    assert result["role_cells_reason"] == "owner_policy_required"
