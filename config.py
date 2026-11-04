"""All settings in one place. Change models, prices and limits here."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "db" / "coach.sqlite"

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
    # Later phases:
    "examiner": HAIKU,
    "pedagogy": SONNET,
    "fact_checker": SONNET,  # switch to OPUS for stricter checking
    "quiz_solver": SONNET,  # switch to OPUS for stricter checking
}

# How hard the model thinks: "low" | "medium" | "high" | "xhigh" | "max".
EFFORT = {
    "coach": "low",
    "scoper": "low",
    "planner": "medium",
    "teacher": "medium",
    "curator_search": "low",
    "curator": "low",
}

MAX_TOKENS = {
    "coach": 4000,
    "scoper": 4000,
    "planner": 8000,
    "teacher": 16000,
    "curator_search": 8000,
    "curator": 4000,
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

# Uploaded PDFs.
PDF_MAX_PAGES = 15  # pages sent to the Teacher per lesson
PDF_MIN_CHARS_PER_PAGE = 100  # less text than this = probably a scanned page

# Curator resources longer than this (minutes) are flagged.
LONG_RESOURCE_MIN = 30

# Rough cost estimate for one lesson (shown next to Regenerate).
EST_TEACHER_OUTPUT_TOKENS = 8000  # includes thinking tokens
EST_CURATOR_INPUT_TOKENS = 40000  # search results read by the Curator (Haiku, cheap)

# Server-side refusal fallbacks (beta). Off by default.
USE_SERVER_FALLBACKS = False
FALLBACK_BETA = "server-side-fallback-2026-07-01"
