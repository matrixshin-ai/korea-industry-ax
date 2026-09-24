"""
Shared KST collection-window helpers.

Collection window: 48h on Monday (KST), 24h Tue-Sat - matched to the daily
Mon-Sat run cadence (Sat -> Mon is a 2-day gap since Sunday doesn't run;
every other consecutive pair is a 1-day gap). The boundary is anchored to
the nominal KST 06:30 run time, not the literal wall-clock time the script
happens to execute at, so a delayed scheduled run or a manual
workflow_dispatch at an arbitrary time still windows against the same daily
cutoff instead of drifting. Sunday: the pipeline does not run at all (see
.github/workflows/update.yml cron).

This is the single definition; collect.py and build.py both import from
here so the collection window and the on-screen display window can never
drift apart.
"""
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))
BOUNDARY_HOUR_KST = 6
BOUNDARY_MINUTE_KST = 30


def get_window_anchor(now_kst: datetime = None) -> datetime:
    """Most recent KST 06:30 at or before now_kst (KST-aware)."""
    now_kst = now_kst or datetime.now(KST)
    anchor = now_kst.replace(hour=BOUNDARY_HOUR_KST, minute=BOUNDARY_MINUTE_KST, second=0, microsecond=0)
    if now_kst < anchor:
        anchor -= timedelta(days=1)
    return anchor


def get_collection_hours(now_kst: datetime = None) -> int:
    """Return 48 on Monday (KST), 24 Tue-Sat - determined by the KST 06:30 anchor day."""
    anchor = get_window_anchor(now_kst)
    return 48 if anchor.weekday() == 0 else 24


def within_window(dt: datetime, hours: int, now_utc: datetime = None) -> bool:
    """True if dt (aware, or naive and treated as UTC) falls within `hours` hours
    before the window anchor. The anchor is the KST 06:30 boundary by default;
    pass an explicit now_utc (e.g. in tests, or for a plain rolling-window check
    like cache pruning) to compare against that instant instead."""
    if not dt:
        return False
    anchor_utc = now_utc if now_utc is not None else get_window_anchor().astimezone(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt >= (anchor_utc - timedelta(hours=hours))
