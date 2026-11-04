"""Quiz solver: answers the Examiner's quiz BLIND, from the lesson.

It never sees the answer key, the accepted alternates or the explanations: `solver_view()`
copies only the fields a learner would see. Python then compares its answers with the key
(checks.answers_match); questions where they disagree are dropped.
"""
import json

from pydantic import BaseModel

from checks import letter

SYSTEM = """You are the Quiz solver in a personal learning app. Answer every question in the
QUIZ using the LESSON (and your own knowledge where the lesson is silent).

Answer format:
- multiple_choice: the letter only, e.g. "B".
- numeric: the number with its unit, e.g. "2200 Ω" or "3.5 m".
- exact_term: the term only.

If a question is ambiguous or has no single correct answer, answer "AMBIGUOUS" and explain in
note. Otherwise note can be a few words on how you got the answer.
"""

VISIBLE_FIELDS = ("id", "kind", "question", "unit")


class SolverAnswer(BaseModel):
    id: str
    answer: str
    note: str


class SolverAnswers(BaseModel):
    answers: list[SolverAnswer]


def solver_view(questions: list[dict]) -> list[dict]:
    """What the solver may see: no answer, alternates, tolerance or explanation."""
    view = []
    for q in questions:
        v = {k: q[k] for k in VISIBLE_FIELDS if q.get(k)}
        if q["kind"] == "multiple_choice":
            v["choices"] = [f"{letter(i)}. {c}" for i, c in enumerate(q["choices"])]
        view.append(v)
    return view


def solve(llm, profile: dict, lesson_text: str, questions: list[dict], profile_id=None) -> dict[str, dict]:
    """Return {question id: {"answer", "note"}}."""
    quiz = json.dumps(solver_view(questions), ensure_ascii=False, indent=1)
    request = f"LESSON:\n{lesson_text}\n\nQUIZ:\n{quiz}"
    result = llm.call_structured("quiz_solver", SYSTEM, [{"role": "user", "content": request}],
                                 SolverAnswers, profile=profile, profile_id=profile_id)
    return {a.id: {"answer": a.answer, "note": a.note} for a in result.answers}
