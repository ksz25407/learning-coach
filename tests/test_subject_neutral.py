"""Phase 5: no subject is hard-coded. Agent prompts and UI wording must not assume Revit or
software; examples from one subject are fine only when other kinds of work are covered too."""
from pathlib import Path

import pytest

from agents import curator, examiner, fact_checker, grader, pedagogy, planner, quiz_solver, scoper, teacher
import orchestrator

PROMPTS = {
    "scoper": scoper.SYSTEM,
    "planner": planner.SYSTEM,
    "curator_search": curator.SEARCH_SYSTEM,
    "curator_pick": curator.PICK_SYSTEM,
    "teacher": teacher.SYSTEM,
    "fact_checker": fact_checker.SYSTEM,
    "pedagogy": pedagogy.SYSTEM,
    "examiner": examiner.SYSTEM,
    "examiner_task": examiner.TASK_SYSTEM,
    "examiner_tags": examiner.TAG_SYSTEM,
    "quiz_solver": quiz_solver.SYSTEM,
    "grader": grader.SYSTEM,
    "coach": orchestrator.CHAT_SYSTEM,
}

# Product names and UI parts of one program: the planner may mention walls as one example
# among others, but no prompt may name a specific product or its panels.
SUBJECT_SPECIFIC = ["revit", "autodesk", "properties palette", "core centerline"]


@pytest.mark.parametrize("name", PROMPTS)
def test_prompt_names_no_specific_product(name):
    text = PROMPTS[name].lower()
    assert not [w for w in SUBJECT_SPECIFIC if w in text]


def test_task_and_grader_prompts_cover_photos_of_working():
    assert "handwritten" in examiner.TASK_SYSTEM
    assert "photo of the second page of working" in grader.SYSTEM  # a photo example, not only screenshots
    assert "last line of the working" in grader.SYSTEM


def test_scoper_asks_for_course_conventions():
    assert "conventions" in scoper.SYSTEM


def test_ui_wording_is_not_screenshot_only():
    app = Path(__file__).resolve().parent.parent.joinpath("ui", "app.py").read_text(encoding="utf-8")
    assert 'f"Screenshots (up to' not in app
    assert "screenshot(s)" not in app
    assert "photos of your working" in app
    assert "course conventions" in app
