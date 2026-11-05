"""Examiner: drafts the quiz + answer key, the practical task + rubric, and concept tags.

Quiz: only formats Python can mark without another AI call: multiple choice, numeric (with
unit and tolerance) and short exact terms (with accepted alternates). Every question is tagged
with the concept it tests (used by spaced review) and the lesson section it comes from (so a
missed question can link back to it).

Task (Phase 4): one practical task matching proof_of_skill, with a rubric written up front.
"""
from typing import Literal

from pydantic import BaseModel

import checks
import config
from agents import formatting
from sources.base import source_documents

SYSTEM = """You are the Examiner in a personal learning app. Write a short quiz that tests the
milestone objective, using only what the LESSON teaches. Match the learner's practice_type
(software: tools, commands, settings, values, order of steps; math: numeric problems with
units; language: exact words and forms; hands-on: measurements, procedures, safety).

Allowed question kinds (no others):
- "multiple_choice": 3-5 choices (plain text, no "A." prefixes). answer = the letter of the one
  correct choice ("A" for the first). Exactly one choice is correct.
- "numeric": answer = the number only (e.g. "2200"); unit = its unit symbol in plain text,
  never LaTeX (e.g. "Ω", "mm", "V", "µA/V²"; "" if none); tolerance_pct = allowed error in
  percent (1 for exact calculations, up to 5 for values read off a chart).
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
- question may use LaTeX and, if it needs a circuit, a netlist block after the question
  sentence (rules below). The question must state every value needed to answer it.
  answer, unit and alternates are always plain text.

""" + formatting.ALL


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


# ---------- practical task (Phase 4; solved problems with a verified key: Phase 5) ----------

TASK_SYSTEM = """You are the Task examiner in a personal learning app. Write ONE practical task
that proves the learner can meet the milestone objective, plus a rubric, using what the LESSON
teaches. Any attached documents are the learner's own course material (and the lesson's
sources): where they use a convention, notation, model or approximation, use the same one.
The task must match the learner's proof_of_skill:

- "file": the learner makes something with their own tools (software, a drawing, a built
  object) and sends IMAGES of it (screenshots or photos). List each image to take in
  `screenshots`, precisely enough that a grader can check every rubric item from the images
  (e.g. "the finished drawing with all dimensions visible", "the settings panel with the
  chosen option selected"). Ask only for things an image can show.
- "solved_problem": give one concrete problem with every value and unit needed, and name
  each final quantity to find with its symbol and unit ("Find $V_{th}$ in V and $R_{th}$ in
  kΩ"). The learner types the solution or photographs handwritten working.
  `screenshots` = ["photo of your handwritten working (all pages)"] or [].
- "explain_back": ask the learner to explain the topic in their own words to a beginner (or
  to a specific audience), covering named points. `screenshots` = [].

worked_solution: for "solved_problem", solve your own problem fully, step by step, BEFORE you
write the rubric; the rubric's values must be exactly the final results of this solution. It
is never shown to the learner before they pass. "" for other proof types.

Rubric rules (3-6 items, doable in the milestone's estimated time):
- For "file" and "explain_back", every item has kind "check": ONE checkable thing phrased as
  a pass condition ("The exported file uses the requested template"), never vague ("good
  work"). evidence = what the grader must SEE to pass it ("image 2 shows the template name in
  the title bar"). quantity = "", value = "", unit = "", tolerance_pct = 0.
- For "solved_problem":
  - 1-2 items of kind "value", one per final quantity the task asks for: quantity = its symbol
    exactly as the task names it (e.g. "V_th"), value = the correct number only (e.g. "6.4"),
    unit = plain text, never LaTeX (e.g. "V", "kΩ", "mA"), tolerance_pct = allowed error in
    percent (2 normally; up to 5 if the course's approximations make small differences
    normal). Work the value out carefully: a blind solver will check it. criterion = "",
    evidence = "" (the app writes them, so the learner doesn't see the answer).
  - 2-4 items of kind "method". A method item must accept ANY valid approach: say what must
    be correctly established, not which technique to use. Good: "Correctly sets up equations
    that determine V_n2, including the dependent source's contribution." Bad: "Uses KCL at
    node n2" (mesh analysis, superposition or a source transformation done correctly must
    also pass). If the answer depends on a convention or approximation from the course
    material, name it in the criterion and say an equivalent exact method also passes.
    evidence = what the grader must see in the working. quantity/value/unit = "", tolerance_pct = 0.

""" + formatting.ALL


