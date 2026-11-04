"""Small helper functions around SQLite. No ORM, just SQL."""
import json
import sqlite3
from datetime import datetime
from pathlib import Path

SCHEMA_FILE = Path(__file__).resolve().parent / "schema.sql"

# Columns added after Phase 1. CREATE TABLE IF NOT EXISTS can't add columns to a table
# that already exists, so connect() adds them (after making a backup).
NEW_COLUMNS = [
    ("lessons", "citations_json", "TEXT"),
    ("lessons", "notes_json", "TEXT"),
    ("lessons", "status", "TEXT"),
    ("lessons", "review_json", "TEXT"),
    ("lessons", "revision_rounds", "INTEGER"),
]


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def connect(path) -> sqlite3.Connection:
    """Open the database, back it up if it needs new columns, and create anything missing."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    missing = _missing_columns(conn)
    if missing:
        backup_database(conn, path)
    conn.executescript(SCHEMA_FILE.read_text(encoding="utf-8"))
    for table, column, sql_type in missing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}")
    conn.commit()
    return conn


def _columns(conn, table: str) -> set[str]:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


def _missing_columns(conn) -> list[tuple]:
    """New columns that an EXISTING table lacks. A brand-new database gets them from schema.sql."""
    missing = []
    for table, column, sql_type in NEW_COLUMNS:
        cols = _columns(conn, table)
        if cols and column not in cols:  # empty set = table doesn't exist yet
            missing.append((table, column, sql_type))
    return missing


def backup_database(conn, path: Path) -> Path:
    """Copy the database to db/<name>.backup-YYYYMMDD-HHMMSS.sqlite before changing it."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = path.with_name(f"{path.stem}.backup-{stamp}{path.suffix}")
    dest = sqlite3.connect(target)
    with dest:
        conn.backup(dest)  # SQLite's own copy: safe even while the file is open
    dest.close()
    return target


# ---------- profiles ----------

def create_profile(conn) -> int:
    cur = conn.execute("INSERT INTO profiles (created_at) VALUES (?)", (now(),))
    conn.commit()
    return cur.lastrowid


def get_profile(conn, profile_id: int):
    return conn.execute("SELECT * FROM profiles WHERE id = ?", (profile_id,)).fetchone()


def list_profiles(conn):
    return conn.execute("SELECT * FROM profiles ORDER BY id").fetchall()


def delete_profile(conn, profile_id: int):
    """Delete a subject and everything that belongs to it, in one transaction.

    usage_log rows are KEPT on purpose, so monthly and all-time spend stay accurate.
    """
    milestone_ids = "SELECT id FROM milestones WHERE profile_id = ?"
    lesson_ids = f"SELECT id FROM lessons WHERE milestone_id IN ({milestone_ids})"
    upload_ids = "SELECT id FROM pdf_uploads WHERE profile_id = ?"
    with conn:  # commits on success, rolls back on error
        # Children first, because of the foreign keys.
        # The web_pages download cache is shared by all subjects, so it is kept.
        conn.execute(f"DELETE FROM pdf_pages WHERE upload_id IN ({upload_ids})", (profile_id,))
        conn.execute("DELETE FROM pdf_uploads WHERE profile_id = ?", (profile_id,))
        conn.execute(f"DELETE FROM resources WHERE milestone_id IN ({milestone_ids})", (profile_id,))
        conn.execute(f"DELETE FROM review_schedule WHERE lesson_id IN ({lesson_ids})", (profile_id,))
        conn.execute(f"DELETE FROM lessons WHERE milestone_id IN ({milestone_ids})", (profile_id,))
        conn.execute(f"DELETE FROM quiz_results WHERE milestone_id IN ({milestone_ids})", (profile_id,))
        conn.execute(f"DELETE FROM quizzes WHERE milestone_id IN ({milestone_ids})", (profile_id,))
        conn.execute(f"DELETE FROM objectives WHERE milestone_id IN ({milestone_ids})", (profile_id,))
        conn.execute("DELETE FROM milestones WHERE profile_id = ?", (profile_id,))
        conn.execute("DELETE FROM chat_messages WHERE profile_id = ?", (profile_id,))
        conn.execute("DELETE FROM profiles WHERE id = ?", (profile_id,))


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

