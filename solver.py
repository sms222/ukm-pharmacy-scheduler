"""
UKM Pharmacy Scheduler - CP-SAT Solver Core
Step 1 of the build: solver logic only. iCal generation and manual
drag-and-drop overrides are separate steps, not covered here.

DESIGN ASSUMPTIONS -- confirm these before I build Step 2 on top:

1. Each course is assigned ONE weekly recurring (day, timeslot, venue)
   pattern for its whole Start Date -> End Date block. Individual date
   exceptions and public holidays are NOT solved here -- they are applied
   later, when generating the iCal feed, as "recurring pattern minus these
   specific dates". This keeps the search fast and matches how a real
   semester timetable actually runs (you don't re-decide the room every week).

2. Timeslots are fixed 2-hour blocks:
      AM1 08:00-10:00   AM2 10:00-12:00   PM1 14:00-16:00   PM2 16:00-18:00
   Friday only offers AM1 and PM2 -- the 12:00-14:00 gap is permanently
   reserved for Friday prayers and is never offered as a candidate slot.
   A course needing more than 2 hours consumes multiple CONSECUTIVE slots
   on the same day (e.g. a 4-hr lab = AM1+AM2).

3. "Fixed" courses (Fixed Day + Fixed Time both set) skip the day search --
   the solver only checks that pinned slot doesn't break a hard constraint
   (double-booking, capacity, blackout). If it does, the model reports
   infeasible rather than silently moving it.

4. A cohort marked "Needs Lab Split" produces two independent
   session-instances (Group A / Group B) for its Lab-type courses, each
   sized at ceil(total_students / 2). The two groups are explicitly
   ALLOWED to run in parallel (different venues, same or different time) --
   only a genuinely different course clashing with the cohort is blocked.
"""

import math
from collections import defaultdict
from ortools.sat.python import cp_model

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
SLOT_ORDER = ["AM1", "AM2", "PM1", "PM2"]
FRIDAY_SLOTS = ["AM1", "PM2"]  # protects the 12:00-14:00 prayer window


def _slots_for_day(day):
    return FRIDAY_SLOTS if day == "Fri" else SLOT_ORDER


def _consecutive_slot_groups(day, n_slots):
    """All valid contiguous slot windows of length n_slots for a given day."""
    avail = _slots_for_day(day)
    groups = []
    for i in range(len(avail) - n_slots + 1):
        window = avail[i:i + n_slots]
        idxs = [SLOT_ORDER.index(s) for s in window]
        if idxs == list(range(idxs[0], idxs[0] + n_slots)):
            groups.append(window)
    return groups


def _expand_lecturer_blackout(row):
    recurring = set()
    val = row.get("Recurring Blackout")
    if isinstance(val, str) and val.strip():
        for d in val.split(","):
            d = d.strip()[:3].title()
            if d in DAYS:
                recurring.add(d)
    one_off = set()
    val2 = row.get("One-off Blackout Dates")
    if isinstance(val2, str) and val2.strip():
        one_off = {d.strip() for d in val2.split(",")}
    return recurring, one_off


def build_sessions(courses_df, cohorts_df):
    """Expand each course row into one or more session-instances
    (handles lab-group splitting)."""
    cohort_size = dict(zip(cohorts_df["Cohort ID"], cohorts_df["Total Students"]))
    needs_split = dict(zip(cohorts_df["Cohort ID"], cohorts_df["Needs Lab Split?"]))

    sessions = []
    for _, c in courses_df.iterrows():
        cohort = c["Cohort ID"]
        size = cohort_size.get(cohort, 0)
        split = bool(needs_split.get(cohort, False)) and c["Session Type"] == "Lab"
        groups = ["A", "B"] if split else [None]

        for g in groups:
            sessions.append({
                "course_code": c["Course Code"],
                "group": g,
                "cohort": cohort,
                "lecturer": c["Lecturer ID"],
                "session_type": c["Session Type"],
                "duration_hrs": c["Duration (Hrs)"],
                "room_type": c["Required Room Type"],
                "start_date": c.get("Start Date"),
                "end_date": c.get("End Date"),
                "depends_on": c.get("Depends On") or None,
                "fixed_day": c.get("Fixed Day") or None,
                "fixed_time": c.get("Fixed Time") or None,
                "exceptions": c.get("Exceptions") or None,
                "size": math.ceil(size / 2) if split else size,
            })
    return sessions


