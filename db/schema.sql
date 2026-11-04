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
    created_at   TEXT NOT NULL,
    citations_json TEXT,                                -- Phase 2: numbered citations
    notes_json     TEXT,                                -- Phase 2: pages picked, URLs skipped... (NULL = Phase 1 lesson)
    status         TEXT,                                -- Phase 3: unchecked | verified | partly_unsourced | flagged (NULL = unchecked)
    review_json    TEXT,                                -- Phase 3: checker flags for every round
    revision_rounds INTEGER                             -- Phase 3: how many times the Teacher rewrote it
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

-- Phase 2: sources ------------------------------------------------------

CREATE TABLE IF NOT EXISTS pdf_uploads (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id   INTEGER NOT NULL REFERENCES profiles(id),
    filename     TEXT NOT NULL,
    page_from    INTEGER,                               -- optional range auto-pick searches in
    page_to      INTEGER,
    n_pages      INTEGER NOT NULL,
    n_text_pages INTEGER NOT NULL,                      -- pages with a real text layer
    uploaded_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pdf_pages (
    upload_id INTEGER NOT NULL REFERENCES pdf_uploads(id),
    page_no   INTEGER NOT NULL,                         -- 1-based, as printed by PDF viewers
    text      TEXT NOT NULL,
    PRIMARY KEY (upload_id, page_no)
);

CREATE TABLE IF NOT EXISTS resources (                  -- Curator picks, per milestone
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    milestone_id INTEGER NOT NULL REFERENCES milestones(id),
    position     INTEGER NOT NULL,
    url          TEXT NOT NULL,
    title        TEXT NOT NULL,
    kind         TEXT NOT NULL,                         -- page | video | pdf
    section      TEXT,                                  -- exact section or timestamps
    minutes      REAL,
    why_best     TEXT,
    role         TEXT NOT NULL                          -- primary | backup
);

CREATE TABLE IF NOT EXISTS web_pages (                  -- shared download cache (any subject)
    url        TEXT PRIMARY KEY,
    text       TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);

-- Phase 3: quizzes -------------------------------------------------------

CREATE TABLE IF NOT EXISTS quizzes (                    -- Examiner quiz, after the blind Quiz solver
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    milestone_id   INTEGER NOT NULL UNIQUE REFERENCES milestones(id),
    questions_json TEXT NOT NULL,                       -- questions that survived (with answer key)
    dropped_json   TEXT NOT NULL,                       -- dropped questions + why
    redrafts       INTEGER NOT NULL,
    flagged        INTEGER NOT NULL,                    -- 1 = fewer than QUIZ_MIN_QUESTIONS survived
    created_at     TEXT NOT NULL
);
