"""Small helper functions around SQLite. No ORM, just SQL."""
import json
import sqlite3
from datetime import datetime
from pathlib import Path

import clock
import config

SCHEMA_FILE = Path(__file__).resolve().parent / "schema.sql"

# Columns added after Phase 1. CREATE TABLE IF NOT EXISTS can't add columns to a table
# that already exists, so connect() adds them (after making a backup).
NEW_COLUMNS = [
    ("lessons", "citations_json", "TEXT"),
    ("lessons", "notes_json", "TEXT"),
    ("lessons", "status", "TEXT"),
    ("lessons", "review_json", "TEXT"),
    ("lessons", "revision_rounds", "INTEGER"),
    ("milestones", "passed_at", "TEXT"),
    ("milestones", "pass_method", "TEXT"),
    ("milestones", "pass_reason", "TEXT"),
    ("profiles", "deadline_date", "TEXT"),
    ("pdf_uploads", "data", "BLOB"),
    ("pdf_pages", "kind", "TEXT"),
    ("pdf_pages", "file_id", "TEXT"),
    ("tasks", "key_status", "TEXT"),
    ("tasks", "key_check_json", "TEXT"),
]


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def connect(path) -> sqlite3.Connection:
    """Open the database, back it up if it needs changes, and create anything missing.
    After a successful upgrade only the newest config.MAX_BACKUPS backups are kept."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    missing = _missing_columns(conn)
    old_reviews = _has_old_review_table(conn)
    if missing or old_reviews:
        backup_database(conn, path)
    if old_reviews:
        _retire_old_review_table(conn)
    conn.executescript(SCHEMA_FILE.read_text(encoding="utf-8"))
    for table, column, sql_type in missing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}")
    # Milestones ticked "done" by hand before the exam gate existed.
    conn.execute("UPDATE milestones SET pass_method = 'manual_pre_phase4'"
                 " WHERE status = 'done' AND pass_method IS NULL")
    conn.commit()
    if missing or old_reviews:
        prune_backups(path)
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


def _has_old_review_table(conn) -> bool:
    """Phase 1-3 review_schedule was keyed by lesson_id (and never used)."""
    return "lesson_id" in _columns(conn, "review_schedule")


def _retire_old_review_table(conn):
    """Drop the old, empty review_schedule so schema.sql makes the Phase 4 one.
    If it somehow has rows, keep them under another name instead of deleting them."""
    n = conn.execute("SELECT COUNT(*) FROM review_schedule").fetchone()[0]
    if n == 0:
        conn.execute("DROP TABLE review_schedule")
    else:
        conn.execute("ALTER TABLE review_schedule RENAME TO review_schedule_phase3")
    conn.commit()


def prune_backups(path: Path, keep: int | None = None) -> list[Path]:
    """Delete all but the newest `keep` backups of this database. Returns the deleted files."""
    path = Path(path)
    keep = config.MAX_BACKUPS if keep is None else keep
    backups = sorted(path.parent.glob(f"{path.stem}.backup-*{path.suffix}"))  # names sort by time
    old = backups[:-keep] if keep > 0 else backups
    for f in old:
        f.unlink()
    return old


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
    upload_ids = "SELECT id FROM pdf_uploads WHERE profile_id = ?"
    with conn:  # commits on success, rolls back on error
        # Children first, because of the foreign keys.
        # The web_pages download cache is shared by all subjects, so it is kept.
        conn.execute(f"DELETE FROM pdf_pages WHERE upload_id IN ({upload_ids})", (profile_id,))
        conn.execute("DELETE FROM pdf_uploads WHERE profile_id = ?", (profile_id,))
        conn.execute(f"DELETE FROM resources WHERE milestone_id IN ({milestone_ids})", (profile_id,))
        conn.execute("DELETE FROM review_log WHERE card_id IN"
                     " (SELECT id FROM review_schedule WHERE profile_id = ?)", (profile_id,))
        conn.execute("DELETE FROM review_schedule WHERE profile_id = ?", (profile_id,))
        conn.execute(f"DELETE FROM question_bank WHERE milestone_id IN ({milestone_ids})", (profile_id,))
        conn.execute(f"DELETE FROM overrides WHERE milestone_id IN ({milestone_ids})", (profile_id,))
        conn.execute(f"DELETE FROM task_images WHERE submission_id IN (SELECT id FROM task_submissions"
                     f" WHERE milestone_id IN ({milestone_ids}))", (profile_id,))
        conn.execute(f"DELETE FROM task_submissions WHERE milestone_id IN ({milestone_ids})", (profile_id,))
        conn.execute(f"DELETE FROM tasks WHERE milestone_id IN ({milestone_ids})", (profile_id,))
        conn.execute(f"DELETE FROM study_log WHERE milestone_id IN ({milestone_ids})", (profile_id,))
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
            n_text_pages: int, data: bytes | None = None, kinds: list[str] | None = None) -> int:
    """`data`: the PDF file (needed to send pages visually). `kinds`: one per page
    (text | equations | figure | scanned); by default a page is text or scanned by its length."""
    if kinds is None:
        kinds = ["text" if len(t) >= config.PDF_MIN_CHARS_PER_PAGE else "scanned" for t in pages]
    with conn:
        cur = conn.execute(
            "INSERT INTO pdf_uploads (profile_id, filename, page_from, page_to, n_pages, n_text_pages,"
            " uploaded_at, data) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (profile_id, filename, page_from, page_to, len(pages), n_text_pages, now(), data),
        )
        upload_id = cur.lastrowid
        conn.executemany(
            "INSERT INTO pdf_pages (upload_id, page_no, text, kind) VALUES (?, ?, ?, ?)",
            [(upload_id, i, text, kind) for i, (text, kind) in enumerate(zip(pages, kinds), start=1)],
        )
    return upload_id


def get_pdf_data(conn, upload_id: int) -> bytes | None:
    row = conn.execute("SELECT data FROM pdf_uploads WHERE id = ?", (upload_id,)).fetchone()
    return row["data"] if row else None


def set_page_file_id(conn, upload_id: int, page_no: int, file_id: str):
    with conn:
        conn.execute("UPDATE pdf_pages SET file_id = ? WHERE upload_id = ? AND page_no = ?",
                     (file_id, upload_id, page_no))


def pdf_file_ids(conn, profile_id: int | None = None, upload_id: int | None = None) -> list[str]:
    """Files API ids of uploaded pages, for one subject or one PDF (to delete them there too)."""
    sql = ("SELECT p.file_id FROM pdf_pages p JOIN pdf_uploads u ON u.id = p.upload_id"
           " WHERE p.file_id IS NOT NULL")
    if upload_id is not None:
        return [r[0] for r in conn.execute(sql + " AND u.id = ?", (upload_id,))]
    return [r[0] for r in conn.execute(sql + " AND u.profile_id = ?", (profile_id,))]


def pdf_page_kinds(conn, upload_id: int) -> dict[str, int]:
    """How many pages of each kind an upload has, e.g. {"text": 12, "equations": 5}."""
    rows = conn.execute("SELECT COALESCE(kind, 'text') AS k, COUNT(*) FROM pdf_pages WHERE upload_id = ?"
                        " GROUP BY k", (upload_id,)).fetchall()
    return {r[0]: r[1] for r in rows}


def list_pdfs(conn, profile_id: int):
    """Uploads without the file itself (get_pdf_data reads that); has_data = 1 if it is stored."""
    return conn.execute(
        "SELECT id, profile_id, filename, page_from, page_to, n_pages, n_text_pages, uploaded_at,"
        " data IS NOT NULL AS has_data FROM pdf_uploads WHERE profile_id = ? ORDER BY id", (profile_id,)
    ).fetchall()


def delete_pdf(conn, upload_id: int):
    with conn:
        conn.execute("DELETE FROM pdf_pages WHERE upload_id = ?", (upload_id,))
        conn.execute("DELETE FROM pdf_uploads WHERE id = ?", (upload_id,))


def get_pdf_pages(conn, upload_id: int):
    """Pages inside the upload's optional page range."""
    u = conn.execute("SELECT page_from, page_to, n_pages FROM pdf_uploads WHERE id = ?", (upload_id,)).fetchone()
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


