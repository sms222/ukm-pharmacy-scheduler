"""
UKM Pharmacy Scheduler - CP-SAT Solver Core (v2)

SCHEMA THIS VERSION EXPECTS
----------------------------
semester: {"start": "YYYY-MM-DD", "end": "YYYY-MM-DD"}

venues_df columns:
    Room ID, Capacity, Availability (All Day/AM Only/PM Only/Custom),
    Custom Start, Custom End (only used if Availability == "Custom"),
    Allowed Session Types (comma-separated, matched against Session Type)

lecturers_df columns:
    Lecturer ID, Lecturer Name, Recurring Blackout, One-off Blackout Dates

cohorts_df columns:
    Cohort ID, Total Students

courses_df columns (one row PER SESSION, grouped by Course Code):
    Course Code, Session ID, Session Type, Cohort ID, Lecturer ID,
    Duration (Hrs), Start Date, End Date,
    Requires Group Split, Number of Groups,
    Depends On (a Session ID, optional),
    Chronology ("Fixed" or "Flexible"),
    Position Rule ("None"/"Must be first"/"Must be last"/
                    "Cannot be first"/"Cannot be last") -- only read if Flexible,
    Fixed Day, Fixed Time (only read if Chronology == "Fixed")

DESIGN ASSUMPTIONS (unchanged from v1, still true)
----------------------------------------------------
1. Each session gets ONE weekly recurring (day, timeslot, venue) pattern for
   its Start Date -> End Date block. Public holidays and one-off exceptions
   are applied later, at iCal generation, as "recurring pattern minus these
   dates" -- not solved here.
2. Timeslots are fixed 2-hour blocks: AM1 08-10, AM2 10-12, PM1 14-16, PM2 16-18.
   Friday only offers AM1 and PM2 (12:00-14:00 permanently reserved for
   Friday prayers, never offered as a candidate).
3. "Fixed" sessions (Chronology == Fixed, with Fixed Day + Fixed Time set)
   skip the day/time search -- the solver only validates the pinned slot
   against the other hard constraints.
4. "Position Rule" (Must/Cannot be first/last) is evaluated PER COURSE, among
   that course's own sessions, per week -- e.g. a quiz that can't be the
   earliest session of NFNF1713 that week, regardless of which day the
   lecture lands on.
"""

import math
from collections import defaultdict
from datetime import datetime
from ortools.sat.python import cp_model

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
DAY_INDEX = {d: i for i, d in enumerate(DAYS)}
SLOT_ORDER = ["AM1", "AM2", "PM1", "PM2"]
SLOT_TIMES = {"AM1": (8, 0), "AM2": (10, 0), "PM1": (14, 0), "PM2": (16, 0)}
SLOT_RANGES_MIN = {  # (start_minute, end_minute) from midnight, for overlap checks
    "AM1": (8 * 60, 10 * 60), "AM2": (10 * 60, 12 * 60),
    "PM1": (14 * 60, 16 * 60), "PM2": (16 * 60, 18 * 60),
}
FRIDAY_SLOTS = ["AM1", "PM2"]  # protects the 12:00-14:00 prayer window


def _slot_group_range_min(slot_group):
    start = SLOT_RANGES_MIN[slot_group[0]][0]
    end = SLOT_RANGES_MIN[slot_group[-1]][1]
    return start, end


def _parse_hhmm(s):
    h, m = s.strip().split(":")
    return int(h) * 60 + int(m)


def _slots_for_day(day):
    return FRIDAY_SLOTS if day == "Fri" else SLOT_ORDER


def _consecutive_slot_groups(day, n_slots):
    avail = _slots_for_day(day)
    groups = []
    for i in range(len(avail) - n_slots + 1):
        window = avail[i:i + n_slots]
        idxs = [SLOT_ORDER.index(s) for s in window]
        if idxs == list(range(idxs[0], idxs[0] + n_slots)):
            groups.append(window)
    return groups


