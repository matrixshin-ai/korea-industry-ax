from datetime import datetime, timedelta, timezone

from timewindow import KST, get_collection_hours, get_window_anchor, within_window

# 2026-01-05 is a Monday (2026-01-01 is a Thursday).
MONDAY = datetime(2026, 1, 5, tzinfo=KST)
SUNDAY = datetime(2026, 1, 4, tzinfo=KST)
TUESDAY = datetime(2026, 1, 6, tzinfo=KST)
SATURDAY = datetime(2026, 1, 10, tzinfo=KST)


def test_monday_run_is_48h():
    assert get_collection_hours(MONDAY.replace(hour=6, minute=33)) == 48


def test_tuesday_through_saturday_runs_are_24h():
    for d in (TUESDAY, SATURDAY):
        assert get_collection_hours(d.replace(hour=6, minute=33)) == 24


def test_sunday_is_24h_even_though_pipeline_does_not_run():
    assert get_collection_hours(SUNDAY.replace(hour=6, minute=33)) == 24


def test_get_window_anchor_before_0630_uses_previous_day():
    # 06:29 KST on Tuesday hasn't reached today's 06:30 anchor yet - still Monday's.
    just_before = TUESDAY.replace(hour=6, minute=29)
    anchor = get_window_anchor(just_before)
    assert anchor == MONDAY.replace(hour=6, minute=30)


def test_get_window_anchor_at_exactly_0630_uses_today():
    exactly = TUESDAY.replace(hour=6, minute=30)
    anchor = get_window_anchor(exactly)
    assert anchor == TUESDAY.replace(hour=6, minute=30)


def test_get_window_anchor_after_0630_uses_today():
    later = TUESDAY.replace(hour=14, minute=0)
    anchor = get_window_anchor(later)
    assert anchor == TUESDAY.replace(hour=6, minute=30)


def test_a_run_just_before_0630_on_monday_still_gets_48h_from_sunday():
    # 06:29 KST Monday hasn't reached Monday's own anchor -> falls back to
    # Sunday's anchor, so get_collection_hours must read Sunday's weekday (24h),
    # not Monday's (48h). This is the boundary the 06:30 anchor exists to fix.
    just_before_monday_anchor = MONDAY.replace(hour=6, minute=29)
    assert get_collection_hours(just_before_monday_anchor) == 24


def test_monday_0630_boundary_is_still_monday_even_though_utc_date_is_sunday():
    # 2026-01-05 06:30:00 KST == 2026-01-04 21:30:00 UTC (still Sunday in UTC).
    monday_anchor_instant = MONDAY.replace(hour=6, minute=31)
    assert monday_anchor_instant.astimezone(timezone.utc).weekday() == 6  # Sunday in UTC
    assert get_collection_hours(monday_anchor_instant) == 48


def test_within_window_true_for_recent():
    now = datetime(2026, 1, 5, 12, 0, tzinfo=timezone.utc)
    dt = now - timedelta(hours=10)
    assert within_window(dt, hours=24, now_utc=now) is True


def test_within_window_false_for_old():
    now = datetime(2026, 1, 5, 12, 0, tzinfo=timezone.utc)
    dt = now - timedelta(hours=25)
    assert within_window(dt, hours=24, now_utc=now) is False


def test_within_window_handles_naive_datetime_as_utc():
    now = datetime(2026, 1, 5, 12, 0, tzinfo=timezone.utc)
    dt = (now - timedelta(hours=1)).replace(tzinfo=None)
    assert within_window(dt, hours=24, now_utc=now) is True


def test_within_window_false_for_none():
    assert within_window(None, hours=24) is False


def test_within_window_defaults_to_kst_0630_anchor():
    # An article published 1 hour before today's 06:30 KST anchor should be
    # inside a 24h window when checked at any point later the same run-day,
    # even if "now" has drifted well past 06:30 (e.g. a late-running job).
    published = TUESDAY.replace(hour=5, minute=30).astimezone(timezone.utc)
    run_at_1400 = TUESDAY.replace(hour=14, minute=0)
    anchor = get_window_anchor(run_at_1400).astimezone(timezone.utc)
    assert within_window(published, hours=24, now_utc=anchor) is True
