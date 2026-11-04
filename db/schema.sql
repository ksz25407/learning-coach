-- Learning Coach database. Safe to run many times (IF NOT EXISTS).

CREATE TABLE IF NOT EXISTS profiles (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    subject      TEXT,                                  -- NULL until the Scoper finishes
    stage        TEXT NOT NULL DEFAULT 'scoping',       -- scoping | confirm_profile | planning | learning
    profile_json TEXT,                                  -- SubjectProfile as JSON
    created_at   TEXT NOT NULL,
    deadline_date TEXT                                  -- Phase 4: ISO date from the date picker (NULL = not set)
);

CREATE TABLE IF NOT EXISTS milestones (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id INTEGER NOT NULL REFERENCES profiles(id),
    position   INTEGER NOT NULL,                        -- 1, 2, 3 ...
    title      TEXT NOT NULL,
    objective  TEXT NOT NULL,                           -- testable objective
    est_hours  REAL,
    status     TEXT NOT NULL DEFAULT 'locked'
               CHECK (status IN ('locked', 'active', 'done')),
    passed_at   TEXT,                                   -- Phase 4: UTC time the gate was passed
    pass_method TEXT,                                   -- Phase 4: exam | manual_reason | manual_pre_phase4
    pass_reason TEXT                                    -- Phase 4: your reason for a manual pass
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

CREATE TABLE IF NOT EXISTS quiz_results (            -- Phase 4: one row per quiz attempt
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    milestone_id INTEGER NOT NULL REFERENCES milestones(id),
    score        REAL,                                  -- 0.0 - 1.0
    passed       INTEGER,
    details_json TEXT,                                  -- {"results": [...], "missed": [...]}
    created_at   TEXT NOT NULL                          -- UTC
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
    uploaded_at  TEXT NOT NULL,
    data         BLOB                                   -- Phase 5: the PDF file (NULL = uploaded before Phase 5)
);

CREATE TABLE IF NOT EXISTS pdf_pages (
    upload_id INTEGER NOT NULL REFERENCES pdf_uploads(id),
    page_no   INTEGER NOT NULL,                         -- 1-based, as printed by PDF viewers
    text      TEXT NOT NULL,
    kind      TEXT,                                     -- Phase 5: text | equations | figure | scanned (NULL = before Phase 5)
    file_id   TEXT,                                     -- Phase 5: Files API id of this page as a 1-page PDF
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

-- Phase 4: exam gate, progress, spaced review ------------------------------
-- All times in these tables are UTC ISO strings ("...+00:00").

CREATE TABLE IF NOT EXISTS question_bank (              -- every verified quiz question, kept for review
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    milestone_id  INTEGER NOT NULL REFERENCES milestones(id),
    concept       TEXT NOT NULL,
    question_json TEXT NOT NULL,                        -- the question with its answer key
    created_at    TEXT NOT NULL,
    last_shown_at TEXT                                  -- NULL = never shown in a review
);

CREATE TABLE IF NOT EXISTS tasks (                      -- Examiner's practical task + rubric
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    milestone_id     INTEGER NOT NULL UNIQUE REFERENCES milestones(id),
    proof_type       TEXT NOT NULL,                     -- file | solved_problem | explain_back
    instructions     TEXT NOT NULL,
    rubric_json      TEXT NOT NULL,                     -- [{"id", "criterion", "evidence"}]
    screenshots_json TEXT NOT NULL,                     -- ["floor plan with dimensions visible", ...]
    created_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_submissions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    milestone_id INTEGER NOT NULL REFERENCES milestones(id),
    text         TEXT NOT NULL,
    grades_json  TEXT,                                  -- [{"id", "verdict", "reason", "evidence", "need"}]
    error        TEXT,                                  -- grader error, if grading failed
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_images (
    submission_id INTEGER NOT NULL REFERENCES task_submissions(id),
    idx           INTEGER NOT NULL,
    media_type    TEXT NOT NULL,
    data          BLOB NOT NULL,
    PRIMARY KEY (submission_id, idx)
);

CREATE TABLE IF NOT EXISTS overrides (                  -- your grade overrides, with reasons
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    milestone_id INTEGER NOT NULL REFERENCES milestones(id),
    submission_id INTEGER NOT NULL REFERENCES task_submissions(id),  -- the graded submission it changes
    item_id      TEXT NOT NULL,                         -- rubric item id
    old_verdict  TEXT NOT NULL,
    new_verdict  TEXT NOT NULL,
    reason       TEXT NOT NULL,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS review_schedule (            -- one py-fsrs card per (milestone, concept)
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id   INTEGER NOT NULL REFERENCES profiles(id),
    milestone_id INTEGER NOT NULL REFERENCES milestones(id),
    concept      TEXT NOT NULL,
    due_at       TEXT NOT NULL,
    card_json    TEXT NOT NULL,                         -- py-fsrs Card.to_json()
    created_at   TEXT NOT NULL,
    UNIQUE (milestone_id, concept)
);

CREATE TABLE IF NOT EXISTS review_log (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    card_id          INTEGER NOT NULL REFERENCES review_schedule(id),
    question_id      INTEGER REFERENCES question_bank(id),  -- NULL = rated from a missed exam question
    correct          INTEGER NOT NULL,
    rating           TEXT NOT NULL,                     -- again | hard | good | easy
    source           TEXT NOT NULL,                     -- review | exam
    card_before_json TEXT NOT NULL,                     -- card state before this rating (for Hard/Easy)
    reviewed_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS study_log (                  -- optional "Log study time"
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    milestone_id INTEGER NOT NULL REFERENCES milestones(id),
    minutes      REAL NOT NULL,
    logged_at    TEXT NOT NULL
);