LESSON_STATUSES = ("unchecked", "verified", "partly_unsourced", "flagged")


def save_lesson(conn, milestone_id: int, content: str, sources: list[dict], model: str,
                citations: list[dict] | None = None, notes: dict | None = None,
                status: str = "unchecked", review: dict | None = None, revision_rounds: int = 0):
    """Insert the lesson, or replace the existing one for this milestone (Regenerate).

    verified = 1 only for status "verified" (every section passed a source check).
    """
    assert status in LESSON_STATUSES, status
    conn.execute(
        "INSERT INTO lessons (milestone_id, content, verified, sources_json, model, created_at,"
        " citations_json, notes_json, status, review_json, revision_rounds)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(milestone_id) DO UPDATE SET content = excluded.content,"
        " verified = excluded.verified, sources_json = excluded.sources_json,"
        " model = excluded.model, created_at = excluded.created_at,"
        " citations_json = excluded.citations_json, notes_json = excluded.notes_json,"
        " status = excluded.status, review_json = excluded.review_json,"
        " revision_rounds = excluded.revision_rounds",
        (milestone_id, content, int(status == "verified"), json.dumps(sources), model, now(),
         json.dumps(citations or []), json.dumps(notes) if notes is not None else None,
         status, json.dumps(review) if review is not None else None, revision_rounds),
    )
    conn.commit()


def lesson_status(lesson) -> str:
    """Status of a lesson row; lessons from before Phase 3 count as unchecked."""
    return lesson["status"] or "unchecked"


def lesson_review(lesson) -> dict | None:
    return json.loads(lesson["review_json"]) if lesson["review_json"] else None


def get_lesson(conn, milestone_id: int):
    return conn.execute("SELECT * FROM lessons WHERE milestone_id = ?", (milestone_id,)).fetchone()


def lesson_notes(lesson) -> dict | None:
    """Phase 2 notes (pages picked, URLs skipped...). None for a Phase 1 lesson."""
    return json.loads(lesson["notes_json"]) if lesson["notes_json"] else None


def lesson_is_outdated(conn, milestone_id: int) -> bool:
    """True if the lesson was written before the Curator existed (Phase 1)
    or before the newest PDF upload for its subject."""
    lesson = get_lesson(conn, milestone_id)
    if lesson is None:
        return False
    if lesson["notes_json"] is None:
        return True
    newest = conn.execute(
        "SELECT MAX(u.uploaded_at) FROM pdf_uploads u JOIN milestones m ON m.profile_id = u.profile_id"
        " WHERE m.id = ?", (milestone_id,)
    ).fetchone()[0]
    return newest is not None and newest > lesson["created_at"]


# ---------- quizzes ----------

def save_quiz(conn, milestone_id: int, questions: list[dict], dropped: list[dict],
              redrafts: int, flagged: bool):
    conn.execute(
        "INSERT INTO quizzes (milestone_id, questions_json, dropped_json, redrafts, flagged, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(milestone_id) DO UPDATE SET questions_json = excluded.questions_json,"
        " dropped_json = excluded.dropped_json, redrafts = excluded.redrafts,"
        " flagged = excluded.flagged, created_at = excluded.created_at",
        (milestone_id, json.dumps(questions), json.dumps(dropped), redrafts, int(flagged), now()),
    )
    conn.commit()


def get_quiz(conn, milestone_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM quizzes WHERE milestone_id = ?", (milestone_id,)).fetchone()
    if row is None:
        return None
    return {"questions": json.loads(row["questions_json"]), "dropped": json.loads(row["dropped_json"]),
            "redrafts": row["redrafts"], "flagged": bool(row["flagged"]), "created_at": row["created_at"]}


def delete_quiz(conn, milestone_id: int):
    conn.execute("DELETE FROM quizzes WHERE milestone_id = ?", (milestone_id,))
    conn.commit()


# ---------- Curator resources ----------

def save_resources(conn, milestone_id: int, resources: list[dict]):
    with conn:
        conn.execute("DELETE FROM resources WHERE milestone_id = ?", (milestone_id,))
        for i, r in enumerate(resources, start=1):
            conn.execute(
                "INSERT INTO resources (milestone_id, position, url, title, kind, section, minutes,"
                " why_best, role) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (milestone_id, i, r["url"], r["title"], r["kind"], r.get("section"),
                 r.get("minutes"), r.get("why_best"), r["role"]),
            )


