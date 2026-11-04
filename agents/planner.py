"""Planner: turns a subject profile into 4-8 milestones with TESTABLE objectives."""
import json

from pydantic import BaseModel, ValidationError, field_validator

SYSTEM = """You are the Planner in a personal learning app. Using the subject profile, write
4 to 8 milestones that take the learner from their current level to their goal, in order.

Each milestone has:
- title: short name
- objective: a TESTABLE objective - something the learner can be checked on, with a
  concrete task and ideally a condition or time limit. Match the profile's proof_of_skill.
  Good: "Model the ground-floor walls from a sketch in 20 minutes."
  Good: "Solve 3 of 4 two-loop circuits with mesh analysis, no notes."
  Bad (vague): "Understand walls", "Learn mesh analysis", "Know the interface".
  Never start an objective with: understand, learn, know, be familiar, explore, study, grasp.
- est_hours: estimated study hours

Fit the total hours to the learner's hours_per_week and deadline.
"""

VAGUE_STARTS = ("understand", "learn", "know", "be familiar", "get familiar",
                "explore", "study", "grasp", "appreciate")


class PlanningFailed(Exception):
    pass


# Shape sent to the API (no custom rules, so the API schema stays simple).
class MilestoneDraft(BaseModel):
    title: str
    objective: str
    est_hours: float


class PlanDraft(BaseModel):
    milestones: list[MilestoneDraft]


# Same shape plus our rules. Checked locally after the API replies.
class Milestone(MilestoneDraft):
    @field_validator("objective")
    @classmethod
    def objective_is_testable(cls, v: str) -> str:
        start = v.strip().lower()
        for word in VAGUE_STARTS:
            if start.startswith(word):
                raise ValueError(f"objective is vague (starts with '{word}'): {v!r}")
        return v


class Plan(BaseModel):
    milestones: list[Milestone]

    @field_validator("milestones")
    @classmethod
    def four_to_eight(cls, v: list) -> list:
        if not 4 <= len(v) <= 8:
            raise ValueError(f"need 4-8 milestones, got {len(v)}")
        return v


def check_plan(draft: PlanDraft) -> tuple[Plan | None, str]:
    """Return (plan, "") if valid, else (None, error message)."""
    try:
        return Plan.model_validate(draft.model_dump()), ""
    except ValidationError as e:
        errors = "; ".join(err["msg"] for err in e.errors())
        return None, errors


def make_plan(llm, profile: dict, profile_id=None) -> Plan:
    """Ask the Planner. One retry with the error message, then raise PlanningFailed."""
    messages = [{"role": "user", "content": "Write the milestone plan for this profile."}]
    draft = llm.call_structured("planner", SYSTEM, messages, PlanDraft, profile=profile, profile_id=profile_id)
    plan, error = check_plan(draft)
    if plan:
        return plan

    messages += [
        {"role": "assistant", "content": json.dumps(draft.model_dump())},
        {"role": "user", "content": f"That plan was rejected: {error}. Return a corrected plan."},
    ]
    draft = llm.call_structured("planner", SYSTEM, messages, PlanDraft, profile=profile, profile_id=profile_id)
    plan, error2 = check_plan(draft)
    if plan:
        return plan
    raise PlanningFailed(f"The Planner failed twice. First: {error}. Second: {error2}")
