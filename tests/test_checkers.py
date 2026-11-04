"""Phase 3 pipeline: Fact-checker + Pedagogy reviewer with the 2-rewrite limit, the blind Quiz
solver, "Run checks" on old lessons, and the database changes. No real API calls."""
import json
import sqlite3
import threading

import pytest

import config
from agents import fact_checker, quiz_solver
from db import store
from llm import BudgetExceeded, LLMError
from orchestrator import Coach
from sources import web
from tests.conftest import (agent_of, cite, fact_flags, mc, pass_all_checks, ped_flags, quiz,
                            quiz_ids, reply, solver_says, text)
from tests.helpers import FakeHTTP, FakeResponse, article_html
from tests.test_orchestrator import URL, queue_curator_with_page, queue_lesson, scope_to_learning

PLAIN = "## Walls\nDraw walls."  # a Teacher reply with no citations -> [Source: model]


@pytest.fixture
def coach(conn, llm, fake):
    pass_all_checks(fake)
    return Coach(conn, llm)


@pytest.fixture
def mid(coach, fake, conn):
    pid = scope_to_learning(coach, fake)
    return store.get_milestones(conn, pid)[0]["id"]


def calls_of(fake, agent):
    return [c for _, c in fake.messages.calls if agent_of(c) == agent]


def cited_teacher_reply():
    """A Teacher reply citing the first source document -> section labeled [Source: web]."""
    return reply(text("## Walls\n"), text("Use the Wall tool.", citations=[cite(0, "Wall tool steps")]))


# ---------- the revision loop ----------

def test_pass_first_time_one_teacher_call_and_verified(coach, fake, conn, mid, monkeypatch):
    monkeypatch.setattr(web.requests, "get", FakeHTTP({URL: FakeResponse(article_html("Wall tool steps"))}).get)
    queue_curator_with_page(fake)
    fake.messages.text_replies.append(cited_teacher_reply())
    lesson = coach.request_lesson(mid)

    assert len(calls_of(fake, "teacher")) == 1
    assert len(calls_of(fake, "fact_checker")) == 1 and len(calls_of(fake, "pedagogy")) == 1
    assert lesson["status"] == "verified" and lesson["verified"] == 1
    assert lesson["revision_rounds"] == 0
    assert len(store.get_quiz(conn, mid)["questions"]) == config.QUIZ_TARGET_QUESTIONS


def test_fact_checker_gets_sources_without_citations(coach, fake, conn, mid, monkeypatch):
    monkeypatch.setattr(web.requests, "get", FakeHTTP({URL: FakeResponse(article_html("Wall tool steps"))}).get)
    queue_curator_with_page(fake)
    fake.messages.text_replies.append(cited_teacher_reply())
    coach.request_lesson(mid)

    content = calls_of(fake, "fact_checker")[0]["messages"][0]["content"]
    docs = [b for b in content if b["type"] == "document"]
    assert len(docs) == 1 and "Wall tool steps" in docs[0]["source"]["data"]
    assert "citations" not in docs[0]  # can't combine citations with structured outputs
    assert "You are the Teacher" not in json.dumps(content)  # never sees the Teacher's prompt


def test_always_failing_stops_after_two_rewrites(coach, fake, conn, mid):
    fake.messages.defaults["fact_checker"] = fact_flags({"quote": "Draw walls."})
    queue_lesson(fake, PLAIN)
    fake.messages.text_replies += [PLAIN, PLAIN]  # the 2 rewrites
    lesson = coach.request_lesson(mid)

    assert len(calls_of(fake, "teacher")) == 3  # first draft + MAX_REVISION_ROUNDS rewrites
    assert len(calls_of(fake, "fact_checker")) == 3
    assert lesson["status"] == "flagged" and lesson["verified"] == 0
    assert lesson["revision_rounds"] == 2
    review = store.lesson_review(lesson)
    assert [f["quote"] for f in review["remaining_major"]] == ["Draw walls."]
    assert len(review["rounds"]) == 3
    assert store.get_quiz(conn, mid) is not None  # flagged lessons are still shown, with a quiz
    assert fake.messages.text_replies == []  # no 4th Teacher call queued or made


def test_fail_then_pass(coach, fake, conn, mid):
    fake.messages.by_agent["pedagogy"] = [ped_flags({"quote": "## Walls"})]  # round 1 only
    queue_lesson(fake, PLAIN)
    fake.messages.text_replies.append("## Walls\nDraw walls with the Wall tool.")
    lesson = coach.request_lesson(mid)

    assert len(calls_of(fake, "teacher")) == 2
    assert lesson["status"] == "partly_unsourced"  # passed; the section is model knowledge
    assert "Wall tool" in lesson["content"]
    assert lesson["revision_rounds"] == 1


