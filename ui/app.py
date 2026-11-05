"""Streamlit UI. Run with:  streamlit run ui/app.py"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # so imports work from ui/

import re

import anthropic
import streamlit as st

import checks
import clock
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


def usd(x: float, digits: int = 2) -> str:
    """A dollar amount for Markdown text and labels. Written as '\\$' so Streamlit doesn't read
    two amounts in one label ('~$0.12, max $0.30') as a LaTeX formula."""
    return f"\\${x:.{digits}f}"


def flag_line(f) -> str:
    who = checks.checker_name(f)
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


# ---------------- Phase 4 helpers ----------------

def local_time(iso: str | None) -> str:
    """A stored UTC time shown in your time zone (config.TIMEZONE)."""
    if not iso:
        return ""
    return clock.from_iso(iso).astimezone(clock.local_tz()).strftime("%b %d, %Y %H:%M")


def anchor(heading: str) -> str:
    """The anchor Streamlit gives a Markdown heading: lower case, words joined by '-'."""
    s = re.sub(r"[^\w\s-]", "", heading.lower()).strip()
    return re.sub(r"[\s_]+", "-", s)


def section_link(section: str) -> str:
    return f"[{section}](#{anchor(section)})" if section else "the lesson"


def key_text(q: dict) -> str:
    """The correct answer as you'd read it."""
    if q["kind"] == "multiple_choice" and q.get("choices"):
        i = ord(q["answer"].strip().upper()[:1] or "A") - 65
        return f"{q['answer']}. {q['choices'][i]}" if 0 <= i < len(q["choices"]) else q["answer"]
    return f"{q['answer']} {q.get('unit') or ''}".strip()


def answer_widget(q: dict, key: str, label: str) -> str:
    """An input that matches the question kind. Returns your answer as text."""
    if q["kind"] == "multiple_choice":
        letters = [chr(65 + j) for j in range(len(q["choices"]))]
        pick = st.radio(label, letters, index=None, key=key,
                        format_func=lambda x: f"{x}. {q['choices'][ord(x) - 65]}")
        return pick or ""
    if q["kind"] == "numeric":
        unit = q.get("unit") or ""
        return st.text_input(label, key=key, placeholder=f"a number in {unit}" if unit else "a number",
                             help=f"A number{', in ' + unit if unit else ''}. Prefixes like k or m are fine.")
    return st.text_input(label, key=key)


def show_missed(missed: list[dict]):
    for x in missed:
        unit = f" {x['unit']}" if x.get("unit") else ""
        right = x["answer"]
        if x.get("choices"):
            i = ord(x["answer"].strip().upper()[:1] or "A") - 65
            right = f"{x['answer']}. {x['choices'][i]}" if 0 <= i < len(x["choices"]) else x["answer"]
        # The question may hold a netlist code block, so it gets its own Markdown call
        # (a code block can't sit inside bold text or a list item).
        st.markdown(f"**Missed:** {x['question']}")
        st.markdown(f"Your answer: {x['given'] or '(blank)'} · Correct: **{right}{unit}**  \n"
                    f"{x['explanation']}  \nRead again: {section_link(x['section'])}")


VERDICT_ICON = {"pass": ":material/check_circle:", "fail": ":material/cancel:", "cant_tell": ":material/help:"}
VERDICT_TEXT = {"pass": ":green[Pass]", "fail": ":red[Fail]", "cant_tell": ":orange[Can't tell]"}
TASK_TEXT = {"passed": "Task passed", "failed": "Task: some items failed", "need_more": "Task: send more evidence",
             "not_submitted": "Task not submitted", "missing": "No task yet"}


