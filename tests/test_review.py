"""Phase 4 plain-Python rules: quiz scoring, task status, evidence rule, FSRS wrapper."""
from datetime import datetime, timedelta, timezone

import pytest

import checks
import clock
import config
import review

# ---------- quiz ----------

QS = [
    {"id": "q1", "kind": "multiple_choice", "choices": ["Wall", "Floor", "Roof"], "answer": "A"},
    {"id": "q2", "kind": "numeric", "answer": "2200", "unit": "Ω", "tolerance_pct": 1},
    {"id": "q3", "kind": "exact_term", "answer": "Core Centerline", "alternates": []},
    {"id": "q4", "kind": "exact_term", "answer": "Level", "alternates": []},
    {"id": "q5", "kind": "exact_term", "answer": "Grid", "alternates": []},
]


def test_grade_quiz_uses_the_answer_key_rules():
    score, results = checks.grade_quiz(QS, {"q1": "a", "q2": "2.2 kΩ", "q3": "core centerline",
                                            "q4": "Level", "q5": ""})
    assert score == pytest.approx(0.8)
    assert [r["correct"] for r in results] == [True, True, True, True, False]


def test_pass_mark_is_80_percent():
    assert checks.quiz_passed(0.8)
    assert not checks.quiz_passed(0.79)
    assert not checks.quiz_passed(None)  # no questions = no quiz score


def test_empty_quiz_has_no_score():
    assert checks.grade_quiz([], {}) == (None, [])


# ---------- practical task ----------

RUBRIC = [{"id": "r1"}, {"id": "r2"}, {"id": "r3"}]


def g(item, verdict, evidence="wall tags visible on grid A"):
    return {"id": item, "verdict": verdict, "reason": "r", "evidence": evidence, "need": ""}


def test_task_needs_every_item_to_pass():
    assert checks.task_status(RUBRIC, [g("r1", "pass"), g("r2", "pass"), g("r3", "pass")]) == "passed"
    assert checks.task_status(RUBRIC, [g("r1", "pass"), g("r2", "fail"), g("r3", "pass")]) == "failed"


def test_cant_tell_asks_for_more_instead_of_failing():
    assert checks.task_status(RUBRIC, [g("r1", "pass"), g("r2", "cant_tell"), g("r3", "pass")]) == "need_more"
    assert checks.task_status(RUBRIC, [g("r1", "pass"), g("r2", "pass")]) == "need_more"  # r3 not graded
    assert checks.task_status(RUBRIC, None) == "need_more"


def test_override_turns_a_fail_into_a_pass():
    grades = [g("r1", "pass"), g("r2", "fail"), g("r3", "pass")]
    overrides = [{"item_id": "r2", "old_verdict": "fail", "new_verdict": "pass", "reason": "visible in 3D"}]
    eff = checks.effective_grades(grades, overrides)
    assert checks.task_status(RUBRIC, eff) == "passed"
    assert eff[1]["override"]["reason"] == "visible in 3D"


# 3. A "pass" must name specific visible evidence.

def test_pass_without_evidence_becomes_cant_tell():
    out = checks.apply_evidence_rule([g("r1", "pass", evidence=""), g("r2", "pass", evidence="  "),
                                      g("r3", "pass")])
    assert [x["verdict"] for x in out] == ["cant_tell", "cant_tell", "pass"]
    assert out[0]["no_evidence"] and out[0]["need"]
    assert "no_evidence" not in out[2]


def test_evidence_rule_leaves_fail_and_cant_tell_alone():
    out = checks.apply_evidence_rule([g("r1", "fail", evidence=""), g("r2", "cant_tell", evidence="")])
    assert [x["verdict"] for x in out] == ["fail", "cant_tell"]


# ---------- FSRS wrapper ----------

NOW = datetime(2026, 11, 4, 15, 0, tzinfo=timezone.utc)


def test_new_card_is_due_now_and_stored_in_utc():
    card, due = review.new_card()
    assert due.endswith("+00:00")
    assert clock.from_iso(due) <= clock.utc_now()


def test_again_is_due_sooner_than_good():
    card, _ = review.new_card()
    _, due_again = review.apply(card, "again", NOW)
    _, due_good = review.apply(card, "good", NOW)
    assert NOW < clock.from_iso(due_again) < clock.from_iso(due_good)
    assert due_good.endswith("+00:00")


def test_correct_answers_space_reviews_further_apart():
    card, _ = review.new_card()
    t, gaps = NOW, []
    for _ in range(4):
        card, due = review.apply(card, "good", t)
        gaps.append(clock.from_iso(due) - t)
        t = clock.from_iso(due)
    assert gaps == sorted(gaps) and gaps[-1] > timedelta(days=1)


def test_pick_question_least_recently_shown():
    qs = [{"bank_id": 1, "last_shown_at": "2026-11-03T10:00:00+00:00"},
          {"bank_id": 2, "last_shown_at": None},
          {"bank_id": 3, "last_shown_at": "2026-11-01T10:00:00+00:00"}]
    assert review.pick_question(qs)["bank_id"] == 2  # never shown first
    qs[1]["last_shown_at"] = "2026-11-04T10:00:00+00:00"
    assert review.pick_question(qs)["bank_id"] == 3
    assert review.pick_question([]) is None


def test_daily_limit():
    due = list(range(20))
    assert len(review.todays_cards(due, reviewed_today=0, limit=15)) == 15
    assert len(review.todays_cards(due, reviewed_today=12, limit=15)) == 3
    assert review.todays_cards(due, reviewed_today=15, limit=15) == []


# 4. Hard/Easy only for the most recent review of the card, on the same local day.

def log(id_=1, correct=True, source="review", when=NOW):
    return {"id": id_, "correct": int(correct), "source": source, "reviewed_at": clock.to_iso(when)}


def test_adjust_allowed_same_day_latest_review():
    ok, _ = review.can_adjust(log(), log(), NOW + timedelta(hours=2))
    assert ok


def test_adjust_refused_for_older_review():
    ok, why = review.can_adjust(log(id_=1), log(id_=2), NOW)
    assert not ok and "most recent" in why


def test_adjust_refused_next_local_day(monkeypatch):
    monkeypatch.setattr(config, "TIMEZONE", "America/New_York")
    evening = datetime(2026, 11, 5, 3, 0, tzinfo=timezone.utc)  # 22:00 Nov 4 in New York
    ok, _ = review.can_adjust(log(when=evening), log(when=evening), evening + timedelta(hours=1, minutes=30))
    assert ok  # 23:30 New York: same local day, although UTC already moved to Nov 5 earlier
    ok, why = review.can_adjust(log(when=evening), log(when=evening), evening + timedelta(hours=2, minutes=30))
    assert not ok and "day" in why  # 00:30 New York, Nov 5


def test_adjust_refused_for_wrong_answers_and_exam_logs():
    assert not review.can_adjust(log(correct=False), log(correct=False), NOW)[0]
    assert not review.can_adjust(log(source="exam"), log(source="exam"), NOW)[0]
