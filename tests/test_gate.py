"""Phase 4 Coach: the Examiner gate, spaced review and progress. No real API calls."""
import io
from datetime import date, datetime, timedelta, timezone

import pytest

import clock
import config
from agents import examiner
from db import store
from orchestrator import Coach
from tests.conftest import agent_of, grading, mc, pass_all_checks, quiz
from tests.test_orchestrator import queue_lesson, scope_to_learning


def five_questions(prefix="Question"):
    """5 MC questions (answer A) over 2 concepts."""
    return quiz(*[mc(f"{prefix} {i}?", concept=f"concept {i % 2}", section="Walls") for i in range(5)])


@pytest.fixture
def coach(conn, llm, fake):
    pass_all_checks(fake)
    fake.messages.defaults["examiner"] = five_questions()
    return Coach(conn, llm)


@pytest.fixture
def mid(coach, fake, conn):
    """The first milestone, with a checked lesson, a 5-question quiz and a 3-item task."""
    pid = scope_to_learning(coach, fake)
    m = store.get_milestones(conn, pid)[0]["id"]
    queue_lesson(fake)
    coach.request_lesson(m)
    return m


def png() -> bytes:
    from PIL import Image
    out = io.BytesIO()
    Image.new("RGB", (8, 8), "white").save(out, format="PNG")
    return out.getvalue()


def answers(st, wrong=0):
    """Answer the current quiz: all "A", except the first `wrong` questions get "B"."""
    return {q["id"]: ("B" if i < wrong else "A") for i, q in enumerate(st["questions"])}


def statuses(conn, mid):
    pid = store.get_milestone(conn, mid)["profile_id"]
    return [m["status"] for m in store.get_milestones(conn, pid)]


# ---------- pipeline ----------

def test_lesson_pipeline_fills_bank_and_writes_task(coach, conn, mid):
    st = coach.gate_state(mid)
    assert st["quiz_status"] == "ready" and st["task_status"] == "not_submitted"
    assert [r["id"] for r in st["task"]["rubric"]] == ["r1", "r2", "r3"]
    assert store.bank_concepts(conn, mid) == ["concept 0", "concept 1"]
    assert not st["needs_prepare"]


# ---------- passing ----------

def test_quiz_and_task_pass_unlocks_next_and_starts_review(coach, fake, conn, mid):
    res = coach.submit_quiz(mid, answers(coach.gate_state(mid)))
    assert res["passed"] and res["score"] == 1.0 and not res["milestone_passed"]  # task still to do
    assert statuses(conn, mid)[:2] == ["active", "locked"]

    fake.messages.by_agent["grader"] = [grading("pass", "pass", "pass")]
    st = coach.submit_task(mid, "I drew the walls", [png()])

    assert st["passed"]
    assert statuses(conn, mid)[:2] == ["done", "active"]
    m = store.get_milestone(conn, mid)
    assert m["pass_method"] == "exam" and m["passed_at"].endswith("+00:00")
    pid = m["profile_id"]
    cards = store.cards(conn, pid)
    assert {c["concept"] for c in cards} == {"concept 0", "concept 1"}
    for c in cards:  # just shown in the exam -> first review in ~2 days
        assert clock.from_iso(c["due_at"]) - clock.utc_now() > timedelta(days=1)
    assert "You passed" in store.get_chat(conn, pid, "learning")[-1]["content"]


def test_80_percent_passes_the_quiz(coach, mid):
    res = coach.submit_quiz(mid, answers(coach.gate_state(mid), wrong=1))
    assert res["passed"] and res["score"] == pytest.approx(0.8)


# ---------- failing ----------