class RubricItem(BaseModel):
    kind: Literal["check", "value", "method"]
    criterion: str
    evidence: str
    quantity: str
    value: str
    unit: str
    tolerance_pct: float


class Task(BaseModel):
    # Field order matters: the reply is written in this order, so a solved problem is worked
    # out in worked_solution BEFORE its values go into the rubric.
    instructions: str
    worked_solution: str
    rubric: list[RubricItem]
    screenshots: list[str]


def task_agent(profile: dict) -> str:
    """Solved problems are drafted by a stronger model (config.MODELS)."""
    return "examiner_task_solved" if profile.get("proof_of_skill") == "solved_problem" else "examiner_task"


def draft_task(llm, profile: dict, milestone: dict, lesson_text: str, profile_id=None,
               problems: list[str] | None = None, chunks=None) -> dict:
    """Return {"proof_type", "instructions", "rubric": [item dicts with "id"], "screenshots"}.
    `chunks`: the lesson's source documents (course conventions). `problems`: what the app's
    checks found wrong with the previous draft (a new task is written)."""
    proof = profile.get("proof_of_skill", "explain_back")
    request = (f"Milestone: {milestone['title']}\nObjective: {milestone['objective']}\n"
               f"Estimated time: {milestone.get('est_hours') or '?'} h\nproof_of_skill: {proof}\n\n"
               f"LESSON:\n{lesson_text}")
    if problems:
        request += ("\n\nYour previous draft was rejected by the app's checks. Write a new task without these "
                    "problems:\n" + "\n".join(f"- {p}" for p in problems))
    content = source_documents(chunks or []) + [{"type": "text", "text": request}]
    task = llm.call_structured(task_agent(profile), TASK_SYSTEM, [{"role": "user", "content": content}], Task,
                               profile=profile, profile_id=profile_id)
    rubric = [finish_item(f"r{i}", item.model_dump()) for i, item in enumerate(task.rubric, 1)]
    return {"proof_type": proof, "instructions": task.instructions, "rubric": rubric,
            "screenshots": task.screenshots, "worked_solution": task.worked_solution}


def finish_item(item_id: str, item: dict) -> dict:
    """Give a final-value item its criterion and evidence. They name the quantity and unit but
    not the value: the rubric is shown to you before you solve the problem."""
    item = {"id": item_id, **item}
    if item["kind"] == "value":
        item["quantity"] = item["quantity"].strip().strip("$")
        item["tolerance_pct"] = item["tolerance_pct"] or config.TASK_DEFAULT_TOLERANCE_PCT
        unit = f" in {item['unit']}" if item["unit"] else ""
        item["criterion"] = f"Final answer for {item['quantity']}{unit} is correct (within {item['tolerance_pct']:g}%)."
        item["evidence"] = f"the final value of {item['quantity']} stated in the working"
    return item


def rubric_problems(task: dict) -> list[str]:
    """Rule checks for a solved-problem rubric (no API call). [] = fine."""
    items = task["rubric"]
    values = [r for r in items if r["kind"] == "value"]
    methods = [r for r in items if r["kind"] == "method"]
    problems = []
    if not 1 <= len(values) <= 2:
        problems.append(f"the rubric needs 1-2 items of kind 'value' (final answers), it has {len(values)}")
    if not 2 <= len(methods) <= 4:
        problems.append(f"the rubric needs 2-4 items of kind 'method', it has {len(methods)}")
    if not 3 <= len(items) <= 6:
        problems.append(f"the rubric needs 3-6 items, it has {len(items)}")
    for r in values:
        if checks.parse_quantity(f"{r['value']} {r['unit']}", r["unit"]) is None:
            problems.append(f"value item {r['quantity'] or r['id']}: '{r['value']} {r['unit']}' is not a number "
                            f"with a readable unit (write the number only, e.g. '6.4', and a plain unit like 'V')")
        if not r["quantity"]:
            problems.append(f"value item {r['id']} has no quantity name")
        elif r["quantity"].replace("_", "").replace("{", "").replace("}", "") not in \
                task["instructions"].replace("_", "").replace("{", "").replace("}", ""):
            problems.append(f"the task never names the quantity '{r['quantity']}' that value item {r['id']} checks")
    return problems


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