def pdf_visual_page_count(conn, profile_id: int) -> int:
    """Pages that would be sent visually (not plain text, and the PDF file is stored)."""
    return conn.execute(
        "SELECT COUNT(*) FROM pdf_pages p JOIN pdf_uploads u ON u.id = p.upload_id"
        " WHERE u.profile_id = ? AND u.data IS NOT NULL AND COALESCE(p.kind, 'text') != 'text'",
        (profile_id,)).fetchone()[0]


def recent_avg_cost(conn, agent: str, n: int) -> tuple[float, int]:
    """Average cost of the agent's last n calls, and how many calls that was."""
    row = conn.execute(
        "SELECT AVG(cost_usd), COUNT(*) FROM"
        " (SELECT cost_usd FROM usage_log WHERE agent = ? ORDER BY id DESC LIMIT ?)", (agent, n)
    ).fetchone()
    return (row[0] or 0.0), row[1]


# =====================================================================
# Phase 4: exam gate, progress, spaced review. Times are UTC (clock.py).
# =====================================================================

def utc_now_iso() -> str:
    return clock.to_iso(clock.utc_now())


def set_deadline(conn, profile_id: int, deadline_date: str | None):
    conn.execute("UPDATE profiles SET deadline_date = ? WHERE id = ?", (deadline_date, profile_id))
    conn.commit()