def test_failed_quiz_shows_misses_keeps_passed_task_and_needs_new_quiz(coach, fake, conn, mid):
    fake.messages.by_agent["grader"] = [grading("pass", "pass", "pass")]
    st = coach.submit_task(mid, "done", [png()])
    assert st["task_status"] == "passed" and not st["passed"]  # quiz still needed

    res = coach.submit_quiz(mid, answers(st, wrong=2))
    assert not res["passed"] and res["score"] == pytest.approx(0.6)
    miss = res["missed"][0]
    assert miss["given"] == "B" and miss["answer"] == "A"
    assert miss["explanation"] and miss["section"] == "Walls" and miss["concept"] == "concept 0"

    # Missed concepts go into review right away (Again -> back tomorrow), even before passing.
    pid = store.get_milestone(conn, mid)["profile_id"]
    cards = {c["concept"]: c for c in store.cards(conn, pid)}
    assert set(cards) == {"concept 0", "concept 1"}  # q1 (concept 0) and q2 (concept 1) missed
    again_due = clock.from_iso(cards["concept 0"]["due_at"])
    assert timedelta(hours=20) < again_due - clock.utc_now() < timedelta(days=1, hours=1)

    st = coach.gate_state(mid)
    assert st["quiz_status"] == "needs_new" and st["task_status"] == "passed"
    with pytest.raises(ValueError, match="new quiz"):
        coach.submit_quiz(mid, answers(st))

    # Fresh quiz: new questions; the old ones are listed under AVOID.
    fake.messages.by_agent["examiner"] = [five_questions("New question")]
    coach.new_quiz(mid)
    req = [c for _, c in fake.messages.calls if agent_of(c) == "examiner"][-1]["messages"][0]["content"]
    assert "AVOID" in req and "Question 0?" in req and "CONCEPTS" in req
    st = coach.gate_state(mid)
    assert st["quiz_status"] == "ready" and st["questions"][0]["question"] == "New question 0?"
    assert len(store.bank(conn, mid)) == 10  # both quizzes kept for review

    res = coach.submit_quiz(mid, answers(st))
    assert res["passed"] and res["milestone_passed"]  # task was already passed
    # The missed concept keeps its Again card; passing doesn't reset it.
    assert store.find_card(conn, mid, "concept 0")["due_at"] == cards["concept 0"]["due_at"]


def test_unchecked_lesson_disables_the_gate(coach, conn, mid):
    lesson = store.get_lesson(conn, mid)
    store.save_lesson(conn, mid, lesson["content"], [], "m", status="unchecked")
    st = coach.gate_state(mid)
    assert "Run checks" in st["disabled"]
    with pytest.raises(ValueError):
        coach.submit_quiz(mid, answers(st))
    with pytest.raises(ValueError):
        coach.submit_task(mid, "done", [])


def test_no_quiz_means_task_alone(coach, fake, conn, mid):
    store.delete_quiz(conn, mid)
    st = coach.gate_state(mid)
    assert st["quiz_status"] == "missing"
    fake.messages.by_agent["grader"] = [grading("pass", "pass", "pass")]
    assert coach.submit_task(mid, "done", [png()])["passed"]


# ---------- task grading ----------

def test_cant_tell_asks_for_more_then_manual_pass_needs_reason(coach, fake, conn, mid, monkeypatch):
    monkeypatch.setattr(config, "MANUAL_PASS_AFTER", 2)
    coach.submit_quiz(mid, answers(coach.gate_state(mid)))
    fake.messages.by_agent["grader"] = [grading("pass", "cant_tell", "pass"), grading("pass", "cant_tell", "pass")]

    st = coach.submit_task(mid, "done", [png()])
    assert st["task_status"] == "need_more" and not st["passed"]
    assert st["grades"][1]["need"]  # tells you which screenshot to send
    assert not st["manual_pass_allowed"]
    with pytest.raises(ValueError):
        coach.manual_pass(mid, "the grader can't read Revit's tiny font")

    st = coach.submit_task(mid, "done again", [png()])
    assert st["manual_pass_allowed"]
    with pytest.raises(ValueError, match="reason"):
        coach.manual_pass(mid, "   ")
    coach.manual_pass(mid, "the grader can't read Revit's tiny font")
    m = store.get_milestone(conn, mid)
    assert m["status"] == "done" and m["pass_method"] == "manual_reason"
    assert m["pass_reason"] == "the grader can't read Revit's tiny font"


# 3. A "pass" without named evidence is "cant_tell", so it can't pass the milestone.

def test_pass_without_evidence_does_not_pass_the_milestone(coach, fake, conn, mid):
    coach.submit_quiz(mid, answers(coach.gate_state(mid)))
    reply = grading("pass", "pass", "pass")
    reply.grades[2].evidence = ""
    fake.messages.by_agent["grader"] = [reply]
    st = coach.submit_task(mid, "done", [png()])
    assert [g["verdict"] for g in st["grades"]] == ["pass", "pass", "cant_tell"]
    assert st["task_status"] == "need_more" and not st["passed"]


def test_override_needs_reason_is_logged_and_only_counts_for_that_submission(coach, fake, conn, mid):
    coach.submit_quiz(mid, answers(coach.gate_state(mid)))
    fake.messages.by_agent["grader"] = [grading("pass", "fail", "pass")]
    assert coach.submit_task(mid, "done", [png()])["task_status"] == "failed"
    with pytest.raises(ValueError):
        coach.override_item(mid, "r2", "")

    coach.override_item(mid, "r2", "The height is in screenshot 1, the grader misread it")

    assert store.get_milestone(conn, mid)["status"] == "done"
    prog = coach.milestone_progress(mid)
    assert prog["overrides"][0]["reason"].startswith("The height")
    assert prog["overrides"][0]["old_verdict"] == "fail" and prog["overrides"][0]["new_verdict"] == "pass"


