"""Coach: a simple state machine that routes each step to the right agent.

Stages for each subject (profile):
    scoping -> confirm_profile -> planning -> learning

Python decides the stage. The LLM is only used inside agents and for free-form chat
while learning.
"""
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

import checks
import clock
import config
import review
from agents import (curator, examiner, fact_checker, formatting, grader, pedagogy, planner, quiz_solver,
                    scoper, teacher)
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

""" + formatting.ALL

CHECK_ERRORS = (BudgetExceeded, RefusalError, LLMError)  # stop the checks, keep the lesson


def quiz_key(questions: list[dict]) -> list[str]:
    """Identifies a quiz by its question texts (a new quiz has new questions)."""
    return [q["question"] for q in questions]


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
        """Remove the subject's profile, plan, lessons and chat. Spend history is kept.
        PDF pages uploaded to the Files API are deleted there too."""
        file_ids = store.pdf_file_ids(self.conn, profile_id=profile_id)
        store.delete_profile(self.conn, profile_id)
        for file_id in file_ids:
            self.llm.delete_file(file_id)

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

        pdf, web = self._pdf_source(pid), WebSource(self.conn, resources)
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
            chunks = teacher.gather_chunks([self._pdf_source(pid), WebSource(self.conn, resources)],
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
            concepts = store.bank_concepts(self.conn, milestone_id)
            questions, dropped, redrafts, flagged = self._make_quiz(profile, milestone, lesson.text, pid,
                                                                     concepts=concepts)
            store.save_quiz(self.conn, milestone_id, questions, dropped, redrafts, flagged)
            store.add_to_bank(self.conn, milestone_id, questions)
        except CHECK_ERRORS as e:
            review["quiz_error"] = str(e)
            save()
        # The practical task is written once per milestone and kept across lesson rewrites,
        # so work you already started still matches it.
        if store.get_task(self.conn, milestone_id) is None:
            try:
                self._make_task(milestone_id, profile, milestone, lesson.text)
            except CHECK_ERRORS as e:
                review["task_error"] = str(e)
                save()

    def _run_checkers(self, profile, milestone, lesson, pid) -> list[dict]:
        """Fact-checker and Pedagogy reviewer, in parallel, plus the netlist check (Python).
        Returns all flags, with unquoted checker flags downgraded to minor."""
        reading = checks.reading_minutes(lesson.text)
        budget = checks.length_budget(milestone.get("est_hours"))
        with ThreadPoolExecutor(max_workers=2) as pool:
            fact = pool.submit(fact_checker.check, self.llm, profile, checks.lesson_body(lesson.text),
                               lesson.chunks, pid)
            ped = pool.submit(pedagogy.review, self.llm, profile, milestone,
                              checks.plain_lesson(lesson.text), reading, budget, pid)
            flags = fact.result() + ped.result()
        return checks.apply_quote_rule(flags, lesson.text) + checks.block_flags(lesson.text)

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

    def _make_quiz(self, profile, milestone, lesson_text, pid, concepts=None, used=None):
        """Examiner drafts, Quiz solver answers blind, Python compares with the key.
        Disagreements are dropped and logged (never fixed). If fewer than QUIZ_MIN_QUESTIONS
        survive, the Examiner writes replacements, at most QUIZ_MAX_REDRAFTS times.
        `concepts`: concept names already used for this milestone. `used`: questions you have
        already seen (a new quiz after a failed attempt must not repeat them).
        Returns (kept questions, dropped log, redrafts, flagged)."""
        plain = checks.plain_lesson(lesson_text)
        kept, dropped = [], []
        avoid = [{"question": q} for q in (used or [])]
        redrafts, next_id = 0, 1
        for draft in range(config.QUIZ_MAX_REDRAFTS + 1):
            n = config.QUIZ_TARGET_QUESTIONS - len(kept)
            questions = examiner.draft_quiz(self.llm, profile, milestone, plain, n, avoid=avoid or None,
                                            concepts=concepts, profile_id=pid)
            for q in questions:
                q["id"] = f"q{next_id}"
                next_id += 1
            # Questions with a broken netlist are dropped before the solver sees them.
            ok = []
            for q in questions:
                problems = [b["problem"] for b in checks.block_problems(q["question"])]
                if not problems:
                    ok.append(q)
                    continue
                reason = "invalid netlist: " + "; ".join(problems)
                dropped.append({**q, "solver_answer": None, "solver_note": "", "reason": reason, "draft": draft})
                avoid.append({"question": q["question"], "problem": reason})
            questions = ok
            answers = quiz_solver.solve(self.llm, profile, plain, questions, profile_id=pid) if questions else {}
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
        """Store a PDF for this subject. Returns a message on how its pages are sent ('' if none).
        Nothing is uploaded to the API yet: a visual page is uploaded the first time a lesson uses it."""
        _, message = ingest_pdf(self.conn, profile_id, filename, data, page_from, page_to)
        return message

    def delete_pdf(self, upload_id: int):
        """Remove a PDF, and its pages from the Files API."""
        file_ids = store.pdf_file_ids(self.conn, upload_id=upload_id)
        store.delete_pdf(self.conn, upload_id)
        for file_id in file_ids:
            self.llm.delete_file(file_id)

    def _pdf_source(self, profile_id: int) -> PdfSource:
        return PdfSource(self.conn, profile_id, upload_file=self.llm.upload_file)

    # ---------- cost estimates ----------

    CURATOR_AGENTS = ("curator_search", "curator")
    CHECK_AGENTS = ("fact_checker", "pedagogy")
    QUIZ_AGENTS = ("examiner", "quiz_solver")
    LESSON_AGENTS = CURATOR_AGENTS + ("teacher",) + CHECK_AGENTS + QUIZ_AGENTS + ("examiner_task",)
    GATE_AGENTS = ("grader",)

    def agent_costs(self, profile_id: int) -> dict[str, float]:
        """Estimated cost (USD) of ONE call of each lesson agent, kept a bit high.

        An agent with at least EST_MIN_LESSONS past calls: average real cost of its recent
        calls (from usage_log) x EST_SAFETY_FACTOR. Otherwise: the rough formula."""
        formula = self._formula_costs(profile_id)
        out = {}
        for agent in self.LESSON_AGENTS + self.GATE_AGENTS:
            avg, n = store.recent_avg_cost(self.conn, agent, config.EST_SAMPLE_CALLS)
            out[agent] = avg * config.EST_SAFETY_FACTOR if n >= config.EST_MIN_LESSONS else formula[agent]
        return out

    def estimate_lesson_cost(self, profile_id: int) -> tuple[float, float]:
        """(typical, max) cost of a new lesson. Typical: it passes the first check.
        Max: 2 rewrites (3 check rounds) and 2 quiz redrafts."""
        c = self.agent_costs(profile_id)
        base = sum(c[a] for a in self.CURATOR_AGENTS) + c["teacher"] + c["examiner_task"]
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

        # Source documents: the biggest PDF pages a lesson could get (visual pages count
        # VISUAL_PAGE_TOKENS each) + 2 web pages. Priced as a cache write (1.25x input), the
        # first call's price; later rounds read them from the cache for much less.
        n_visual = min(store.pdf_visual_page_count(self.conn, profile_id), config.VISUAL_PDF_MAX_PAGES)
        text_chars = sum(store.pdf_page_lengths(self.conn, profile_id)[: config.PDF_MAX_PAGES - n_visual])
        sources_in = n_visual * config.VISUAL_PAGE_TOKENS + text_chars / 4 + 2 * config.PAGE_MAX_TOKENS
        lesson, out = config.EST_LESSON_TOKENS, config.EST_OUTPUT_TOKENS

        def with_sources(agent, tokens_in, tokens_out):
            p = price_for(config.MODELS[agent])
            return cost(agent, tokens_in, tokens_out) + sources_in * p["cache_write"] / 1e6
        return {
            "curator_search": config.WEB_SEARCH_MAX_USES * config.WEB_SEARCH_USD
                              + cost("curator_search", config.EST_CURATOR_INPUT_TOKENS, config.MAX_TOKENS["curator"]),
            "curator": cost("curator", config.MAX_TOKENS["curator"], config.MAX_TOKENS["curator"]),
            "teacher": with_sources("teacher", lesson + 2000, config.EST_TEACHER_OUTPUT_TOKENS),
            "fact_checker": with_sources("fact_checker", lesson + 2000, out["fact_checker"]),
            "pedagogy": cost("pedagogy", lesson + 2000, out["pedagogy"]),
            "examiner": cost("examiner", lesson + 2000, out["examiner"]),
            "quiz_solver": cost("quiz_solver", lesson + 2000, out["quiz_solver"]),
            "examiner_task": cost("examiner_task", lesson + 2000, out["examiner_task"]),
            # a few full-size screenshots (~4,800 tokens each at the 2,576 px limit)
            "grader": cost("grader", config.EST_GRADER_INPUT_TOKENS + 3 * 4800, out["grader"]),
        }

    def estimate_new_quiz_cost(self, profile_id: int) -> tuple[float, float]:
        """(typical, max) cost of a fresh quiz after a failed attempt."""
        c = self.agent_costs(profile_id)
        quiz_round = sum(c[a] for a in self.QUIZ_AGENTS)
        return quiz_round, (1 + config.QUIZ_MAX_REDRAFTS) * quiz_round

    def estimate_grading_cost(self, profile_id: int) -> float:
        return self.agent_costs(profile_id)["grader"]

    def estimate_prepare_cost(self, profile_id: int) -> float:
        """"Prepare exam" for an old lesson: concept tags + practical task."""
        c = self.agent_costs(profile_id)
        return c["examiner"] + c["examiner_task"]

    # =================================================================
    # Phase 4: the Examiner gate
    # =================================================================
    #
    # A milestone is passed when BOTH parts pass:
    #   quiz: >= QUIZ_PASS_PCT right on the verified quiz (graded by Python, $0).
    #         No quiz (none survived the solver) -> the task alone, with a warning.
    #   task: every rubric item "pass" after your overrides (graded by the Grader).
    # A part you passed stays passed. A failed quiz needs a fresh quiz before the next try.

    def _lesson_and_profile(self, milestone_id: int):
        m = store.get_milestone(self.conn, milestone_id)
        if m is None:
            raise ValueError("That milestone doesn't exist.")
        return dict(m), store.get_lesson(self.conn, milestone_id), store.get_profile_data(self.conn, m["profile_id"])

    def _make_task(self, milestone_id, profile, milestone, lesson_text):
        """Draft the task. A broken netlist gets one redraft; if it's still broken, the task is
        saved with the problems shown at the top so you (and the Grader) know."""
        plain = checks.plain_lesson(lesson_text)
        task = examiner.draft_task(self.llm, profile, milestone, plain, profile_id=milestone["profile_id"])
        problems = [b["problem"] for b in checks.block_problems(task["instructions"])]
        if problems:
            task = examiner.draft_task(self.llm, profile, milestone, plain, profile_id=milestone["profile_id"],
                                       problems=problems)
            problems = [b["problem"] for b in checks.block_problems(task["instructions"])]
        if problems:
            task["instructions"] = ("**Warning:** the app's netlist check found problems in this task, so "
                                    "the circuit may be wrong: " + "; ".join(problems) + "\n\n" + task["instructions"])
        store.save_task(self.conn, milestone_id, task["proof_type"], task["instructions"],
                        task["rubric"], task["screenshots"])

    def _current_grades(self, milestone_id: int):
        """(latest graded submission or None, its grades with your overrides applied)."""
        graded = [s for s in store.submissions(self.conn, milestone_id) if s["grades"] is not None]
        if not graded:
            return None, None
        last = graded[-1]
        # An override counts only for the submission it was made on, not for newer ones.
        ovs = [o for o in store.overrides(self.conn, milestone_id) if o["submission_id"] == last["id"]]
        return last, checks.effective_grades(last["grades"], ovs)

    def gate_state(self, milestone_id: int) -> dict:
        """Everything the UI needs to show the exam for one milestone."""
        m, lesson, _ = self._lesson_and_profile(milestone_id)
        quiz = store.get_quiz(self.conn, milestone_id)
        questions = quiz["questions"] if quiz else []
        attempts = store.quiz_results(self.conn, milestone_id)
        task = store.get_task(self.conn, milestone_id)
        subs = store.submissions(self.conn, milestone_id)
        last_sub, grades = self._current_grades(milestone_id)

        disabled = ""
        if lesson is None:
            disabled = "Open the lesson first."
        elif store.lesson_status(lesson) == "unchecked":
            disabled = "This lesson hasn't been checked yet. Use 'Run checks' first (it also makes the quiz)."

        if any(a["passed"] for a in attempts):
            quiz_status = "passed"
        elif not questions:
            quiz_status = "missing"  # gate = task alone (with a warning)
        elif attempts and attempts[-1]["details"].get("quiz_key") == quiz_key(questions):
            quiz_status = "needs_new"  # you already failed this exact quiz
        else:
            quiz_status = "ready"

        task_status = checks.task_status(task["rubric"], grades) if task else "missing"
        if task and last_sub is None:
            task_status = "not_submitted"
        # "Pass with a reason": only when the task can't be graded, again and again.
        ungradable = sum(1 for s in subs if s["error"] or (
            task and s["grades"] is not None and checks.task_status(task["rubric"], s["grades"]) == "need_more"))
        untagged = any(not q.get("concept") for q in questions)
        return {
            "milestone": m,
            "passed": m["status"] == "done",
            "disabled": disabled,
            "quiz": quiz, "questions": questions, "quiz_status": quiz_status,
            "quiz_flagged": bool(quiz and quiz["flagged"] and questions),
            "attempts": attempts,
            "task": task, "task_status": task_status, "grades": grades, "last_submission": last_sub,
            "submissions": subs,
            "manual_pass_allowed": (m["status"] != "done" and not disabled and task is not None
                                    and quiz_status in ("passed", "missing")
                                    and ungradable >= config.MANUAL_PASS_AFTER),
            "needs_prepare": not disabled and (task is None or untagged),
        }

    def prepare_exam(self, milestone_id: int):
        """For lessons made before Phase 4: tag quiz questions with concepts and write the task."""
        m, lesson, profile = self._lesson_and_profile(milestone_id)
        if lesson is None or store.lesson_status(lesson) == "unchecked":
            raise ValueError("Run the checks on this lesson first.")
        self._ensure_tagged(milestone_id, profile, lesson)
        if store.get_task(self.conn, milestone_id) is None:
            self._make_task(milestone_id, profile, m, lesson["content"])

    def _ensure_tagged(self, milestone_id, profile, lesson):
        """Give old quiz questions a concept + section, and put them in the question bank."""
        quiz = store.get_quiz(self.conn, milestone_id)
        if quiz is None or not quiz["questions"]:
            return
        questions = quiz["questions"]
        untagged = [q for q in questions if not q.get("concept")]
        if untagged:
            pid = store.get_milestone(self.conn, milestone_id)["profile_id"]
            tags = examiner.tag_concepts(self.llm, profile, checks.plain_lesson(lesson["content"]),
                                         untagged, profile_id=pid)
            questions = [{**q, **tags[q["id"]]} if q["id"] in tags else q for q in questions]
            store.save_quiz(self.conn, milestone_id, questions, quiz["dropped"], quiz["redrafts"], quiz["flagged"])
        store.add_to_bank(self.conn, milestone_id, questions)

    def submit_quiz(self, milestone_id: int, answers: dict[str, str]) -> dict:
        """Grade your quiz answers ({question id: answer}). Missed concepts go into spaced
        review right away (rated Again). Returns {"score", "passed", "missed", "milestone_passed"}."""
        st = self.gate_state(milestone_id)
        if st["disabled"]:
            raise ValueError(st["disabled"])
        if st["quiz_status"] != "ready":
            raise ValueError({"needs_new": "Get a new quiz first.", "passed": "You already passed this quiz.",
                              "missing": "There is no quiz for this milestone."}[st["quiz_status"]])
        questions = st["questions"]
        score, results = checks.grade_quiz(questions, answers)
        passed = checks.quiz_passed(score)
        by_id = {q["id"]: q for q in questions}
        missed = []
        for r in results:
            if r["correct"]:
                continue
            q = by_id[r["id"]]
            missed.append({"id": q["id"], "question": q["question"], "given": r["given"], "answer": q["answer"],
                           "unit": q.get("unit", ""), "choices": q.get("choices", []),
                           "explanation": q["explanation"], "section": q.get("section", ""),
                           "concept": q.get("concept", "")})
        store.add_quiz_result(self.conn, milestone_id, score, passed,
                              {"results": results, "missed": missed, "quiz_key": quiz_key(questions)})
        pid = st["milestone"]["profile_id"]
        for concept in {x["concept"] for x in missed if x["concept"]}:
            self._rate_concept(pid, milestone_id, concept, correct=False, source="exam")
        return {"score": score, "passed": passed, "missed": missed,
                "milestone_passed": self._maybe_pass(milestone_id)}

    def new_quiz(self, milestone_id: int):
        """A fresh quiz after a failed attempt: new questions, verified by the Quiz solver again."""
        m, lesson, profile = self._lesson_and_profile(milestone_id)
        if lesson is None or store.lesson_status(lesson) == "unchecked":
            raise ValueError("Run the checks on this lesson first.")
        used = [q["question"] for q in store.bank(self.conn, milestone_id)]
        questions, dropped, redrafts, flagged = self._make_quiz(
            profile, m, lesson["content"], m["profile_id"],
            concepts=store.bank_concepts(self.conn, milestone_id), used=used)
        store.save_quiz(self.conn, milestone_id, questions, dropped, redrafts, flagged)
        store.add_to_bank(self.conn, milestone_id, questions)

    def submit_task(self, milestone_id: int, text: str, images: list[bytes]) -> dict:
        """Send your task (text + images) to the Grader. Returns the new gate state."""
        st = self.gate_state(milestone_id)
        if st["disabled"]:
            raise ValueError(st["disabled"])
        if st["task"] is None:
            raise ValueError("There is no task yet. Use 'Prepare exam'.")
        if len(images) > config.IMAGE_MAX_COUNT:
            raise ValueError(f"Send at most {config.IMAGE_MAX_COUNT} images.")
        if not text.strip() and not images:
            raise ValueError("Add your answer or at least one image.")
        try:
            prepared = [grader.prepare_image(b) for b in images]
        except Exception as e:  # Pillow raises many kinds of errors for a bad file
            raise ValueError(f"Couldn't read one of the images: {e}") from e
        m, _, profile = self._lesson_and_profile(milestone_id)
        try:
            grades = grader.grade_task(self.llm, profile, m, st["task"], text, prepared, profile_id=m["profile_id"])
            store.add_submission(self.conn, milestone_id, text, prepared, grades)
        except CHECK_ERRORS as e:
            store.add_submission(self.conn, milestone_id, text, prepared, None, error=str(e))
            raise
        self._maybe_pass(milestone_id)
        return self.gate_state(milestone_id)

    def override_item(self, milestone_id: int, item_id: str, reason: str, new_verdict: str = "pass"):
        """Change one rubric grade yourself. Needs a reason; it's logged and shown on the milestone."""
        reason = (reason or "").strip()
        if not reason:
            raise ValueError("Write a short reason for the override.")
        if new_verdict not in checks.VERDICTS:
            raise ValueError(f"Unknown verdict: {new_verdict}")
        last, grades = self._current_grades(milestone_id)
        old = next((g["verdict"] for g in grades or [] if g["id"] == item_id), None)
        if old is None:
            raise ValueError("That rubric item hasn't been graded yet.")
        store.add_override(self.conn, milestone_id, last["id"], item_id, old, new_verdict, reason)
        self._maybe_pass(milestone_id)

    def manual_pass(self, milestone_id: int, reason: str):
        """Last resort when the task can't be graded: pass with a written reason."""
        reason = (reason or "").strip()
        if not reason:
            raise ValueError("Write a short reason for passing without a grade.")
        if not self.gate_state(milestone_id)["manual_pass_allowed"]:
            raise ValueError(f"Passing with a reason is only possible after {config.MANUAL_PASS_AFTER} task "
                             "submissions that couldn't be graded (and a passed or missing quiz).")
        self._finish_pass(milestone_id, "manual_reason", reason)

    def _maybe_pass(self, milestone_id: int) -> bool:
        st = self.gate_state(milestone_id)
        if st["passed"] or st["disabled"]:
            return st["passed"]
        if st["quiz_status"] in ("passed", "missing") and st["task_status"] == "passed":
            self._finish_pass(milestone_id, "exam")
            return True
        return False

    def _finish_pass(self, milestone_id: int, method: str, reason: str | None = None):
        """Mark passed, start review cards for its concepts, unlock the next milestone."""
        m = store.get_milestone(self.conn, milestone_id)
        store.mark_passed(self.conn, milestone_id, method, reason)
        for concept in store.bank_concepts(self.conn, milestone_id):
            if store.find_card(self.conn, milestone_id, concept) is None:
                # You just showed you know it: first review in a couple of days.
                self._rate_concept(m["profile_id"], milestone_id, concept, correct=True, source="exam")
        nxt = None
        for row in store.get_milestones(self.conn, m["profile_id"]):
            if row["position"] == m["position"] + 1 and row["status"] == "locked":
                store.set_milestone_status(self.conn, row["id"], "active")
                nxt = row
        how = "with a written reason" if method == "manual_reason" else "the exam"
        msg = f"You passed **{m['title']}** ({how})."
        msg += f" Next up: **{nxt['title']}**." if nxt else " That was the last milestone. Well done!"
        store.add_chat(self.conn, m["profile_id"], "learning", "assistant", msg)

    def add_to_review(self, milestone_id: int) -> int:
        """For milestones ticked done before Phase 4: put their quiz concepts into review
        (no exam needed). Returns how many cards were created."""
        m, lesson, profile = self._lesson_and_profile(milestone_id)
        if m["pass_method"] != "manual_pre_phase4":
            raise ValueError("Only for milestones passed manually before Phase 4.")
        if lesson is not None:
            self._ensure_tagged(milestone_id, profile, lesson)
        concepts = store.bank_concepts(self.conn, milestone_id)
        if not concepts:
            raise ValueError("This milestone has no verified quiz questions to review.")
        made = 0
        for concept in concepts:
            if store.find_card(self.conn, milestone_id, concept) is None:
                card_json, due = review.new_card()  # due now: you haven't been tested on it
                store.add_card(self.conn, m["profile_id"], milestone_id, concept, card_json, due)
                made += 1
        return made

    # =================================================================
    # Phase 4: spaced review
    # =================================================================

    def _rate_concept(self, pid, milestone_id, concept, correct: bool, source: str,
                      question_id=None, now=None) -> int:
        """Rate a concept's card (making it if needed). Returns the review_log id."""
        now = now or clock.utc_now()
        card = store.find_card(self.conn, milestone_id, concept)
        if card is None:
            card_json, due = review.new_card()
            card_id = store.add_card(self.conn, pid, milestone_id, concept, card_json, due)
        else:
            card_id, card_json = card["id"], card["card_json"]
        rating = review.rating_for(correct)
        new_json, due = review.apply(card_json, rating, now)
        store.update_card(self.conn, card_id, new_json, due)
        return store.add_review_log(self.conn, card_id, question_id, correct, rating, source, card_json,
                                    when=clock.to_iso(now))

    def due_reviews(self, profile_id: int, now=None) -> list[dict]:
        """Today's cards (due now, within the daily limit), each with the question to show."""
        now = now or clock.utc_now()
        start, end = clock.local_day_bounds(now)
        done_today = len(store.reviews_between(self.conn, profile_id, start, end))
        out = []
        for card in review.todays_cards(store.due_cards(self.conn, profile_id, clock.to_iso(now)), done_today):
            q = review.pick_question(store.bank(self.conn, card["milestone_id"], card["concept"]))
            if q is not None:
                out.append({"card": dict(card), "question": q})
        return out

    def answer_review(self, card_id: int, bank_id: int, answer: str, now=None) -> dict:
        """Grade a review answer: wrong = Again, right = Good. Returns
        {"correct", "question", "log_id", "rating"}."""
        now = now or clock.utc_now()
        card = store.get_card(self.conn, card_id)
        q = next((x for x in store.bank(self.conn, card["milestone_id"], card["concept"]) if x["bank_id"] == bank_id),
                 None)
        if q is None:
            raise ValueError("That question doesn't belong to this card.")
        correct = bool((answer or "").strip()) and checks.answers_match(q, answer)
        log_id = self._rate_concept(card["profile_id"], card["milestone_id"], card["concept"], correct,
                                    "review", question_id=bank_id, now=now)
        store.mark_shown(self.conn, bank_id, clock.to_iso(now))
        return {"correct": correct, "question": q, "log_id": log_id, "rating": review.rating_for(correct)}

    def adjust_review(self, log_id: int, rating: str, now=None):
        """Change a correct review's Good to Hard or Easy (or back to Good). Only for the card's
        most recent review, on the same local day. Re-applied to the card state before that review."""
        if rating not in ("hard", "good", "easy"):
            raise ValueError("Pick Hard, Good or Easy.")
        log = store.get_review_log(self.conn, log_id)
        ok, why = review.can_adjust(log, store.last_review_log(self.conn, log["card_id"]) if log else None, now)
        if not ok:
            raise ValueError(why)
        new_json, due = review.apply(log["card_before_json"], rating, clock.from_iso(log["reviewed_at"]))
        store.update_card(self.conn, log["card_id"], new_json, due)
        store.set_log_rating(self.conn, log_id, rating)

    # =================================================================
    # Phase 4: progress
    # =================================================================

    def set_deadline(self, profile_id: int, deadline: date | None):
        store.set_deadline(self.conn, profile_id, deadline.isoformat() if deadline else None)

    def log_study(self, milestone_id: int, minutes: float):
        if minutes <= 0:
            raise ValueError("Minutes must be more than 0.")
        store.log_study(self.conn, milestone_id, minutes)

    def subject_overview(self, profile_id: int, today: date | None = None) -> dict:
        """Passed/total, hours (from the plan's estimates) and on-track vs the deadline.
        On track = remaining estimated hours at your hours/week end by the deadline."""
        today = today or clock.local_date(clock.utc_now())
        ms = store.get_milestones(self.conn, profile_id)
        profile = store.get_profile_data(self.conn, profile_id) or {}
        done = [m for m in ms if m["status"] == "done"]
        hours_total = sum(m["est_hours"] or 0 for m in ms)
        hours_done = sum(m["est_hours"] or 0 for m in done)
        hours_left = hours_total - hours_done
        per_week = profile.get("hours_per_week") or 0
        finish = today + timedelta(days=round(hours_left / per_week * 7)) if per_week else None
        deadline_s = store.get_profile(self.conn, profile_id)["deadline_date"]
        deadline = date.fromisoformat(deadline_s) if deadline_s else None
        on_track = None
        if deadline and finish:
            on_track = finish <= deadline
        return {"passed": len(done), "total": len(ms), "hours_done": hours_done, "hours_total": hours_total,
                "hours_left": hours_left, "hours_per_week": per_week, "projected_finish": finish,
                "deadline": deadline, "on_track": on_track}

    def review_stats(self, profile_id: int, now=None) -> dict:
        now = now or clock.utc_now()
        start, end = clock.local_day_bounds(now)
        all_cards = store.cards(self.conn, profile_id)
        due = store.due_cards(self.conn, profile_id, clock.to_iso(now))
        today = store.reviews_between(self.conn, profile_id, start, end)
        month = store.reviews_between(self.conn, profile_id, clock.to_iso(now - timedelta(days=30)), end)
        recall = sum(r["correct"] for r in month) / len(month) if month else None
        # Weakest concepts: most misses (exam + review), then the lowest share right.
        every = store.reviews_between(self.conn, profile_id, "0000", "9999", source=None)
        per = {}
        for r in every:
            k = (r["milestone_id"], r["concept"])
            right, total = per.get(k, (0, 0))
            per[k] = (right + r["correct"], total + 1)
        weak = sorted(((c, total - right, right / total) for (_, c), (right, total) in per.items() if total > right),
                      key=lambda x: (-x[1], x[2]))[:5]
        return {"total_cards": len(all_cards), "due_now": len(due), "reviewed_today": len(today),
                "left_today": len(review.todays_cards(due, len(today))), "daily_limit": config.REVIEW_DAILY_LIMIT,
                "recall_30d": recall, "reviews_30d": len(month),
                "weakest": [{"concept": c, "misses": n, "right_share": s} for c, n, s in weak]}

    def milestone_progress(self, milestone_id: int) -> dict:
        m = dict(store.get_milestone(self.conn, milestone_id))
        attempts = store.quiz_results(self.conn, milestone_id)
        subs = store.submissions(self.conn, milestone_id)
        task = store.get_task(self.conn, milestone_id)
        _, grades = self._current_grades(milestone_id)
        return {
            "quiz_attempts": len(attempts),
            "best_score": max((a["score"] for a in attempts if a["score"] is not None), default=None),
            "task_submissions": len(subs),
            "task_status": (checks.task_status(task["rubric"], grades) if task and grades else
                            ("not_submitted" if task else "missing")),
            "passed_at": m["passed_at"], "pass_method": m["pass_method"], "pass_reason": m["pass_reason"],
            "overrides": store.overrides(self.conn, milestone_id),
            "study_minutes": store.study_minutes(self.conn, milestone_id),
            "est_hours": m["est_hours"],
        }

    def attempt_history(self, milestone_id: int) -> list[dict]:
        """Quiz attempts and task submissions, oldest first."""
        items = [{"kind": "quiz", "at": a["created_at"], "score": a["score"], "passed": a["passed"],
                  "missed": a["details"].get("missed", [])} for a in store.quiz_results(self.conn, milestone_id)]
        items += [{"kind": "task", "at": s["created_at"], "grades": s["grades"], "error": s["error"],
                   "n_images": s["n_images"]} for s in store.submissions(self.conn, milestone_id)]
        return sorted(items, key=lambda x: x["at"])

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
