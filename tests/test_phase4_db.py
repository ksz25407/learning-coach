"""Phase 4: migration, backups, UTC storage / local-day math, subject delete."""
import sqlite3
from datetime import datetime, timezone

import clock
import config
from db import store

# A Phase 3 database: old review_schedule (keyed by lesson_id), milestones without pass columns.
PHASE3_TABLES = """
CREATE TABLE profiles (id INTEGER PRIMARY KEY AUTOINCREMENT, subject TEXT,
    stage TEXT NOT NULL DEFAULT 'scoping', profile_json TEXT, created_at TEXT NOT NULL);
CREATE TABLE milestones (id INTEGER PRIMARY KEY AUTOINCREMENT, profile_id INTEGER NOT NULL,
    position INTEGER NOT NULL, title TEXT NOT NULL, objective TEXT NOT NULL, est_hours REAL,
    status TEXT NOT NULL DEFAULT 'locked');
CREATE TABLE review_schedule (id INTEGER PRIMARY KEY AUTOINCREMENT, lesson_id INTEGER NOT NULL,
    due_at TEXT NOT NULL, card_json TEXT);
INSERT INTO profiles (subject, stage, created_at) VALUES ('Revit', 'learning', '2026-10-01T10:00:00');
INSERT INTO milestones (profile_id, position, title, objective, est_hours, status)
    VALUES (1, 1, 'Walls', 'Draw walls', 2, 'done'), (1, 2, 'Floors', 'Draw floors', 2, 'active');
"""


def make_phase3_db(path):
    old = sqlite3.connect(path)
    old.executescript(PHASE3_TABLES)
    old.close()


def test_phase3_database_upgrades(tmp_path):
    path = tmp_path / "coach.sqlite"
    make_phase3_db(path)

    conn = store.connect(path)

    ms = store.get_milestones(conn, 1)
    assert ms[0]["pass_method"] == "manual_pre_phase4"  # ticked done before the exam existed
    assert ms[1]["pass_method"] is None
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(review_schedule)")}
    assert {"profile_id", "milestone_id", "concept"} <= cols and "lesson_id" not in cols
    assert "deadline_date" in {r["name"] for r in conn.execute("PRAGMA table_info(profiles)")}
    assert len(list(tmp_path.glob("coach.backup-*.sqlite"))) == 1
    conn.close()

    store.connect(path).close()  # already upgraded: no new backup
    assert len(list(tmp_path.glob("coach.backup-*.sqlite"))) == 1


def test_old_review_rows_are_kept_not_deleted(tmp_path):
    path = tmp_path / "coach.sqlite"
    make_phase3_db(path)
    old = sqlite3.connect(path)
    old.execute("INSERT INTO review_schedule (lesson_id, due_at) VALUES (1, '2026-10-01')")
    old.commit()
    old.close()

    conn = store.connect(path)
    assert conn.execute("SELECT COUNT(*) FROM review_schedule_phase3").fetchone()[0] == 1
    conn.close()