def mark_passed(conn, milestone_id: int, method: str, reason: str | None = None, when: str | None = None):
    assert method in ("exam", "manual_reason"), method
    conn.execute("UPDATE milestones SET status = 'done', passed_at = ?, pass_method = ?, pass_reason = ?"
                 " WHERE id = ?", (when or utc_now_iso(), method, reason, milestone_id))
    conn.commit()


# ---------- question bank ----------

def add_to_bank(conn, milestone_id: int, questions: list[dict]):
    """Keep verified questions for review. A question already in the bank (same text) is skipped."""
    have = {json.loads(r["question_json"])["question"]
            for r in conn.execute("SELECT question_json FROM question_bank WHERE milestone_id = ?",
                                  (milestone_id,))}
    with conn:
        for q in questions:
            if q["question"] in have:
                continue
            conn.execute("INSERT INTO question_bank (milestone_id, concept, question_json, created_at)"
                         " VALUES (?, ?, ?, ?)", (milestone_id, q["concept"], json.dumps(q), utc_now_iso()))
            have.add(q["question"])


def bank(conn, milestone_id: int, concept: str | None = None) -> list[dict]:
    sql, args = "SELECT * FROM question_bank WHERE milestone_id = ?", [milestone_id]
    if concept is not None:
        sql, args = sql + " AND concept = ?", args + [concept]
    rows = conn.execute(sql + " ORDER BY id", args).fetchall()
    return [{**json.loads(r["question_json"]), "bank_id": r["id"], "concept": r["concept"],
             "last_shown_at": r["last_shown_at"]} for r in rows]


def bank_concepts(conn, milestone_id: int) -> list[str]:
    rows = conn.execute("SELECT DISTINCT concept FROM question_bank WHERE milestone_id = ? ORDER BY concept",
                        (milestone_id,)).fetchall()
    return [r[0] for r in rows]


def retag_bank(conn, tags: dict[int, str]):
    """Set the concept of bank questions: {bank_id: concept}."""
    with conn:
        for bank_id, concept in tags.items():
            row = conn.execute("SELECT question_json FROM question_bank WHERE id = ?", (bank_id,)).fetchone()
            q = {**json.loads(row["question_json"]), "concept": concept}
            conn.execute("UPDATE question_bank SET concept = ?, question_json = ? WHERE id = ?",
                         (concept, json.dumps(q), bank_id))


