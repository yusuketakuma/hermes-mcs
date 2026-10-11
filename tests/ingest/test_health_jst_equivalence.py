"""health_watch._jst renders the same JST minute as the old fixed +9h formula."""
import time

import pytest

import health_watch


@pytest.mark.parametrize("ts", [0, 1, 1700000000, 1900000000.5,
                                1767193199, 1767193200, 4102444800])
def test_jst_matches_fixed_offset(ts):
    expected = time.strftime("%m-%d %H:%M", time.gmtime(ts + 9 * 3600))
    assert health_watch._jst(ts) == expected


@pytest.mark.parametrize("ts", [None, "1", float("nan"), 1e300, -1e300, True])
def test_unrenderable_times_stay_unknown(ts):
    assert health_watch._jst(ts) == "不明"
