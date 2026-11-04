"""Phase 5 step 4: solved-problem tasks with a verified answer key. No real API calls.

- the rubric has final-value items (number + unit + tolerance) and method items that accept
  any valid approach; final values are never shown to you before you solve the problem
- a blind Task solver (lesson + sources, never the rubric) checks the key; disagreement or a
  broken rubric sends the task back to the Examiner, at most TASK_MAX_REDRAFTS times
- the task is saved as "verified" or "unverified", and the UI shows which
"""
import json

import pytest
from streamlit.testing.v1 import AppTest

import checks
import config
from agents import examiner, grader, task_solver
from agents.examiner import RubricItem, Task
from agents.task_solver import Solution, SolvedValue
from db import store
from orchestrator import Coach
from sources.base import SourceChunk
from tests.conftest import agent_of, pass_all_checks
from tests.test_agents import PROFILE
from tests.test_orchestrator import queue_lesson, scope_to_learning

SOLVED = {**PROFILE, "subject": "Circuit analysis", "practice_type": "math", "proof_of_skill": "solved_problem"}
MILESTONE = {"title": "Thevenin", "objective": "Find Thevenin equivalents of 2-source circuits", "est_hours": 2,
             "profile_id": 1}
INSTRUCTIONS = ("Find $V_{th}$ in V and $R_{th}$ in kΩ seen from a-b.\n\n"
                "```spice\nV1 in 0 DC 12\nR1 in a 4k\nR2 a 0 12k\nR3 a b 3k\n```")


def item(kind, **kw):
    base = dict(kind=kind, criterion="", evidence="", quantity="", value="", unit="", tolerance_pct=0)
    return RubricItem(**{**base, **kw})


def solved_draft(vth="9", rth="6", instructions=INSTRUCTIONS, methods=2):
    return Task(instructions=instructions, screenshots=["photo of your handwritten working (all pages)"],
                rubric=[item("value", quantity="V_th", value=vth, unit="V", tolerance_pct=2),
                        item("value", quantity="$R_{th}$", value=rth, unit="kΩ", tolerance_pct=0)]
                       + [item("method", criterion=f"Correctly establishes step {i}", evidence=f"step {i} in the working")
                          for i in range(1, methods + 1)])


def solution(vth="9", rth="6", ambiguous=False, note=""):
    return Solution(values=[SolvedValue(quantity="V_th", value=vth, unit="V"),
                            SolvedValue(quantity="R_th", value=rth, unit="kΩ")],
                    working="Divider: 12*12/16 = 9 V; 3k + 4k||12k = 6k.", ambiguous=ambiguous, note=note)


def as_dict(draft):
    """A drafted task as draft_task() returns it (ids added, value items finished)."""
    return {"instructions": draft.instructions,
            "rubric": [examiner.finish_item(f"r{i}", r.model_dump()) for i, r in enumerate(draft.rubric, 1)]}


# ---------- rubric rules (Python) ----------

def test_value_items_get_criteria_without_the_value():
    rubric = as_dict(solved_draft())["rubric"]
    vth, rth = rubric[0], rubric[1]
    assert vth["criterion"] == "Final answer for V_th in V is correct (within 2%)."
    assert "9" not in vth["criterion"] + vth["evidence"]
    assert rth["quantity"] == "R_{th}" and rth["tolerance_pct"] == config.TASK_DEFAULT_TOLERANCE_PCT
    assert rubric[2]["criterion"] == "Correctly establishes step 1"  # method items untouched


def test_good_rubric_has_no_problems():
    assert examiner.rubric_problems(as_dict(solved_draft())) == []


@pytest.mark.parametrize("draft,expect", [
    (Task(instructions=INSTRUCTIONS, screenshots=[], rubric=[item("method", criterion="a")] * 3),
     "needs 1-2 items of kind 'value'"),
    (solved_draft(methods=1), "needs 2-4 items of kind 'method'"),
    (solved_draft(vth="about nine"), "is not a number"),
    (solved_draft(instructions="Find the voltage."), "never names the quantity 'V_th'"),
])
def test_bad_rubrics_are_caught(draft, expect):
    problems = examiner.rubric_problems(as_dict(draft))
    assert any(expect in p for p in problems), problems


# ---------- comparing the key with the blind solver ----------

RUBRIC = as_dict(solved_draft())["rubric"]


def test_compare_key_matches_names_loosely_and_uses_tolerance():
    sol = {"values": {"Vth": "9.1 V", "R_{th}": "6000 Ω"}, "ambiguous": False}
    comparison = checks.compare_key(RUBRIC, sol)
    assert [(c["quantity"], c["match"]) for c in comparison] == [("V_th", True), ("R_{th}", True)]
    assert checks.key_problems(comparison, sol) == []