def _expand_lecturer_blackout(row):
    """Recurring Blackout supports two entry styles, semicolon-separated:
       'Friday'              -> full-day blackout every Friday
       'Thu 14:00-17:00'     -> only that time window blocked, every Thursday
    Returns (full_day_set, timed_list[(day, start_min, end_min)], one_off_dates)."""
    full_day = set()
    timed = []
    val = row.get("Recurring Blackout")
    if isinstance(val, str) and val.strip():
        for entry in val.split(";"):
            entry = entry.strip()
            if not entry:
                continue
            parts = entry.split(None, 1)
            day = parts[0][:3].title()
            if day not in DAYS:
                continue
            if len(parts) == 1:
                full_day.add(day)
            else:
                try:
                    start_s, end_s = parts[1].split("-")
                    timed.append((day, _parse_hhmm(start_s), _parse_hhmm(end_s)))
                except ValueError:
                    full_day.add(day)  # malformed time -> fail safe to full-day block

    one_off = set()
    val2 = row.get("One-off Blackout Dates")
    if isinstance(val2, str) and val2.strip():
        one_off = {d.strip() for d in val2.split(",")}
    return full_day, timed, one_off


def _venue_windows(availability_df):
    """Room ID -> list of (day_or_None_for_'All', start_min, end_min).
    A room with NO rows here is treated as available all day, every day
    (backward compatible with rooms nobody has restricted yet)."""
    windows = defaultdict(list)
    if availability_df is None or availability_df.empty:
        return windows
    for _, row in availability_df.iterrows():
        room = row["Room ID"]
        day = row.get("Day")
        day = None if (not day or str(day).strip().lower() == "all") else str(day).strip()[:3].title()
        try:
            start_min = _parse_hhmm(str(row["Start Time"]))
            end_min = _parse_hhmm(str(row["End Time"]))
        except Exception:
            continue
        windows[room].append((day, start_min, end_min))
    return windows


def _venue_available(room, day, slot_group, windows):
    if room not in windows or not windows[room]:
        return True  # no restrictions defined -> fully available
    g_start, g_end = _slot_group_range_min(slot_group)
    for w_day, w_start, w_end in windows[room]:
        if w_day is not None and w_day != day:
            continue
        if w_start <= g_start and g_end <= w_end:
            return True
    return False


def _venue_blocked_by_lecturer_time(day, slot_group, timed_blackouts):
    g_start, g_end = _slot_group_range_min(slot_group)
    for b_day, b_start, b_end in timed_blackouts:
        if b_day != day:
            continue
        if g_start < b_end and g_end > b_start:  # overlap
            return True
    return False


def _check_semester_bounds(courses_df, semester):
    sem_start = datetime.strptime(semester["start"], "%Y-%m-%d").date()
    sem_end = datetime.strptime(semester["end"], "%Y-%m-%d").date()
    problems = []
    for _, c in courses_df.iterrows():
        if not c.get("Start Date") or not c.get("End Date"):
            continue  # blank -> will default to semester bounds, always valid
        cs = datetime.strptime(c["Start Date"], "%Y-%m-%d").date()
        ce = datetime.strptime(c["End Date"], "%Y-%m-%d").date()
        if cs < sem_start or ce > sem_end:
            problems.append(
                f"{c['Course Code']} / {c['Session ID']}: "
                f"{c['Start Date']} to {c['End Date']} falls outside "
                f"semester bounds {semester['start']} to {semester['end']}"
            )
    return problems


