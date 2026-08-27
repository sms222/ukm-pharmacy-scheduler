"""
UKM Pharmacy Scheduler - iCal Generator
Step 2: turns solver.py's weekly-recurring assignments into individual
calendar events, then writes one .ics per cohort and one per lecturer.

ON HOLIDAYS -- read this before using in production:
I am not hardcoding specific Malaysian public holiday dates into this file.
Gazetted dates shift year to year and by state, and I'm not going to guess
at 2025/2026 dates and have that be silently wrong in a real timetable.
Pass your own verified list of ISO dates (YYYY-MM-DD) as `holidays` --
source it from the Prime Minister's Department federal gazette plus the
Selangor state gazette (UKM Bangi/KL campuses), and re-verify it yourself
before trusting it. Special dates (Convocation, etc.) go in the same list.
"""

from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo
from ics import Calendar, Event

MY_TZ = ZoneInfo("Asia/Kuala_Lumpur")

SLOT_TIMES = {
    "AM1": (time(8, 0), time(10, 0)),
    "AM2": (time(10, 0), time(12, 0)),
    "PM1": (time(14, 0), time(16, 0)),
    "PM2": (time(16, 0), time(18, 0)),
}
DAY_TO_WEEKDAY = {"Mon": 0, "Tue": 1, "Wed": 2, "Thu": 3, "Fri": 4, "Sat": 5}


def _parse_date(d):
    return datetime.strptime(d, "%Y-%m-%d").date()


def _parse_exceptions(exceptions_str):
    """Exceptions column holds comma-separated dates OR 'date:action' pairs,
    e.g. '2025-10-14,2025-11-03:move=2025-11-04 PM1'. For now this generator
    supports the simple case (skip that date); a moved-session action is
    parsed but not yet re-placed -- flagged in the return value so you know
    it needs a manual look rather than silently dropping it."""
    skip_dates = set()
    needs_review = []
    if not exceptions_str:
        return skip_dates, needs_review
    for part in str(exceptions_str).split(","):
        part = part.strip()
        if not part:
            continue
        if ":" in part:
            date_part = part.split(":")[0].strip()
            skip_dates.add(date_part)
            needs_review.append(part)
        else:
            skip_dates.add(part)
    return skip_dates, needs_review


def _weekly_occurrences(start_date, end_date, weekday):
    d = _parse_date(start_date)
    end = _parse_date(end_date)
    while d.weekday() != weekday:
        d += timedelta(days=1)
    while d <= end:
        yield d
        d += timedelta(weeks=1)


def generate_events(assignments, holidays=None):
    """Expand each solver assignment into individual dated events, skipping
    exception dates and holiday dates. Returns (event_records, flags) where
    flags lists any exception entries that need a human look (moved sessions)."""
    holidays = set(holidays or [])
    event_records = []
    flags = []

    for a in assignments:
        weekday = DAY_TO_WEEKDAY[a["day"]]
        skip_dates, needs_review = _parse_exceptions(a.get("exceptions"))
        flags.extend(f"{a['course_code']}: {x}" for x in needs_review)

        start_slot = a["slots"][0]
        end_slot = a["slots"][-1]
        start_t = SLOT_TIMES[start_slot][0]
        end_t = SLOT_TIMES[end_slot][1]

        for occ_date in _weekly_occurrences(a["start_date"], a["end_date"], weekday):
            iso = occ_date.isoformat()
            if iso in skip_dates or iso in holidays:
                continue
            ev = Event()
            group_suffix = f" (Grp {a['group']})" if a.get("group") else ""
            ev.name = f"{a['course_code']}{group_suffix} - {a['venue']}"
            ev.begin = datetime.combine(occ_date, start_t, tzinfo=MY_TZ)
            ev.end = datetime.combine(occ_date, end_t, tzinfo=MY_TZ)
            ev.location = a["venue"]
            ev.description = f"Lecturer: {a['lecturer']} | Cohort: {a['cohort']}"
            event_records.append({
                "event": ev, "cohort": a["cohort"], "lecturer": a["lecturer"]
            })
    return event_records, flags


def write_ics_files(assignments, holidays=None, out_dir="."):
    """Writes one .ics per cohort and one per lecturer. Returns
    (list_of_file_paths, flags_needing_manual_review)."""
    event_records, flags = generate_events(assignments, holidays)

    by_cohort = {}
    by_lecturer = {}
    for rec in event_records:
        by_cohort.setdefault(rec["cohort"], Calendar()).events.add(rec["event"])
        by_lecturer.setdefault(rec["lecturer"], Calendar()).events.add(rec["event"])

    written = []
    for cohort, cal in by_cohort.items():
        path = f"{out_dir}/{cohort}.ics"
        with open(path, "w") as f:
            f.writelines(cal.serialize_iter())
        written.append(path)
    for lect, cal in by_lecturer.items():
        path = f"{out_dir}/{lect}.ics"
        with open(path, "w") as f:
            f.writelines(cal.serialize_iter())
        written.append(path)
    return written, flags
