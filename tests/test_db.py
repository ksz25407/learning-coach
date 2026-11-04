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
