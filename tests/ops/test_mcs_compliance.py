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
    assert row["patients"] == 3 and row["flags"] == ["many_patients"]
