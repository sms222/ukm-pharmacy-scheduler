import streamlit as st
import pandas as pd
import holidays as holidays_lib
from datetime import datetime
from solver_v2 import solve_schedule
from ical_generator import write_ics_files, expand_blockout_periods

st.set_page_config(page_title="UKM Pharmacy Scheduler", layout="wide")
st.title("UKM Pharmacy Scheduling Interface")
st.write("Input your constraints below to generate the master timetable.")

STATE_CODES = {
    "Selangor": "10",
    "Kuala Lumpur": "14",
    "Putrajaya": "16",
}

tabs = st.tabs([
    "Semester Settings", "Session Types", "Venues", "Lecturers",
    "Cohorts", "Courses", "Block Outs", "Generate Schedule"
])
(tab_sem, tab_types, tab_venues, tab_lect, tab_cohort, tab_courses, tab_blockout, tab_gen) = tabs

# ---------------------------------------------------------------- Semester
with tab_sem:
    st.header("Semester Settings")
    st.write("This is the calendar boundary — every course session must fall within it.")
    col1, col2 = st.columns(2)
    with col1:
        sem_start = st.date_input("Semester Start", value=datetime(2025, 9, 1))
    with col2:
        sem_end = st.date_input("Semester End", value=datetime(2026, 1, 31))
    semester = {"start": sem_start.isoformat(), "end": sem_end.isoformat()}
    st.session_state["semester"] = semester

# ---------------------------------------------------------------- Session Types
with tab_types:
    st.header("Session Types")
    st.write("The master list — Venues and Courses both pick from here.")
    session_types_df = pd.DataFrame({
        "Session Type": ["Lecture", "Lab", "Quiz", "Workshop", "Tutorial", "Clinical", "Case Study"],
    })
    session_types_df = st.data_editor(session_types_df, num_rows="dynamic", key="session_types")
    valid_types = set(session_types_df["Session Type"].dropna())

# ---------------------------------------------------------------- Venues
with tab_venues:
    st.header("Venue Constraints")
    st.write("Define rooms, capacities, availability, and which session types each room accepts.")
    venues_df = pd.DataFrame({
        "Room ID": ["DK1", "DK4", "BSH3", "MAF"],
        "Capacity": [200, 150, 60, 70],
        "Availability": ["All Day", "AM Only", "PM Preferred", "All Day"],
        "Allowed Session Types": ["Lecture,Workshop", "Lecture,Quiz", "Workshop,Quiz", "Lab,Quiz"],
    })
    venues_df = st.data_editor(
        venues_df, num_rows="dynamic", key="venues",
        column_config={
            "Availability": st.column_config.SelectboxColumn(
                options=["All Day", "AM Only", "PM Only", "Custom"]
            ),
        },
    )
    st.caption(
        "Allowed Session Types: comma-separated, must match names in the Session Types tab "
        f"({', '.join(sorted(valid_types))})."
    )

# ---------------------------------------------------------------- Lecturers
with tab_lect:
    st.header("Lecturer Constraints")
    st.write("Blackout dates — split recurring (weekly) from one-off dates.")
    lecturer_seed = {
        "MMB": "Prof. Dr Mohd Makmor Bakry", "AMR": "Assoc. Prof. Dr Adyani Md Redzuan",
        "CEW": "Dr Chua Eng Wee", "NSY": "Dr. Nor Syafinaz Yaakob",
        "SDY": "Dr. Syaratul Dalina Yusoff", "HA": "Dr. Hanisah Azhari",
        "KH": "PM Dr. Khairana Husain", "TMTM": "Dr. Tuan Mazlelaa Tuan Mahmood",
        "ZAZ": "Dr. Zainol Akbar Zainal", "SMS": "Dr. Shamin Mohd Saffian",
        "MHZ": "PM Dr. Mohd Hanif Zulfakar", "NSF": "Dr. Nor Syafinaz Yaakob",
        "LKW": "PM Dr. Lam Kok Wai", "JJ": "PM Dr. Juriyati Jalil",
        "NMF": "PM Dr. Norsyahida Mohd Fauzi", "NVM": "Dr. Nur Vaizura Mohamad",
        "HK": "Prof. Dr. Haliza Katas",
    }
    lec_df = pd.DataFrame({
        "Lecturer ID": list(lecturer_seed.keys()),
        "Lecturer Name": list(lecturer_seed.values()),
        "Recurring Blackout": [""] * len(lecturer_seed),
        "One-off Blackout Dates": [""] * len(lecturer_seed),
    })
    lec_df = st.data_editor(lec_df, num_rows="dynamic", key="lecturers")
    if lec_df["Lecturer Name"].duplicated().any():
        dupes = lec_df[lec_df["Lecturer Name"].duplicated(keep=False)]
        st.warning(
            "Same name under two different Lecturer IDs — the solver treats these as "
            "DIFFERENT people and won't catch a double-booking between them:\n\n"
            + dupes[["Lecturer ID", "Lecturer Name"]].to_string(index=False)
        )
    st.caption("Recurring Blackout: day name(s) e.g. 'Friday'. One-off dates: comma-separated YYYY-MM-DD.")

