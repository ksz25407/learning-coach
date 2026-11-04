"""Phase 4 agents: Examiner concepts + task, concept tagger, Grader (with the evidence rule)."""
import io

from agents import examiner, grader
from tests.conftest import grading, mc, quiz, task_draft
from tests.test_agents import PROFILE

MILESTONE = {"title": "Walls", "objective": "Model ground-floor walls in 20 min", "est_hours": 2}
TASK = {"proof_type": "file", "instructions": "Model the walls.", "screenshots": ["floor plan"],
        "rubric": [{"id": "r1", "criterion": "Walls drawn", "evidence": "walls in plan"},
                   {"id": "r2", "criterion": "Core Centerline", "evidence": "Properties palette"},
                   {"id": "r3", "criterion": "Height 3 m", "evidence": "Unconnected height 3000"}]}


def png(w=10, h=10) -> bytes:
    from PIL import Image
    out = io.BytesIO()
    Image.new("RGB", (w, h), "white").save(out, format="PNG")
    return out.getvalue()


def request_text(fake, i=-1):
    content = fake.messages.calls[i][1]["messages"][0]["content"]
    return content if isinstance(content, str) else content[0]["text"]


# ---------- Examiner ----------

def test_quiz_questions_get_clean_concepts_and_sections(llm, fake):
    fake.messages.parse_replies = [quiz(mc(concept="  Wall   Tool ", section="## Drawing walls"))]
    qs = examiner.draft_quiz(llm, PROFILE, MILESTONE, "## Drawing walls\nUse Wall.", 1,
                             concepts=["wall tool", "location line"])
    assert qs[0]["concept"] == "wall tool"
    assert qs[0]["section"] == "Drawing walls"
    req = request_text(fake)
    assert "CONCEPTS" in req and "- location line" in req


def test_task_rubric_gets_ids_and_matches_proof_of_skill(llm, fake):
    fake.messages.by_agent["examiner_task"] = [task_draft(n_items=3)]
    task = examiner.draft_task(llm, PROFILE, MILESTONE, "lesson")
    assert [r["id"] for r in task["rubric"]] == ["r1", "r2", "r3"]
    assert task["proof_type"] == "file" and task["screenshots"]
    assert "proof_of_skill: file" in request_text(fake)
    assert fake.messages.calls[-1][1]["model"] == "claude-haiku-5-5"


def test_concept_tagger_fills_gaps_with_general(llm, fake):
    tags = examiner.ConceptTags(tags=[examiner.ConceptTag(id="q1", concept="Wall Tool", section="# Walls")])
    fake.messages.by_agent["concept_tagger"] = [tags]
    out = examiner.tag_concepts(llm, PROFILE, "lesson", [{"id": "q1", "question": "A?"},
                                                          {"id": "q2", "question": "B?"}])
    assert out == {"q1": {"concept": "wall tool", "section": "Walls"},
                   "q2": {"concept": "general", "section": ""}}


# ---------- Grader ----------

def test_grader_sends_screenshots_as_images_without_the_lesson(llm, fake):
    fake.messages.by_agent["grader"] = [grading("pass", "pass", "pass")]
    grades = grader.grade_task(llm, PROFILE, MILESTONE, TASK, "I drew them",
                               [("image/png", png()), ("image/png", png())])
    assert [g["verdict"] for g in grades] == ["pass", "pass", "pass"]
    kwargs = fake.messages.calls[-1][1]
    content = kwargs["messages"][0]["content"]
    images = [b for b in content if b["type"] == "image"]
    assert len(images) == 2 and images[0]["source"]["type"] == "base64"
    assert "Image 2:" in [b.get("text") for b in content]
    assert "LESSON" not in content[0]["text"]
    assert kwargs["model"] == "claude-sonnet-5-5"


# 3. A "pass" must name specific visible evidence; otherwise it is "cant_tell".

def test_grader_pass_without_evidence_becomes_cant_tell(llm, fake):
    reply = grading("pass", "pass", "fail")
    reply.grades[1].evidence = ""  # the model said pass but named no evidence
    fake.messages.by_agent["grader"] = [reply]
    grades = grader.grade_task(llm, PROFILE, MILESTONE, TASK, "done", [("image/png", png())])
    assert [g["verdict"] for g in grades] == ["pass", "cant_tell", "fail"]
    assert grades[1]["need"]  # asks for another screenshot


def test_grader_prompt_states_the_evidence_rule():
    assert "MUST fill `evidence`" in grader.SYSTEM
    assert '"cant_tell"' in grader.SYSTEM


def test_grader_missing_items_are_cant_tell_and_extra_ids_ignored(llm, fake):
    reply = grading("pass")  # only r1
    reply.grades.append(grader.ItemGrade(id="r9", verdict="fail", evidence="x", reason="?", need="", learner_value=""))
    fake.messages.by_agent["grader"] = [reply]
    grades = grader.grade_task(llm, PROFILE, MILESTONE, TASK, "done", [])
    assert [(g["id"], g["verdict"]) for g in grades] == [("r1", "pass"), ("r2", "cant_tell"), ("r3", "cant_tell")]


def test_big_screenshots_are_shrunk(monkeypatch):
    import config
    from PIL import Image
    monkeypatch.setattr(config, "IMAGE_MAX_PX", 100)
    media, data = grader.prepare_image(png(400, 200))
    assert media == "image/png"
    assert Image.open(io.BytesIO(data)).size == (100, 50)
    small = png(50, 50)
    assert grader.prepare_image(small) == ("image/png", small)  # small enough: unchanged
