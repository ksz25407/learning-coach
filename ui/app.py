"""Streamlit UI. Run with:  streamlit run ui/app.py"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # so imports work from ui/

import anthropic
import streamlit as st

import config
from agents.curator import is_long
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


def flag_line(f) -> str:
    who = "Fact-checker" if f["checker"] == "fact" else "Pedagogy reviewer"
    return f"**{who}** ({f['kind']}): “{f['quote']}”  \n{f['problem']}  \n_Evidence: {f['evidence']}_"


def show_status(lesson, review):
    """Status box above the lesson. Flagged lessons list their remaining problems on top."""
    status = store.lesson_status(lesson)
    if status == "verified":
        st.success("Every section was checked against its sources and passed.", title="Verified",
                   icon=":material/verified:")
    elif status == "partly_unsourced":
        st.info("Passed the checks. Sections marked [Source: model] have no source: they were "
                "checked against the Fact-checker's own knowledge only.",
                title="Checked (partly unsourced)", icon=":material/rule:")
    elif status == "flagged":
        rounds = review.get("revisions", 0)
        problems = "\n\n".join(flag_line(f) for f in review.get("remaining_major", []))
        st.error(f"Still has problems after {rounds} rewrite(s). Read these parts with care:\n\n{problems}",
                 title="Flagged", icon=":material/flag:")
    else:
        why = f" The checks stopped: {review['error']}" if review.get("error") else ""
        st.warning(f"Not checked yet. Use 'Run checks' to fact-check it.{why}", title="Unchecked",
                   icon=":material/warning:")


def show_review_details(review):
    if not review.get("rounds"):
        return
    with st.expander(f"Review details ({len(review.get('minor', []))} minor notes, "
                     f"{review.get('revisions', 0)} rewrite(s))"):
        for f in review.get("minor", []):
            st.markdown("- " + flag_line(f).replace("  \n", " · "))
        for r in review["rounds"]:
            majors = [f for f in r["flags"] if f["severity"] == "major"]
            st.markdown(f"**Round {r['round'] + 1}:** {len(majors)} major, "
                        f"{len(r['flags']) - len(majors)} minor flag(s)")


def show_quiz(quiz, review):
    if review.get("quiz_error"):
        st.warning(f"No quiz: {review['quiz_error']}", icon=":material/quiz:")
    if quiz is None:
        return
    st.markdown("#### Quick quiz")
    if quiz["flagged"]:
        st.warning(f"Only {len(quiz['questions'])} question(s) survived the Quiz solver's check.",
                   icon=":material/flag:")
    for i, q in enumerate(quiz["questions"], 1):
        st.markdown(f"**{i}. {q['question']}**")
        if q["kind"] == "multiple_choice":
            st.markdown("  \n".join(f"{chr(65 + j)}. {c}" for j, c in enumerate(q["choices"])))
        elif q["kind"] == "numeric" and q.get("unit"):
            st.caption(f"Answer in {q['unit']}")
    if quiz["questions"]:
        with st.expander("Show answers"):
            for i, q in enumerate(quiz["questions"], 1):
                unit = f" {q['unit']}" if q.get("unit") else ""
                st.markdown(f"**{i}.** {q['answer']}{unit}: {q['explanation']}")
    if quiz["dropped"]:
        with st.expander(f"Dropped questions ({len(quiz['dropped'])})"):
            for q in quiz["dropped"]:
                st.markdown(f"- {q['question']}  \n  Key: {q['answer']} · Solver: {q['solver_answer']} "
                            f"· {q['reason']}. {q['solver_note']}")


# ---------------- sidebar ----------------
with st.sidebar:
    st.title("Learning Coach")

    profiles = store.list_profiles(conn)
    labels = {p["id"]: p["subject"] or f"New subject #{p['id']}" for p in profiles}
    if st.button("+ New subject", width="stretch"):
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
            if st.button("Delete subject", key="delete", width="stretch"):
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

        # PDFs for this subject. They beat web pages and model knowledge in lessons.
        with st.expander("PDF sources", icon=":material/picture_as_pdf:"):
            for up in store.list_pdfs(conn, pid):
                c1, c2 = st.columns([4, 1], vertical_alignment="center")
                rng = ""
                if up["page_from"] or up["page_to"]:
                    rng = f", pages {up['page_from'] or 1}-{up['page_to'] or up['n_pages']}"
                c1.caption(f"{up['filename']}: {up['n_text_pages']} text pages{rng}")
                if c2.button("", key=f"rmpdf{up['id']}", icon=":material/delete:", help="Remove this PDF"):
                    store.delete_pdf(conn, up["id"])
                    st.rerun()
            with st.form("pdf_upload", clear_on_submit=True):
                pdf_file = st.file_uploader("Add a PDF", type="pdf")
                c1, c2 = st.columns(2)
                p_from = c1.number_input("From page", min_value=1, value=None, step=1,
                                         help="Optional: only search these pages")
                p_to = c2.number_input("To page", min_value=1, value=None, step=1)
                if st.form_submit_button("Upload") and pdf_file is not None:
                    warning = run_safely(coach.upload_pdf, pid, pdf_file.name, pdf_file.getvalue(),
                                         p_from, p_to)
                    if warning:
                        st.session_state.pdf_warning = warning
                    st.rerun()
            if warning := st.session_state.pop("pdf_warning", None):
                st.warning(warning)

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
            with st.spinner("Curator is searching, Teacher is writing, checkers are reviewing "
                            "(can take a few minutes)..."):
                lesson = run_safely(coach.request_lesson, mid)
            if lesson is None:
                st.session_state.open_milestone = None
                st.rerun()
        lesson = store.get_lesson(conn, mid)

        # Curator's picks (videos included: shown to you, not taught from).
        resources = [r for r in store.get_resources(conn, mid) if r["role"] == "primary"]
        if resources:
            with st.container(border=True):
                st.markdown("**Recommended resources**")
                for r in resources:
                    long = " :orange-badge[Over 30 min]" if is_long(r) else ""
                    st.markdown(f"[{r['title']}]({r['url']}) · {r['kind']} · ~{r['minutes']:g} min{long}  \n"
                                f"_{r['section']}_  \n{r['why_best']}")

        with st.container(horizontal=True, vertical_alignment="center"):
            if store.lesson_is_outdated(conn, mid):
                st.badge("Outdated: written before current sources", icon=":material/history:", color="orange")
            typical, worst = coach.estimate_lesson_cost(m["profile_id"])
            if st.button(f"Regenerate with current sources (~${typical:.2f}, max ${worst:.2f})",
                         key=f"regen{mid}", icon=":material/refresh:"):
                with st.spinner("Regenerating and checking (the old lesson stays until the new one is ready)..."):
                    run_safely(coach.request_lesson, mid, True)
                st.rerun()
            if store.lesson_status(lesson) == "unchecked":
                typical, worst = coach.estimate_check_cost(m["profile_id"])
                if st.button(f"Run checks (~${typical:.2f}, max ${worst:.2f})", key=f"check{mid}",
                             icon=":material/fact_check:"):
                    with st.spinner("Fact-checker and Pedagogy reviewer are checking..."):
                        run_safely(coach.run_checks, mid)
                    st.rerun()

        review = store.lesson_review(lesson) or {}
        show_status(lesson, review)
        st.markdown(lesson["content"])
        show_review_details(review)
        show_quiz(store.get_quiz(conn, mid), review)

        notes = store.lesson_notes(lesson)
        if notes is not None:
            with st.expander("What this lesson used"):
                used_any = False
                for n in notes.get("pdf", []):
                    if n["type"] == "pdf_pages":
                        used_any = True
                        st.markdown(f"PDF **{n['file']}**: pages {', '.join(map(str, n['pages']))}")
                    else:
                        st.markdown(f"PDF: {n['reason']}")
                for n in notes.get("web", []):
                    if n["type"] == "web_used":
                        used_any = True
                        backup = " (backup)" if n.get("role") == "backup" else ""
                        st.markdown(f"Web: [{n['title']}]({n['url']}){backup}")
                    else:
                        st.markdown(f"Skipped {n['url']}: {n['reason']}")
                cur = notes.get("curator", {})
                for url in cur.get("dropped_urls", []):
                    st.markdown(f"Dropped {url}: the Curator named it, but it wasn't in the search results")
                for err in cur.get("search_errors", []):
                    st.markdown(f"Web search error: {err}")
                if not used_any:
                    st.markdown("No source was usable, so this lesson comes from model knowledge.")