def get_resources(conn, milestone_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM resources WHERE milestone_id = ? ORDER BY position", (milestone_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def has_resources(conn, milestone_id: int) -> bool:
    return conn.execute("SELECT 1 FROM resources WHERE milestone_id = ?", (milestone_id,)).fetchone() is not None


# ---------- PDFs ----------

def add_pdf(conn, profile_id: int, filename: str, pages: list[str], page_from, page_to,
            n_text_pages: int) -> int:
    with conn:
        cur = conn.execute(
            "INSERT INTO pdf_uploads (profile_id, filename, page_from, page_to, n_pages, n_text_pages,"
            " uploaded_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (profile_id, filename, page_from, page_to, len(pages), n_text_pages, now()),
        )
        upload_id = cur.lastrowid
        conn.executemany(
            "INSERT INTO pdf_pages (upload_id, page_no, text) VALUES (?, ?, ?)",
            [(upload_id, i, text) for i, text in enumerate(pages, start=1)],
        )
    return upload_id


def list_pdfs(conn, profile_id: int):
    return conn.execute(
        "SELECT * FROM pdf_uploads WHERE profile_id = ? ORDER BY id", (profile_id,)
    ).fetchall()


def delete_pdf(conn, upload_id: int):
    with conn:
        conn.execute("DELETE FROM pdf_pages WHERE upload_id = ?", (upload_id,))
        conn.execute("DELETE FROM pdf_uploads WHERE id = ?", (upload_id,))


def get_pdf_pages(conn, upload_id: int):
    """Pages inside the upload's optional page range."""
    u = conn.execute("SELECT * FROM pdf_uploads WHERE id = ?", (upload_id,)).fetchone()
    lo = u["page_from"] or 1
    hi = u["page_to"] or u["n_pages"]
    return conn.execute(
        "SELECT * FROM pdf_pages WHERE upload_id = ? AND page_no BETWEEN ? AND ? ORDER BY page_no",
        (upload_id, lo, hi),
    ).fetchall()


# ---------- web page cache ----------

def get_cached_page(conn, url: str):
    return conn.execute("SELECT * FROM web_pages WHERE url = ?", (url,)).fetchone()


def cache_page(conn, url: str, text: str):
    conn.execute(
        "INSERT INTO web_pages (url, text, fetched_at) VALUES (?, ?, ?)"
        " ON CONFLICT(url) DO UPDATE SET text = excluded.text, fetched_at = excluded.fetched_at",
        (url, text, now()),
    )
    conn.commit()


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


def month_spend(conn, today: datetime | None = None, agent: str | None = None) -> float:
    """This month's spend, for all agents or just one."""
    today = today or datetime.now()
    month_start = today.strftime("%Y-%m-01")
    sql, args = "SELECT COALESCE(SUM(cost_usd), 0) FROM usage_log WHERE ts >= ?", [month_start]
    if agent is not None:
        sql, args = sql + " AND agent = ?", args + [agent]
    return conn.execute(sql, args).fetchone()[0]


def pdf_page_lengths(conn, profile_id: int) -> list[int]:
    """Text length of every usable PDF page for a subject, longest first (for cost estimates)."""
    rows = conn.execute(
        "SELECT LENGTH(p.text) FROM pdf_pages p JOIN pdf_uploads u ON u.id = p.upload_id"
        " WHERE u.profile_id = ? ORDER BY LENGTH(p.text) DESC", (profile_id,)
    ).fetchall()
    return [r[0] for r in rows]


def recent_avg_cost(conn, agent: str, n: int) -> tuple[float, int]:
    """Average cost of the agent's last n calls, and how many calls that was."""
    row = conn.execute(
        "SELECT AVG(cost_usd), COUNT(*) FROM"
        " (SELECT cost_usd FROM usage_log WHERE agent = ? ORDER BY id DESC LIMIT ?)", (agent, n)
    ).fetchone()
    return (row[0] or 0.0), row[1]