def show_key_status(task: dict, passed: bool):
    """Solved problems: was the answer key confirmed by the blind Task solver? The details name
    the expected values, so they are only shown once you have passed the task."""
    status = task.get("key_status")
    if status == "verified":
        st.badge("Answer key verified", icon=":material/verified:", color="green")
    elif status == "unverified":
        st.badge("Answer key unverified", icon=":material/warning:", color="orange")
        st.caption("A blind solver couldn't confirm the expected final values, so a final-value grade may "
                   "be wrong. If you're sure of your answer, use Override with your reason.")
    if status and passed and task.get("key_check"):
        with st.expander("Answer key check"):
            for a in task["key_check"]["attempts"]:
                lines = [f"{c['quantity']}: key {c['key']}, solver {c['solver'] or '(none)'} "
                         f"({'match' if c['match'] else 'differs'})" for c in a.get("comparison", [])]
                lines += a["problems"]
                st.markdown(f"**Draft {a['attempt'] + 1}:**  \n" + ("  \n".join(lines) or "no problems"))
            last = task["key_check"]["attempts"][-1] if task["key_check"]["attempts"] else {}
            if last.get("examiner_working"):
                st.caption("Worked solution (Examiner, last draft):")
                st.markdown(last["examiner_working"])
            if last.get("solver_working"):
                st.caption("Blind solver's working (last draft):")
                st.markdown(last["solver_working"])


def show_dropped(quiz):
    if quiz and quiz["dropped"]:
        with st.expander(f"Dropped questions ({len(quiz['dropped'])})"):
            for q in quiz["dropped"]:
                st.markdown(q["question"])
                solver = f" · Solver: {q['solver_answer']}" if q.get("solver_answer") is not None else ""
                st.caption(f"Key: {q['answer']}{solver} · {q['reason']}. {q['solver_note']}")


def show_history(mid):
    hist = coach.attempt_history(mid)
    if not hist:
        return
    with st.expander(f"Attempt history ({len(hist)})", icon=":material/history:"):
        for h in hist:
            when = local_time(h["at"])
            if h["kind"] == "quiz":
                result = "passed" if h["passed"] else "failed"
                st.markdown(f"**{when} · Quiz {h['score']:.0%} ({result})**")
                if h["missed"]:
                    st.caption("Missed: " + "; ".join(x["question"] for x in h["missed"]))
            elif h["error"]:
                st.markdown(f"**{when} · Task: grading failed** ({h['error']})")
            else:
                counts = {v: sum(g["verdict"] == v for g in h["grades"]) for v in VERDICT_TEXT}
                st.markdown(f"**{when} · Task** ({h['n_images']} image(s)): {counts['pass']} pass, "
                            f"{counts['fail']} fail, {counts['cant_tell']} can't tell")


