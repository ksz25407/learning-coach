"""Examiner: drafts the quiz + answer key, the practical task + rubric, and concept tags.

Quiz: only formats Python can mark without another AI call: multiple choice, numeric (with
unit and tolerance) and short exact terms (with accepted alternates). Every question is tagged
with the concept it tests (used by spaced review) and the lesson section it comes from (so a
missed question can link back to it).

Task (Phase 4): one practical task matching proof_of_skill, with a rubric written up front.
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
  "V", "µA/V²"; "" if none); tolerance_pct = allowed error in percent (1 for exact calculations, up to 5
  for values read off a chart).
- "exact_term": answer = one short term or name; alternates = other spellings or forms that are
  also correct (e.g. British/American spelling, abbreviation).

Rules:
- Every question has ONE unambiguous correct answer that follows from the lesson.
- explanation: one or two sentences on why the answer is right, pointing to the lesson.
- concept: the idea the question tests, as a short noun phrase (2-5 words, lower case), e.g.
  "wall location line" or "voltage divider ratio". If CONCEPTS are given and one fits, use
  that exact name. Questions on the same idea share a concept name.
- section: the exact text of the lesson heading (without the # marks) of the section the
  answer comes from. "" if the lesson has no headings.
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
    concept: str
    section: str


class Quiz(BaseModel):
    questions: list[Question]


def draft_quiz(llm, profile: dict, milestone: dict, lesson_text: str, n: int,
               avoid: list[dict] | None = None, concepts: list[str] | None = None,
               profile_id=None) -> list[dict]:
    """Return n (or so) questions as dicts. `avoid`: [{"question", "problem"}] from earlier drafts.
    `concepts`: concept names already in use for this milestone (reused when they fit)."""
    request = (f"Milestone: {milestone['title']}\nObjective: {milestone['objective']}\n"
               f"Write {n} questions.\n\nLESSON:\n{lesson_text}")
    if concepts:
        request += "\n\nCONCEPTS (reuse these names when they fit):\n" + "\n".join(f"- {c}" for c in concepts)
    if avoid:
        lines = "\n".join(f"- {a['question']}" + (f" (problem: {a['problem']})" if a.get("problem") else "")
                          for a in avoid)
        request += f"\n\nAVOID (already used or dropped):\n{lines}"
    quiz = llm.call_structured("examiner", SYSTEM, [{"role": "user", "content": request}], Quiz,
                               profile=profile, profile_id=profile_id)
    return [clean_question(q.model_dump()) for q in quiz.questions]


def clean_question(q: dict) -> dict:
    q["concept"] = " ".join(q.get("concept", "").lower().split()) or "general"
    q["section"] = q.get("section", "").strip().lstrip("#").strip()
    return q


# ---------- practical task (Phase 4) ----------

TASK_SYSTEM = """You are the Task examiner in a personal learning app. Write ONE practical task
that proves the learner can meet the milestone objective, plus a rubric, using only what the
LESSON teaches. The task must match the learner's proof_of_skill:

- "file": the learner makes something with their own tools (software, a drawing, a built
  object) and sends IMAGES of it (screenshots or photos). List each image to take in
  `screenshots`, precisely enough that a grader can check every rubric item from the images
  (e.g. "the finished drawing with all dimensions visible", "the settings panel with the
  chosen option selected"). Ask only for things an image can show.
- "solved_problem": give one concrete problem with all numbers and units. The learner types
  the solution with working (or photographs handwritten working). `screenshots` = [] unless a
  photo of working is useful ("photo of your handwritten working").
- "explain_back": ask the learner to explain the topic in their own words to a beginner (or
  to a specific audience), covering named points. `screenshots` = [].

Rubric rules:
- 3-6 items. Each item is ONE checkable thing, phrased as a pass condition ("The output
  voltage is 3.3 V within 2%", "The exported file uses the requested template"), never vague
  ("good work", "correct method").
- evidence: what the grader must actually SEE in the submission to pass the item (e.g. "the
  final line of the working states V_out ≈ 3.3 V", "image 2 shows the template name in the
  title bar").
- Doable in the milestone's estimated time.
"""


class RubricItem(BaseModel):
    criterion: str
    evidence: str


class Task(BaseModel):
    instructions: str
    rubric: list[RubricItem]
    screenshots: list[str]


def draft_task(llm, profile: dict, milestone: dict, lesson_text: str, profile_id=None) -> dict:
    """Return {"proof_type", "instructions", "rubric": [{"id", "criterion", "evidence"}], "screenshots"}."""
    proof = profile.get("proof_of_skill", "explain_back")
    request = (f"Milestone: {milestone['title']}\nObjective: {milestone['objective']}\n"
               f"Estimated time: {milestone.get('est_hours') or '?'} h\nproof_of_skill: {proof}\n\n"
               f"LESSON:\n{lesson_text}")
    task = llm.call_structured("examiner_task", TASK_SYSTEM, [{"role": "user", "content": request}], Task,
                               profile=profile, profile_id=profile_id)
    rubric = [{"id": f"r{i}", **item.model_dump()} for i, item in enumerate(task.rubric, 1)]
    return {"proof_type": proof, "instructions": task.instructions, "rubric": rubric,
            "screenshots": task.screenshots}


# ---------- concept tags for old quizzes (Phase 4 backfill) ----------

TAG_SYSTEM = """You are the Concept tagger in a personal learning app. For each QUESTION, give
the concept it tests as a short noun phrase (2-5 words, lower case), and the exact text of the
LESSON heading (without # marks) of the section its answer comes from ("" if no headings).
Questions on the same idea must share the same concept name. Return one tag per question id.
"""


class ConceptTag(BaseModel):
    id: str
    concept: str
    section: str


class ConceptTags(BaseModel):
    tags: list[ConceptTag]


def tag_concepts(llm, profile: dict, lesson_text: str, questions: list[dict], profile_id=None) -> dict:
    """Return {question id: {"concept", "section"}}. Questions the tagger skipped get "general"."""
    lines = "\n".join(f"- {q['id']}: {q['question']}" for q in questions)
    request = f"LESSON:\n{lesson_text}\n\nQUESTIONS:\n{lines}"
    result = llm.call_structured("examiner", TAG_SYSTEM, [{"role": "user", "content": request}], ConceptTags,
                                 profile=profile, profile_id=profile_id)
    got = {t.id: clean_question({"concept": t.concept, "section": t.section}) for t in result.tags}
    return {q["id"]: got.get(q["id"], {"concept": "general", "section": ""}) for q in questions}
