import pytest

import config
from agents.curator import CuratorDraft, ResourceDraft
from agents.planner import PlanningFailed
from agents.scoper import ScoperTurn, SubjectProfile
from db import store
from llm import RefusalError
from orchestrator import Coach
from sources import web
from tests.conftest import agent_of, pass_all_checks, reply, search_results, text
from tests.helpers import FakeHTTP, FakeResponse, article_html, make_pdf
from tests.test_agents import PROFILE, plan_draft


def queue_lesson(fake, teacher_text="## Walls\nDraw walls."):
    """Queue a Curator search that finds nothing, then the Teacher's reply."""
    fake.messages.text_replies += [reply(text("Nothing useful found.")), teacher_text]


@pytest.fixture
def coach(conn, llm, fake):
    pass_all_checks(fake)  # Phase 3 checkers pass unless a test says otherwise
    return Coach(conn, llm)


def scope_to_learning(coach, fake):
    """Drive a new subject all the way to the learning stage."""
    pid = coach.new_subject()
    fake.messages.parse_replies = [
        ScoperTurn(done=False, next_question="What's your goal?", profile=None),
        ScoperTurn(done=True, next_question="", profile=SubjectProfile(**PROFILE)),
        plan_draft(4),
    ]
    assert coach.handle_message(pid, "Revit") == "What's your goal?"
    coach.handle_message(pid, "Model my house")
    assert coach.stage(pid) == "confirm_profile"
    coach.confirm_profile(pid, {**PROFILE, "hours_per_week": 6})
    assert coach.stage(pid) == "planning"
    coach.make_plan(pid)
    assert coach.stage(pid) == "learning"
    return pid


def test_full_flow(coach, fake, conn):
    pid = scope_to_learning(coach, fake)
    assert store.get_profile_data(conn, pid)["hours_per_week"] == 6  # edit was saved
    assert len(store.get_milestones(conn, pid)) == 4


def test_scoper_history_starts_with_user(coach, fake):
    pid = coach.new_subject()
    fake.messages.parse_replies = [ScoperTurn(done=False, next_question="Q?", profile=None)]
    coach.handle_message(pid, "Revit")
    sent = fake.messages.calls[0][1]["messages"]
    assert sent == [{"role": "user", "content": "Revit"}]  # greeting not sent


def test_scoper_told_to_finish_after_max_questions(coach, fake, conn):
    pid = coach.new_subject()
    for i in range(config.SCOPER_MAX_QUESTIONS):
        store.add_chat(conn, pid, "scoping", "user", f"a{i}")
        store.add_chat(conn, pid, "scoping", "assistant", f"q{i}")
    fake.messages.parse_replies = [ScoperTurn(done=True, next_question="", profile=SubjectProfile(**PROFILE))]
    coach.handle_message(pid, "last answer")
    system = fake.messages.calls[0][1]["system"]
    assert "maximum number of questions" in system[-1]["text"]


def test_planning_failure_keeps_stage(coach, fake):
    pid = coach.new_subject()
    store.save_profile_data(coach.conn, pid, PROFILE)
    store.set_stage(coach.conn, pid, "planning")
    fake.messages.parse_replies = [plan_draft(2), plan_draft(10)]
    with pytest.raises(PlanningFailed):
        coach.make_plan(pid)
    assert coach.stage(pid) == "planning"


def test_complete_unlocks_next(coach, fake, conn):
    pid = scope_to_learning(coach, fake)
    ms = store.get_milestones(conn, pid)
    coach.complete_milestone(ms[0]["id"])
    assert [m["status"] for m in store.get_milestones(conn, pid)] == ["done", "active", "locked", "locked"]


def test_locked_lesson_refused(coach, fake, conn):
    pid = scope_to_learning(coach, fake)
    locked = store.get_milestones(conn, pid)[1]
    with pytest.raises(ValueError):
        coach.request_lesson(locked["id"])


def test_lesson_generated_once(coach, fake, conn):
    pid = scope_to_learning(coach, fake)
    first = store.get_milestones(conn, pid)[0]
    queue_lesson(fake)
    lesson1 = coach.request_lesson(first["id"])
    calls_after_first = len(fake.messages.calls)
    lesson2 = coach.request_lesson(first["id"])
    assert len(fake.messages.calls) == calls_after_first  # no new API call
    assert lesson1["id"] == lesson2["id"]
    assert lesson1["verified"] == 0  # passed, but written from model knowledge
    assert lesson1["status"] == "partly_unsourced"


def count(conn, table, where="1=1", args=()):
    return conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}", args).fetchone()[0]


