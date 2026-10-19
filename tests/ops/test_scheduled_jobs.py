"""Shared calendar planning leaves host state and the local clock to its caller."""
import calendar
import copy
import time

from scheduled_jobs import latest_calendar_slots


def test_calendar_selects_latest_slot_and_includes_catchup_boundary():
    now = calendar.timegm((2026, 10, 19, 5, 17, 0))
    schedules = [("daily", [{"Hour": 5, "Minute": 10}]),
                 ("frequent", [{"Minute": 0}, {"Minute": 15}, {"Minute": 30}]),
                 ("exact_minute", [{"Hour": 5, "Minute": 17}]),
                 ("outside", [{"Hour": 4, "Minute": 0}])]
    before = copy.deepcopy(schedules)
    assert latest_calendar_slots(schedules, now // 60, 7, time.gmtime) == {
        "daily": now - 7 * 60, "frequent": now - 2 * 60, "exact_minute": now}
    assert schedules == before
    assert latest_calendar_slots([("daily", [{"Hour": 5, "Minute": 10}])],
                                  now // 60, 6, time.gmtime) == {}