def test_key_problems_name_the_disagreement_and_missing_values():
    sol = {"values": {"V_th": "7.2 V"}, "ambiguous": True, "note": "R2's value is unclear"}
    problems = checks.key_problems(checks.compare_key(RUBRIC, sol), sol)
    assert any("ambiguous or incomplete: R2's value is unclear" in p for p in problems)
    assert any("got V_th = 7.2 V, but your key says 9 V" in p for p in problems)
    assert any("could not give a value for R_{th}" in p for p in problems)


# ---------- the agents' requests ----------

DOC = SourceChunk("Course notes: use the ideal source model.", "pdf", "notes.pdf p.4", "notes.pdf p.4")


def test_solved_tasks_are_drafted_by_sonnet_with_the_course_documents(llm, fake):
    fake.messages.parse_replies = [solved_draft()]
    task = examiner.draft_task(llm, SOLVED, MILESTONE, "LESSON TEXT", chunks=[DOC])
    kwargs = fake.messages.calls[0][1]
    assert kwargs["model"] == config.MODELS["examiner_task_solved"] == config.SONNET
    content = kwargs["messages"][0]["content"]
    assert content[0]["type"] == "document" and "citations" not in content[0]
    assert content[1]["cache_control"] == {"type": "ephemeral"}
    assert "LESSON TEXT" in content[-1]["text"]
    assert task["rubric"][0]["criterion"].startswith("Final answer for V_th")


def test_other_proof_types_keep_the_cheap_model(llm, fake):
    fake.messages.parse_replies = [solved_draft()]
    examiner.draft_task(llm, PROFILE, MILESTONE, "L")
    assert fake.messages.calls[0][1]["model"] == config.MODELS["examiner_task"]


def test_task_solver_sees_problem_and_sources_but_never_the_key(llm, fake):
    fake.messages.parse_replies = [solution()]
    task = as_dict(solved_draft(vth="9.75", rth="6.25"))  # values that appear nowhere else
    out = task_solver.solve(llm, SOLVED, MILESTONE, task, "LESSON TEXT", chunks=[DOC])
    kwargs = fake.messages.calls[0][1]
    assert agent_of(kwargs) == "task_solver" and kwargs["model"] == config.SONNET
    sent = json.dumps(kwargs["messages"], ensure_ascii=False)
    assert "9.75" not in sent and "6.25" not in sent and "Correctly establishes" not in sent
    request = kwargs["messages"][0]["content"][-1]["text"]
    assert "LESSON TEXT" in request and "V_{th}" in request
    assert "- V_th (in V)" in request and "- R_{th} (in kΩ)" in request
    assert kwargs["messages"][0]["content"][0]["type"] == "document"
    assert out["values"] == {"V_th": "9 V", "R_th": "6 kΩ"}


def test_grader_gets_the_correct_value_separately():
    vth = grader.rubric_for_grader(RUBRIC[0])
    assert vth["correct value"] == "9 V" and vth["allowed error"] == "2%"
    assert "any valid method" in grader.rubric_for_grader(RUBRIC[2])["note"]
    assert "Never write the correct value" in grader.SYSTEM


# ---------- the pipeline ----------

@pytest.fixture
def coach(conn, llm, fake):
    pass_all_checks(fake)
    fake.messages.defaults["examiner_task"] = solved_draft()
    fake.messages.defaults["task_solver"] = solution()
    return Coach(conn, llm)


@pytest.fixture
def mid(coach, fake, conn):
    pid = scope_to_learning(coach, fake)
    store.save_profile_data(conn, pid, SOLVED)
    return store.get_milestones(conn, pid)[0]["id"]


def calls_of(fake, agent):
    return [c for _, c in fake.messages.calls if agent_of(c) == agent]


def test_matching_key_is_verified_first_time(coach, fake, conn, mid):
    queue_lesson(fake)
    coach.request_lesson(mid)
    task = store.get_task(conn, mid)
    assert task["key_status"] == "verified" and task["key_check"]["problems"] == []
    assert len(calls_of(fake, "examiner_task")) == 1 and len(calls_of(fake, "task_solver")) == 1
    assert calls_of(fake, "examiner_task")[0]["model"] == config.SONNET
    assert task["rubric"][0]["value"] == "9"  # kept for the Grader, not shown in the criterion


def test_disagreement_is_redrafted_then_verified(coach, fake, conn, mid):
    fake.messages.by_agent["examiner_task"] = [solved_draft(vth="8"), solved_draft()]
    queue_lesson(fake)
    coach.request_lesson(mid)
    drafts = calls_of(fake, "examiner_task")
    assert len(drafts) == 2 and len(calls_of(fake, "task_solver")) == 2
    feedback = drafts[1]["messages"][0]["content"][-1]["text"]
    assert "a blind solver got V_th = 9 V, but your key says 8 V" in feedback
    task = store.get_task(conn, mid)
    assert task["key_status"] == "verified" and len(task["key_check"]["attempts"]) == 2