def test_delete_subject_removes_data_but_keeps_spend(coach, fake, conn):
    keep = scope_to_learning(coach, fake)
    gone = scope_to_learning(coach, fake)
    for pid in (keep, gone):
        first = store.get_milestones(conn, pid)[0]
        queue_lesson(fake)
        coach.request_lesson(first["id"])
    gone_milestones = [m["id"] for m in store.get_milestones(conn, gone)]
    usage_before = count(conn, "usage_log")
    month_before, total_before = store.month_spend(conn), store.total_spend(conn)
    assert count(conn, "usage_log", "profile_id = ?", (gone,)) > 0

    coach.delete_subject(gone)

    # The deleted subject's data is gone...
    assert store.get_profile(conn, gone) is None
    assert store.get_milestones(conn, gone) == []
    assert store.get_chat(conn, gone) == []
    for mid in gone_milestones:
        assert store.get_lesson(conn, mid) is None
    # ...its spend history is kept...
    assert count(conn, "usage_log") == usage_before
    assert store.month_spend(conn) == month_before
    assert store.total_spend(conn) == total_before
    # ...and the other subject is untouched.
    assert coach.stage(keep) == "learning"
    assert len(store.get_milestones(conn, keep)) == 4
    assert store.get_lesson(conn, store.get_milestones(conn, keep)[0]["id"]) is not None
    assert store.get_chat(conn, keep) != []


def test_delete_subject_mid_scoping(coach, fake, conn):
    pid = coach.new_subject()  # no profile data, no milestones yet
    coach.delete_subject(pid)
    assert store.get_profile(conn, pid) is None
    assert store.get_chat(conn, pid) == []


def test_chat_sends_only_last_n(coach, fake, conn):
    pid = scope_to_learning(coach, fake)
    for i in range(30):
        store.add_chat(conn, pid, "learning", "user", f"u{i}")
        store.add_chat(conn, pid, "learning", "assistant", f"a{i}")
    fake.messages.text_replies = ["ok"]
    coach.handle_message(pid, "new question")
    sent = fake.messages.calls[-1][1]["messages"]
    assert len(sent) <= config.CHAT_HISTORY_LIMIT
    assert sent[0]["role"] == "user"
    assert sent[-1] == {"role": "user", "content": "new question"}


# ---------- Phase 2: sources pipeline ----------

URL = "https://docs.example.com/walls"


def queue_curator_with_page(fake):
    fake.messages.text_replies.append(reply(text("notes"), search_results({URL: "Walls guide"}), searches=1))
    fake.messages.parse_replies.append(CuratorDraft(resources=[ResourceDraft(
        url=URL, title="Walls guide", kind="page", section="Creating walls", minutes=12,
        why_best="official docs", role="primary")]))


def test_lesson_uses_curator_page_and_pdf(coach, fake, conn, monkeypatch):
    http = FakeHTTP({URL: FakeResponse(article_html("Wall tool steps"))})
    monkeypatch.setattr(web.requests, "get", http.get)
    pid = scope_to_learning(coach, fake)
    coach.upload_pdf(pid, "manual.pdf", make_pdf(["Ground floor walls: use the Wall tool. " * 5]))
    mid = store.get_milestones(conn, pid)[0]["id"]

    queue_curator_with_page(fake)
    fake.messages.text_replies.append("## Walls\nDraw walls.")
    lesson = coach.request_lesson(mid)

    teacher_call = [c for _, c in fake.messages.calls if agent_of(c) == "teacher"][-1]
    docs = [b for b in teacher_call["messages"][0]["content"] if b["type"] == "document"]
    assert [d["context"].split(".")[0] for d in docs] == ["Source kind: pdf", "Source kind: web"]
    notes = store.lesson_notes(lesson)
    assert notes["pdf"][0]["pages"] == [1]
    assert notes["web"][0]["type"] == "web_used"
    assert store.get_resources(conn, mid)[0]["url"] == URL
    assert not store.lesson_is_outdated(conn, mid)


def test_regenerate_failure_keeps_old_lesson(coach, fake, conn):
    pid = scope_to_learning(coach, fake)
    mid = store.get_milestones(conn, pid)[0]["id"]
    queue_lesson(fake, "## Walls\nOld lesson.")
    coach.request_lesson(mid)

    queue_lesson(fake, "")
    fake.messages.stop_reason = "refusal"  # the Teacher (and search) calls now refuse
    with pytest.raises(RefusalError):
        coach.request_lesson(mid, regenerate=True)
    assert "Old lesson." in store.get_lesson(conn, mid)["content"]