def show_exam(mid, review):
    """The Examiner gate under the lesson: quiz + practical task."""
    st.subheader("Exam", anchor="exam")
    for err in ("quiz_error", "task_error"):
        if review.get(err):
            st.warning(f"{'Quiz' if err == 'quiz_error' else 'Task'} couldn't be made: {review[err]}",
                       icon=":material/warning:")
    gs = coach.gate_state(mid)
    m = gs["milestone"]

    if gs["passed"]:
        how = {"exam": "by exam", "manual_reason": "with a written reason",
               "manual_pre_phase4": "manually (before Phase 4)"}[m["pass_method"] or "manual_pre_phase4"]
        when = f" on {local_time(m['passed_at'])}" if m["passed_at"] else ""
        st.success(f"You passed this milestone {how}{when}." +
                   (f"  \nReason: {m['pass_reason']}" if m["pass_reason"] else ""), icon=":material/verified:")
        show_history(mid)
        show_dropped(gs["quiz"])
        return
    if gs["disabled"]:
        st.info(gs["disabled"], icon=":material/lock:")
        show_dropped(gs["quiz"])
        return
    if gs["quiz_flagged"]:
        st.warning(f"Only {len(gs['questions'])} question(s) survived the Quiz solver's check.",
                   icon=":material/flag:")
    if gs["needs_prepare"]:
        st.info("This lesson was made before the exam existed. Prepare it first: the Examiner tags the quiz "
                "questions with concepts and writes the practical task.", icon=":material/build:")
        cost = coach.estimate_prepare_cost(m["profile_id"])
        if st.button(f"Prepare exam (~{usd(cost)})", key=f"prep{mid}", icon=":material/build:"):
            with st.spinner("Examiner is preparing the exam..."):
                run_safely(coach.prepare_exam, mid)
            st.rerun()
        show_dropped(gs["quiz"])
        return

    # ---- part 1: quiz ----
    with st.container(border=True):
        st.markdown(f"**1. Quiz** (pass mark {config.QUIZ_PASS_PCT:.0%}, graded instantly, free)")
        attempts = gs["attempts"]
        last = attempts[-1] if attempts else None
        if gs["quiz_status"] == "passed":
            best = max(a["score"] for a in attempts if a["passed"])
            st.success(f"Quiz passed ({best:.0%}).", icon=":material/check_circle:")
            if last and last["passed"] and last["details"].get("missed"):
                with st.expander("Questions you missed (they're in your review now)"):
                    show_missed(last["details"]["missed"])
        elif gs["quiz_status"] == "missing":
            st.warning("No verified quiz questions survived for this milestone, so the exam is the "
                       "practical task alone.", icon=":material/warning:")
        elif gs["quiz_status"] == "needs_new":
            st.error(f"Last try: {last['score']:.0%}. You need {config.QUIZ_PASS_PCT:.0%}. "
                     "These concepts are now in your spaced review.", icon=":material/cancel:")
            show_missed(last["details"].get("missed", []))
            typical, worst = coach.estimate_new_quiz_cost(m["profile_id"])
            if st.button(f"Get a new quiz (~{usd(typical)}, max {usd(worst)})", key=f"newquiz{mid}",
                         icon=":material/refresh:", type="primary"):
                with st.spinner("Examiner is writing new questions, Quiz solver is checking them..."):
                    run_safely(coach.new_quiz, mid)
                st.rerun()
        else:
            with st.form(f"quiz{mid}-{len(attempts)}"):
                given = {}
                for i, q in enumerate(gs["questions"], 1):
                    st.markdown(f"**{i}.** {q['question']}")
                    given[q["id"]] = answer_widget(q, f"qa{mid}-{len(attempts)}-{q['id']}", "Your answer")
                if st.form_submit_button("Submit answers", type="primary"):
                    run_safely(coach.submit_quiz, mid, given)
                    st.rerun()

    # ---- part 2: practical task ----
    task = gs["task"]
    with st.container(border=True):
        st.markdown(f"**2. Practical task** ({TASK_TEXT[gs['task_status']]})")
        if task is None:
            st.warning("There is no task yet.", icon=":material/warning:")
            return
        show_key_status(task, passed=gs["task_status"] == "passed")
        st.markdown(task["instructions"])
        st.markdown("**Rubric** (every item must pass)  \n" +
                    "  \n".join(f"{r['id']}. {r['criterion']} · _needs: {r['evidence']}_" for r in task["rubric"]))
        if task["screenshots"]:
            st.markdown("**Images to send**  \n" + "  \n".join(f"- {s}" for s in task["screenshots"]))

        grades = gs["grades"]
        if grades:
            st.markdown(f"**Grades for your last submission** ({local_time(gs['last_submission']['created_at'])})")
            for g in grades:
                crit = next((r["criterion"] for r in task["rubric"] if r["id"] == g["id"]), g["id"])
                with st.container(horizontal=True, vertical_alignment="center"):
                    st.markdown(f"{VERDICT_ICON[g['verdict']]} **{g['id']}. {crit}**: {VERDICT_TEXT[g['verdict']]}"
                                f"  \n{g['reason']}" + (" _(value checked by the app)_" if g.get("checked_by") == "app" else "")
                                + (f"  \n_Evidence: {g['evidence']}_" if g.get("evidence") else "")
                                + (f"  \n:orange[Send: {g['need']}]" if g["verdict"] == "cant_tell" and g.get("need") else "")
                                + (f"  \n:blue[Overridden by you: {g['override']['reason']}]" if g.get("override") else ""))
                    if g["verdict"] != "pass" and gs["task_status"] != "passed":
                        with st.popover("Override", icon=":material/edit:"):
                            reason = st.text_input("Why does this item pass?", key=f"ovr{mid}-{g['id']}")
                            if st.button("Mark as pass", key=f"ovb{mid}-{g['id']}"):
                                run_safely(coach.override_item, mid, g["id"], reason)
                                st.rerun()
        elif gs["last_submission"] is None and gs["submissions"] and gs["submissions"][-1]["error"]:
            st.warning(f"Grading failed: {gs['submissions'][-1]['error']}", icon=":material/warning:")

        if gs["task_status"] != "passed":
            cost = coach.estimate_grading_cost(m["profile_id"])
            with st.form(f"task{mid}", clear_on_submit=True):
                text = st.text_area("Your answer / notes" if task["screenshots"] else "Your answer",
                                    height=200 if task["proof_type"] != "file" else 100)
                files = st.file_uploader(f"Images: screenshots or photos of your working (up to {config.IMAGE_MAX_COUNT})",
                                         type=["png", "jpg", "jpeg", "heic", "heif"],
                                         help="Phone photos are fine (iPhone HEIC too); they are turned "
                                              "upright automatically. One photo per page of working.",
                                         accept_multiple_files=True)
                if st.form_submit_button(f"Submit for grading (~{usd(cost)})", type="primary"):
                    with st.spinner("Grader is checking each rubric item..."):
                        run_safely(coach.submit_task, mid, text, [f.getvalue() for f in files or []])
                    st.rerun()

    if gs["manual_pass_allowed"]:
        with st.expander("The task can't be graded? Pass with a written reason", icon=":material/gavel:"):
            st.caption("Last resort. Your reason is logged and shown on the milestone.")
            reason = st.text_area("Reason", key=f"mpr{mid}")
            if st.button("Pass this milestone", key=f"mpb{mid}"):
                run_safely(coach.manual_pass, mid, reason)
                st.rerun()
    show_history(mid)
    show_dropped(gs["quiz"])


