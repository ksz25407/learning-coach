"""Phase 4 UI smoke tests: exam form (no checkbox, no visible answers), missed-question feedback,
Due today review panel, overview. No API calls."""
from datetime import datetime, timedelta, timezone

from streamlit.testing.v1 import AppTest

import clock
import config
from db import store
from tests.test_agents import PROFILE

APP = str(config.ROOT / "ui" / "app.py")


def q(i, concept="wall tool"):
    return {"id": f"q{i}", "kind": "multiple_choice", "question": f"Question {i}?", "choices": ["Wall", "Floor"],
            "answer": "A", "unit": "", "tolerance_pct": 0, "alternates": [], "explanation": f"Because {i}.",
            "concept": concept, "section": "Drawing walls"}


def setup(tmp_path, monkeypatch, n_questions=5):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "ui.sqlite")
    conn = store.connect(config.DB_PATH)
    pid = store.create_profile(conn)
    store.save_profile_data(conn, pid, PROFILE)
    store.set_stage(conn, pid, "learning")
    store.save_milestones(conn, pid, [{"title": "Walls", "objective": "Model walls", "est_hours": 2}] * 4)
    mid = store.get_milestones(conn, pid)[0]["id"]
    store.save_lesson(conn, mid, "## Drawing walls\nUse the Wall tool.", [], "m", notes={},
                      status="partly_unsourced", review={"rounds": [], "revisions": 0})
    qs = [q(i) for i in range(1, n_questions + 1)]
    store.save_quiz(conn, mid, qs, [], 0, False)
    store.add_to_bank(conn, mid, qs)
    store.save_task(conn, mid, "file", "Model the ground floor walls.",
                    [{"id": "r1", "criterion": "Walls drawn", "evidence": "walls in plan view"}],
                    ["floor plan with dimensions"])
    return conn, pid, mid


def open_app(pid, mid=None):
    at = AppTest.from_file(APP, default_timeout=30)
    at.session_state["profile_id"] = pid
    if mid:
        at.session_state["open_milestone"] = mid
    return at.run()


def test_exam_replaces_checkbox_and_hides_answers(tmp_path, monkeypatch):
    conn, pid, mid = setup(tmp_path, monkeypatch)
    conn.close()
    at = open_app(pid, mid)
    assert not at.exception
    assert len(at.checkbox) == 0  # the old "Mark done" checkbox is gone
    labels = [e.label for e in at.expander]
    assert "Show answers" not in labels
    page = " ".join(m.value for m in at.markdown)
    assert "Question 1?" in page and "Because 1." not in page  # explanations only after you answer
    assert "Model the ground floor walls." in page and "floor plan with dimensions" in page
    assert len(at.radio) == 5
    assert any(b.label.startswith("Submit for grading (~\\$") for b in at.button)


def test_failed_quiz_shows_correct_answers_and_section_links(tmp_path, monkeypatch):
    conn, pid, mid = setup(tmp_path, monkeypatch)
    conn.close()
    at = open_app(pid, mid)
    for r in at.radio:
        r.set_value("B")  # all wrong
    quiz_submit = next(b for b in at.button if b.label == "Submit answers")
    quiz_submit.click().run()
    assert not at.exception
    page = " ".join(m.value for m in at.markdown)
    assert "Correct: **A. Wall**" in page and "Because 1." in page
    assert "[Drawing walls](#drawing-walls)" in page
    assert any(b.label.startswith("Get a new quiz (~\\$") for b in at.button)
    conn = store.connect(config.DB_PATH)
    assert store.get_milestone(conn, mid)["status"] == "active"  # not passed
    assert len(store.cards(conn, pid)) == 1  # missed concept went into review


def test_due_today_review_and_hard_easy(tmp_path, monkeypatch):
    conn, pid, mid = setup(tmp_path, monkeypatch, n_questions=1)
    from review import new_card
    card_json, due = new_card()
    store.add_card(conn, pid, mid, "wall tool", card_json, due)
    conn.close()

    at = open_app(pid)
    assert not at.exception
    page = " ".join(m.value for m in at.markdown)
    assert "Due today: 1" in page and "Question 1?" in page
    at.radio[0].set_value("A")
    next(b for b in at.button if b.label == "Check").click().run()
    assert not at.exception
    assert any("Right: A. Wall" in s.value for s in at.success)
    at.button(key="adjeasy").click().run()
    assert not at.exception
    conn = store.connect(config.DB_PATH)
    log = conn.execute("SELECT rating FROM review_log").fetchone()
    assert log["rating"] == "easy"
    at.button(key="review_next").click().run()
    assert "Nothing left for today." in " ".join(c.value for c in at.caption)


def test_overview_and_passed_badges(tmp_path, monkeypatch):
    conn, pid, mid = setup(tmp_path, monkeypatch)
    store.mark_passed(conn, mid, "exam")
    store.set_deadline(conn, pid, "2020-01-01")  # long past -> behind
    conn.close()
    at = open_app(pid)
    assert not at.exception
    assert [m.value for m in at.metric][:2] == ["1/4", "2 / 8 h"]
    assert any("Behind" in w.value for w in at.warning)
    page = " ".join(m.value for m in at.markdown)
    assert "Passed by exam" in page