def test_too_many_screenshots_refused(coach, mid, monkeypatch):
    monkeypatch.setattr(config, "IMAGE_MAX_COUNT", 1)
    with pytest.raises(ValueError, match="at most 1"):
        coach.submit_task(mid, "done", [png(), png()])


def test_grader_error_is_logged_and_counts_as_ungradable(coach, fake, conn, mid):
    from llm import LLMError
    fake.messages.by_agent["grader"] = [LLMError("cut off")]
    with pytest.raises(LLMError):
        coach.submit_task(mid, "done", [png()])
    sub = store.submissions(conn, mid)[-1]
    assert sub["error"] == "cut off" and sub["n_images"] == 1


# ---------- old data ----------

def test_prepare_exam_tags_old_quiz_and_writes_task(coach, fake, conn, mid):
    old_qs = [{k: v for k, v in q.items() if k not in ("concept", "section")}
              for q in store.get_quiz(conn, mid)["questions"]]
    store.save_quiz(conn, mid, old_qs, [], 0, False)
    conn.execute("DELETE FROM tasks")
    conn.execute("DELETE FROM question_bank")
    conn.commit()
    assert coach.gate_state(mid)["needs_prepare"]

    fake.messages.by_agent["concept_tagger"] = [examiner.ConceptTags(tags=[
        examiner.ConceptTag(id=q["id"], concept="Old Concept", section="## Walls") for q in old_qs])]
    coach.prepare_exam(mid)

    st = coach.gate_state(mid)
    assert not st["needs_prepare"] and st["task"] is not None
    assert {q["concept"] for q in st["questions"]} == {"old concept"}
    assert store.bank_concepts(conn, mid) == ["old concept"]


def test_add_to_review_for_milestones_ticked_before_phase4(coach, conn, mid):
    with pytest.raises(ValueError, match="before Phase 4"):
        coach.add_to_review(mid)
    conn.execute("UPDATE milestones SET status = 'done', pass_method = 'manual_pre_phase4' WHERE id = ?", (mid,))
    conn.commit()
    assert coach.add_to_review(mid) == 2
    assert coach.add_to_review(mid) == 0  # already there
    pid = store.get_milestone(conn, mid)["profile_id"]
    assert len(coach.due_reviews(pid)) == 2  # due now: never tested


# ---------- spaced review ----------

T = datetime(2026, 11, 20, 3, 0, tzinfo=timezone.utc)  # 22:00 on Nov 19 in New York


@pytest.fixture
def two_cards(coach, conn, mid):
    conn.execute("UPDATE milestones SET status = 'done', pass_method = 'manual_pre_phase4' WHERE id = ?", (mid,))
    conn.commit()
    coach.add_to_review(mid)
    return store.get_milestone(conn, mid)["profile_id"]


def review_one(coach, pid, now, right=True):
    item = coach.due_reviews(pid, now)[0]
    answer = item["question"]["answer"] if right else "B"
    return item, coach.answer_review(item["card"]["id"], item["question"]["bank_id"], answer, now)


def test_review_right_is_good_wrong_is_again(coach, conn, two_cards):
    _, res = review_one(coach, two_cards, T, right=True)
    assert res["correct"] and res["rating"] == "good"
    _, res2 = review_one(coach, two_cards, T, right=False)
    assert not res2["correct"] and res2["rating"] == "again"
    log = store.get_review_log(conn, res["log_id"])
    assert log["reviewed_at"] == "2026-11-20T03:00:00+00:00"  # stored in UTC


def test_review_rotates_questions_within_a_concept(coach, conn, two_cards):
    first, _ = review_one(coach, two_cards, T)
    card_id = first["card"]["id"]
    later = T + timedelta(days=30)
    nxt = next(i for i in coach.due_reviews(two_cards, later) if i["card"]["id"] == card_id)
    assert nxt["question"]["bank_id"] != first["question"]["bank_id"]


# 1. Daily limit counts per LOCAL (New York) day, not per UTC day.