def test_regenerate_replaces_lesson(coach, fake, conn):
    pid = scope_to_learning(coach, fake)
    mid = store.get_milestones(conn, pid)[0]["id"]
    queue_lesson(fake, "## Walls\nOld lesson.")
    first = coach.request_lesson(mid)
    queue_lesson(fake, "## Walls\nNew lesson.")
    second = coach.request_lesson(mid, regenerate=True)
    assert second["id"] == first["id"]
    assert "New lesson." in second["content"]


def test_curator_picks_reused_after_teacher_failure(coach, fake, conn, monkeypatch):
    monkeypatch.setattr(web.requests, "get", FakeHTTP({}).get)
    pid = scope_to_learning(coach, fake)
    mid = store.get_milestones(conn, pid)[0]["id"]
    queue_curator_with_page(fake)
    fake.messages.text_replies.append(reply(text("cut off"), stop_reason="max_tokens"))
    with pytest.raises(Exception):
        coach.request_lesson(mid)  # Teacher fails...
    assert store.get_lesson(conn, mid) is None
    searches_before = sum(1 for c in fake.messages.calls if "tools" in c[1])

    fake.messages.text_replies.append("## Walls\nDraw walls.")
    coach.request_lesson(mid)  # ...retry reuses the Curator's picks: no new search
    assert sum(1 for c in fake.messages.calls if "tools" in c[1]) == searches_before


def test_cost_estimate_grows_with_pdf(coach, fake, conn):
    pid = scope_to_learning(coach, fake)
    typical, worst = coach.estimate_lesson_cost(pid)
    assert typical >= config.WEB_SEARCH_MAX_USES * config.WEB_SEARCH_USD
    assert worst > typical
    coach.upload_pdf(pid, "manual.pdf", make_pdf(["walls " * 400] * 5))
    assert coach.estimate_lesson_cost(pid)[0] > typical


def test_delete_subject_removes_pdfs_and_resources_keeps_web_cache(coach, fake, conn):
    pid = scope_to_learning(coach, fake)
    mid = store.get_milestones(conn, pid)[0]["id"]
    coach.upload_pdf(pid, "manual.pdf", make_pdf(["Walls and more walls. " * 10]))
    store.save_resources(conn, mid, [{"url": URL, "title": "t", "kind": "page", "role": "primary"}])
    store.cache_page(conn, URL, "cached")

    coach.delete_subject(pid)

    assert count(conn, "pdf_uploads") == 0 and count(conn, "pdf_pages") == 0
    assert count(conn, "resources") == 0
    assert count(conn, "web_pages") == 1  # shared cache kept


def log_calls(conn, agent, costs):
    for c in costs:
        store.log_usage(conn, None, agent, "m", 1, 1, 0, 0, c)


def test_estimate_uses_formula_until_enough_calls(coach, conn):
    pid = store.create_profile(conn)
    formula = coach._formula_costs(pid)
    log_calls(conn, "teacher", [0.03] * (config.EST_MIN_LESSONS - 1))
    assert coach.agent_costs(pid)["teacher"] == pytest.approx(formula["teacher"])


def test_estimate_uses_real_averages_kept_high(coach, conn):
    pid = store.create_profile(conn)
    log_calls(conn, "teacher", [0.03, 0.05, 0.04])
    assert coach.agent_costs(pid)["teacher"] == pytest.approx(0.04 * config.EST_SAFETY_FACTOR)


def test_estimate_uses_only_recent_calls(coach, conn, monkeypatch):
    monkeypatch.setattr(config, "EST_SAMPLE_CALLS", 3)
    pid = store.create_profile(conn)
    log_calls(conn, "fact_checker", [1.0] * 5)  # old, expensive
    log_calls(conn, "fact_checker", [0.01] * 3)  # recent
    assert coach.agent_costs(pid)["fact_checker"] == pytest.approx(0.01 * config.EST_SAFETY_FACTOR)


def test_estimate_typical_and_max(coach, conn, monkeypatch):
    pid = store.create_profile(conn)
    monkeypatch.setattr(coach, "agent_costs", lambda _pid: {a: 1.0 for a in coach.LESSON_AGENTS})
    # typical: 2 curator + teacher + 2 checkers + 2 quiz agents = 7 calls
    # max: + 2 rewrites (2 teacher + 4 checker calls) + 2 quiz redrafts (4 calls) = 17 calls
    assert coach.estimate_lesson_cost(pid) == (7.0, 17.0)
    assert coach.estimate_check_cost(pid) == (4.0, 4.0 + 2 + 4 + 4)
