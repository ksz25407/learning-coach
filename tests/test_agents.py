import pytest
from pydantic import ValidationError

from agents.planner import MilestoneDraft, PlanDraft, PlanningFailed, check_plan, make_plan
from agents.scoper import SubjectProfile
from agents.sources import ModelKnowledgeSource, SourceChunk, merge_sources
from agents.teacher import ensure_source_tags, write_lesson

PROFILE = dict(subject="Revit", goal="Model a small off-grid house", level="beginner",
               hours_per_week=5, deadline="none", practice_type="software",
               proof_of_skill="file", notes="metric units")


def plan_draft(n, objective="Model the ground-floor walls from a sketch in 20 minutes"):
    return PlanDraft(milestones=[MilestoneDraft(title=f"M{i}", objective=objective, est_hours=2)
                                 for i in range(n)])


# ---------- Scoper schema ----------

def test_profile_valid():
    assert SubjectProfile(**PROFILE).subject == "Revit"


def test_profile_rejects_bad_practice_type():
    with pytest.raises(ValidationError):
        SubjectProfile(**{**PROFILE, "practice_type": "cooking"})


def test_profile_rejects_bad_proof():
    with pytest.raises(ValidationError):
        SubjectProfile(**{**PROFILE, "proof_of_skill": "vibes"})


# ---------- Planner rules ----------

@pytest.mark.parametrize("n", [4, 6, 8])
def test_plan_accepts_4_to_8(n):
    plan, err = check_plan(plan_draft(n))
    assert plan is not None and err == ""


@pytest.mark.parametrize("n", [3, 9])
def test_plan_rejects_wrong_count(n):
    plan, err = check_plan(plan_draft(n))
    assert plan is None and "4-8" in err


@pytest.mark.parametrize("vague", ["Understand walls", "Learn mesh analysis", "know the UI", "  Explore families"])
def test_plan_rejects_vague_objectives(vague):
    plan, err = check_plan(plan_draft(5, objective=vague))
    assert plan is None and "vague" in err


def test_planner_retries_once_then_succeeds(llm, fake):
    fake.messages.parse_replies = [plan_draft(3), plan_draft(5)]
    plan = make_plan(llm, PROFILE)
    assert len(plan.milestones) == 5
    retry_messages = fake.messages.calls[1][1]["messages"]
    assert "rejected" in retry_messages[-1]["content"]


def test_planner_fails_after_two_bad_plans(llm, fake):
    fake.messages.parse_replies = [plan_draft(3), plan_draft(9)]
    with pytest.raises(PlanningFailed):
        make_plan(llm, PROFILE)
    assert len(fake.messages.calls) == 2  # no third attempt


# ---------- Sources ----------

def test_merge_sources_priority():
    chunks = [SourceChunk("a", "model", "m"), SourceChunk("b", "web", "w"),
              SourceChunk("c", "pdf", "p"), SourceChunk("d", "web", "w2")]
    assert [c.text for c in merge_sources(chunks)] == ["c", "b", "d", "a"]


def test_model_knowledge_source_is_empty():
    assert ModelKnowledgeSource().get_chunks("obj", PROFILE) == []


# ---------- Teacher ----------

def test_ensure_source_tags_adds_missing():
    text = "## Intro\nHello\n\n## Walls\n[Source: model]\nBody"
    out = ensure_source_tags(text)
    assert out.count("[Source: model]") == 2
    assert out.splitlines()[1] == "[Source: model]"


def test_teacher_lesson_tagged_and_sourced(llm, fake):
    fake.messages.text_replies = ["## Step 1\nDo this.\n\n## Practice\nTry it."]
    text, sources, model = write_lesson(llm, PROFILE, {"title": "Walls", "objective": "Model walls"})
    assert text.count("[Source: model]") == 2
    assert sources == [{"kind": "model", "ref": "claude-sonnet-5-5"}]