def show_due_reviews(pid):
    """'Due today' at the top of the learning page: one card at a time."""
    due = coach.due_reviews(pid)
    stats = coach.review_stats(pid)
    if not stats["total_cards"]:
        return
    result = st.session_state.get("review_result")
    with st.container(border=True):
        st.markdown(f"**Due today: {len(due)}** · reviewed today {stats['reviewed_today']} "
                    f"(limit {stats['daily_limit']})")
        if result:
            q = result["question"]
            if result["correct"]:
                st.success(f"Right: {key_text(q)}", icon=":material/check_circle:")
                st.caption("Rated Good. Was it hard or easy to remember? (You can change it today only.)")
                with st.container(horizontal=True):
                    for r in ("hard", "good", "easy"):
                        kind = "primary" if result.get("rating", "good") == r else "secondary"
                        if st.button(r.capitalize(), key=f"adj{r}", type=kind):
                            run_safely(coach.adjust_review, result["log_id"], r)
                            if "error" not in st.session_state:  # run_safely stores errors there
                                result["rating"] = r
                            st.rerun()
            else:
                st.error(f"Not quite. Correct: {key_text(q)}", icon=":material/cancel:")
                st.markdown(f"{q['explanation']}  \nThis comes back tomorrow.")
            if st.button("Next", key="review_next", icon=":material/arrow_forward:"):
                st.session_state.pop("review_result")
                st.rerun()
        elif due:
            item = due[0]
            q, card = item["question"], item["card"]
            mtitle = store.get_milestone(conn, card["milestone_id"])["title"]
            st.caption(f"{mtitle} · {card['concept']}")
            with st.form(f"review{card['id']}-{q['bank_id']}"):
                st.markdown(q["question"])
                given = answer_widget(q, f"ra{card['id']}-{q['bank_id']}", "Your answer")
                if st.form_submit_button("Check", type="primary"):
                    res = run_safely(coach.answer_review, card["id"], q["bank_id"], given)
                    if res:
                        st.session_state.review_result = res
                    st.rerun()
        else:
            st.caption("Nothing left for today.")
        with st.expander("Review stats", icon=":material/insights:"):
            recall = f"{stats['recall_30d']:.0%} of {stats['reviews_30d']}" if stats["recall_30d"] is not None else "no reviews yet"
            st.markdown(f"Cards: **{stats['total_cards']}** · due now: **{stats['due_now']}** · "
                        f"recalled in the last 30 days: **{recall}**")
            if stats["weakest"]:
                st.markdown("Weakest concepts:  \n" + "  \n".join(
                    f"- {w['concept']}: {w['misses']} miss(es), {w['right_share']:.0%} right" for w in stats["weakest"]))


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
                if up["has_data"]:
                    kinds = store.pdf_page_kinds(conn, up["id"])
                    n_vis = sum(kinds.get(k, 0) for k in ("equations", "figure", "scanned"))
                    detail = f"{up['n_pages']} pages, {n_vis} sent as images"
                else:  # uploaded before Phase 5: only its text was kept
                    detail = (f"{up['n_text_pages']} text pages (text only: re-upload to send equations "
                              "and figures as images)")
                c1.caption(f"{up['filename']}: {detail}{rng}")
                if c2.button("", key=f"rmpdf{up['id']}", icon=":material/delete:", help="Remove this PDF"):
                    run_safely(coach.delete_pdf, up["id"])
                    st.rerun()
            with st.form("pdf_upload", clear_on_submit=True):
                pdf_file = st.file_uploader(
                    "Add a PDF", type="pdf",
                    help=f"Pages with equations, figures or no text (scans, handwriting) are sent to Claude as "
                         f"images, at most {config.VISUAL_PDF_MAX_PAGES} per lesson; the rest as text.")
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
    st.metric("Spent this month", f"{usd(month, 4)}")
    st.caption(f"All time: {usd(total, 4)}")
    if month >= config.STOP_USD:
        st.error(f"Stopped: monthly limit {usd(config.STOP_USD, 0)} reached. No new API calls.")
    elif month >= config.WARN_USD:
        st.warning(f"Warning: over {usd(config.WARN_USD, 0)} this month (stop at {usd(config.STOP_USD, 0)}).")

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
        deadline = st.text_input("Deadline (as you described it)", p["deadline"])
        deadline_date = st.date_input("Deadline date (for the on-track check)", value=None)
        practice = st.selectbox("Practice type", practice_opts, index=practice_opts.index(p["practice_type"]))
        proof = st.selectbox("Proof of skill", proof_opts, index=proof_opts.index(p["proof_of_skill"]))
        notes = st.text_area("Notes (units, tools, versions, course conventions)", p["notes"])
        if st.form_submit_button("Confirm and build my plan"):
            coach.confirm_profile(pid, SubjectProfile(
                subject=subject, goal=goal, level=level, hours_per_week=hours, deadline=deadline,
                practice_type=practice, proof_of_skill=proof, notes=notes,
            ).model_dump())
            coach.set_deadline(pid, deadline_date)
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

    # ---- overview: passed, hours (plan estimates), on track vs deadline ----
    ov = coach.subject_overview(pid)
    with st.container(horizontal=True, vertical_alignment="bottom"):
        st.metric("Milestones passed", f"{ov['passed']}/{ov['total']}", border=True)
        st.metric("Hours done (plan estimate)", f"{ov['hours_done']:g} / {ov['hours_total']:g} h", border=True)
        st.metric("Projected finish", ov["projected_finish"].strftime("%b %d, %Y") if ov["projected_finish"]
                  else "—", border=True, help=f"Remaining estimated hours at {ov['hours_per_week']:g} h/week.")
        with st.popover("Deadline", icon=":material/event:"):
            new_deadline = st.date_input("Deadline date", value=ov["deadline"], key="deadline_pick")
            if st.button("Save deadline", key="deadline_save"):
                coach.set_deadline(pid, new_deadline)
                st.rerun()
    if ov["deadline"] is None:
        st.caption("Set a deadline date to see if you're on track.")
    elif ov["on_track"]:
        st.success(f"On track for your deadline ({ov['deadline']:%b %d, %Y}).", icon=":material/trending_up:")
    elif ov["on_track"] is False:
        st.warning(f"Behind: at {ov['hours_per_week']:g} h/week you'd finish {ov['projected_finish']:%b %d, %Y}, "
                   f"after your deadline ({ov['deadline']:%b %d, %Y}).", icon=":material/trending_down:")

    show_due_reviews(pid)

    left, right = st.columns([1, 1])

    with left:
        st.subheader(f"Milestones ({ov['passed']}/{len(milestones)})")
        st.progress(ov["passed"] / len(milestones) if milestones else 0.0)
        in_review = {c["milestone_id"] for c in store.cards(conn, pid)}
        for m in milestones:
            icon = {"done": ":material/check_circle:", "active": ":material/play_circle:",
                    "locked": ":material/lock:"}[m["status"]]
            prog = coach.milestone_progress(m["id"])
            with st.container(border=True):
                st.markdown(f"{icon} **{m['position']}. {m['title']}**  \n{m['objective']}")
                logged = prog["study_minutes"] / 60
                hours = f"~{m['est_hours']:g} h planned" + (f" · {logged:.1f} h logged" if logged else "")
                badges = []
                if m["pass_method"] == "exam":
                    badges.append(f":green-badge[Passed by exam {local_time(m['passed_at'])}]")
                elif m["pass_method"] == "manual_reason":
                    badges.append(f":orange-badge[Passed with a reason {local_time(m['passed_at'])}]")
                elif m["pass_method"] == "manual_pre_phase4":
                    badges.append(":gray-badge[Passed manually (before Phase 4)]")
                if prog["overrides"]:
                    badges.append(f":blue-badge[{len(prog['overrides'])} override(s)]")
                details = []
                if prog["quiz_attempts"]:
                    details.append(f"quiz tries: {prog['quiz_attempts']}, best {prog['best_score']:.0%}")
                if prog["task_submissions"]:
                    details.append(f"{TASK_TEXT[prog['task_status']].lower()} ({prog['task_submissions']} submission(s))")
                st.markdown(" ".join(badges) + ("  \n" if badges else "") + f"_{hours}_" +
                            (f"  \n{' · '.join(details)}" if details else ""))
                if m["pass_reason"]:
                    st.caption(f"Reason: {m['pass_reason']}")
                for o in prog["overrides"]:
                    st.caption(f"Override {o['item_id']} ({o['old_verdict']} → {o['new_verdict']}): {o['reason']}")
                if m["status"] != "locked":
                    with st.container(horizontal=True, vertical_alignment="center"):
                        if st.button("Open lesson", key=f"open{m['id']}"):
                            st.session_state.open_milestone = m["id"]
                        with st.popover("Log time", icon=":material/timer:"):
                            mins = st.number_input("Minutes studied", min_value=5, value=30, step=5,
                                                   key=f"mins{m['id']}")
                            if st.button("Log", key=f"log{m['id']}"):
                                run_safely(coach.log_study, m["id"], mins)
                                st.rerun()
                        if m["pass_method"] == "manual_pre_phase4" and m["id"] not in in_review:
                            if st.button("Add to review", key=f"addrev{m['id']}", icon=":material/repeat:",
                                         help="Tag this milestone's quiz questions and add them to spaced review "
                                              "(no exam needed; one small Examiner call)."):
                                n = run_safely(coach.add_to_review, m["id"])
                                if n is not None:
                                    st.toast(f"Added {n} concept(s) to your review.")
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
            if st.button(f"Regenerate with current sources (~{usd(typical)}, max {usd(worst)})",
                         key=f"regen{mid}", icon=":material/refresh:"):
                with st.spinner("Regenerating and checking (the old lesson stays until the new one is ready)..."):
                    run_safely(coach.request_lesson, mid, True)
                st.rerun()
            if store.lesson_status(lesson) == "unchecked":
                typical, worst = coach.estimate_check_cost(m["profile_id"])
                if st.button(f"Run checks (~{usd(typical)}, max {usd(worst)})", key=f"check{mid}",
                             icon=":material/fact_check:"):
                    with st.spinner("Fact-checker and Pedagogy reviewer are checking..."):
                        run_safely(coach.run_checks, mid)
                    st.rerun()

        review = store.lesson_review(lesson) or {}
        show_status(lesson, review)
        st.markdown(lesson["content"])
        show_review_details(review)
        show_exam(mid, review)

        notes = store.lesson_notes(lesson)
        if notes is not None:
            with st.expander("What this lesson used"):
                used_any = False
                for n in notes.get("pdf", []):
                    if n["type"] == "pdf_pages":
                        used_any = True
                        visual = n.get("visual_pages") or []  # older lessons have no such field
                        as_images = f" (as images: {', '.join(map(str, visual))})" if visual else ""
                        st.markdown(f"PDF **{n['file']}**: pages {', '.join(map(str, n['pages']))}{as_images}")
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
