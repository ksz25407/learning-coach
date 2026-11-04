"""Pedagogy reviewer: is the lesson aimed at the objective, right for the learner's level,
and the right length? Python measures the reading time; the reviewer judges the rest."""
from typing import Literal

from pydantic import BaseModel

SYSTEM = """You are the Pedagogy reviewer in a personal learning app. You get one LESSON, the
milestone objective it must get the learner to pass, and the learner's subject profile.
Check three things:

1. objective: does the lesson teach what is needed to pass the objective, and does its
   "## Practice" task mirror the objective? Flag missing steps or knowledge the objective
   needs, and long detours it doesn't need.
2. level: does it fit the learner's level? (Beginners: no unexplained jargon or skipped steps.
   Advanced learners: no long explanations of basics.)
3. length: the app measured the reading time and the reading budget (given below). If the
   reading time is more than 25% over budget, flag it as major with kind "length", quote the
   heading of the longest section and say what to cut. If it is under a third of the budget
   and leaves out things the objective needs, flag that too.

Rules:
- Don't check facts; another reviewer does that.
- quote: copy the exact lesson text the flag is about, word for word (one sentence or less,
  or a section heading). A flag without an exact quote is ignored.
- reason: why this is a problem for THIS learner and objective.
- severity "major": it would stop the learner from reaching the objective, or the length rule
  above. Everything else is "minor".
- If the lesson is fine, return an empty list. Never invent problems.
"""


class PedagogyFlag(BaseModel):
    quote: str
    problem: str
    reason: str
    kind: Literal["objective", "level", "length", "other"]
    severity: Literal["major", "minor"]


class PedagogyReview(BaseModel):
    flags: list[PedagogyFlag]


def review(llm, profile: dict, milestone: dict, lesson_text: str, reading_min: float,
           budget_min: float, profile_id=None) -> list[dict]:
    hours = milestone.get("est_hours")
    request = (f"Milestone: {milestone['title']}\nObjective: {milestone['objective']}\n"
               f"Milestone time: {hours if hours else 'unknown'} h\n"
               f"Reading time: {reading_min:.1f} min (budget {budget_min:.1f} min)\n\n"
               f"LESSON:\n{lesson_text}")
    result = llm.call_structured("pedagogy", SYSTEM, [{"role": "user", "content": request}],
                                 PedagogyReview, profile=profile, profile_id=profile_id)
    return [{"checker": "pedagogy", "quote": f.quote, "problem": f.problem, "evidence": f.reason,
             "kind": f.kind, "severity": f.severity} for f in result.flags]