def test_daily_limit_resets_at_local_midnight(coach, two_cards, monkeypatch):
    monkeypatch.setattr(config, "REVIEW_DAILY_LIMIT", 1)
    monkeypatch.setattr(config, "TIMEZONE", "America/New_York")
    review_one(coach, two_cards, T, right=False)  # 22:00 NY, Nov 19
    # 04:30 UTC Nov 20 = 23:30 NY Nov 19: same local day -> limit reached
    assert coach.due_reviews(two_cards, T + timedelta(hours=1, minutes=30)) == []
    # 05:30 UTC Nov 20 = 00:30 NY Nov 20: a new local day -> the other card is offered
    assert len(coach.due_reviews(two_cards, T + timedelta(hours=2, minutes=30))) == 1


# 4. Hard/Easy only for the card's most recent review, on the same local day.

def test_adjust_to_easy_reapplies_from_the_state_before(coach, conn, two_cards):
    item, res = review_one(coach, two_cards, T)
    good_due = store.get_card(conn, item["card"]["id"])["due_at"]
    coach.adjust_review(res["log_id"], "easy", T + timedelta(minutes=5))
    easy_due = store.get_card(conn, item["card"]["id"])["due_at"]
    assert easy_due > good_due
    assert store.get_review_log(conn, res["log_id"])["rating"] == "easy"
    coach.adjust_review(res["log_id"], "good", T + timedelta(minutes=6))  # changing back works
    assert store.get_card(conn, item["card"]["id"])["due_at"] == good_due


def test_adjust_refused_on_the_next_local_day(coach, two_cards, monkeypatch):
    monkeypatch.setattr(config, "TIMEZONE", "America/New_York")
    _, res = review_one(coach, two_cards, T)  # 22:00 NY
    with pytest.raises(ValueError, match="day"):
        coach.adjust_review(res["log_id"], "hard", T + timedelta(hours=3))  # 01:00 NY next day


def test_adjust_refused_for_an_older_review_of_the_card(coach, conn, two_cards):
    item, res = review_one(coach, two_cards, T)
    later = T + timedelta(days=30)
    card_id = item["card"]["id"]
    nxt = next(i for i in coach.due_reviews(two_cards, later) if i["card"]["id"] == card_id)
    coach.answer_review(card_id, nxt["question"]["bank_id"], nxt["question"]["answer"], later)
    with pytest.raises(ValueError, match="most recent"):
        coach.adjust_review(res["log_id"], "easy", later)


def test_adjust_refused_for_wrong_answers(coach, two_cards):
    _, res = review_one(coach, two_cards, T, right=False)
    with pytest.raises(ValueError):
        coach.adjust_review(res["log_id"], "easy", T)


# ---------- progress ----------

def test_overview_on_track_uses_plan_estimates_and_deadline(coach, fake, conn, mid):
    pid = store.get_milestone(conn, mid)["profile_id"]
    # 4 milestones x 2 h = 8 h at 6 h/week -> about 9 days left
    today = date(2026, 11, 4)
    ov = coach.subject_overview(pid, today)
    assert ov["hours_total"] == 8 and ov["on_track"] is None  # no deadline yet
    coach.set_deadline(pid, date(2026, 12, 1))
    assert coach.subject_overview(pid, today)["on_track"] is True
    coach.set_deadline(pid, date(2026, 11, 6))
    ov = coach.subject_overview(pid, today)
    assert ov["on_track"] is False and ov["projected_finish"] == date(2026, 11, 13)


def test_logged_time_is_shown_but_not_used_for_on_track(coach, conn, mid):
    pid = store.get_milestone(conn, mid)["profile_id"]
    before = coach.subject_overview(pid, date(2026, 11, 4))
    coach.log_study(mid, 90)
    assert coach.milestone_progress(mid)["study_minutes"] == 90
    assert coach.subject_overview(pid, date(2026, 11, 4)) == before
    with pytest.raises(ValueError):
        coach.log_study(mid, 0)


def test_progress_and_history(coach, fake, conn, mid):
    coach.submit_quiz(mid, answers(coach.gate_state(mid), wrong=2))
    fake.messages.by_agent["grader"] = [grading("pass", "fail", "pass")]
    coach.submit_task(mid, "done", [png()])
    prog = coach.milestone_progress(mid)
    assert prog["quiz_attempts"] == 1 and prog["best_score"] == pytest.approx(0.6)
    assert prog["task_status"] == "failed" and prog["task_submissions"] == 1
    hist = coach.attempt_history(mid)
    assert [h["kind"] for h in hist] == ["quiz", "task"]
    assert len(hist[0]["missed"]) == 2


def test_review_stats(coach, conn, two_cards):
    review_one(coach, two_cards, T, right=False)
    stats = coach.review_stats(two_cards, T)
    assert stats["total_cards"] == 2 and stats["reviewed_today"] == 1
    assert stats["recall_30d"] == 0.0 and stats["weakest"][0]["misses"] == 1
