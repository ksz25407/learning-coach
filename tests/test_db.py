from datetime import datetime

from db import store


def test_all_tables_created(conn):
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for t in ["profiles", "milestones", "objectives", "lessons", "quiz_results",
              "review_schedule", "usage_log", "chat_messages"]:
        assert t in names


def test_connect_twice_is_safe(tmp_path):
    store.connect(tmp_path / "x.sqlite").close()
    store.connect(tmp_path / "x.sqlite").close()


def test_first_milestone_active_rest_locked(conn):
    pid = store.create_profile(conn)
    store.save_milestones(conn, pid, [{"title": f"M{i}", "objective": "Do X", "est_hours": 1} for i in range(4)])
    assert [m["status"] for m in store.get_milestones(conn, pid)] == ["active", "locked", "locked", "locked"]


def test_month_spend_ignores_last_month(conn):
    store.log_usage(conn, None, "coach", "m", 1, 1, 0, 0, 2.0, ts="2026-10-31T23:59:00")
    store.log_usage(conn, None, "coach", "m", 1, 1, 0, 0, 3.0, ts="2026-11-02T10:00:00")
    assert store.month_spend(conn, today=datetime(2026, 11, 15)) == 3.0
    assert store.total_spend(conn) == 5.0


# ---------- Phase 2 ----------

import sqlite3  # noqa: E402

PHASE1_LESSONS = """CREATE TABLE lessons (
    id INTEGER PRIMARY KEY AUTOINCREMENT, milestone_id INTEGER NOT NULL UNIQUE,
    content TEXT NOT NULL, verified INTEGER NOT NULL DEFAULT 0, sources_json TEXT NOT NULL,
    model TEXT, created_at TEXT NOT NULL)"""


def test_phase1_database_is_backed_up_then_upgraded(tmp_path):
    path = tmp_path / "coach.sqlite"
    old = sqlite3.connect(path)
    old.execute(PHASE1_LESSONS)
    old.execute("INSERT INTO lessons (milestone_id, content, sources_json, created_at)"
                " VALUES (1, 'old lesson', '[]', '2026-10-01T10:00:00')")
    old.commit()
    old.close()

    conn = store.connect(path)

    cols = {r["name"] for r in conn.execute("PRAGMA table_info(lessons)")}
    assert {"citations_json", "notes_json"} <= cols
    assert conn.execute("SELECT content FROM lessons").fetchone()[0] == "old lesson"
    backups = list(tmp_path.glob("coach.backup-*.sqlite"))
    assert len(backups) == 1
    b = sqlite3.connect(backups[0])
    assert {r[1] for r in b.execute("PRAGMA table_info(lessons)")} == {
        "id", "milestone_id", "content", "verified", "sources_json", "model", "created_at"}  # pre-upgrade copy
    b.close()
    conn.close()

    store.connect(path).close()  # nothing left to upgrade -> no second backup
    assert len(list(tmp_path.glob("coach.backup-*.sqlite"))) == 1


def test_new_database_makes_no_backup(tmp_path):
    store.connect(tmp_path / "new.sqlite").close()
    assert list(tmp_path.glob("*.backup-*")) == []


def _milestone(conn):
    pid = store.create_profile(conn)
    store.save_milestones(conn, pid, [{"title": "M", "objective": "Do X", "est_hours": 1}])
    return pid, store.get_milestones(conn, pid)[0]["id"]


def test_save_lesson_upserts(conn):
    _, mid = _milestone(conn)
    store.save_lesson(conn, mid, "v1", [], "m")
    store.save_lesson(conn, mid, "v2", [], "m", citations=[{"n": 1}], notes={"pdf": []})
    lesson = store.get_lesson(conn, mid)
    assert lesson["content"] == "v2"
    assert store.lesson_notes(lesson) == {"pdf": []}
    assert conn.execute("SELECT COUNT(*) FROM lessons").fetchone()[0] == 1


def test_outdated_flag(conn):
    pid, mid = _milestone(conn)
    store.save_lesson(conn, mid, "phase 1", [], "m")  # no notes = Phase 1 lesson
    assert store.lesson_is_outdated(conn, mid)
    store.save_lesson(conn, mid, "phase 2", [], "m", notes={})
    assert not store.lesson_is_outdated(conn, mid)
    conn.execute("UPDATE lessons SET created_at = '2026-01-01T00:00:00'")
    store.add_pdf(conn, pid, "new.pdf", ["text"], None, None, 1)  # PDF newer than the lesson
    assert store.lesson_is_outdated(conn, mid)