def mark_shown(conn, bank_id: int, when: str | None = None):
    conn.execute("UPDATE question_bank SET last_shown_at = ? WHERE id = ?", (when or utc_now_iso(), bank_id))
    conn.commit()


# ---------- quiz attempts ----------

def add_quiz_result(conn, milestone_id: int, score: float, passed: bool, details: dict) -> int:
    cur = conn.execute("INSERT INTO quiz_results (milestone_id, score, passed, details_json, created_at)"
                       " VALUES (?, ?, ?, ?, ?)",
                       (milestone_id, score, int(passed), json.dumps(details), utc_now_iso()))
    conn.commit()
    return cur.lastrowid


def quiz_results(conn, milestone_id: int) -> list[dict]:
    rows = conn.execute("SELECT * FROM quiz_results WHERE milestone_id = ? ORDER BY id", (milestone_id,))
    return [{**dict(r), "passed": bool(r["passed"]), "details": json.loads(r["details_json"] or "{}")}
            for r in rows]


# ---------- practical task ----------

def save_task(conn, milestone_id: int, proof_type: str, instructions: str, rubric: list[dict],
              screenshots: list[str], key_status: str | None = None, key_check: dict | None = None):
    """key_status: "verified" | "unverified" for a solved problem's answer key, else None."""
    conn.execute(
        "INSERT INTO tasks (milestone_id, proof_type, instructions, rubric_json, screenshots_json, created_at,"
        " key_status, key_check_json)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(milestone_id) DO UPDATE SET"
        " proof_type = excluded.proof_type, instructions = excluded.instructions,"
        " rubric_json = excluded.rubric_json, screenshots_json = excluded.screenshots_json,"
        " created_at = excluded.created_at, key_status = excluded.key_status,"
        " key_check_json = excluded.key_check_json",
        (milestone_id, proof_type, instructions, json.dumps(rubric), json.dumps(screenshots), utc_now_iso(),
         key_status, json.dumps(key_check) if key_check is not None else None))
    conn.commit()


def get_task(conn, milestone_id: int) -> dict | None:
    r = conn.execute("SELECT * FROM tasks WHERE milestone_id = ?", (milestone_id,)).fetchone()
    if r is None:
        return None
    return {"proof_type": r["proof_type"], "instructions": r["instructions"],
            "rubric": json.loads(r["rubric_json"]), "screenshots": json.loads(r["screenshots_json"]),
            "created_at": r["created_at"], "key_status": r["key_status"],
            "key_check": json.loads(r["key_check_json"]) if r["key_check_json"] else None}


def add_submission(conn, milestone_id: int, text: str, images: list[tuple[str, bytes]],
                   grades: list[dict] | None, error: str | None = None) -> int:
    """images: [(media_type, bytes)]."""
    with conn:
        cur = conn.execute("INSERT INTO task_submissions (milestone_id, text, grades_json, error, created_at)"
                           " VALUES (?, ?, ?, ?, ?)",
                           (milestone_id, text, json.dumps(grades) if grades is not None else None,
                            error, utc_now_iso()))
        sid = cur.lastrowid
        conn.executemany("INSERT INTO task_images (submission_id, idx, media_type, data) VALUES (?, ?, ?, ?)",
                         [(sid, i, mt, data) for i, (mt, data) in enumerate(images)])
    return sid


def submissions(conn, milestone_id: int) -> list[dict]:
    rows = conn.execute("SELECT s.*, (SELECT COUNT(*) FROM task_images i WHERE i.submission_id = s.id)"
                        " AS n_images FROM task_submissions s WHERE milestone_id = ? ORDER BY id",
                        (milestone_id,)).fetchall()
    return [{**dict(r), "grades": json.loads(r["grades_json"]) if r["grades_json"] else None} for r in rows]


