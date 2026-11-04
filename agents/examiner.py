"""Examiner (minimal, Phase 3): drafts a short quiz + answer key for one lesson.

Only formats Python can mark without another AI call: multiple choice, numeric (with unit and
tolerance) and short exact terms (with accepted alternates). Grading the learner's answers
comes in Phase 4.
"""
from typing import Literal

from pydantic import BaseModel

SYSTEM = """You are the Examiner in a personal learning app. Write a short quiz that tests the
milestone objective, using only what the LESSON teaches. Match the learner's practice_type
(software: tools, commands, settings, values, order of steps; math: numeric problems with
units; language: exact words and forms; hands-on: measurements, procedures, safety).

Allowed question kinds (no others):
- "multiple_choice": 3-5 choices (plain text, no "A." prefixes). answer = the letter of the one
  correct choice ("A" for the first). Exactly one choice is correct.
- "numeric": answer = the number only (e.g. "2200"); unit = its unit symbol (e.g. "Ω", "mm",
  "V"; "" if none); tolerance_pct = allowed error in percent (1 for exact calculations, up to 5
  for values read off a chart).
- "exact_term": answer = one short term or name; alternates = other spellings or forms that are
  also correct (e.g. British/American spelling, abbreviation).

Rules:
- Every question has ONE unambiguous correct answer that follows from the lesson.
- explanation: one or two sentences on why the answer is right, pointing to the lesson.
- Unused fields: choices = [], unit = "", tolerance_pct = 0, alternates = [].
- If AVOID is given, write NEW questions that don't repeat any of those, and avoid the
  problems listed (a question another solver got "wrong" was probably ambiguous).
"""


class Question(BaseModel):
    kind: Literal["multiple_choice", "numeric", "exact_term"]
    question: str
    choices: list[str]
    answer: str
    unit: str
    tolerance_pct: float
    alternates: list[str]
    explanation: str


class Quiz(BaseModel):
    questions: list[Question]


def draft_quiz(llm, profile: dict, milestone: dict, lesson_text: str, n: int,
               avoid: list[dict] | None = None, profile_id=None) -> list[dict]:
    """Return n (or so) questions as dicts. `avoid`: [{"question", "problem"}] from earlier drafts."""
    request = (f"Milestone: {milestone['title']}\nObjective: {milestone['objective']}\n"
               f"Write {n} questions.\n\nLESSON:\n{lesson_text}")
    if avoid:
        lines = "\n".join(f"- {a['question']}" + (f" (problem: {a['problem']})" if a.get("problem") else "")
                          for a in avoid)
        request += f"\n\nAVOID (already used or dropped):\n{lines}"
    quiz = llm.call_structured("examiner", SYSTEM, [{"role": "user", "content": request}], Quiz,
                               profile=profile, profile_id=profile_id)
    return [q.model_dump() for q in quiz.questions]