def test_minor_flags_never_cause_a_rewrite(coach, fake, conn, mid):
    fake.messages.defaults["fact_checker"] = fact_flags({"quote": "Draw walls.", "severity": "minor"})
    queue_lesson(fake, PLAIN)
    lesson = coach.request_lesson(mid)
    assert len(calls_of(fake, "teacher")) == 1
    assert lesson["status"] == "partly_unsourced"
    assert [f["quote"] for f in store.lesson_review(lesson)["minor"]] == ["Draw walls."]


def test_unquoted_major_flag_is_minor_and_causes_no_rewrite(coach, fake, conn, mid):
    fake.messages.defaults["fact_checker"] = fact_flags({"quote": "Text that is not in the lesson"})
    queue_lesson(fake, PLAIN)
    lesson = coach.request_lesson(mid)
    assert len(calls_of(fake, "teacher")) == 1
    minor = store.lesson_review(lesson)["minor"]
    assert minor[0]["downgraded"] and not minor[0]["quote_found"]


def test_rewrite_reuses_sources_and_gets_feedback(coach, fake, conn, mid, monkeypatch):
    http = FakeHTTP({URL: FakeResponse(article_html("Wall tool steps"))})
    monkeypatch.setattr(web.requests, "get", http.get)
    fake.messages.by_agent["fact_checker"] = [fact_flags({"quote": "Use the Wall tool.",
                                                          "problem": "Wrong tool name"})]
    queue_curator_with_page(fake)
    fake.messages.text_replies += [cited_teacher_reply(), cited_teacher_reply()]
    coach.request_lesson(mid)

    first, second = calls_of(fake, "teacher")
    docs = lambda call: [b for b in call["messages"][0]["content"] if b["type"] == "document"]  # noqa: E731
    assert docs(first) == docs(second)  # same sources
    request = second["messages"][0]["content"][-1]["text"]
    assert "PREVIOUS DRAFT" in request and "Wrong tool name" in request and "Use the Wall tool." in request
    assert "[Source:" not in request.split("REVIEWER FEEDBACK")[0]  # draft without app labels
    assert sum(1 for _, c in fake.messages.calls if "tools" in c) == 1  # one Curator search only
    assert len(http.calls) == 1  # page downloaded once


def test_checkers_run_in_parallel(coach, fake, conn, mid):
    barrier = threading.Barrier(2, timeout=5)  # breaks unless both checkers wait at the same time

    def together(reply_):
        def run(_kwargs):
            barrier.wait()
            return reply_
        return run

    fake.messages.defaults["fact_checker"] = together(fact_flags())
    fake.messages.defaults["pedagogy"] = together(ped_flags())
    queue_lesson(fake, PLAIN)
    assert coach.request_lesson(mid)["status"] == "partly_unsourced"


def test_pedagogy_gets_reading_time_and_budget(coach, fake, conn, mid):
    queue_lesson(fake, "## Walls\n" + "word " * 1000)
    coach.request_lesson(mid)
    request = calls_of(fake, "pedagogy")[0]["messages"][0]["content"]
    assert "Reading time: 5.0 min (budget 20.0 min)" in request  # est_hours = 2 -> capped at 20


# ---------- when checks can't finish ----------

def test_budget_reached_during_checks_saves_unchecked(coach, fake, conn, mid):
    fake.messages.defaults["fact_checker"] = BudgetExceeded("This month's spend has reached $15.00.")
    queue_lesson(fake, PLAIN)
    lesson = coach.request_lesson(mid)
    assert lesson["status"] == "unchecked" and "Draw walls." in lesson["content"]
    assert "spend has reached" in store.lesson_review(lesson)["error"]
    assert store.get_quiz(conn, mid) is None
    assert calls_of(fake, "examiner") == []


def test_quiz_failure_keeps_checked_lesson(coach, fake, conn, mid):
    fake.messages.defaults["examiner"] = LLMError("examiner: reply was cut off (max_tokens).")
    queue_lesson(fake, PLAIN)
    lesson = coach.request_lesson(mid)
    assert lesson["status"] == "partly_unsourced"
    assert "cut off" in store.lesson_review(lesson)["quiz_error"]
    assert store.get_quiz(conn, mid) is None


# ---------- "Run checks" on old lessons ----------

