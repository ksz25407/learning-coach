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
    # Later phases:
    "curator": HAIKU,
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
}

MAX_TOKENS = {
    "coach": 4000,
    "scoper": 4000,
    "planner": 8000,
    "teacher": 16000,
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

# Server-side refusal fallbacks (beta). Off by default.
USE_SERVER_FALLBACKS = False
FALLBACK_BETA = "server-side-fallback-2026-07-01"
