"""Small helper functions around SQLite. No ORM, just SQL."""
import json
import sqlite3
from datetime import datetime
from pathlib import Path

SCHEMA_FILE = Path(__file__).resolve().parent / "schema.sql"


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def connect(path) -> sqlite3.Connection:
    """Open the database and create any missing tables."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA_FILE.read_text(encoding="utf-8"))
    return conn


# ---------- profiles ----------

def create_profile(conn) -> int:
    cur = conn.execute("INSERT INTO profiles (created_at) VALUES (?)", (now(),))
    conn.commit()
    return cur.lastrowid


def get_profile(conn, profile_id: int):
    return conn.execute("SELECT * FROM profiles WHERE id = ?", (profile_id,)).fetchone()


def list_profiles(conn):
    return conn.execute("SELECT * FROM profiles ORDER BY id").fetchall()


def set_stage(conn, profile_id: int, stage: str):
    conn.execute("UPDATE profiles SET stage = ? WHERE id = ?", (stage, profile_id))
    conn.commit()


def save_profile_data(conn, profile_id: int, profile: dict):
    conn.execute(
        "UPDATE profiles SET subject = ?, profile_json = ? WHERE id = ?",
        (profile["subject"], json.dumps(profile, sort_keys=True), profile_id),
    )
    conn.commit()


def get_profile_data(conn, profile_id: int) -> dict | None:
    row = get_profile(conn, profile_id)
    if row is None or row["profile_json"] is None:
        return None
    return json.loads(row["profile_json"])


# ---------- chat ----------

def add_chat(conn, profile_id: int, stage: str, role: str, content: str):
    conn.execute(
        "INSERT INTO chat_messages (profile_id, stage, role, content, created_at) VALUES (?, ?, ?, ?, ?)",
        (profile_id, stage, role, content, now()),
    )
    conn.commit()


def get_chat(conn, profile_id: int, stage: str | None = None):
    if stage is None:
        sql, args = "SELECT * FROM chat_messages WHERE profile_id = ? ORDER BY id", (profile_id,)
    else:
        sql = "SELECT * FROM chat_messages WHERE profile_id = ? AND stage = ? ORDER BY id"
        args = (profile_id, stage)
    return conn.execute(sql, args).fetchall()


# ---------- milestones ----------

def save_milestones(conn, profile_id: int, milestones: list[dict]):
    """Replace the plan. First milestone is active, the rest locked."""
    conn.execute("DELETE FROM milestones WHERE profile_id = ?", (profile_id,))
    for i, m in enumerate(milestones, start=1):
        conn.execute(
            "INSERT INTO milestones (profile_id, position, title, objective, est_hours, status)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (profile_id, i, m["title"], m["objective"], m.get("est_hours"), "active" if i == 1 else "locked"),
        )
    conn.commit()


def get_milestones(conn, profile_id: int):
    return conn.execute(
        "SELECT * FROM milestones WHERE profile_id = ? ORDER BY position", (profile_id,)
    ).fetchall()


def get_milestone(conn, milestone_id: int):
    return conn.execute("SELECT * FROM milestones WHERE id = ?", (milestone_id,)).fetchone()


def set_milestone_status(conn, milestone_id: int, status: str):
    conn.execute("UPDATE milestones SET status = ? WHERE id = ?", (status, milestone_id))
    conn.commit()


# ---------- lessons ----------

def save_lesson(conn, milestone_id: int, content: str, sources: list[dict], model: str, verified=False):
    conn.execute(
        "INSERT INTO lessons (milestone_id, content, verified, sources_json, model, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (milestone_id, content, int(verified), json.dumps(sources), model, now()),
    )
    conn.commit()


def get_lesson(conn, milestone_id: int):
    return conn.execute("SELECT * FROM lessons WHERE milestone_id = ?", (milestone_id,)).fetchone()


# ---------- usage / cost ----------

def log_usage(conn, profile_id, agent, model, input_tokens, output_tokens,
              cache_write_tokens, cache_read_tokens, cost_usd, ts=None):
    conn.execute(
        "INSERT INTO usage_log (ts, profile_id, agent, model, input_tokens, output_tokens,"
        " cache_write_tokens, cache_read_tokens, cost_usd) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (ts or now(), profile_id, agent, model, input_tokens, output_tokens,
         cache_write_tokens, cache_read_tokens, cost_usd),
    )
    conn.commit()


def total_spend(conn) -> float:
    return conn.execute("SELECT COALESCE(SUM(cost_usd), 0) FROM usage_log").fetchone()[0]


def month_spend(conn, today: datetime | None = None) -> float:
    today = today or datetime.now()
    month_start = today.strftime("%Y-%m-01")
    return conn.execute(
        "SELECT COALESCE(SUM(cost_usd), 0) FROM usage_log WHERE ts >= ?", (month_start,)
    ).fetchone()[0]