def test_only_newest_backups_are_kept(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MAX_BACKUPS", 5)
    for i in range(7):  # 7 old backups with increasing timestamps
        (tmp_path / f"coach.backup-20261001-00000{i}.sqlite").write_bytes(b"x")
    (tmp_path / "other.backup-20200101-000000.sqlite").write_bytes(b"x")  # another database's: untouched
    path = tmp_path / "coach.sqlite"
    make_phase3_db(path)

    store.connect(path).close()  # migration makes the 8th backup, then prunes

    left = sorted(p.name for p in tmp_path.glob("coach.backup-*.sqlite"))
    assert len(left) == 5
    assert "coach.backup-20261001-000000.sqlite" not in left  # oldest gone
    assert any(n.startswith("coach.backup-2026") and n > "coach.backup-20261001-000006" for n in left)  # new one kept
    assert (tmp_path / "other.backup-20200101-000000.sqlite").exists()


def test_no_pruning_without_a_migration(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MAX_BACKUPS", 1)
    path = tmp_path / "coach.sqlite"
    store.connect(path).close()
    for i in range(3):
        (tmp_path / f"coach.backup-20261001-00000{i}.sqlite").write_bytes(b"x")
    store.connect(path).close()  # nothing to upgrade
    assert len(list(tmp_path.glob("coach.backup-*.sqlite"))) == 3


# ---------- 1. UTC storage, local "today" ----------

def test_timestamps_are_stored_in_utc(conn):
    pid = store.create_profile(conn)
    store.save_milestones(conn, pid, [{"title": "M", "objective": "X", "est_hours": 1}])
    mid = store.get_milestones(conn, pid)[0]["id"]
    store.log_study(conn, mid, 30)
    logged = conn.execute("SELECT logged_at FROM study_log").fetchone()[0]
    assert logged.endswith("+00:00")
    assert abs(clock.from_iso(logged) - datetime.now(timezone.utc)).total_seconds() < 60


def test_local_day_uses_new_york_not_utc(monkeypatch):
    monkeypatch.setattr(config, "TIMEZONE", "America/New_York")
    # 2026-11-05 02:00 UTC is still 2026-11-04 21:00 in New York (EST, UTC-5).
    late_evening = datetime(2026, 11, 5, 2, 0, tzinfo=timezone.utc)
    assert str(clock.local_date(late_evening)) == "2026-11-04"
    start, end = clock.local_day_bounds(late_evening)
    assert start == "2026-11-04T05:00:00+00:00"
    assert end == "2026-11-05T05:00:00+00:00"


def test_local_day_handles_daylight_saving(monkeypatch):
    monkeypatch.setattr(config, "TIMEZONE", "America/New_York")
    # DST ends 2026-11-01: that local day is 25 hours long (EDT midnight -> EST midnight).
    start, end = clock.local_day_bounds(datetime(2026, 11, 1, 15, 0, tzinfo=timezone.utc))
    assert start == "2026-11-01T04:00:00+00:00"
    assert end == "2026-11-02T05:00:00+00:00"


# ---------- delete ----------

def test_delete_subject_removes_phase4_rows_keeps_usage(conn):
    pid = store.create_profile(conn)
    store.save_milestones(conn, pid, [{"title": "M", "objective": "X", "est_hours": 1}])
    mid = store.get_milestones(conn, pid)[0]["id"]
    q = {"question": "Q?", "concept": "walls", "kind": "exact_term", "answer": "a"}
    store.add_to_bank(conn, mid, [q])
    store.save_task(conn, mid, "file", "Do it", [{"id": "r1", "criterion": "c", "evidence": "e"}], ["shot"])
    sid = store.add_submission(conn, mid, "done", [("image/png", b"\x89PNG")], [])
    store.add_override(conn, mid, "r1", "fail", "pass", "I checked")
    card = store.add_card(conn, pid, mid, "walls", "{}", "2026-11-04T00:00:00+00:00")
    store.add_review_log(conn, card, None, False, "again", "exam", "{}")
    store.add_quiz_result(conn, mid, 0.5, False, {})
    store.log_study(conn, mid, 10)
    store.log_usage(conn, pid, "grader", "m", 1, 1, 0, 0, 0.01)

    store.delete_profile(conn, pid)

    for table in ("question_bank", "tasks", "task_submissions", "task_images", "overrides",
                  "review_schedule", "review_log", "quiz_results", "study_log", "milestones"):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, table
    assert conn.execute("SELECT COUNT(*) FROM usage_log").fetchone()[0] == 1
    assert sid  # (used above)


def test_bank_skips_duplicate_questions(conn):
    pid = store.create_profile(conn)
    store.save_milestones(conn, pid, [{"title": "M", "objective": "X", "est_hours": 1}])
    mid = store.get_milestones(conn, pid)[0]["id"]
    q = {"question": "Q?", "concept": "walls"}
    store.add_to_bank(conn, mid, [q, q])
    store.add_to_bank(conn, mid, [q])
    assert len(store.bank(conn, mid)) == 1
