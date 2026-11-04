"""Coach: a simple state machine that routes each step to the right agent.

Stages for each subject (profile):
    scoping -> confirm_profile -> planning -> learning

Python decides the stage. The LLM is only used inside agents and for free-form chat
while learning.
"""
import json
from concurrent.futures import ThreadPoolExecutor

import checks
import config
from agents import curator, examiner, fact_checker, pedagogy, planner, quiz_solver, scoper, teacher
from agents.planner import PlanningFailed  # noqa: F401  (re-exported for the UI)
from db import store
from llm import BudgetExceeded, LLMError, RefusalError, price_for
from sources.pdf import PdfSource, ingest_pdf
from sources.web import WebSource

GREETING = "Hi! What would you like to learn? Tell me the subject and anything you already know about your goal."

CHAT_SYSTEM = """You are the Coach in a personal learning app. The learner is working through a
milestone plan. Answer their questions briefly and concretely, at their level. If a question
belongs to a later milestone, say so and give a short answer. Lessons cite web pages and the
learner's uploaded PDFs and are checked by a Fact-checker, but your chat answers come from
your own knowledge and are NOT checked, so flag anything you are unsure about.
"""

CHECK_ERRORS = (BudgetExceeded, RefusalError, LLMError)  # stop the checks, keep the lesson


class CheckerStopped(Exception):
    """The checks couldn't finish. Carries the last good lesson and the review so far."""

    def __init__(self, lesson, review):
        super().__init__(review.get("error", ""))
        self.lesson, self.review = lesson, review


