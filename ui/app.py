"""Streamlit UI. Run with:  streamlit run ui/app.py"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # so imports work from ui/

import anthropic
import streamlit as st

import config
from agents.scoper import SubjectProfile
from db import store
from llm import LLM, BudgetExceeded, LLMError, RefusalError
from orchestrator import Coach, PlanningFailed

st.set_page_config(page_title="Learning Coach", layout="wide")

conn = store.connect(config.DB_PATH)
coach = Coach(conn, LLM(conn))

KNOWN_ERRORS = (BudgetExceeded, RefusalError, LLMError, PlanningFailed, ValueError, anthropic.APIError)


def run_safely(fn, *args):
    """Call fn; show a red error box instead of crashing. Returns None on error."""
    try:
        return fn(*args)
    except KNOWN_ERRORS as e:
        st.session_state.error = str(e)
        return None


def show_chat(profile_id, stage=None):
    for row in store.get_chat(conn, profile_id, stage):
        with st.chat_message(row["role"]):
            st.markdown(row["content"])


# ---------------- sidebar ----------------
with st.sidebar:
    st.title("Learning Coach")

    profiles = store.list_profiles(conn)
    labels = {p["id"]: p["subject"] or f"New subject #{p['id']}" for p in profiles}
    if st.button("+ New subject", use_container_width=True):
        st.session_state.profile_id = coach.new_subject()
        st.session_state.open_milestone = None
        st.rerun()
    if profiles:
        ids = list(labels)
        current = st.session_state.get("profile_id", ids[-1])
        pid = st.selectbox("Subject", ids, index=ids.index(current) if current in ids else len(ids) - 1,
                           format_func=lambda i: labels[i])
        if pid != st.session_state.get("profile_id"):
            st.session_state.open_milestone = None
            st.session_state.confirm_delete = None
        st.session_state.profile_id = pid

        # Delete needs two clicks: "Delete subject", then "Yes, delete".
        if st.session_state.get("confirm_delete") != pid:
            if st.button("Delete subject", key="delete", use_container_width=True):
                st.session_state.confirm_delete = pid
                st.rerun()
        else:
            st.warning(f"Delete **{labels[pid]}**? Its plan, lessons and chat will be removed. "
                       "Spend history is kept.")
            c1, c2 = st.columns(2)
            if c1.button("Yes, delete", key="delete_yes", type="primary"):
                coach.delete_subject(pid)
                for k in ("profile_id", "open_milestone", "confirm_delete"):
                    st.session_state.pop(k, None)
                st.rerun()
            if c2.button("Cancel", key="delete_cancel"):
                st.session_state.confirm_delete = None
                st.rerun()

    st.divider()
    month, total = store.month_spend(conn), store.total_spend(conn)
    st.metric("Spent this month", f"${month:.4f}")
    st.caption(f"All time: ${total:.4f}")
    if month >= config.STOP_USD:
        st.error(f"Stopped: monthly limit ${config.STOP_USD:.0f} reached. No new API calls.")
    elif month >= config.WARN_USD:
        st.warning(f"Warning: over ${config.WARN_USD:.0f} this month (stop at ${config.STOP_USD:.0f}).")

# ---------------- main ----------------
if st.session_state.get("error"):
    st.error(st.session_state.pop("error"))

pid = st.session_state.get("profile_id")
if pid is None:
    st.info("Click **+ New subject** in the sidebar to start.")
    st.stop()

stage = coach.stage(pid)

# --- scoping: chat with the Scoper ---
if stage == "scoping":
    st.subheader("Let's scope your subject")
    show_chat(pid)
    if text := st.chat_input("Your answer"):
        with st.spinner("Thinking..."):
            run_safely(coach.handle_message, pid, text)
        st.rerun()

# --- confirm_profile: editable form ---
elif stage == "confirm_profile":
    st.subheader("Check your profile")
    p = store.get_profile_data(conn, pid)
    practice_opts = ["software", "math", "language", "hands-on"]
    proof_opts = ["file", "solved_problem", "explain_back"]
    with st.form("profile"):
        subject = st.text_input("Subject", p["subject"])
        goal = st.text_area("Goal", p["goal"])
        level = st.text_input("Current level", p["level"])
        hours = st.number_input("Hours per week", min_value=0.5, value=float(p["hours_per_week"]), step=0.5)
        deadline = st.text_input("Deadline", p["deadline"])
        practice = st.selectbox("Practice type", practice_opts, index=practice_opts.index(p["practice_type"]))
        proof = st.selectbox("Proof of skill", proof_opts, index=proof_opts.index(p["proof_of_skill"]))
        notes = st.text_area("Notes (units, tools, versions)", p["notes"])
        if st.form_submit_button("Confirm and build my plan"):
            coach.confirm_profile(pid, SubjectProfile(
                subject=subject, goal=goal, level=level, hours_per_week=hours, deadline=deadline,
                practice_type=practice, proof_of_skill=proof, notes=notes,
            ).model_dump())
            with st.spinner("Planner is writing your milestones..."):
                run_safely(coach.make_plan, pid)
            st.rerun()
    with st.expander("Scoping conversation"):
        show_chat(pid)

# --- planning: build (or retry) the plan ---
elif stage == "planning":
    st.subheader("Build your plan")
    st.write("Your profile is saved. The Planner will now write 4-8 milestones.")
    if st.button("Build my plan / Try again", type="primary"):
        with st.spinner("Planner is writing your milestones..."):
            run_safely(coach.make_plan, pid)
        st.rerun()

# --- learning: milestones + lesson + chat ---
elif stage == "learning":
    milestones = store.get_milestones(conn, pid)
    done = sum(1 for m in milestones if m["status"] == "done")
    left, right = st.columns([1, 1])

    with left:
        st.subheader(f"Milestones ({done}/{len(milestones)})")
        st.progress(done / len(milestones) if milestones else 0.0)
        for m in milestones:
            icon = {"done": "✅", "active": "▶️", "locked": "🔒"}[m["status"]]
            with st.container(border=True):
                st.markdown(f"{icon} **{m['position']}. {m['title']}**  \n{m['objective']}  \n"
                            f"_~{m['est_hours']:g} h_")
                if m["status"] != "locked":
                    c1, c2 = st.columns(2)
                    if c1.button("Open lesson", key=f"open{m['id']}"):
                        st.session_state.open_milestone = m["id"]
                    ticked = c2.checkbox("Mark done", value=m["status"] == "done",
                                         disabled=m["status"] == "done", key=f"done{m['id']}")
                    if ticked and m["status"] == "active":
                        coach.complete_milestone(m["id"])
                        st.rerun()

    with right:
        st.subheader("Chat with your Coach")
        box = st.container(height=500)
        with box:
            show_chat(pid, "learning")
        if text := st.chat_input("Ask the Coach"):
            with st.spinner("Thinking..."):
                run_safely(coach.handle_message, pid, text)
            st.rerun()

    mid = st.session_state.get("open_milestone")
    if mid:
        st.divider()
        m = store.get_milestone(conn, mid)
        st.subheader(f"Lesson: {m['title']}")
        if store.get_lesson(conn, mid) is None:
            with st.spinner("Teacher is writing this lesson (can take a minute)..."):
                lesson = run_safely(coach.request_lesson, mid)
            if lesson is None:
                st.session_state.open_milestone = None
                st.rerun()
        lesson = store.get_lesson(conn, mid)
        if not lesson["verified"]:
            st.warning("⚠️ Unverified: written from the model's own knowledge, not yet fact-checked. "
                       "Double-check important details.")
        st.markdown(lesson["content"])
