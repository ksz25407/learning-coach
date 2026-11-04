"""Coach: a simple state machine that routes each step to the right agent.

Stages for each subject (profile):
    scoping -> confirm_profile -> planning -> learning

Python decides the stage. The LLM is only used inside agents and for free-form chat
while learning.
"""
import config
from agents import planner, scoper, teacher
from agents.planner import PlanningFailed  # noqa: F401  (re-exported for the UI)
from db import store

GREETING = "Hi! What would you like to learn? Tell me the subject and anything you already know about your goal."

CHAT_SYSTEM = """You are the Coach in a personal learning app. The learner is working through a
milestone plan. Answer their questions briefly and concretely, at their level. If a question
belongs to a later milestone, say so and give a short answer. Lessons are generated from your
own knowledge and are not yet fact-checked, so flag anything you are unsure about.
"""


class Coach:
    def __init__(self, conn, llm):
        self.conn = conn
        self.llm = llm

    # ---------- subjects ----------

    def new_subject(self) -> int:
        pid = store.create_profile(self.conn)
        store.add_chat(self.conn, pid, "greeting", "assistant", GREETING)
        return pid

    def stage(self, profile_id: int) -> str:
        return store.get_profile(self.conn, profile_id)["stage"]

    # ---------- chat entry point ----------

    def handle_message(self, profile_id: int, text: str) -> str:
        """Route a chat message based on the current stage. Returns the reply."""
        stage = self.stage(profile_id)
        if stage == "scoping":
            return self._scoping(profile_id, text)
        if stage == "learning":
            return self._chat(profile_id, text)
        if stage == "confirm_profile":
            return "Please review and confirm your profile in the form below."
        return "Your plan isn't ready yet. Use the 'Build my plan' button."

    # ---------- scoping ----------

    def _scoping(self, profile_id: int, text: str) -> str:
        store.add_chat(self.conn, profile_id, "scoping", "user", text)
        rows = store.get_chat(self.conn, profile_id, "scoping")
        history = [{"role": r["role"], "content": r["content"]} for r in rows]
        asked = sum(1 for r in rows if r["role"] == "assistant")

        turn = scoper.next_turn(self.llm, history, asked, profile_id=profile_id)

        if turn.done and turn.profile is not None:
            store.save_profile_data(self.conn, profile_id, turn.profile.model_dump())
            store.set_stage(self.conn, profile_id, "confirm_profile")
            reply = "Thanks! Here is your profile. Edit anything that's wrong, then confirm."
        else:
            reply = turn.next_question or "Could you tell me a bit more?"
        store.add_chat(self.conn, profile_id, "scoping", "assistant", reply)
        return reply

    def confirm_profile(self, profile_id: int, profile: dict):
        """Save the (possibly edited) profile and move on to planning."""
        profile = scoper.SubjectProfile.model_validate(profile).model_dump()
        store.save_profile_data(self.conn, profile_id, profile)
        store.set_stage(self.conn, profile_id, "planning")

    # ---------- planning ----------

    def make_plan(self, profile_id: int):
        """Run the Planner. Raises PlanningFailed (stage stays 'planning') if it fails twice."""
        profile = store.get_profile_data(self.conn, profile_id)
        plan = planner.make_plan(self.llm, profile, profile_id=profile_id)
        store.save_milestones(self.conn, profile_id, [m.model_dump() for m in plan.milestones])
        store.set_stage(self.conn, profile_id, "learning")
        store.add_chat(self.conn, profile_id, "learning", "assistant",
                       "Your plan is ready. Open the first lesson, or ask me anything.")
        return plan

    # ---------- lessons ----------

    def request_lesson(self, milestone_id: int):
        """Return the lesson row. Generates it only the first time."""
        lesson = store.get_lesson(self.conn, milestone_id)
        if lesson is not None:
            return lesson
        m = store.get_milestone(self.conn, milestone_id)
        if m["status"] == "locked":
            raise ValueError("This milestone is locked. Finish the previous one first.")
        profile = store.get_profile_data(self.conn, m["profile_id"])
        text, sources, model = teacher.write_lesson(
            self.llm, profile, dict(m), profile_id=m["profile_id"]
        )
        store.save_lesson(self.conn, milestone_id, text, sources, model, verified=False)
        return store.get_lesson(self.conn, milestone_id)

    def complete_milestone(self, milestone_id: int):
        """Mark done and unlock the next milestone (manual tick in Phase 1)."""
        m = store.get_milestone(self.conn, milestone_id)
        if m["status"] != "active":
            return
        store.set_milestone_status(self.conn, milestone_id, "done")
        for nxt in store.get_milestones(self.conn, m["profile_id"]):
            if nxt["position"] == m["position"] + 1 and nxt["status"] == "locked":
                store.set_milestone_status(self.conn, nxt["id"], "active")

    # ---------- free chat ----------

    def chat_history(self, profile_id: int, limit: int | None = None) -> list[dict]:
        """Last `limit` learning-chat messages, starting with a user message (API rule)."""
        limit = limit or config.CHAT_HISTORY_LIMIT
        rows = store.get_chat(self.conn, profile_id, "learning")[-limit:]
        msgs = [{"role": r["role"], "content": r["content"]} for r in rows]
        while msgs and msgs[0]["role"] != "user":
            msgs.pop(0)
        return msgs

    def _chat(self, profile_id: int, text: str) -> str:
        store.add_chat(self.conn, profile_id, "learning", "user", text)
        profile = store.get_profile_data(self.conn, profile_id)
        status = "\n".join(
            f"{m['position']}. [{m['status']}] {m['title']}: {m['objective']}"
            for m in store.get_milestones(self.conn, profile_id)
        )
        reply, _ = self.llm.call_text(
            "coach", CHAT_SYSTEM, self.chat_history(profile_id),
            profile=profile, extra="Milestones:\n" + status, profile_id=profile_id,
        )
        store.add_chat(self.conn, profile_id, "learning", "assistant", reply)
        return reply
