"""Shared test helpers: a temp database and a fake Anthropic client (no real API calls)."""
import json
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import store  # noqa: E402
from llm import LLM  # noqa: E402


def usage(inp=100, out=50, cw=0, cr=0, searches=0):
    return SimpleNamespace(input_tokens=inp, output_tokens=out,
                           cache_creation_input_tokens=cw, cache_read_input_tokens=cr,
                           server_tool_use=SimpleNamespace(web_search_requests=searches))


# ---- builders for fake content blocks ----

def text(t, citations=None):
    return SimpleNamespace(type="text", text=t, citations=citations)


def cite(doc_index, cited_text="quoted text"):
    return SimpleNamespace(type="char_location", document_index=doc_index, cited_text=cited_text,
                           start_char_index=0, end_char_index=len(cited_text))


def search_results(found: dict):
    """A web_search_tool_result block listing {url: title}."""
    return SimpleNamespace(type="web_search_tool_result", content=[
        SimpleNamespace(type="web_search_result", url=u, title=t) for u, t in found.items()])


def search_error(code="max_uses_exceeded"):
    return SimpleNamespace(type="web_search_tool_result",
                           content=SimpleNamespace(type="web_search_tool_result_error", error_code=code))


def reply(*blocks, stop_reason="end_turn", searches=0):
    """A create() reply with content blocks, a stop reason and a web search count."""
    return {"content": list(blocks), "stop_reason": stop_reason, "searches": searches}


# The checker agents run in parallel threads, so their replies can't come from one ordered
# queue. They are found by the first words of their system prompt instead.
AGENT_MARKERS = {
    "fact_checker": "You are the Fact-checker",
    "pedagogy": "You are the Pedagogy reviewer",
    "examiner": "You are the Examiner",
    "quiz_solver": "You are the Quiz solver",
    "teacher": "You are the Teacher",
    "examiner_task": "You are the Task examiner",
    "concept_tagger": "You are the Concept tagger",
    "grader": "You are the Grader",
    "task_solver": "You are the Task solver",
}


def agent_of(kwargs) -> str | None:
    system = kwargs.get("system")
    first = system[0]["text"] if isinstance(system, list) else (system or "")
    for name, marker in AGENT_MARKERS.items():
        if first.startswith(marker):
            return name
    return None


class FakeMessages:
    """Replays queued replies. parse() replies are Pydantic objects. create() replies are
    strings (one text block) or reply(...) dicts (blocks, stop reason, search count).

    Checker agents: by_agent[name] is a queue just for that agent; when it is empty,
    defaults[name] is used (a reply, or a function of the request kwargs)."""

    def __init__(self):
        self.parse_replies = []
        self.text_replies = []
        self.by_agent = {}
        self.defaults = {}
        self.calls = []
        self.model = "claude-sonnet-5-5"
        self.stop_reason = "end_turn"
        self.lock = threading.Lock()

    def parse(self, **kwargs):
        with self.lock:
            self.calls.append(("parse", kwargs))
            agent = agent_of(kwargs)
            if self.by_agent.get(agent):
                out = self.by_agent[agent].pop(0)
            elif agent in self.defaults:
                out = self.defaults[agent]
            else:
                out = self.parse_replies.pop(0)
        if callable(out):
            out = out(kwargs)
        if isinstance(out, Exception):
            raise out
        return SimpleNamespace(parsed_output=out, usage=usage(),
                               model=self.model, stop_reason=self.stop_reason, content=[])

    def create(self, **kwargs):
        self.calls.append(("create", kwargs))
        r = self.text_replies.pop(0)
        if isinstance(r, str):
            r = reply(text(r), stop_reason=self.stop_reason)
        return SimpleNamespace(content=r["content"], usage=usage(searches=r["searches"]),
                               model=self.model, stop_reason=r["stop_reason"])

    def count_tokens(self, **kwargs):
        self.calls.append(("count_tokens", kwargs))
        return SimpleNamespace(input_tokens=999_999)


# ---- Phase 3: checker replies ----

def mc(question="Which tool draws walls?", answer="A", choices=("Wall", "Floor", "Roof"),
       concept="wall tool", section="Walls"):
    """An Examiner multiple-choice question."""
    from agents.examiner import Question
    return Question(kind="multiple_choice", question=question, choices=list(choices), answer=answer,
                    unit="", tolerance_pct=0, alternates=[], explanation="The lesson says so.",
                    concept=concept, section=section)


def task_draft(n_items=3, screenshots=("floor plan with dimensions",)):
    """An Examiner practical task (every rubric item a plain "check")."""
    from agents.examiner import RubricItem, Task
    return Task(instructions="Model the ground floor walls.",
                rubric=[RubricItem(kind="check", criterion=f"Item {i}", evidence=f"visible thing {i}",
                                   quantity="", value="", unit="", tolerance_pct=0)
                        for i in range(1, n_items + 1)],
                screenshots=list(screenshots))


def grading(*verdicts, evidence="Screenshot 1: dimension 4200 between grids A and B"):
    """A Grader reply: one verdict per rubric item r1, r2, ..."""
    from agents.grader import Grading, ItemGrade
    return Grading(grades=[ItemGrade(id=f"r{i}", verdict=v, evidence=evidence if v != "cant_tell" else "",
                                     reason="because", need="a closer screenshot" if v == "cant_tell" else "")
                           for i, v in enumerate(verdicts, 1)])


def quiz(*questions):
    from agents.examiner import Quiz
    return Quiz(questions=list(questions))


def quiz_ids(kwargs) -> list[str]:
    """Question ids in a Quiz solver request."""
    text_ = kwargs["messages"][0]["content"]
    return [q["id"] for q in json.loads(text_.split("QUIZ:\n", 1)[1])]


def solver_says(answer_for):
    """A Quiz solver reply built from the request: answer_for(question id) -> answer string."""
    from agents.quiz_solver import SolverAnswer, SolverAnswers

    def build(kwargs):
        return SolverAnswers(answers=[SolverAnswer(id=i, answer=answer_for(i), note="")
                                      for i in quiz_ids(kwargs)])
    return build


def fact_flags(*flags):
    from agents.fact_checker import FactCheck, FactFlag
    return FactCheck(flags=[FactFlag(**{"problem": "wrong", "evidence": "source says otherwise",
                                        "basis": "source", "severity": "major", **f}) for f in flags])


def ped_flags(*flags):
    from agents.pedagogy import PedagogyFlag, PedagogyReview
    return PedagogyReview(flags=[PedagogyFlag(**{"problem": "too long", "reason": "budget",
                                                 "kind": "length", "severity": "major", **f}) for f in flags])


def pass_all_checks(fake, n_questions=5):
    """Every checker passes; the quiz has n MC questions (answer A) the solver gets right."""
    fake.messages.defaults.update({
        "fact_checker": fact_flags(),
        "pedagogy": ped_flags(),
        "examiner": quiz(*[mc(f"Question {i}?") for i in range(n_questions)]),
        "quiz_solver": solver_says(lambda qid: "A"),
        "examiner_task": task_draft(),
    })


@pytest.fixture
def conn(tmp_path):
    c = store.connect(tmp_path / "test.sqlite")
    yield c
    c.close()


@pytest.fixture
def fake():
    return SimpleNamespace(messages=FakeMessages())


@pytest.fixture
def llm(conn, fake):
    return LLM(conn, client=fake)