def add_override(conn, milestone_id: int, submission_id: int, item_id: str, old: str, new: str, reason: str):
    conn.execute("INSERT INTO overrides (milestone_id, submission_id, item_id, old_verdict, new_verdict, reason,"
                 " created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                 (milestone_id, submission_id, item_id, old, new, reason, utc_now_iso()))
    conn.commit()


def overrides(conn, milestone_id: int) -> list[dict]:
    rows = conn.execute("SELECT * FROM overrides WHERE milestone_id = ? ORDER BY id", (milestone_id,))
    return [dict(r) for r in rows]


# ---------- review cards ----------

def get_card(conn, card_id: int):
    return conn.execute("SELECT * FROM review_schedule WHERE id = ?", (card_id,)).fetchone()


def find_card(conn, milestone_id: int, concept: str):
    return conn.execute("SELECT * FROM review_schedule WHERE milestone_id = ? AND concept = ?",
                        (milestone_id, concept)).fetchone()


def add_card(conn, profile_id: int, milestone_id: int, concept: str, card_json: str, due_at: str) -> int:
    cur = conn.execute("INSERT INTO review_schedule (profile_id, milestone_id, concept, due_at, card_json,"
                       " created_at) VALUES (?, ?, ?, ?, ?, ?)",
                       (profile_id, milestone_id, concept, due_at, card_json, utc_now_iso()))
    conn.commit()
    return cur.lastrowid


def update_card(conn, card_id: int, card_json: str, due_at: str):
    conn.execute("UPDATE review_schedule SET card_json = ?, due_at = ? WHERE id = ?", (card_json, due_at, card_id))
    conn.commit()


def cards(conn, profile_id: int) -> list:
    return conn.execute("SELECT * FROM review_schedule WHERE profile_id = ? ORDER BY due_at, id",
                        (profile_id,)).fetchall()


def due_cards(conn, profile_id: int, now_iso: str) -> list:
    """Cards due by `now_iso` (UTC), most overdue first."""
    return conn.execute("SELECT * FROM review_schedule WHERE profile_id = ? AND due_at <= ?"
                        " ORDER BY due_at, id", (profile_id, now_iso)).fetchall()


def add_review_log(conn, card_id: int, question_id: int | None, correct: bool, rating: str,
                   source: str, card_before_json: str, when: str | None = None) -> int:
    cur = conn.execute("INSERT INTO review_log (card_id, question_id, correct, rating, source,"
                       " card_before_json, reviewed_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                       (card_id, question_id, int(correct), rating, source, card_before_json,
                        when or utc_now_iso()))
    conn.commit()
    return cur.lastrowid


def get_review_log(conn, log_id: int):
    return conn.execute("SELECT * FROM review_log WHERE id = ?", (log_id,)).fetchone()


def last_review_log(conn, card_id: int):
    return conn.execute("SELECT * FROM review_log WHERE card_id = ? ORDER BY id DESC LIMIT 1",
                        (card_id,)).fetchone()


def set_log_rating(conn, log_id: int, rating: str):
    conn.execute("UPDATE review_log SET rating = ? WHERE id = ?", (rating, log_id))
    conn.commit()


def reviews_between(conn, profile_id: int, start_iso: str, end_iso: str, source: str | None = "review") -> list:
    """Review log rows for this subject with start <= reviewed_at < end (UTC strings)."""
    sql = ("SELECT l.*, c.concept, c.milestone_id FROM review_log l JOIN review_schedule c ON c.id = l.card_id"
           " WHERE c.profile_id = ? AND l.reviewed_at >= ? AND l.reviewed_at < ?")
    args = [profile_id, start_iso, end_iso]
    if source is not None:
        sql, args = sql + " AND l.source = ?", args + [source]
    return conn.execute(sql + " ORDER BY l.id", args).fetchall()


# ---------- study time ----------

def log_study(conn, milestone_id: int, minutes: float):
    conn.execute("INSERT INTO study_log (milestone_id, minutes, logged_at) VALUES (?, ?, ?)",
                 (milestone_id, minutes, utc_now_iso()))
    conn.commit()


def study_minutes(conn, milestone_id: int) -> float:
    return conn.execute("SELECT COALESCE(SUM(minutes), 0) FROM study_log WHERE milestone_id = ?",
                        (milestone_id,)).fetchone()[0]
