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
    assert row["patients"] == 3 and row["patients_at_least"] and row["flags"] == ["many_patients"]


def test_review_never_sums_overflow_counts_of_repeated_reads():
    day = 1_790_000_000 - 1_790_000_000 % 86400 + 3 * 3600
    listed = list(range(200))
    entries = [{"via": "slack", "actor": "d", "ts": day, "outcome": "ok", "project_id": None,
                "patients": listed, "patients_more": 1} for _ in range(30)]
    entries.append({"via": "slack", "actor": "d", "ts": day, "outcome": "ok", "project_id": 500})
    row, = mcs_compliance.review(entries, max_patients=220)
    # The unlisted patient may be patient 500, so 201 is the safe lower bound, not 231.
    assert row["patients"] == 201 and row["patients_at_least"] and row["flags"] == []
