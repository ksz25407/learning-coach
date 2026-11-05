"""Smoke test: the Streamlit page loads and 'New subject' works. No API calls."""
from streamlit.testing.v1 import AppTest

import config


def test_app_loads_and_starts_subject(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "ui.sqlite")  # never touch the real DB
    at = AppTest.from_file(str(config.ROOT / "ui" / "app.py"), default_timeout=30).run()
    assert not at.exception
    assert any("New subject" in i.value for i in at.info)

    at.button[0].click().run()  # "+ New subject"
    assert not at.exception
    assert at.subheader[0].value == "Let's scope your subject"
    assert any("What would you like to learn" in m.markdown[0].value for m in at.chat_message)


def test_delete_needs_confirmation(tmp_path, monkeypatch):
    from db import store

    monkeypatch.setattr(config, "DB_PATH", tmp_path / "ui.sqlite")
    conn = store.connect(config.DB_PATH)
    store.create_profile(conn)  # a saved subject ("+ New subject" alone saves nothing)
    conn.close()
    at = AppTest.from_file(str(config.ROOT / "ui" / "app.py"), default_timeout=30).run()

    at.button(key="delete").click().run()  # first click: only asks
    assert not at.exception
    assert len(at.selectbox[0].options) == 1  # not deleted yet
    assert any("Delete" in w.value for w in at.warning)

    at.button(key="delete_cancel").click().run()  # cancel keeps it
    assert len(at.selectbox[0].options) == 1

    at.button(key="delete").click().run()
    at.button(key="delete_yes").click().run()  # second click: deletes
    assert not at.exception
    assert len(at.selectbox) == 0  # no subjects left
    assert any("New subject" in i.value for i in at.info)


def test_lesson_view_shows_sources(tmp_path, monkeypatch):
    """A Phase 2 lesson shows resources, the Outdated badge, Regenerate with a cost, and notes."""
    from db import store
    from tests.test_agents import PROFILE

    monkeypatch.setattr(config, "DB_PATH", tmp_path / "ui.sqlite")
    conn = store.connect(config.DB_PATH)
    pid = store.create_profile(conn)
    store.save_profile_data(conn, pid, PROFILE)
    store.set_stage(conn, pid, "learning")
    store.save_milestones(conn, pid, [{"title": "Walls", "objective": "Model walls in 20 min", "est_hours": 2}] * 4)
    mid = store.get_milestones(conn, pid)[0]["id"]
    store.save_resources(conn, mid, [
        {"url": "https://e.com/walls", "title": "Walls guide", "kind": "page", "section": "Creating walls",
         "minutes": 45, "why_best": "official docs", "role": "primary"}])
    store.save_lesson(conn, mid, "## Walls\n[Source: web]\nUse the Wall tool[1].", [{"kind": "web", "ref": "x"}],
                      "m", citations=[{"n": 1, "kind": "web"}],
                      notes={"pdf": [{"type": "pdf_pages", "file": "manual.pdf", "pages": [3, 7]}],
                             "web": [{"type": "web_skipped", "url": "https://e.com/js", "reason": "HTTP 404"}],
                             "curator": {"dropped_urls": [], "search_errors": []}})
    conn.execute("UPDATE lessons SET created_at = '2026-01-01T00:00:00'")
    store.add_pdf(conn, pid, "manual.pdf", ["text " * 50], None, None, 1)  # newer than the lesson
    conn.close()

    at = AppTest.from_file(str(config.ROOT / "ui" / "app.py"), default_timeout=30)
    at.session_state["profile_id"] = pid
    at.session_state["open_milestone"] = mid
    at.run()
    assert not at.exception

    page = " ".join(m.value for m in at.markdown)
    assert "Walls guide" in page and "Over 30 min" in page
    assert "pages 3, 7" in page and "HTTP 404" in page
    assert any("Not checked yet" in w.value for w in at.warning)
    assert any(b.label.startswith("Regenerate with current sources (~\\$") for b in at.button)
    assert any(b.label.startswith("Run checks (~\\$") for b in at.button)
    assert any("Outdated" in str(e.proto) for e in at.get("badge")) or "Outdated" in page


def test_flagged_lesson_shows_problems_and_quiz(tmp_path, monkeypatch):
    """A flagged lesson lists its remaining problems on top; the quiz shows survivors + drops."""
    from db import store
    from tests.test_agents import PROFILE

    monkeypatch.setattr(config, "DB_PATH", tmp_path / "ui.sqlite")
    conn = store.connect(config.DB_PATH)
    pid = store.create_profile(conn)
    store.save_profile_data(conn, pid, PROFILE)
    store.set_stage(conn, pid, "learning")
    store.save_milestones(conn, pid, [{"title": "Walls", "objective": "Model walls", "est_hours": 2}] * 4)
    mid = store.get_milestones(conn, pid)[0]["id"]
    flag = {"checker": "fact", "quote": "Walls are 3 m thick", "problem": "Wrong thickness",
            "evidence": "Source: 300 mm", "kind": "source", "severity": "major", "quote_found": True}
    store.save_lesson(conn, mid, "## Walls\n[Source: web]\nWalls are 3 m thick[1].", [], "m",
                      notes={}, status="flagged", revision_rounds=2,
                      review={"rounds": [{"round": r, "flags": [flag]} for r in range(3)], "revisions": 2,
                              "remaining_major": [flag], "minor": []})
    q = {"id": "q1", "kind": "exact_term", "question": "Which tool draws walls?", "choices": [],
         "answer": "Wall", "unit": "", "tolerance_pct": 0, "alternates": [], "explanation": "See step 1."}
    store.save_quiz(conn, mid, [q], [{**q, "id": "q2", "question": "Odd one?", "solver_answer": "Floor",
                                      "solver_note": "", "reason": "solver's answer differs from the key",
                                      "draft": 0}], redrafts=2, flagged=True)
    conn.close()

    at = AppTest.from_file(str(config.ROOT / "ui" / "app.py"), default_timeout=30)
    at.session_state["profile_id"] = pid
    at.session_state["open_milestone"] = mid
    at.run()
    assert not at.exception
    assert any("after 2 rewrite(s)" in e.value and "Walls are 3 m thick" in e.value for e in at.error)
    assert not any(b.label.startswith("Run checks") for b in at.button)  # already checked
    page = " ".join(m.value for m in at.markdown)
    # Phase 4: a quiz from before the exam existed must be prepared first (concept tags + task).
    assert "Which tool draws walls?" not in page
    assert any(b.label.startswith("Prepare exam (~\\$") for b in at.button)
    assert "Odd one?" in page and any("Solver: Floor" in c.value for c in at.caption)
    assert any("Only 1 question(s) survived" in w.value for w in at.warning)
