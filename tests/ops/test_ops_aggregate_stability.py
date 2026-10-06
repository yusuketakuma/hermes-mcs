"""Corrupt aggregate fields cannot escape validation or echo arbitrary text."""
import json
import sqlite3

import pytest

import mcs_refstats
import mcs_signals


@pytest.mark.parametrize("timestamp", [float("inf"), float("-inf"), float("nan")])
def test_nonfinite_reference_as_of_is_classified_as_corrupt(timestamp):
    with pytest.raises(ValueError, match="refstat_corrupt"):
        mcs_refstats._fold_as_of({"synthetic": {"scope": {"as_of": timestamp}}})


@pytest.mark.parametrize("wanted,actual", [
    (2**53 + 1, float(2**53)),
    (10**1000, 1.0),
], ids=["integer_precision", "integer_float_overflow"])
def test_reference_diff_does_not_round_large_integer_mismatches_away(wanted, actual):
    assert mcs_refstats._diff(wanted, actual)


@pytest.mark.parametrize("type_value", ["synthetic-private-canary", ["synthetic-private-canary"],
                                       {"synthetic-private-canary": 1}])
def test_dismissal_counts_never_echo_unknown_record_type(type_value):
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE artifacts(kind TEXT,project_id INTEGER,content TEXT)")
    for type_ in (type_value, "request_overdue"):
        db.execute("INSERT INTO artifacts VALUES('signal_v1',1,?)", (json.dumps({
            "state": "dismissed", "type": type_, "dismiss_reason_code": "not_applicable",
        }),))
    try:
        report = mcs_signals.dismiss_reason_counts(db, project_id=1)
    finally:
        db.close()
    assert set(report) == {"unknown", "request_overdue"}
    assert sum(sum(row.values()) for row in report.values()) == 2
    assert "synthetic-private-canary" not in json.dumps(report)