class Coach:
    def __init__(self, conn, llm):
        self.conn = conn
        self.llm = llm

    # ---------- subjects ----------

    def new_subject(self) -> int:
        pid = store.create_profile(self.conn)
        store.add_chat(self.conn, pid, "greeting", "assistant", GREETING)
        return pid

    def delete_subject(self, profile_id: int):
        """Remove the subject's profile, plan, lessons and chat. Spend history is kept."""
        store.delete_profile(self.conn, profile_id)

    def stage(self, profile_id: int) -> str:
        return store.get_profile(self.conn, profile_id)["stage"]

    # ---------- chat entry point ----------

    def handle_message(self, profile_id: int, text: str) -> str:
        """Route a chat message based on the current stage. Returns the reply."""
        stage = self.stage(profile_id)
        if stage == "scoping":
            return self._scoping(profile_id, text)
        if stage == "learning":
            return self._chat(profile_id, text)
        if stage == "confirm_profile":
            return "Please review and confirm your profile in the form below."
        return "Your plan isn't ready yet. Use the 'Build my plan' button."

    # ---------- scoping ----------

    def _scoping(self, profile_id: int, text: str) -> str:
        store.add_chat(self.conn, profile_id, "scoping", "user", text)
        rows = store.get_chat(self.conn, profile_id, "scoping")
        history = [{"role": r["role"], "content": r["content"]} for r in rows]
        asked = sum(1 for r in rows if r["role"] == "assistant")

        turn = scoper.next_turn(self.llm, history, asked, profile_id=profile_id)

        if turn.done and turn.profile is not None:
            store.save_profile_data(self.conn, profile_id, turn.profile.model_dump())
            store.set_stage(self.conn, profile_id, "confirm_profile")
            reply = "Thanks! Here is your profile. Edit anything that's wrong, then confirm."
        else:
            reply = turn.next_question or "Could you tell me a bit more?"
        store.add_chat(self.conn, profile_id, "scoping", "assistant", reply)
        return reply

    def confirm_profile(self, profile_id: int, profile: dict):
        """Save the (possibly edited) profile and move on to planning."""
        profile = scoper.SubjectProfile.model_validate(profile).model_dump()
        store.save_profile_data(self.conn, profile_id, profile)
        store.set_stage(self.conn, profile_id, "planning")

    # ---------- planning ----------

    def make_plan(self, profile_id: int):
        """Run the Planner. Raises PlanningFailed (stage stays 'planning') if it fails twice."""
        profile = store.get_profile_data(self.conn, profile_id)
        plan = planner.make_plan(self.llm, profile, profile_id=profile_id)
        store.save_milestones(self.conn, profile_id, [m.model_dump() for m in plan.milestones])
        store.set_stage(self.conn, profile_id, "learning")
        store.add_chat(self.conn, profile_id, "learning", "assistant",
                       "Your plan is ready. Open the first lesson, or ask me anything.")
        return plan

    # ---------- lessons ----------

    def request_lesson(self, milestone_id: int, regenerate: bool = False):
        """Return the lesson row. Generates it the first time, or again when regenerate=True.

        Pipeline: Curator (web search, reused unless regenerating) -> download pages + pick
        PDF pages -> Teacher (cited) -> Fact-checker + Pedagogy reviewer (up to 2 rewrites)
        -> Examiner -> blind Quiz solver. On regenerate nothing is saved until the Teacher
        succeeds, so a failed regenerate keeps the old lesson and resources.
        """
        lesson = store.get_lesson(self.conn, milestone_id)
        if lesson is not None and not regenerate:
            return lesson
        m = store.get_milestone(self.conn, milestone_id)
        if m["status"] == "locked":
            raise ValueError("This milestone is locked. Finish the previous one first.")
        pid = m["profile_id"]
        profile = store.get_profile_data(self.conn, pid)

        curator_info = {"reused": True}
        new_resources = None
        if regenerate or not store.has_resources(self.conn, milestone_id):
            new_resources, curator_info = curator.find_resources(self.llm, profile, dict(m), profile_id=pid)
            resources = new_resources
            if lesson is None:  # first lesson: keep the picks now, so a retry doesn't search again
                store.save_resources(self.conn, milestone_id, new_resources)
                new_resources = None
        else:
            resources = store.get_resources(self.conn, milestone_id)

        pdf, web = PdfSource(self.conn, pid), WebSource(self.conn, resources)
        result = teacher.write_lesson(self.llm, profile, dict(m), sources=[pdf, web], profile_id=pid)

        if new_resources is not None:
            store.save_resources(self.conn, milestone_id, new_resources)
        notes = {"pdf": pdf.notes, "web": web.notes, "curator": curator_info}
        self._check_and_save(milestone_id, profile, dict(m), result, notes)
        return store.get_lesson(self.conn, milestone_id)

    def run_checks(self, milestone_id: int):
        """Check an existing lesson: checkers, rewrites and quiz. No Curator call.

        The checkers get the same kind of source text the Teacher used: the saved Curator picks
        (from the download cache) and the subject's PDFs. A Phase 1 lesson had no sources.
        """
        row = store.get_lesson(self.conn, milestone_id)
        if row is None:
            raise ValueError("There is no lesson to check yet.")
        m = dict(store.get_milestone(self.conn, milestone_id))
        pid = m["profile_id"]
        profile = store.get_profile_data(self.conn, pid)
        notes = store.lesson_notes(row)
        chunks = []
        if notes is not None:  # Phase 2+ lesson: rebuild its sources
            resources = store.get_resources(self.conn, milestone_id)
            chunks = teacher.gather_chunks([PdfSource(self.conn, pid), WebSource(self.conn, resources)],
                                           m, profile)
        lesson = teacher.Lesson(text=row["content"], sources=json.loads(row["sources_json"]),
                                citations=json.loads(row["citations_json"] or "[]"),
                                model=row["model"] or "", chunks=chunks)
        self._check_and_save(milestone_id, profile, m, lesson, notes)
        return store.get_lesson(self.conn, milestone_id)

    def _check_and_save(self, milestone_id, profile, milestone, lesson, notes):
        """Run the checkers (with rewrites), save the lesson, then make the quiz.
        If the checks can't finish (budget reached, refusal, cut-off reply), the newest
        lesson is still saved, as unchecked, with the error in its review."""
        pid = milestone["profile_id"]
        try:
            lesson, status, review = self._check_and_revise(profile, milestone, lesson, pid)
        except CheckerStopped as stop:
            lesson, status, review = stop.lesson, "unchecked", stop.review

        def save():
            store.save_lesson(self.conn, milestone_id, lesson.text, lesson.sources, lesson.model,
                              citations=lesson.citations, notes=notes, status=status, review=review,
                              revision_rounds=review["revisions"])

        save()
        store.delete_quiz(self.conn, milestone_id)  # an old quiz would not match this lesson
        if status == "unchecked":
            return
        try:
            questions, dropped, redrafts, flagged = self._make_quiz(profile, milestone, lesson.text, pid)
            store.save_quiz(self.conn, milestone_id, questions, dropped, redrafts, flagged)
        except CHECK_ERRORS as e:
            review["quiz_error"] = str(e)
            save()

    def _run_checkers(self, profile, milestone, lesson, pid) -> list[dict]:
        """Fact-checker and Pedagogy reviewer, in parallel. Returns both checkers' flags,
        with unquoted flags downgraded to minor."""
        reading = checks.reading_minutes(lesson.text)
        budget = checks.length_budget(milestone.get("est_hours"))
        with ThreadPoolExecutor(max_workers=2) as pool:
            fact = pool.submit(fact_checker.check, self.llm, profile, checks.lesson_body(lesson.text),
                               lesson.chunks, pid)
            ped = pool.submit(pedagogy.review, self.llm, profile, milestone,
                              checks.plain_lesson(lesson.text), reading, budget, pid)
            flags = fact.result() + ped.result()
        return checks.apply_quote_rule(flags, lesson.text)

    def _check_and_revise(self, profile, milestone, lesson, pid):
        """Check; on any major flag the Teacher rewrites with the same sources, at most
        MAX_REVISION_ROUNDS times. Returns (final lesson, status, review log)."""
        rounds = []
        for rnd in range(config.MAX_REVISION_ROUNDS + 1):
            try:
                flags = self._run_checkers(profile, milestone, lesson, pid)
            except CHECK_ERRORS as e:
                raise CheckerStopped(lesson, {"rounds": rounds, "revisions": rnd, "error": str(e)})
            rounds.append({"round": rnd, "flags": flags})
            major = [f for f in flags if f["severity"] == "major"]
            if not major or rnd == config.MAX_REVISION_ROUNDS:
                break
            try:
                lesson = teacher.write_lesson(self.llm, profile, milestone, profile_id=pid,
                                              chunks=lesson.chunks,
                                              previous=checks.plain_lesson(lesson.text),
                                              feedback=checks.feedback_text(major))
            except CHECK_ERRORS as e:
                raise CheckerStopped(lesson, {"rounds": rounds, "revisions": rnd, "error": str(e)})
        status = checks.lesson_status(not major, teacher.section_labels(lesson.text))
        review = {"rounds": rounds, "revisions": len(rounds) - 1, "remaining_major": major,
                  "minor": [f for f in flags if f["severity"] == "minor"]}
        return lesson, status, review

    def _make_quiz(self, profile, milestone, lesson_text, pid):
        """Examiner drafts, Quiz solver answers blind, Python compares with the key.
        Disagreements are dropped and logged (never fixed). If fewer than QUIZ_MIN_QUESTIONS
        survive, the Examiner writes replacements, at most QUIZ_MAX_REDRAFTS times.
        Returns (kept questions, dropped log, redrafts, flagged)."""
        plain = checks.plain_lesson(lesson_text)
        kept, dropped, avoid = [], [], []
        redrafts, next_id = 0, 1
        for draft in range(config.QUIZ_MAX_REDRAFTS + 1):
            n = config.QUIZ_TARGET_QUESTIONS - len(kept)
            questions = examiner.draft_quiz(self.llm, profile, milestone, plain, n,
                                            avoid=avoid or None, profile_id=pid)
            for q in questions:
                q["id"] = f"q{next_id}"
                next_id += 1
            answers = quiz_solver.solve(self.llm, profile, plain, questions, profile_id=pid)
            for q in questions:
                got = answers.get(q["id"])
                if got is not None and checks.answers_match(q, got["answer"]):
                    kept.append(q)
                    avoid.append({"question": q["question"]})
                    continue
                reason = "solver gave no answer" if got is None else "solver's answer differs from the key"
                solver_answer = got["answer"] if got else None
                solver_note = got["note"] if got else ""
                dropped.append({**q, "solver_answer": solver_answer, "solver_note": solver_note,
                                "reason": reason, "draft": draft})
                avoid.append({"question": q["question"],
                              "problem": f"{reason} (solver: {solver_answer}, key: {q['answer']}). {solver_note}"})
            if len(kept) >= config.QUIZ_MIN_QUESTIONS or draft == config.QUIZ_MAX_REDRAFTS:
                break
            redrafts += 1
        return kept, dropped, redrafts, len(kept) < config.QUIZ_MIN_QUESTIONS

    # ---------- PDF sources ----------

    def upload_pdf(self, profile_id: int, filename: str, data: bytes, page_from=None, page_to=None) -> str:
        """Store a PDF's text for this subject. Returns a warning ('' if all is fine)."""
        _, warning = ingest_pdf(self.conn, profile_id, filename, data, page_from, page_to)
        return warning

    # ---------- cost estimates ----------

    CURATOR_AGENTS = ("curator_search", "curator")
    CHECK_AGENTS = ("fact_checker", "pedagogy")
    QUIZ_AGENTS = ("examiner", "quiz_solver")
    LESSON_AGENTS = CURATOR_AGENTS + ("teacher",) + CHECK_AGENTS + QUIZ_AGENTS

    def agent_costs(self, profile_id: int) -> dict[str, float]:
        """Estimated cost (USD) of ONE call of each lesson agent, kept a bit high.

        An agent with at least EST_MIN_LESSONS past calls: average real cost of its recent
        calls (from usage_log) x EST_SAFETY_FACTOR. Otherwise: the rough formula."""
        formula = self._formula_costs(profile_id)
        out = {}
        for agent in self.LESSON_AGENTS:
            avg, n = store.recent_avg_cost(self.conn, agent, config.EST_SAMPLE_CALLS)
            out[agent] = avg * config.EST_SAFETY_FACTOR if n >= config.EST_MIN_LESSONS else formula[agent]
        return out

    def estimate_lesson_cost(self, profile_id: int) -> tuple[float, float]:
        """(typical, max) cost of a new lesson. Typical: it passes the first check.
        Max: 2 rewrites (3 check rounds) and 2 quiz redrafts."""
        c = self.agent_costs(profile_id)
        base = sum(c[a] for a in self.CURATOR_AGENTS) + c["teacher"]
        return (base + self._check_costs(c, 0, 0),
                base + self._check_costs(c, config.MAX_REVISION_ROUNDS, config.QUIZ_MAX_REDRAFTS))

    def estimate_check_cost(self, profile_id: int) -> tuple[float, float]:
        """(typical, max) cost of "Run checks" on an existing lesson."""
        c = self.agent_costs(profile_id)
        return (self._check_costs(c, 0, 0),
                self._check_costs(c, config.MAX_REVISION_ROUNDS, config.QUIZ_MAX_REDRAFTS))

    def _check_costs(self, c: dict, rewrites: int, redrafts: int) -> float:
        check_round = sum(c[a] for a in self.CHECK_AGENTS)
        quiz_round = sum(c[a] for a in self.QUIZ_AGENTS)
        return rewrites * c["teacher"] + (1 + rewrites) * check_round + (1 + redrafts) * quiz_round

    def _formula_costs(self, profile_id: int) -> dict[str, float]:
        """Rough upper estimate per call from config limits, used until real usage exists."""
        def cost(agent, tokens_in, tokens_out):
            p = price_for(config.MODELS[agent])
            return (tokens_in * p["input"] + tokens_out * p["output"]) / 1e6

        pdf_chars = sum(store.pdf_page_lengths(self.conn, profile_id)[: config.PDF_MAX_PAGES])
        sources_in = pdf_chars / 4 + 2 * config.PAGE_MAX_TOKENS  # PDF pages + 2 web pages
        lesson, out = config.EST_LESSON_TOKENS, config.EST_OUTPUT_TOKENS
        return {
            "curator_search": config.WEB_SEARCH_MAX_USES * config.WEB_SEARCH_USD
                              + cost("curator_search", config.EST_CURATOR_INPUT_TOKENS, config.MAX_TOKENS["curator"]),
            "curator": cost("curator", config.MAX_TOKENS["curator"], config.MAX_TOKENS["curator"]),
            "teacher": cost("teacher", sources_in + lesson + 2000, config.EST_TEACHER_OUTPUT_TOKENS),
            "fact_checker": cost("fact_checker", sources_in + lesson + 2000, out["fact_checker"]),
            "pedagogy": cost("pedagogy", lesson + 2000, out["pedagogy"]),
            "examiner": cost("examiner", lesson + 2000, out["examiner"]),
            "quiz_solver": cost("quiz_solver", lesson + 2000, out["quiz_solver"]),
        }

    def complete_milestone(self, milestone_id: int):
        """Mark done and unlock the next milestone (manual tick in Phase 1)."""
        m = store.get_milestone(self.conn, milestone_id)
        if m["status"] != "active":
            return
        store.set_milestone_status(self.conn, milestone_id, "done")
        for nxt in store.get_milestones(self.conn, m["profile_id"]):
            if nxt["position"] == m["position"] + 1 and nxt["status"] == "locked":
                store.set_milestone_status(self.conn, nxt["id"], "active")

    # ---------- free chat ----------

    def chat_history(self, profile_id: int, limit: int | None = None) -> list[dict]:
        """Last `limit` learning-chat messages, starting with a user message (API rule)."""
        limit = limit or config.CHAT_HISTORY_LIMIT
        rows = store.get_chat(self.conn, profile_id, "learning")[-limit:]
        msgs = [{"role": r["role"], "content": r["content"]} for r in rows]
        while msgs and msgs[0]["role"] != "user":
            msgs.pop(0)
        return msgs

    def _chat(self, profile_id: int, text: str) -> str:
        store.add_chat(self.conn, profile_id, "learning", "user", text)
        profile = store.get_profile_data(self.conn, profile_id)
        status = "\n".join(
            f"{m['position']}. [{m['status']}] {m['title']}: {m['objective']}"
            for m in store.get_milestones(self.conn, profile_id)
        )
        reply, _ = self.llm.call_text(
            "coach", CHAT_SYSTEM, self.chat_history(profile_id),
            profile=profile, extra="Milestones:\n" + status, profile_id=profile_id,
        )
        store.add_chat(self.conn, profile_id, "learning", "assistant", reply)
        return reply
