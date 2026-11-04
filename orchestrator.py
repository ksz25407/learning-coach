"""Coach: a simple state machine that routes each step to the right agent.

Stages for each subject (profile):
    scoping -> confirm_profile -> planning -> learning

Python decides the stage. The LLM is only used inside agents and for free-form chat
while learning.
"""
import config
from agents import curator, planner, scoper, teacher
from agents.planner import PlanningFailed  # noqa: F401  (re-exported for the UI)
from db import store
from llm import price_for
from sources.pdf import PdfSource, ingest_pdf
from sources.web import WebSource

GREETING = "Hi! What would you like to learn? Tell me the subject and anything you already know about your goal."

CHAT_SYSTEM = """You are the Coach in a personal learning app. The learner is working through a
milestone plan. Answer their questions briefly and concretely, at their level. If a question
belongs to a later milestone, say so and give a short answer. Lessons cite web pages and the
learner's uploaded PDFs, but they are not yet fact-checked, and your chat answers come from
your own knowledge, so flag anything you are unsure about.
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

    def delete_subject(self, profile_id: int):
        """Remove the subject's profile, plan, lessons and chat. Spend history is kept."""
        store.delete_profile(self.conn, profile_id)

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

    def request_lesson(self, milestone_id: int, regenerate: bool = False):
        """Return the lesson row. Generates it the first time, or again when regenerate=True.

        Pipeline: Curator (web search, reused unless regenerating) -> download pages + pick
        PDF pages -> Teacher (cited). On regenerate nothing is saved until the Teacher
        succeeds, so a failed regenerate keeps the old lesson and resources.
        """
        lesson = store.get_lesson(self.conn, milestone_id)
        if lesson is not None and not regenerate:
            return lesson
        m = store.get_milestone(self.conn, milestone_id)
        if m["status"] == "locked":
            raise ValueError("This milestone is locked. Finish the previous one first.")
        pid = m["profile_id"]
        profile = store.get_profile_data(self.conn, pid)

        curator_info = {"reused": True}
        new_resources = None
        if regenerate or not store.has_resources(self.conn, milestone_id):
            new_resources, curator_info = curator.find_resources(self.llm, profile, dict(m), profile_id=pid)
            resources = new_resources
            if lesson is None:  # first lesson: keep the picks now, so a retry doesn't search again
                store.save_resources(self.conn, milestone_id, new_resources)
                new_resources = None
        else:
            resources = store.get_resources(self.conn, milestone_id)

        pdf, web = PdfSource(self.conn, pid), WebSource(self.conn, resources)
        result = teacher.write_lesson(self.llm, profile, dict(m), sources=[pdf, web], profile_id=pid)

        if new_resources is not None:
            store.save_resources(self.conn, milestone_id, new_resources)
        notes = {"pdf": pdf.notes, "web": web.notes, "curator": curator_info}
        store.save_lesson(self.conn, milestone_id, result.text, result.sources, result.model,
                          verified=False, citations=result.citations, notes=notes)
        return store.get_lesson(self.conn, milestone_id)

    # ---------- PDF sources ----------

    def upload_pdf(self, profile_id: int, filename: str, data: bytes, page_from=None, page_to=None) -> str:
        """Store a PDF's text for this subject. Returns a warning ('' if all is fine)."""
        _, warning = ingest_pdf(self.conn, profile_id, filename, data, page_from, page_to)
        return warning

    def estimate_lesson_cost(self, profile_id: int) -> float:
        """Rough upper estimate (USD) for one Curator + Teacher run."""
        haiku, teacher_p = price_for(config.MODELS["curator"]), price_for(config.MODELS["teacher"])
        curator_cost = (config.WEB_SEARCH_MAX_USES * config.WEB_SEARCH_USD
                        + config.EST_CURATOR_INPUT_TOKENS * haiku["input"] / 1e6
                        + 2 * config.MAX_TOKENS["curator"] * haiku["output"] / 1e6)
        pdf_chars = sum(store.pdf_page_lengths(self.conn, profile_id)[: config.PDF_MAX_PAGES])
        teacher_in = pdf_chars / 4 + 2 * config.PAGE_MAX_TOKENS + 2000  # PDF + 2 web pages + prompt
        teacher_cost = (teacher_in * teacher_p["input"]
                        + config.EST_TEACHER_OUTPUT_TOKENS * teacher_p["output"]) / 1e6
        return curator_cost + teacher_cost

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
