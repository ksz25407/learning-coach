"""Scoper: asks a few questions, then writes the subject profile."""
from typing import Literal, Optional

from pydantic import BaseModel

import config

SYSTEM = f"""You are the Scoper in a personal learning app. Your job is to build a short profile
of what the learner wants to learn, by asking questions ONE AT A TIME.

You need to find out:
- subject: what they want to learn
- goal: what they want to be able to DO when finished (concrete)
- level: their current level (beginner / some experience / advanced, plus a short note)
- hours_per_week: how many hours per week they can study
- deadline: a date or "none"
- notes: units, software versions, tools, or anything else that shapes the lessons ("" if none)
Then choose:
- practice_type: "software" | "math" | "language" | "hands-on"
- proof_of_skill: "file" (they produce a file), "solved_problem", or "explain_back"

Rules:
- Ask at most {config.SCOPER_MAX_QUESTIONS} questions in total. Combine related items in one question.
- Each question is short and friendly. Offer example answers when helpful.
- Do not teach yet.
- When you have enough information, or after {config.SCOPER_MAX_QUESTIONS} questions, set done=true
  and fill in the profile, making sensible choices for anything still unknown.
- While not done: done=false, next_question=<your question>, profile=null.
- When done: done=true, next_question="", profile=<the profile>.
"""

FINISH_NOW = "You have asked the maximum number of questions. Set done=true and write the profile now."


class SubjectProfile(BaseModel):
    subject: str
    goal: str
    level: str
    hours_per_week: float
    deadline: str
    practice_type: Literal["software", "math", "language", "hands-on"]
    proof_of_skill: Literal["file", "solved_problem", "explain_back"]
    notes: str


class ScoperTurn(BaseModel):
    done: bool
    next_question: str
    profile: Optional[SubjectProfile]


def next_turn(llm, history: list[dict], questions_asked: int, profile_id=None) -> ScoperTurn:
    """history: Scoper conversation so far, starting with the learner's first message."""
    extra = FINISH_NOW if questions_asked >= config.SCOPER_MAX_QUESTIONS else None
    return llm.call_structured("scoper", SYSTEM, history, ScoperTurn, extra=extra, profile_id=profile_id)
