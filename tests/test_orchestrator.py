import pytest

import config
from agents.planner import PlanningFailed
from agents.scoper import ScoperTurn, SubjectProfile
from db import store
from orchestrator import Coach
from tests.test_agents import PROFILE, plan_draft


@pytest.fixture
def coach(conn, llm):
    return Coach(conn, llm)


def scope_to_learning(coach, fake):
    """Drive a new subject all the way to the learning stage."""
    pid = coach.new_subject()
    fake.messages.parse_replies = [
        ScoperTurn(done=False, next_question="What's your goal?", profile=None),
        ScoperTurn(done=True, next_question="", profile=SubjectProfile(**PROFILE)),
        plan_draft(4),
    ]
    assert coach.handle_message(pid, "Revit") == "What's your goal?"
    coach.handle_message(pid, "Model my house")
    assert coach.stage(pid) == "confirm_profile"
    coach.confirm_profile(pid, {**PROFILE, "hours_per_week": 6})
    assert coach.stage(pid) == "planning"
    coach.make_plan(pid)
    assert coach.stage(pid) == "learning"
    return pid


def test_full_flow(coach, fake, conn):
    pid = scope_to_learning(coach, fake)
    assert store.get_profile_data(conn, pid)["hours_per_week"] == 6  # edit was saved
    assert len(store.get_milestones(conn, pid)) == 4


def test_scoper_history_starts_with_user(coach, fake):
    pid = coach.new_subject()
    fake.messages.parse_replies = [ScoperTurn(done=False, next_question="Q?", profile=None)]
    coach.handle_message(pid, "Revit")
    sent = fake.messages.calls[0][1]["messages"]
    assert sent == [{"role": "user", "content": "Revit"}]  # greeting not sent


def test_scoper_told_to_finish_after_max_questions(coach, fake, conn):
    pid = coach.new_subject()
    for i in range(config.SCOPER_MAX_QUESTIONS):
        store.add_chat(conn, pid, "scoping", "user", f"a{i}")
        store.add_chat(conn, pid, "scoping", "assistant", f"q{i}")
    fake.messages.parse_replies = [ScoperTurn(done=True, next_question="", profile=SubjectProfile(**PROFILE))]
    coach.handle_message(pid, "last answer")
    system = fake.messages.calls[0][1]["system"]
    assert "maximum number of questions" in system[-1]["text"]


def test_planning_failure_keeps_stage(coach, fake):
    pid = coach.new_subject()
    store.save_profile_data(coach.conn, pid, PROFILE)
    store.set_stage(coach.conn, pid, "planning")
    fake.messages.parse_replies = [plan_draft(2), plan_draft(10)]
    with pytest.raises(PlanningFailed):
        coach.make_plan(pid)
    assert coach.stage(pid) == "planning"


def test_complete_unlocks_next(coach, fake, conn):
    pid = scope_to_learning(coach, fake)
    ms = store.get_milestones(conn, pid)
    coach.complete_milestone(ms[0]["id"])
    assert [m["status"] for m in store.get_milestones(conn, pid)] == ["done", "active", "locked", "locked"]


def test_locked_lesson_refused(coach, fake, conn):
    pid = scope_to_learning(coach, fake)
    locked = store.get_milestones(conn, pid)[1]
    with pytest.raises(ValueError):
        coach.request_lesson(locked["id"])


def test_lesson_generated_once(coach, fake, conn):
    pid = scope_to_learning(coach, fake)
    first = store.get_milestones(conn, pid)[0]
    fake.messages.text_replies = ["## Walls\nDraw walls."]
    lesson1 = coach.request_lesson(first["id"])
    calls_after_first = len(fake.messages.calls)
    lesson2 = coach.request_lesson(first["id"])
    assert len(fake.messages.calls) == calls_after_first  # no new API call
    assert lesson1["id"] == lesson2["id"]
    assert lesson1["verified"] == 0


def test_chat_sends_only_last_n(coach, fake, conn):
    pid = scope_to_learning(coach, fake)
    for i in range(30):
        store.add_chat(conn, pid, "learning", "user", f"u{i}")
        store.add_chat(conn, pid, "learning", "assistant", f"a{i}")
    fake.messages.text_replies = ["ok"]
    coach.handle_message(pid, "new question")
    sent = fake.messages.calls[-1][1]["messages"]
    assert len(sent) <= config.CHAT_HISTORY_LIMIT
    assert sent[0]["role"] == "user"
    assert sent[-1] == {"role": "user", "content": "new question"}