def build_sessions(courses_df, cohorts_df, semester):
    """Expand each course-session row into one or more instances
    (handles per-session group splitting). Blank Start/End Date fall back
    to the Semester Settings bounds."""
    cohort_size = dict(zip(cohorts_df["Cohort ID"], cohorts_df["Total Students"]))

    sessions = []
    for _, c in courses_df.iterrows():
        cohort = c["Cohort ID"]
        size = cohort_size.get(cohort, 0)
        split = bool(c.get("Requires Group Split", False))
        n_groups = int(c["Number of Groups"]) if split and c.get("Number of Groups") else 1
        groups = [f"Group {i+1}" for i in range(n_groups)] if split and n_groups > 1 else [None]

        chronology = c.get("Chronology", "Flexible")
        start_date = c.get("Start Date") or semester["start"]
        end_date = c.get("End Date") or semester["end"]
        for g in groups:
            sessions.append({
                "session_id": c["Session ID"],
                "course_code": c["Course Code"],
                "group": g,
                "cohort": cohort,
                "lecturer": c["Lecturer ID"],
                "session_type": c["Session Type"],
                "duration_hrs": c["Duration (Hrs)"],
                "start_date": start_date,
                "end_date": end_date,
                "depends_on": c.get("Depends On") or None,
                "chronology": chronology,
                "position_rule": c.get("Position Rule", "None") if chronology == "Flexible" else "None",
                "fixed_day": c.get("Fixed Day") or None if chronology == "Fixed" else None,
                "fixed_time": c.get("Fixed Time") or None if chronology == "Fixed" else None,
                "size": math.ceil(size / n_groups) if split and n_groups > 1 else size,
            })
    return sessions


