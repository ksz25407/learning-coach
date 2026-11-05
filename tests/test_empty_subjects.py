"""Bug fix: '+ New subject' must not leave empty subjects in the sidebar. No API calls."""
from streamlit.testing.v1 import AppTest

import config
from db import store
from scripts.clean_empty_subjects import clean
from tests.test_agents import PROFILE


def run_app():
    return AppTest.from_file(str(config.ROOT / "ui" / "app.py"), default_timeout=30).run()


def test_new_subject_click_saves_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "ui.sqlite")
    at = run_app()
    at.button[0].click().run()  # "+ New subject"
    assert not at.exception
    assert at.subheader[0].value == "Let's scope your subject"
    assert any("What would you like to learn" in m.markdown[0].value for m in at.chat_message)

    at.button[0].click().run()  # click again: still nothing saved
    conn = store.connect(config.DB_PATH)
    assert store.list_profiles(conn) == []
    conn.close()

    at = run_app()  # "leaving": a fresh session shows no subject in the sidebar
    assert len(at.selectbox) == 0


def test_first_answer_saves_subject(tmp_path, monkeypatch):
    import orchestrator

    def fake_handle(self, pid, text):  # stands in for the Scoper call: stores the answer only
        store.add_chat(self.conn, pid, "scoping", "user", text)
        store.add_chat(self.conn, pid, "scoping", "assistant", "What's your goal?")

    monkeypatch.setattr(config, "DB_PATH", tmp_path / "ui.sqlite")
    monkeypatch.setattr(orchestrator.Coach, "handle_message", fake_handle)
    at = run_app()
    at.button[0].click().run()
    at.chat_input[0].set_value("Op-amp circuits").run()
    assert not at.exception

    conn = store.connect(config.DB_PATH)
    profiles = store.list_profiles(conn)
    assert len(profiles) == 1
    assert [r["content"] for r in store.get_chat(conn, profiles[0]["id"], "scoping")] == \
        ["Op-amp circuits", "What's your goal?"]
    assert store.empty_profile_ids(conn) == []
    conn.close()
    assert at.session_state["profile_id"] == profiles[0]["id"]
    assert len(at.selectbox) == 1  # now listed in the sidebar


def test_draft_ends_when_picking_saved_subject(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "ui.sqlite")
    conn = store.connect(config.DB_PATH)
    pid = store.create_profile(conn)
    store.save_profile_data(conn, pid, PROFILE)
    conn.close()

    at = run_app()
    at.button[0].click().run()  # start a draft
    assert at.selectbox[0].value is None  # no saved subject selected while drafting
    at.selectbox[0].select(pid).run()
    assert not at.exception
    assert at.session_state["profile_id"] == pid and not at.session_state["drafting"]
    conn = store.connect(config.DB_PATH)
    assert len(store.list_profiles(conn)) == 1
    conn.close()


def test_cleanup_deletes_only_empty_subjects(conn):
    empty = store.create_profile(conn)
    store.add_chat(conn, empty, "greeting", "assistant", "Hi! What would you like to learn?")

    answered = store.create_profile(conn)
    store.add_chat(conn, answered, "scoping", "user", "Op-amps")

    planned = store.create_profile(conn)
    store.save_milestones(conn, planned, [{"title": "A", "objective": "B", "est_hours": 1}])

    with_pdf = store.create_profile(conn)
    store.add_pdf(conn, with_pdf, "notes.pdf", ["text " * 50], None, None, 1)

    profiled = store.create_profile(conn)
    store.save_profile_data(conn, profiled, PROFILE)

    assert store.empty_profile_ids(conn) == [empty]
    assert clean(conn, delete=False) == [empty]  # dry run deletes nothing
    assert len(store.list_profiles(conn)) == 5

    assert clean(conn, delete=True) == [empty]
    left = {p["id"] for p in store.list_profiles(conn)}
    assert left == {answered, planned, with_pdf, profiled}
    assert store.get_chat(conn, empty) == []
