-- Learning Coach database. Safe to run many times (IF NOT EXISTS).

CREATE TABLE IF NOT EXISTS profiles (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    subject      TEXT,                                  -- NULL until the Scoper finishes
    stage        TEXT NOT NULL DEFAULT 'scoping',       -- scoping | confirm_profile | planning | learning
    profile_json TEXT,                                  -- SubjectProfile as JSON
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS milestones (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id INTEGER NOT NULL REFERENCES profiles(id),
    position   INTEGER NOT NULL,                        -- 1, 2, 3 ...
    title      TEXT NOT NULL,
    objective  TEXT NOT NULL,                           -- testable objective
    est_hours  REAL,
    status     TEXT NOT NULL DEFAULT 'locked'
               CHECK (status IN ('locked', 'active', 'done'))
);

CREATE TABLE IF NOT EXISTS lessons (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    milestone_id INTEGER NOT NULL UNIQUE REFERENCES milestones(id),
    content      TEXT NOT NULL,
    verified     INTEGER NOT NULL DEFAULT 0,            -- 0 until the checkers pass it (Phase 3)
    sources_json TEXT NOT NULL,                         -- [{"kind": "model"|"pdf"|"web", "ref": ...}]
    model        TEXT,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chat_messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id INTEGER NOT NULL REFERENCES profiles(id),
    stage      TEXT NOT NULL,                           -- which stage the message belongs to
    role       TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content    TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS usage_log (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                 TEXT NOT NULL,
    profile_id         INTEGER,
    agent              TEXT NOT NULL,
    model              TEXT NOT NULL,                   -- the model that actually answered
    input_tokens       INTEGER NOT NULL,
    output_tokens      INTEGER NOT NULL,
    cache_write_tokens INTEGER NOT NULL,
    cache_read_tokens  INTEGER NOT NULL,
    cost_usd           REAL NOT NULL
);

-- Later phases (created now, empty in Phase 1) --------------------------

CREATE TABLE IF NOT EXISTS objectives (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    milestone_id INTEGER NOT NULL REFERENCES milestones(id),
    text         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS quiz_results (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    milestone_id INTEGER NOT NULL REFERENCES milestones(id),
    score        REAL,
    passed       INTEGER,
    details_json TEXT,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS review_schedule (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    lesson_id INTEGER NOT NULL REFERENCES lessons(id),
    due_at    TEXT NOT NULL,
    card_json TEXT                                      -- py-fsrs card state
);