def solve_schedule(venues_df, availability_df, lecturers_df, cohorts_df, courses_df, semester):
    bound_problems = _check_semester_bounds(courses_df, semester)
    if bound_problems:
        raise ValueError(
            "These sessions fall outside the semester dates:\n" + "\n".join(bound_problems)
        )

    model = cp_model.CpModel()
    sessions = build_sessions(courses_df, cohorts_df, semester)

    lecturer_blackout = {
        row["Lecturer ID"]: _expand_lecturer_blackout(row)
        for _, row in lecturers_df.iterrows()
    }
    venue_info = {}
    for _, row in venues_df.iterrows():
        venue_info[row["Room ID"]] = {
            "capacity": row["Capacity"],
            "types": {t.strip() for t in str(row["Allowed Session Types"]).split(",")},
        }
    venue_windows = _venue_windows(availability_df)

    # --- Candidate (day, slot_group, venue) options per session ---
    session_options = {}
    for idx, s in enumerate(sessions):
        n_slots = max(1, math.ceil(s["duration_hrs"] / 2))
        options = []
        candidate_days = [s["fixed_day"]] if s["fixed_day"] else DAYS
        for day in candidate_days:
            if day is None:
                continue
            full_day_bo, timed_bo, _ = lecturer_blackout.get(s["lecturer"], (set(), [], set()))
            if day in full_day_bo:
                continue
            for slot_group in _consecutive_slot_groups(day, n_slots):
                if s["fixed_time"] and slot_group[0] != s["fixed_time"]:
                    continue
                if _venue_blocked_by_lecturer_time(day, slot_group, timed_bo):
                    continue
                for room, info in venue_info.items():
                    if info["capacity"] < s["size"]:
                        continue
                    if s["session_type"] not in info["types"]:
                        continue
                    if not _venue_available(room, day, slot_group, venue_windows):
                        continue
                    options.append((day, tuple(slot_group), room))
        if not options:
            raise ValueError(
                f"No feasible slot/venue for {s['course_code']} / {s['session_id']} "
                f"(group {s['group']}) -- check capacity/room-type/blackout/availability/fixed-slot data."
            )
        session_options[idx] = options

    # --- Decision variables ---
    choice = {}
    for idx, options in session_options.items():
        for opt_i in range(len(options)):
            choice[(idx, opt_i)] = model.NewBoolVar(f"s{idx}_o{opt_i}")
        model.Add(sum(choice[(idx, o)] for o in range(len(options))) == 1)

    # --- Day-index integer variable per session (for position-rule constraints) ---
    day_idx_var = {}
    for idx, options in session_options.items():
        var = model.NewIntVar(0, len(DAYS) - 1, f"day_idx_{idx}")
        model.Add(var == sum(
            DAY_INDEX[options[o][0]] * choice[(idx, o)] for o in range(len(options))
        ))
        day_idx_var[idx] = var

    # --- No double-booking: venue / lecturer / cohort, per (day, slot) ---
    day_slot_usage = defaultdict(list)
    for idx, options in session_options.items():
        for opt_i, (day, slot_group, room) in enumerate(options):
            for slot in slot_group:
                day_slot_usage[(day, slot)].append((idx, opt_i, room))

    for (day, slot), entries in day_slot_usage.items():
        by_room = defaultdict(list)
        by_lecturer = defaultdict(list)
        by_cohort = defaultdict(list)
        for idx, opt_i, room in entries:
            by_room[room].append(choice[(idx, opt_i)])
            by_lecturer[sessions[idx]["lecturer"]].append(choice[(idx, opt_i)])
            by_cohort[sessions[idx]["cohort"]].append((choice[(idx, opt_i)], sessions[idx]["course_code"], sessions[idx]["group"]))

        for vars_ in by_room.values():
            model.Add(sum(vars_) <= 1)
        for vars_ in by_lecturer.values():
            model.Add(sum(vars_) <= 1)
        for triples in by_cohort.values():
            # same-course parallel groups are allowed to overlap; different courses may not
            courses_here = set(t[1] for t in triples)
            if len(courses_here) > 1:
                model.Add(sum(v for v, _, _ in triples) <= 1)

    # --- Sequencing: depends_on session must be on an earlier weekday ---
    id_to_idx = defaultdict(list)
    for idx, s in enumerate(sessions):
        id_to_idx[s["session_id"]].append(idx)

    for idx, s in enumerate(sessions):
        if not s["depends_on"]:
            continue
        for p_idx in id_to_idx.get(s["depends_on"], []):
            model.Add(day_idx_var[idx] > day_idx_var[p_idx])

    # --- Position rules: evaluated among sessions of the SAME course ---
    by_course = defaultdict(list)
    for idx, s in enumerate(sessions):
        by_course[s["course_code"]].append(idx)

    for course, idxs in by_course.items():
        if len(idxs) < 2:
            continue
        for idx in idxs:
            rule = sessions[idx]["position_rule"]
            siblings = [j for j in idxs if j != idx]
            if rule == "Must be first":
                for j in siblings:
                    model.Add(day_idx_var[idx] <= day_idx_var[j])
            elif rule == "Must be last":
                for j in siblings:
                    model.Add(day_idx_var[idx] >= day_idx_var[j])
            elif rule == "Cannot be first":
                # at least one sibling must be on the same day or earlier
                b_vars = []
                for j in siblings:
                    b = model.NewBoolVar(f"notfirst_{idx}_{j}")
                    model.Add(day_idx_var[j] <= day_idx_var[idx]).OnlyEnforceIf(b)
                    model.Add(day_idx_var[j] > day_idx_var[idx]).OnlyEnforceIf(b.Not())
                    b_vars.append(b)
                model.AddBoolOr(b_vars)
            elif rule == "Cannot be last":
                b_vars = []
                for j in siblings:
                    b = model.NewBoolVar(f"notlast_{idx}_{j}")
                    model.Add(day_idx_var[j] >= day_idx_var[idx]).OnlyEnforceIf(b)
                    model.Add(day_idx_var[j] < day_idx_var[idx]).OnlyEnforceIf(b.Not())
                    b_vars.append(b)
                model.AddBoolOr(b_vars)

    # --- Solve ---
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 30
    status = solver.Solve(model)

    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return {"status": "INFEASIBLE", "assignments": []}

    assignments = []
    for idx, options in session_options.items():
        for opt_i, (day, slot_group, room) in enumerate(options):
            if solver.Value(choice[(idx, opt_i)]):
                s = sessions[idx]
                assignments.append({
                    "session_id": s["session_id"],
                    "course_code": s["course_code"],
                    "group": s["group"],
                    "cohort": s["cohort"],
                    "lecturer": s["lecturer"],
                    "day": day,
                    "slots": slot_group,
                    "venue": room,
                    "start_date": s["start_date"],
                    "end_date": s["end_date"],
                })
    return {"status": "FEASIBLE", "assignments": assignments}
