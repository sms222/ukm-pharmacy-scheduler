import streamlit as st
import pandas as pd
from solver import solve_schedule
from ical_generator import write_ics_files

st.set_page_config(page_title="UKM Pharmacy Scheduler", layout="wide")
st.title("UKM Pharmacy Scheduling Interface")
st.write("Input your constraints below to generate the master timetable.")

tab1, tab2, tab3, tab4, tab5 = st.tabs([
    "Venues", "Lecturers", "Cohorts", "Courses", "Generate Schedule"
])

with tab1:
    st.header("Venue Constraints")
    st.write("Define rooms, capacities, availability, and which session types each room accepts.")
    venues_df = pd.DataFrame({
        "Room ID": ["DK1", "DK4", "BSH3", "MAF"],
        "Capacity": [200, 150, 60, 70],
        "Availability": ["All Day", "AM Only", "PM Preferred", "All Day"],
        "Allowed Session Types": ["Lecture,Workshop", "Lecture,Quiz", "Workshop,Quiz", "Lab,Quiz"],
    })
    venues_df = st.data_editor(venues_df, num_rows="dynamic", key="venues")
    st.caption("Allowed Session Types: comma-separated (e.g. 'Lab,Quiz'). Must match a course's Session Type exactly.")

with tab2:
    st.header("Lecturer Constraints")
    st.write("Add lecturers and their blackout dates — split recurring (weekly) from one-off dates.")
    lec_df = pd.DataFrame({
        "Lecturer ID": ["MMB", "AMR", "CEW"],
        "Full Name": ["Prof. Dr Mohd Makmor Bakry", "Assoc. Prof. Dr Adyani Md Redzuan", "Dr Chua Eng Wee"],
        "Recurring Blackout": ["", "", ""],
        "One-off Blackout Dates": ["", "2025-10-14", ""],
    })
    lec_df = st.data_editor(lec_df, num_rows="dynamic", key="lecturers")
    st.caption("Recurring Blackout: day name(s) e.g. 'Friday'. One-off dates: comma-separated YYYY-MM-DD.")

with tab3:
    st.header("Cohorts (Programs & Years)")
    st.write("Define student groups and sizes.")
    cohort_df = pd.DataFrame({
        "Cohort ID": ["UG_Y1", "UG_Y2", "PG_MClinPharm"],
        "Total Students": [120, 110, 30],
        "Needs Lab Split?": [True, True, False],
    })
    cohort_df = st.data_editor(cohort_df, num_rows="dynamic", key="cohorts")

with tab4:
    st.header("Course Sessions (The Blocks)")
    st.write("Define sessions, their cohort/lecturer, block dates, sequencing, and any fixed weekly slot.")
    course_df = pd.DataFrame({
        "Course Code": ["NFNF1713", "NFNF1713_QUIZ", "NFNF2632", "NFNK6332"],
        "Cohort ID": ["UG_Y1", "UG_Y1", "UG_Y2", "PG_MClinPharm"],
        "Lecturer ID": ["CEW", "CEW", "MMB", "AMR"],
        "Session Type": ["Lab", "Quiz", "Lecture", "Workshop"],
        "Duration (Hrs)": [3, 1, 2, 2],
        "Required Room Type": ["Wet Lab", "Quiz", "Lecture Hall", "Seminar Room"],
        "Start Date": ["2025-09-01"] * 4,
        "End Date": ["2025-11-01"] * 4,
        "Depends On": ["", "NFNF1713", "", ""],
        "Fixed Day": ["", "Fri", "", ""],
        "Fixed Time": ["", "AM1", "", ""],
        "Exceptions": ["", "", "", ""],
    })
    course_df = st.data_editor(course_df, num_rows="dynamic", key="courses")
    st.caption(
        "Fixed Day/Time: leave blank to let the solver choose. Timeslots: AM1 08-10, "
        "AM2 10-12, PM1 14-16, PM2 16-18. Depends On: course code that must come first "
        "in the same week (e.g. quiz depends on its lecture). "
        "Exceptions: comma-separated YYYY-MM-DD dates to skip for this course only."
    )

with tab5:
    st.header("Generate & Export")
    st.write("Once your data is locked in, run the OR-Tools solver.")

    holidays_text = st.text_area(
        "Holiday dates to skip (comma-separated YYYY-MM-DD)",
        help="Verify these against the official federal + Selangor state gazette before relying on them — "
             "this list is not auto-populated.",
    )
    holidays = [d.strip() for d in holidays_text.split(",") if d.strip()]

    if st.button("Run OR-Tools Solver", type="primary"):
        try:
            result = solve_schedule(venues_df, lec_df, cohort_df, course_df)
        except ValueError as e:
            st.error(f"Could not build a schedule: {e}")
            result = None

        if result and result["status"] == "FEASIBLE":
            st.success(f"✅ Schedule generated — {len(result['assignments'])} weekly session patterns placed.")
            st.session_state["assignments"] = result["assignments"]
            st.dataframe(pd.DataFrame(result["assignments"]))
        elif result:
            st.error("⚠️ No feasible schedule found with the current constraints. Check capacity, room types, and blackout dates for conflicts.")

    st.divider()
    st.write("Export the generated schedule:")

    if "assignments" in st.session_state:
        if st.button("📥 Generate .ics Feeds (per cohort & lecturer)"):
            written, flags = write_ics_files(st.session_state["assignments"], holidays=holidays, out_dir="/tmp")
            st.success(f"Generated {len(written)} .ics files.")
            if flags:
                st.warning("These exception entries need a manual look (moved sessions aren't auto-replaced yet): " + "; ".join(flags))
            for path in written:
                with open(path, "rb") as f:
                    st.download_button(f"Download {path.split('/')[-1]}", f, file_name=path.split("/")[-1])
    else:
        st.info("Run the solver first to enable export.")