def test_run_checks_on_phase2_lesson_needs_no_curator(coach, fake, conn, mid, monkeypatch):
    monkeypatch.setattr(web.requests, "get", FakeHTTP({}).get)  # would 404: must use the cache
    store.save_resources(conn, mid, [{"url": URL, "title": "Walls guide", "kind": "page", "role": "primary"}])
    store.cache_page(conn, URL, "Cached page: the Wall tool draws walls. " * 20)
    store.save_lesson(conn, mid, "## Walls\n[Source: web]\nUse the Wall tool[1].", [{"kind": "web", "ref": URL}],
                      "m", citations=[{"n": 1, "kind": "web"}], notes={"pdf": [], "web": []})
    calls_before = len(fake.messages.calls)

    lesson = coach.run_checks(mid)

    new_calls = fake.messages.calls[calls_before:]
    assert not any("tools" in c for _, c in new_calls)  # no Curator search
    assert {agent_of(c) for _, c in new_calls} == {"fact_checker", "pedagogy", "examiner", "quiz_solver",
                                                   "examiner_task"}  # Phase 4: task written once
    docs = [b for b in calls_of(fake, "fact_checker")[0]["messages"][0]["content"] if b["type"] == "document"]
    assert "Cached page" in docs[0]["source"]["data"]
    assert lesson["status"] == "verified"
    assert store.get_quiz(conn, mid) is not None


def test_run_checks_on_phase1_lesson_has_no_sources(coach, fake, conn, mid):
    store.save_lesson(conn, mid, PLAIN, [{"kind": "model", "ref": "m"}], "m")  # notes=None: Phase 1
    lesson = coach.run_checks(mid)
    request = calls_of(fake, "fact_checker")[0]["messages"][0]["content"]
    assert [b["type"] for b in request] == ["text"] and "No source documents" in request[0]["text"]
    assert lesson["status"] == "partly_unsourced"


def test_run_checks_without_lesson_is_refused(coach, mid):
    with pytest.raises(ValueError):
        coach.run_checks(mid)


# ---------- quiz: blind solver ----------

def test_solver_never_sees_the_answer_key(coach, fake, conn, mid):
    secret = mc("Which tool draws walls?", answer="C", choices=["Floor", "Roof", "Wall"])
    secret.alternates = ["SECRET-ALTERNATE"]
    secret.explanation = "SECRET-EXPLANATION"
    fake.messages.defaults["examiner"] = quiz(secret, secret, secret)
    fake.messages.defaults["quiz_solver"] = solver_says(lambda qid: "C")
    queue_lesson(fake, PLAIN)
    coach.request_lesson(mid)

    request = calls_of(fake, "quiz_solver")[0]["messages"][0]["content"]
    assert "SECRET" not in request
    view = json.loads(request.split("QUIZ:\n", 1)[1])
    for q in view:
        assert set(q) <= {"id", "kind", "question", "unit", "choices"}
    assert view[0]["choices"] == ["A. Floor", "B. Roof", "C. Wall"]


def test_solver_view_drops_key_fields():
    q = {"id": "q1", "kind": "numeric", "question": "R?", "choices": [], "answer": "2200", "unit": "Ω",
         "tolerance_pct": 1, "alternates": ["x"], "explanation": "because"}
    assert quiz_solver.solver_view([q]) == [{"id": "q1", "kind": "numeric", "question": "R?", "unit": "Ω"}]


def test_disagreements_are_dropped_and_logged(coach, fake, conn, mid):
    fake.messages.defaults["quiz_solver"] = solver_says(lambda qid: "B" if qid in ("q1", "q2") else "A")
    queue_lesson(fake, PLAIN)
    coach.request_lesson(mid)

    saved = store.get_quiz(conn, mid)
    assert [q["id"] for q in saved["questions"]] == ["q3", "q4", "q5"]
    assert [(d["id"], d["answer"], d["solver_answer"]) for d in saved["dropped"]] == [("q1", "A", "B"),
                                                                                     ("q2", "A", "B")]
    assert saved["dropped"][0]["reason"] == "solver's answer differs from the key"
    assert saved["redrafts"] == 0 and not saved["flagged"]
    assert len(calls_of(fake, "examiner")) == 1  # 3 left = enough, no redraft


def test_too_few_left_means_redraft(coach, fake, conn, mid):
    fake.messages.defaults["quiz_solver"] = solver_says(lambda qid: "B" if qid in ("q1", "q2", "q3") else "A")
    queue_lesson(fake, PLAIN)
    coach.request_lesson(mid)

    examiner_calls = calls_of(fake, "examiner")
    assert len(examiner_calls) == 2
    redraft = examiner_calls[1]["messages"][0]["content"]
    assert "Write 3 questions." in redraft  # replaces the 3 dropped ones
    assert "AVOID" in redraft and "solver: B, key: A" in redraft
    saved = store.get_quiz(conn, mid)
    assert len(saved["questions"]) == 2 + 5 and saved["redrafts"] == 1 and not saved["flagged"]
    assert len({q["id"] for q in saved["questions"] + saved["dropped"]}) == 10  # ids never reused


