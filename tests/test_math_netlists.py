"""Phase 5 step 2: LaTeX and SPICE netlists through the pipeline. No real API calls.

- citation markers never land inside maths or code blocks
- the quote rule ignores LaTeX $ delimiters
- a lesson with a broken netlist goes back to the Teacher (major flag from Python)
- quiz questions with a broken netlist are dropped before the Quiz solver sees them
- a task with a broken netlist gets one redraft, then a visible warning
- agents are told how to write maths and netlists; dollar amounts in the UI are escaped
"""
import re

import pytest

import checks
import config
from agents import examiner, formatting, grader, teacher
from agents.teacher import MARK, move_marks_out, write_lesson
from db import store
from orchestrator import CHAT_SYSTEM, Coach
from sources.base import SourceChunk
from tests.conftest import agent_of, cite, mc, pass_all_checks, quiz, quiz_ids, reply, solver_says, task_draft, text
from tests.test_orchestrator import queue_lesson, scope_to_learning
from tests.test_teacher import MILESTONE, PROFILE

M1, M2 = MARK.format(1), MARK.format(2)
GOOD = "```spice\nV1 in 0 DC 5\nR1 in out 1k\nR2 out 0 2.2k\n```"
BAD = "```spice\nV1 in 0 DC 5\nR1 in out 1M\nR2 out 0 2.2k\n```"


# ---------- citation markers ----------

@pytest.mark.parametrize("before,after", [
    (f"Ohm's law is $V = IR{M1}$ here.", f"Ohm's law is $V = IR${M1} here."),
    (f"$$\nV_{{out}} = V_{{in}}\\frac{{R_2}}{{R_1+R_2}}{M1}\n$$\nNext.",
     f"$$\nV_{{out}} = V_{{in}}\\frac{{R_2}}{{R_1+R_2}}\n$$\n{M1}\nNext."),
    (f"{GOOD[:-4]}{M1}\n```\nText.", f"{GOOD}\n{M1}\nText."),
    (f"Use `R1 in out 1k{M2}` here.", f"Use `R1 in out 1k`{M2} here."),
    (f"It costs \\$5 and \\$10{M1}.", f"It costs \\$5 and \\$10{M1}."),  # escaped $: not maths
    (f"Plain text{M1} stays.", f"Plain text{M1} stays."),
])
def test_marks_move_out_of_maths_and_code(before, after):
    assert move_marks_out(before) == after


def test_lesson_citation_inside_equation_ends_up_after_it(llm, fake):
    chunk = SourceChunk("V = IR", "pdf", "notes.pdf p.3", "notes.pdf p.3")
    fake.messages.text_replies = [reply(
        text("## Ohm\nThe law is $V = "),
        text("IR", [cite(0, "V = IR")]),
        text("$ for resistors.\n"),
    )]
    lesson = write_lesson(llm, PROFILE, MILESTONE, chunks=[chunk])
    assert "$V = IR$[1] for resistors." in lesson.text


# ---------- quote rule ----------

def test_quote_rule_ignores_dollar_delimiters():
    lesson = "## A\nThe gain is $A_v = -g_m R_D$ in saturation."
    assert checks.quote_found("A_v = -g_m R_D", lesson)
    assert checks.quote_found("$A_v = -g_m R_D$ in saturation", lesson)


# ---------- prompts ----------

def test_agents_get_the_formatting_rules():
    for system in (teacher.SYSTEM, examiner.SYSTEM, examiner.TASK_SYSTEM, CHAT_SYSTEM):
        assert formatting.MATH in system and formatting.CIRCUITS in system
    assert formatting.MATH in grader.SYSTEM
    assert "only when the subject involves electric circuits" in formatting.CIRCUITS
    assert "plain text" in examiner.SYSTEM  # units/answers stay plain text, never LaTeX


def test_formatting_example_netlist_is_valid():
    block = formatting.CIRCUITS.split("```spice\n", 1)[1].split("```", 1)[0]
    assert checks.block_problems(f"```spice\n{block}```") == []


# ---------- pipeline ----------

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


