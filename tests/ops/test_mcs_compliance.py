"""Synthetic checks for the periodic access review summary."""
import mcs_compliance


def test_review_flags_breadth_denials_and_off_hours():
    day = 1_790_000_000 - 1_790_000_000 % 86400 + 3 * 3600  # 12:00 JST
    entries = [{"via": "slack", "actor": "a", "ts": day, "outcome": "ok", "project_id": n}
               for n in range(3)]
    entries += [{"via": "slack", "actor": "b", "ts": day + 10 * 3600, "outcome": "user_not_allowed",
                 "project_id": None}]
    rows = {row["actor"]: row for row in mcs_compliance.review(entries, max_patients=2)}
    assert rows["a"]["patients"] == 3 and rows["a"]["flags"] == ["many_patients"]
    assert rows["b"]["flags"] == ["denied", "off_hours"]


def test_review_counts_patients_a_list_view_returned():
    day = 1_790_000_000 - 1_790_000_000 % 86400 + 3 * 3600
    entries = [{"via": "slack", "actor": "c", "ts": day, "outcome": "ok", "project_id": None,
                "patients": [1, 2], "patients_more": 1}]
    row, = mcs_compliance.review(entries, max_patients=2)
    assert row["patients"] == 3 and row["flags"] == ["many_patients", "patients_incomplete"]
    assert row["patients_exact"] is False and row["patients_unlisted_views"] == 1


def test_review_does_not_count_repeated_unlisted_patients_as_distinct():
    entry = {"via": "slack", "actor": "c", "ts": 1_790_000_000, "outcome": "ok",
             "project_id": None, "patients": list(range(1, 201)), "patients_more": 5}
    row, = mcs_compliance.review([entry, entry, {**entry, "patients": [],
                                                "patients_more": 0, "project_id": 201}],
                                  max_patients=205)
    assert row["patients"] == 205 and "many_patients" not in row["flags"]
    assert "patients_incomplete" in row["flags"]
    assert row["patients_exact"] is False and row["patients_unlisted_views"] == 10
    later = {**entry, "patients": list(range(201, 211)), "patients_more": 0}
    row, = mcs_compliance.review([entry, later], max_patients=205)
    assert row["patients"] == 210 and "many_patients" in row["flags"]


def test_review_exact_count_when_no_ids_are_omitted():
    entry = {"via": "slack", "actor": "c", "ts": 1_790_000_000, "outcome": "ok",
             "project_id": 2, "patients": [1, 2], "patients_more": 0}
    row, = mcs_compliance.review([entry, entry], max_patients=2)
    assert row["patients"] == 2 and row["patients_exact"] is True
    assert row["patients_unlisted_views"] == 0 and "many_patients" not in row["flags"]


def test_review_marks_unattributed_summary_and_legacy_list_as_incomplete():
    entry = {"via": "slack", "actor": "c", "ts": 1_790_000_000, "outcome": "ok",
             "project_id": None}
    summary = {**entry, "op": "summary", "patients": [10, 11], "patients_more": 0}
    legacy_list = {**entry, "op": "read"}
    denied = {**legacy_list, "outcome": "user_not_allowed"}
    row, = mcs_compliance.review([summary, legacy_list, denied], max_patients=2)
    assert row["patients"] == 0 and row["patients_exact"] is False
    assert row["patients_unknown_records"] == 2 and "patients_incomplete" in row["flags"]


def test_show_project_warns_when_an_omitted_id_cannot_be_excluded(tmp_path, monkeypatch, capsys):
    data = tmp_path / "data"
    data.mkdir()
    mcs_compliance.access_log.record(data, via="slack", actor="synthetic", op="read",
                                     patients=range(1, 206), now=1_790_000_000)
    mcs_compliance.access_log.record(data, via="slack", actor="synthetic", op="summary",
                                     patients=[1, 205], now=1_790_000_000)
    monkeypatch.setattr(mcs_compliance, "HOME", str(tmp_path))
    monkeypatch.setattr(mcs_compliance.time, "time", lambda: 1_790_000_000)
    assert mcs_compliance.main(["access-log", "show", "--project", "205"]) == 0
    output = capsys.readouterr()
    assert '"op": "summary"' in output.out and "患者IDを特定できない記録が2件" in output.err
    assert mcs_compliance.main(["access-log", "show", "--project", "1"]) == 0
    output = capsys.readouterr()
    assert '"project_id": null' in output.out and "患者IDを特定できない記録が1件" in output.err