def solve_schedule(venues_df, lecturers_df, cohorts_df, courses_df):
    model = cp_model.CpModel()
    sessions = build_sessions(courses_df, cohorts_df)

    lecturer_blackout = {
        row["Lecturer ID"]: _expand_lecturer_blackout(row)
        for _, row in lecturers_df.iterrows()
    }
    venue_info = {
        row["Room ID"]: {
            "capacity": row["Capacity"],
            "types": {t.strip() for t in str(row["Allowed Session Types"]).split(",")},
        }
        for _, row in venues_df.iterrows()
    }

    # --- Build candidate (day, slot_group, venue) options per session ---
    session_options = {}
    for idx, s in enumerate(sessions):
        n_slots = max(1, math.ceil(s["duration_hrs"] / 2))
        options = []
        candidate_days = [s["fixed_day"]] if s["fixed_day"] else DAYS
        for day in candidate_days:
            if day is None:
                continue
            recurring_bo, _ = lecturer_blackout.get(s["lecturer"], (set(), set()))
            if day in recurring_bo:
                continue
            for slot_group in _consecutive_slot_groups(day, n_slots):
                if s["fixed_time"] and slot_group[0] != s["fixed_time"]:
                    continue
                for room, info in venue_info.items():
                    if info["capacity"] < s["size"]:
                        continue
                    if s["session_type"] not in info["types"]:
                        continue
                    options.append((day, tuple(slot_group), room))
        if not options:
            raise ValueError(
                f"No feasible slot/venue for {s['course_code']} "
                f"(group {s['group']}) -- check capacity/room-type/blackout data."
            )
        session_options[idx] = options

    # --- Decision variables ---
    choice = {}
    for idx, options in session_options.items():
        for opt_i in range(len(options)):
            choice[(idx, opt_i)] = model.NewBoolVar(f"s{idx}_o{opt_i}")
        model.Add(sum(choice[(idx, o)] for o in range(len(options))) == 1)

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
            by_cohort[sessions[idx]["cohort"]].append((choice[(idx, opt_i)], sessions[idx]["course_code"]))

        for vars_ in by_room.values():
            model.Add(sum(vars_) <= 1)
        for vars_ in by_lecturer.values():
            model.Add(sum(vars_) <= 1)
        for pairs in by_cohort.values():
            # same-course parallel groups (A/B) may overlap; different courses may not
            if len(set(p[1] for p in pairs)) > 1:
                model.Add(sum(v for v, _ in pairs) <= 1)

    # --- Sequencing: dependent course must be on a later weekday ---
    code_to_idx = defaultdict(list)
    for idx, s in enumerate(sessions):
        code_to_idx[s["course_code"]].append(idx)
    day_index = {d: i for i, d in enumerate(DAYS)}

    for idx, s in enumerate(sessions):
        if not s["depends_on"]:
            continue
        for p_idx in code_to_idx.get(s["depends_on"], []):
            for opt_i, (day, _, _) in enumerate(session_options[idx]):
                for p_opt_i, (p_day, _, _) in enumerate(session_options[p_idx]):
                    if day_index[day] <= day_index[p_day]:
                        model.AddBoolOr([
                            choice[(idx, opt_i)].Not(),
                            choice[(p_idx, p_opt_i)].Not(),
                        ])

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
                    "course_code": s["course_code"],
                    "group": s["group"],
                    "cohort": s["cohort"],
                    "lecturer": s["lecturer"],
                    "day": day,
                    "slots": slot_group,
                    "venue": room,
                    "start_date": s["start_date"],
                    "end_date": s["end_date"],
                    "exceptions": s["exceptions"],
                })
    return {"status": "FEASIBLE", "assignments": assignments}