# ---------------------------------------------------------------- Cohorts
with tab_cohort:
    st.header("Cohorts (Programs & Years)")
    cohort_df = pd.DataFrame({
        "Cohort ID": ["UG_Y1", "UG_Y2", "PG_MClinPharm"],
        "Total Students": [120, 110, 30],
    })
    cohort_df = st.data_editor(cohort_df, num_rows="dynamic", key="cohorts")

# ---------------------------------------------------------------- Courses
with tab_courses:
    st.header("Courses — enter by course, session by session")
    st.write("Pick a course code (or create one), then define its sessions below.")

    if "course_sessions" not in st.session_state:
        st.session_state["course_sessions"] = pd.DataFrame({
            "Course Code": ["NFNF1713", "NFNF1713", "NFNF1713"],
            "Session ID": ["LEC1", "LAB1", "QUIZ1"],
            "Session Type": ["Lecture", "Lab", "Quiz"],
            "Cohort ID": ["UG_Y1", "UG_Y1", "UG_Y1"],
            "Lecturer ID": ["CEW", "CEW", "CEW"],
            "Duration (Hrs)": [2, 3, 1],
            "Start Date": ["2025-09-01"] * 3,
            "End Date": ["2025-11-01"] * 3,
            "Requires Group Split": [False, True, False],
            "Number of Groups": [1, 2, 1],
            "Depends On": ["", "", "LEC1"],
            "Chronology": ["Flexible", "Flexible", "Flexible"],
            "Position Rule": ["None", "Cannot be first", "Cannot be first"],
            "Fixed Day": ["", "", ""],
            "Fixed Time": ["", "", ""],
        })

    full_df = st.session_state["course_sessions"]
    existing_codes = sorted(full_df["Course Code"].dropna().unique().tolist())
    pick = st.selectbox("Course Code", existing_codes + ["+ New Course Code"])
    if pick == "+ New Course Code":
        course_code = st.text_input("New course code")
    else:
        course_code = pick

    if course_code:
        filtered = full_df[full_df["Course Code"] == course_code].copy()
        if filtered.empty:
            filtered = pd.DataFrame({col: [] for col in full_df.columns})

        edited = st.data_editor(
            filtered, num_rows="dynamic", key=f"sessions_{course_code}",
            column_config={
                "Chronology": st.column_config.SelectboxColumn(options=["Fixed", "Flexible"]),
                "Position Rule": st.column_config.SelectboxColumn(
                    options=["None", "Must be first", "Must be last", "Cannot be first", "Cannot be last"]
                ),
                "Fixed Day": st.column_config.SelectboxColumn(options=["", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]),
                "Fixed Time": st.column_config.SelectboxColumn(options=["", "AM1", "AM2", "PM1", "PM2"]),
            },
        )
        edited["Course Code"] = course_code
        st.session_state["course_sessions"] = pd.concat(
            [full_df[full_df["Course Code"] != course_code], edited], ignore_index=True
        )

    st.caption(
        "Depends On: another Session ID within this SAME course that must come first. "
        "Position Rule only applies when Chronology = Flexible. "
        "Fixed Day/Time only applies when Chronology = Fixed."
    )
    with st.expander("View all courses (read-only)"):
        st.dataframe(st.session_state["course_sessions"])

    course_df = st.session_state["course_sessions"]