def test_key_never_confirmed_is_saved_unverified(coach, fake, conn, mid):
    fake.messages.defaults["task_solver"] = solution(vth="5")
    queue_lesson(fake)
    coach.request_lesson(mid)
    assert len(calls_of(fake, "examiner_task")) == 1 + config.TASK_MAX_REDRAFTS
    task = store.get_task(conn, mid)
    assert task["key_status"] == "unverified"
    assert any("got V_th = 5 V" in p for p in task["key_check"]["problems"])


def test_broken_rubric_is_redrafted_without_asking_the_solver(coach, fake, conn, mid):
    fake.messages.by_agent["examiner_task"] = [solved_draft(methods=0), solved_draft()]
    queue_lesson(fake)
    coach.request_lesson(mid)
    assert len(calls_of(fake, "examiner_task")) == 2 and len(calls_of(fake, "task_solver")) == 1
    assert store.get_task(conn, mid)["key_status"] == "verified"


def test_other_proof_types_have_no_key_check(coach, fake, conn, mid):
    store.save_profile_data(conn, store.get_milestone(conn, mid)["profile_id"], PROFILE)  # "file"
    queue_lesson(fake)
    coach.request_lesson(mid)
    assert calls_of(fake, "task_solver") == []
    task = store.get_task(conn, mid)
    assert task["key_status"] is None and task["key_check"] is None


def test_phase4_task_table_gets_key_columns(tmp_path):
    import sqlite3
    path = tmp_path / "coach.sqlite"
    old = sqlite3.connect(path)
    old.executescript("""
        CREATE TABLE tasks (id INTEGER PRIMARY KEY AUTOINCREMENT, milestone_id INTEGER NOT NULL UNIQUE,
            proof_type TEXT NOT NULL, instructions TEXT NOT NULL, rubric_json TEXT NOT NULL,
            screenshots_json TEXT NOT NULL, created_at TEXT NOT NULL);
        INSERT INTO tasks (milestone_id, proof_type, instructions, rubric_json, screenshots_json, created_at)
            VALUES (1, 'file', 'Old task', '[{"id": "r1", "criterion": "c", "evidence": "e"}]', '[]', 'x');
    """)
    old.close()
    conn = store.connect(path)
    task = store.get_task(conn, 1)
    assert task["key_status"] is None and task["key_check"] is None  # old task: nothing to verify
    assert grader.rubric_for_grader(task["rubric"][0]) == {"id": "r1", "criterion": "c", "evidence needed": "e"}
    conn.close()


# ---------- UI ----------

def ui_setup(tmp_path, monkeypatch, key_status):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "ui.sqlite")
    conn = store.connect(config.DB_PATH)
    pid = store.create_profile(conn)
    store.save_profile_data(conn, pid, SOLVED)
    store.set_stage(conn, pid, "learning")
    store.save_milestones(conn, pid, [{"title": "Thevenin", "objective": "Find V_th", "est_hours": 2}] * 4)
    mid = store.get_milestones(conn, pid)[0]["id"]
    store.save_lesson(conn, mid, "## Thevenin\nText.", [], "m", notes={}, status="partly_unsourced",
                      review={"rounds": [], "revisions": 0})
    rubric = as_dict(solved_draft(vth="9.75"))["rubric"]
    check = {"attempts": [{"attempt": 0, "problems": ["a blind solver got V_th = 9 V, but your key says 9.75 V"],
                           "comparison": checks.compare_key(rubric, {"values": {"V_th": "9 V"}})}],
             "problems": ["a blind solver got V_th = 9 V, but your key says 9.75 V"]}
    store.save_task(conn, mid, "solved_problem", INSTRUCTIONS, rubric, [], key_status=key_status,
                    key_check=check)
    conn.close()
    at = AppTest.from_file(str(config.ROOT / "ui" / "app.py"), default_timeout=30)
    at.session_state["profile_id"] = pid
    at.session_state["open_milestone"] = mid
    return at.run()


def test_ui_shows_unverified_key_without_revealing_the_answer(tmp_path, monkeypatch):
    at = ui_setup(tmp_path, monkeypatch, "unverified")
    assert not at.exception
    page = " ".join(m.value for m in at.markdown) + " ".join(c.value for c in at.caption)
    assert "couldn't confirm the expected final values" in page
    assert "Final answer for V_th in V" in page
    assert "9.75" not in page  # the expected value is hidden until you pass
    assert "Answer key check" not in [e.label for e in at.expander]


def test_ui_verified_key_has_no_warning(tmp_path, monkeypatch):
    at = ui_setup(tmp_path, monkeypatch, "verified")
    assert not at.exception
    page = " ".join(c.value for c in at.caption)
    assert "couldn't confirm" not in page