def test_broken_netlist_in_lesson_sends_it_back_to_the_teacher(coach, fake, mid):
    queue_lesson(fake, f"## Divider\n{BAD}\nFind V_out.")
    fake.messages.text_replies.append(f"## Divider\n{GOOD}\nFind V_out.")  # the rewrite
    lesson = coach.request_lesson(mid)

    teacher_calls = calls_of(fake, "teacher")
    assert len(teacher_calls) == 2
    feedback = teacher_calls[1]["messages"][0]["content"][-1]["text"]
    assert "Netlist check" in feedback and "R1 in out 1M" in feedback and "'M' means milli" in feedback
    assert lesson["status"] != "flagged" and "1M" not in lesson["content"]


def test_netlist_flag_stays_if_never_fixed(coach, fake, mid):
    queue_lesson(fake, f"## Divider\n{BAD}")
    fake.messages.text_replies += [f"## Divider\n{BAD}"] * config.MAX_REVISION_ROUNDS
    lesson = coach.request_lesson(mid)
    assert lesson["status"] == "flagged"
    assert len(calls_of(fake, "teacher")) == 1 + config.MAX_REVISION_ROUNDS


def test_quiz_question_with_broken_netlist_is_dropped_before_the_solver(coach, fake, conn, mid):
    good = [mc(f"Question {i}?") for i in range(4)]
    broken = mc(f"Find the current.\n\n{BAD}")
    fake.messages.by_agent["examiner"] = [quiz(*good, broken)]
    seen = []
    fake.messages.defaults["quiz_solver"] = lambda kw: (seen.append(quiz_ids(kw)), solver_says(lambda q: "A")(kw))[1]
    queue_lesson(fake)
    coach.request_lesson(mid)

    q = store.get_quiz(conn, mid)
    assert len(q["questions"]) == 4
    assert seen == [["q1", "q2", "q3", "q4"]]  # q5 never reached the solver
    drop = q["dropped"][0]
    assert drop["id"] == "q5" and drop["reason"].startswith("invalid netlist") and drop["solver_answer"] is None


def test_quiz_question_with_good_netlist_is_kept(coach, fake, conn, mid):
    fake.messages.by_agent["examiner"] = [quiz(*[mc(f"Question {i}?\n\n{GOOD}") for i in range(5)])]
    queue_lesson(fake)
    coach.request_lesson(mid)
    assert len(store.get_quiz(conn, mid)["questions"]) == 5


def test_task_with_broken_netlist_is_redrafted_once(coach, fake, conn, mid):
    bad, good = task_draft(), task_draft()
    bad.instructions = f"Find V_out.\n\n{BAD}"
    good.instructions = f"Find V_out.\n\n{GOOD}"
    fake.messages.by_agent["examiner_task"] = [bad, good]
    queue_lesson(fake)
    coach.request_lesson(mid)

    calls = calls_of(fake, "examiner_task")
    assert len(calls) == 2 and "'M' means milli" in calls[1]["messages"][0]["content"][-1]["text"]
    assert store.get_task(conn, mid)["instructions"] == good.instructions


def test_task_still_broken_after_redrafts_shows_a_warning(coach, fake, conn, mid):
    bad = task_draft()
    bad.instructions = f"Find V_out.\n\n{BAD}"
    fake.messages.by_agent["examiner_task"] = [bad] * (1 + config.TASK_MAX_REDRAFTS)
    queue_lesson(fake)
    coach.request_lesson(mid)
    instructions = store.get_task(conn, mid)["instructions"]
    assert instructions.startswith("**Warning:** the app's netlist check found problems")
    assert instructions.endswith(bad.instructions)


# ---------- UI ----------

def test_no_unescaped_dollar_amounts_in_the_ui():
    # ui/app.py runs Streamlit code on import, so check its source. Every amount goes through
    # usd(), which writes '\$' (the UI tests check the rendered button labels).
    src = (config.ROOT / "ui" / "app.py").read_text(encoding="utf-8")
    assert not re.search(r"(?<!\\)\$\{", src)