# ---------------------------------------------------------------- Block Outs
with tab_blockout:
    st.header("Block Outs")

    st.subheader("Public Holidays")
    state_choice = st.selectbox("State / Territory (for holiday gazette)", list(STATE_CODES.keys()))

    if "ph_df" not in st.session_state:
        st.session_state["ph_df"] = pd.DataFrame({"Date": [], "Holiday": []})

    if st.button("Import Malaysian Public Holidays for this Semester"):
        sem = st.session_state.get("semester", semester)
        start_y = datetime.fromisoformat(sem["start"]).year
        end_y = datetime.fromisoformat(sem["end"]).year
        years = list(range(start_y, end_y + 1))
        my_holidays = holidays_lib.Malaysia(years=years, subdiv=STATE_CODES[state_choice])
        rows = [
            {"Date": d.isoformat(), "Holiday": name}
            for d, name in sorted(my_holidays.items())
            if sem["start"] <= d.isoformat() <= sem["end"]
        ]
        st.session_state["ph_df"] = pd.DataFrame(rows) if rows else pd.DataFrame({"Date": [], "Holiday": []})
        st.success(f"Imported {len(rows)} holiday date(s) for {state_choice}, within your semester dates.")

    st.caption(
        "Sourced from the `holidays` Python library (Holidays Act 1951 gazette). "
        "Review and delete any rows you don't want before generating the calendar feed — "
        "always verify against the official gazette before relying on this for a real semester."
    )
    ph_df = st.data_editor(st.session_state["ph_df"], num_rows="dynamic", key="ph_editor")
    st.session_state["ph_df"] = ph_df

    st.divider()
    st.subheader("Other Block Out Periods")
    st.write("Faculty meetings, convocation, and similar — supports multi-day ranges.")
    if "blockout_periods" not in st.session_state:
        st.session_state["blockout_periods"] = pd.DataFrame({
            "Name": ["Mesyuarat Fakulti", "Convocation"],
            "Start Date": ["2025-11-05", "2025-10-20"],
            "End Date": ["2025-11-05", "2025-10-22"],
        })
    st.session_state["blockout_periods"] = st.data_editor(
        st.session_state["blockout_periods"], num_rows="dynamic", key="periods_editor"
    )

# ---------------------------------------------------------------- Generate
with tab_gen:
    st.header("Generate & Export")

    if st.button("Run OR-Tools Solver", type="primary"):
        try:
            result = solve_schedule(
                venues_df, lec_df, cohort_df, course_df, st.session_state["semester"]
            )
        except ValueError as e:
            st.error(f"Could not build a schedule: {e}")
            result = None

        if result and result["status"] == "FEASIBLE":
            st.success(f"✅ Schedule generated — {len(result['assignments'])} weekly session patterns placed.")
            st.session_state["assignments"] = result["assignments"]
            st.dataframe(pd.DataFrame(result["assignments"]))
        elif result:
            st.error("⚠️ No feasible schedule found. Check capacity, room types, blackout dates, and fixed slots for conflicts.")

    st.divider()
    st.write("Export the generated schedule:")

    if "assignments" in st.session_state:
        if st.button("📥 Generate .ics Feeds (per cohort & lecturer)"):
            blockout_dates = set(st.session_state["ph_df"]["Date"].dropna().tolist())
            blockout_dates |= expand_blockout_periods(st.session_state["blockout_periods"])
            written, flags = write_ics_files(
                st.session_state["assignments"], holidays=blockout_dates, out_dir="/tmp"
            )
            st.success(f"Generated {len(written)} .ics files ({len(blockout_dates)} block-out dates applied).")
            if flags:
                st.warning("Needs manual review: " + "; ".join(flags))
            for path in written:
                with open(path, "rb") as f:
                    st.download_button(f"Download {path.split('/')[-1]}", f, file_name=path.split("/")[-1])
    else:
        st.info("Run the solver first to enable export.")

# ---------------------------------------------------------------- Footer
st.divider()
st.caption(f"Developed by Shamin Mohd Saffian · © {datetime.now().year} UKM Faculty of Pharmacy")
