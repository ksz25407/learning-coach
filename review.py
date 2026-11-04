"""Spaced review with py-fsrs. Plain Python, no API calls.

One card per (milestone, concept). A review shows one question for that concept from the
question bank (the one you saw least recently), Python grades it, and the result becomes an
FSRS rating: wrong = Again, right = Good. Right after a correct answer you can change Good to
Hard or Easy, but only for the card's most recent review and only on the same local day.
The change is re-applied to the card state from BEFORE that review, so FSRS still sees exactly
one rating per review.
"""
from datetime import datetime

from fsrs import Card, Rating, Scheduler

import clock
import config

RATINGS = {"again": Rating.Again, "hard": Rating.Hard, "good": Rating.Good, "easy": Rating.Easy}

# Fuzzing off: the same answers always give the same due date (easier to test and to explain).
_scheduler = Scheduler(enable_fuzzing=False)


def new_card() -> tuple[str, str]:
    """(card_json, due_at) for a brand-new card. A new card is due right away."""
    card = Card()
    return card.to_json(), clock.to_iso(card.due)


def apply(card_json: str, rating: str, now: datetime | None = None) -> tuple[str, str]:
    """Rate a card. Returns (new card_json, new due_at as UTC ISO)."""
    card, _ = _scheduler.review_card(Card.from_json(card_json), RATINGS[rating], now or clock.utc_now())
    return card.to_json(), clock.to_iso(card.due)


def rating_for(correct: bool) -> str:
    return "good" if correct else "again"


def pick_question(questions: list[dict]) -> dict | None:
    """The bank question you saw least recently (never-shown ones first, then oldest id)."""
    if not questions:
        return None
    return min(questions, key=lambda q: (q.get("last_shown_at") or "", q["bank_id"]))


def todays_cards(due: list, reviewed_today: int, limit: int | None = None) -> list:
    """The due cards still allowed today under the daily limit."""
    limit = config.REVIEW_DAILY_LIMIT if limit is None else limit
    return list(due)[: max(0, limit - reviewed_today)]


def can_adjust(log, last_log_for_card, now: datetime | None = None) -> tuple[bool, str]:
    """May this review's Good be changed to Hard or Easy? Returns (allowed, reason if not)."""
    if log is None:
        return False, "That review doesn't exist."
    if log["source"] != "review":
        return False, "Only reviews can be adjusted, not exam results."
    if not log["correct"]:
        return False, "Hard/Easy is only for answers you got right."
    if last_log_for_card is None or last_log_for_card["id"] != log["id"]:
        return False, "Only the most recent review of a card can be adjusted."
    if not clock.same_local_day(clock.from_iso(log["reviewed_at"]), now or clock.utc_now()):
        return False, "Hard/Easy can only be changed on the day of the review."
    return True, ""
