"""All settings in one place. Change models, prices and limits here."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
# Phase 5: set COACH_DB_PATH to use another database (e.g. a scratch copy for test runs), so
# your real subjects, lessons and spend are untouched:  $env:COACH_DB_PATH = "C:\temp\test.sqlite"
DB_PATH = Path(os.environ.get("COACH_DB_PATH") or ROOT / "db" / "coach.sqlite")

# Which model each agent uses (IDs checked against the Claude API docs, 2026-11).
SONNET = "claude-sonnet-5-5"
HAIKU = "claude-haiku-5-5"
OPUS = "claude-opus-5-5"

MODELS = {
    "coach": SONNET,
    "scoper": SONNET,
    "planner": SONNET,
    "teacher": SONNET,
    "curator_search": HAIKU,  # Curator step 1: web search
    "curator": HAIKU,  # Curator step 2: pick resources (JSON)
    # Phase 3: checkers
    "fact_checker": OPUS,  # switch to SONNET to halve the cost (compare with the eval first)
    "pedagogy": SONNET,
    "examiner": HAIKU,  # minimal Examiner: drafts the quiz + answer key
    "quiz_solver": SONNET,  # switch to OPUS for stricter checking
    # Phase 4
    "examiner_task": HAIKU,  # writes the practical task + rubric
    "grader": SONNET,  # grades the practical task (text + screenshots)
    # Phase 5: solved-problem tasks need correct numbers, so Sonnet drafts them and a blind
    # solver checks the answer key (set examiner_task_solved to HAIKU to save money).
    "examiner_task_solved": SONNET,
    "task_solver": SONNET,
}

# How hard the model thinks: "low" | "medium" | "high" | "xhigh" | "max".
EFFORT = {
    "coach": "low",
    "scoper": "low",
    "planner": "medium",
    "teacher": "medium",
    "curator_search": "low",
    "curator": "low",
    "fact_checker": "high",  # set explicitly: Opus 5.5 defaults to "medium"
    "pedagogy": "medium",
    "examiner": "low",
    "quiz_solver": "medium",
    "examiner_task": "low",
    "grader": "medium",
    "examiner_task_solved": "medium",
    "task_solver": "medium",
}

MAX_TOKENS = {
    "coach": 4000,
    "scoper": 4000,
    "planner": 8000,
    "teacher": 16000,
    "curator_search": 8000,
    "curator": 4000,
    "fact_checker": 8000,  # includes thinking; the eval's cost cap uses this as the worst case
    "pedagogy": 6000,
    "examiner": 6000,
    "quiz_solver": 6000,
    "examiner_task": 6000,
    "grader": 6000,
    "examiner_task_solved": 10000,
    "task_solver": 10000,
}

# USD per 1M tokens. cache_write = 1.25x input (5-minute cache), cache_read = 0.1x input.
PRICES = {
    SONNET: {"input": 2.00, "output": 10.00, "cache_write": 2.50, "cache_read": 0.20},
    OPUS: {"input": 4.00, "output": 20.00, "cache_write": 5.00, "cache_read": 0.20},
    # Haiku 5.5 prices are for prompts up to 100K tokens ($0.50 / $2.50 above that).
    HAIKU: {"input": 0.10, "output": 0.50, "cache_write": 0.125, "cache_read": 0.01},
}

# Monthly spending limits (USD).
WARN_USD = 10.0
STOP_USD = 15.0

# Coach chat only sends this many recent messages (until context editing is added).
CHAT_HISTORY_LIMIT = 20

# Scoper asks at most this many questions.
SCOPER_MAX_QUESTIONS = 5

# Count tokens with the API first if the prompt looks bigger than this
# (rough estimate: 4 characters per token). Refuse prompts above MAX_INPUT_TOKENS.
TOKEN_COUNT_THRESHOLD = 20_000
MAX_INPUT_TOKENS = 150_000

# ---------- Phase 2: sources ----------

# Curator web search (Anthropic server tool). Billed per search on top of tokens.
WEB_SEARCH_TOOL = "web_search_20250305"  # basic version: we only need URLs and titles
WEB_SEARCH_MAX_USES = 5  # searches per Curator run
WEB_SEARCH_USD = 0.01  # $10 per 1,000 searches
PAUSE_TURN_MAX_ROUNDS = 3  # how often to resume a paused server-tool turn

# Downloading the Curator's web pages (done by this app, not by Claude).
PAGE_MAX_TOKENS = 8000  # text kept per page (rough: 4 characters per token)
FETCH_TIMEOUT_S = 15
FETCH_USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
MAX_DOWNLOAD_BYTES = 5_000_000  # abort bigger downloads as "too large"
WEB_CACHE_MAX_DAYS = 30  # cached pages older than this are downloaded again

# Sites that refuse downloads (e.g. HTTP 403). The Curator is told to prefer other sources,
# and the app never tries to download them. Entry = domain, optionally with a path prefix:
# "autodesk.com/learn" matches www.autodesk.com/learn/... but not help.autodesk.com.
BLOCKED_DOMAINS = [
    "autodesk.com/learn",
]

# Uploaded PDFs.
PDF_MAX_PAGES = 15  # pages sent to the Teacher per lesson
PDF_MIN_CHARS_PER_PAGE = 100  # less text than this = probably a scanned page

# Phase 5: visual PDF pages. Pages with equations, figures or no text layer are sent to Claude
# as PDF pages (Claude reads each page's text AND an image of it), uploaded once via the
# Files API. They cost more, so they have their own cap within PDF_MAX_PAGES. A page over the
# cap is sent as extracted text instead (or skipped if it has none).
VISUAL_PDF_MAX_PAGES = 8
# Rough tokens per visual page, for cost estimates and the size check (count_tokens can't count
# Files API documents). The docs give 1,500-3,000 text tokens per page plus the page image
# (about (w/28) x (h/28) tokens), so this is kept on the high side.
VISUAL_PAGE_TOKENS = 4500
# A page counts as "equations" (sent visually) when its extracted text has at least this many
# maths symbols or '=' signs, or this share of very short lines (garbled fractions, figure labels).
PDF_MATH_MIN_SYMBOLS = 3
PDF_SHORT_LINE_SHARE = 0.2

# Curator resources longer than this (minutes) are flagged.
LONG_RESOURCE_MIN = 30

# Cost estimate for one lesson (shown next to Regenerate). Once EST_MIN_LESSONS lessons exist,
# it uses the average real cost of the last EST_SAMPLE_CALLS calls per agent, times
# EST_SAFETY_FACTOR (kept a bit high). Before that, the rough formula below is used.
EST_MIN_LESSONS = 3
EST_SAMPLE_CALLS = 20
EST_SAFETY_FACTOR = 1.25
EST_TEACHER_OUTPUT_TOKENS = 8000  # includes thinking tokens
EST_CURATOR_INPUT_TOKENS = 40000  # search results read by the Curator (Haiku, cheap)

# ---------- Phase 3: checkers ----------

# Fact-checker + Pedagogy reviewer can send a lesson back to the Teacher this many times.
# After that the lesson is shown anyway, marked "Flagged", with the remaining problems on top.
MAX_REVISION_ROUNDS = 2

# Lesson length: reading time = words / READING_WPM. Budget = READING_SHARE of the milestone's
# estimated time, but never more than READING_CAP_MIN minutes.
READING_WPM = 200
READING_SHARE = 0.25
READING_CAP_MIN = 20

# Quiz (Examiner + blind Quiz solver). Questions the solver gets "wrong" are dropped.
QUIZ_TARGET_QUESTIONS = 5
QUIZ_MIN_QUESTIONS = 3  # fewer survivors -> the Examiner writes replacements
QUIZ_MAX_REDRAFTS = 2
QUIZ_DEFAULT_TOLERANCE_PCT = 1.0  # numeric answers, when the Examiner gives no tolerance

# Rough output tokens per call (thinking included), for cost estimates before real usage exists.
EST_LESSON_TOKENS = 4000  # a lesson as input to the checkers
EST_OUTPUT_TOKENS = {"fact_checker": 4000, "pedagogy": 2000, "examiner": 3000, "quiz_solver": 2000}

# ---------- Phase 4: exam gate, progress, spaced review ----------

# Your local time zone. Timestamps are stored in UTC; "today" and the daily review limit use this.
TIMEZONE = "America/New_York"

# Database backups made before a migration: keep only the newest ones.
MAX_BACKUPS = 5

# Exam gate. Quiz: this share of questions right. Task: every rubric item must pass.
QUIZ_PASS_PCT = 0.8
# "Pass with a reason" appears after this many task submissions that still had a
# "can't tell" item (or when the grader keeps failing).
MANUAL_PASS_AFTER = 2

# Screenshots for the practical task. Bigger images are shrunk to this long edge first
# (the API's limit for current models is 2,576 px; cost is about (w/28) x (h/28) tokens).
IMAGE_MAX_PX = 2576
IMAGE_MAX_COUNT = 6

# Spaced review (py-fsrs): at most this many cards per subject per (local) day.
REVIEW_DAILY_LIMIT = 15

# Rough output tokens per grader call, for the cost shown on the submit button.
EST_GRADER_INPUT_TOKENS = 6000  # task + rubric + your text
# Phase 5: one full-size image: a 4:3 photo at the 2,576 px limit is (2576/28) x (1932/28) =
# 92 x 69 = about 6,350 tokens. The grader estimate assumes EST_GRADER_IMAGES of them.
EST_IMAGE_TOKENS = 6400
EST_GRADER_IMAGES = 3
EST_OUTPUT_TOKENS["grader"] = 2000
EST_OUTPUT_TOKENS["examiner_task"] = 2000

# ---------- Phase 5: verified answer keys for solved problems ----------

# A solved-problem task whose answer key the blind Task solver can't confirm (or whose rubric
# or netlist is broken) is redrafted at most this many times; then it is shown as "unverified".
TASK_MAX_REDRAFTS = 2
# Final-value rubric items: allowed error (%) when the Examiner gives none.
TASK_DEFAULT_TOLERANCE_PCT = 2.0
EST_OUTPUT_TOKENS["examiner_task_solved"] = 4000
EST_OUTPUT_TOKENS["task_solver"] = 5000

# Fact-checker eval (evals/fact_check_eval.py). It refuses to start if this month's eval
# spend + the run's worst-case cost would go over this.
EVAL_MONTHLY_CAP_USD = 3.0

# Server-side refusal fallbacks (beta). Off by default.
USE_SERVER_FALLBACKS = False
FALLBACK_BETA = "server-side-fallback-2026-07-01"