def test_redrafts_stop_at_two_then_flag(coach, fake, conn, mid):
    fake.messages.defaults["quiz_solver"] = solver_says(lambda qid: "B")  # always disagrees
    queue_lesson(fake, PLAIN)
    coach.request_lesson(mid)

    assert len(calls_of(fake, "examiner")) == 1 + config.QUIZ_MAX_REDRAFTS
    saved = store.get_quiz(conn, mid)
    assert saved["questions"] == [] and saved["flagged"] and saved["redrafts"] == 2
    assert len(saved["dropped"]) == 15


def test_missing_solver_answer_is_dropped(coach, fake, conn, mid):
    from agents.quiz_solver import SolverAnswer, SolverAnswers
    fake.messages.defaults["quiz_solver"] = lambda kw: SolverAnswers(answers=[
        SolverAnswer(id=i, answer="A", note="") for i in quiz_ids(kw) if i != "q2"])
    queue_lesson(fake, PLAIN)
    coach.request_lesson(mid)
    dropped = store.get_quiz(conn, mid)["dropped"]
    assert [(d["id"], d["reason"]) for d in dropped] == [("q2", "solver gave no answer")]


def test_regenerate_replaces_quiz(coach, fake, conn, mid):
    queue_lesson(fake, PLAIN)
    coach.request_lesson(mid)
    fake.messages.defaults["examiner"] = quiz(*[mc(f"New {i}?") for i in range(5)])
    queue_lesson(fake, PLAIN)
    coach.request_lesson(mid, regenerate=True)
    assert store.get_quiz(conn, mid)["questions"][0]["question"] == "New 0?"


# ---------- schemas + database ----------

def test_checker_schemas_round_trip():
    from agents.examiner import Quiz
    from agents.fact_checker import FactCheck
    from agents.pedagogy import PedagogyReview
    from agents.quiz_solver import SolverAnswers
    for obj in (fact_flags({"quote": "x"}), ped_flags({"quote": "x"}), quiz(mc()),
                SolverAnswers(answers=[{"id": "q1", "answer": "A", "note": ""}])):
        assert type(obj).model_validate_json(obj.model_dump_json()) == obj
    for schema in (FactCheck, PedagogyReview, Quiz, SolverAnswers):
        assert schema.model_json_schema()["type"] == "object"


def test_fact_check_schema_rejects_bad_severity():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        fact_checker.FactFlag(quote="x", problem="p", evidence="e", basis="source", severity="critical")


PHASE2_LESSONS = """CREATE TABLE lessons (
    id INTEGER PRIMARY KEY AUTOINCREMENT, milestone_id INTEGER NOT NULL UNIQUE,
    content TEXT NOT NULL, verified INTEGER NOT NULL DEFAULT 0, sources_json TEXT NOT NULL,
    model TEXT, created_at TEXT NOT NULL, citations_json TEXT, notes_json TEXT)"""


def test_phase2_database_gets_phase3_columns(tmp_path):
    path = tmp_path / "coach.sqlite"
    old = sqlite3.connect(path)
    old.execute(PHASE2_LESSONS)
    old.execute("INSERT INTO lessons (milestone_id, content, sources_json, created_at)"
                " VALUES (1, 'old lesson', '[]', '2026-10-01T10:00:00')")
    old.commit()
    old.close()

    conn = store.connect(path)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(lessons)")}
    assert {"status", "review_json", "revision_rounds"} <= cols
    row = conn.execute("SELECT * FROM lessons").fetchone()
    assert row["content"] == "old lesson" and store.lesson_status(row) == "unchecked"
    assert len(list(tmp_path.glob("coach.backup-*.sqlite"))) == 1
    conn.close()


def test_delete_subject_removes_quizzes(coach, fake, conn, mid):
    queue_lesson(fake, PLAIN)
    coach.request_lesson(mid)
    pid = store.get_milestone(conn, mid)["profile_id"]
    coach.delete_subject(pid)
    assert conn.execute("SELECT COUNT(*) FROM quizzes").fetchone()[0] == 0


def test_month_spend_for_one_agent(conn):
    store.log_usage(conn, None, "fact_checker_eval", "m", 1, 1, 0, 0, 0.5)
    store.log_usage(conn, None, "teacher", "m", 1, 1, 0, 0, 2.0)
    assert store.month_spend(conn, agent="fact_checker_eval") == 0.5
    assert store.month_spend(conn) == 2.5
