"""Phase 5 step 5: grading photos of handwritten worked solutions. No real API calls.

- phone photos: turned upright (EXIF rotation), HEIC converted, big photos shrunk
- the Grader reads your final value off the page; Python decides final-value items with the
  tolerance (like a quiz answer) and never states the correct value
- the Grader is told how to handle handwriting: quote lines, never guess illegible parts
"""
import io

import pytest
from PIL import Image

import checks
import config
from agents import grader
from agents.grader import Grading, ItemGrade
from db import store
from orchestrator import Coach
from tests.conftest import agent_of, pass_all_checks
from tests.test_orchestrator import queue_lesson, scope_to_learning
from tests.test_solved_task import SOLVED, as_dict, solution, solved_draft

RUBRIC = as_dict(solved_draft(vth="9", rth="6"))["rubric"]  # r1 V_th 9 V ±2%, r2 R_th 6 kΩ ±2%, r3-r4 methods


# ---------- images ----------

def jpeg(w=40, h=20, orientation=None, mode="RGB") -> bytes:
    img = Image.new(mode, (w, h), "white")
    out = io.BytesIO()
    if orientation:
        exif = Image.Exif()
        exif[0x0112] = orientation
        img.save(out, format="JPEG", exif=exif.tobytes())
    else:
        img.save(out, format="JPEG")
    return out.getvalue()


def heic(w=40, h=20) -> bytes:
    from pillow_heif import register_heif_opener
    register_heif_opener()
    out = io.BytesIO()
    Image.new("RGB", (w, h), "white").save(out, format="HEIF")
    return out.getvalue()


def size_of(data: bytes):
    return Image.open(io.BytesIO(data)).size


def test_rotated_phone_photo_is_turned_upright():
    media_type, data = grader.prepare_image(jpeg(40, 20, orientation=6))  # 6 = "rotate 90° to view"
    assert media_type == "image/jpeg" and size_of(data) == (20, 40)
    assert Image.open(io.BytesIO(data)).getexif().get(0x0112, 1) in (0, 1)  # no rotation left to apply


def test_upright_small_jpeg_is_sent_unchanged():
    data = jpeg()
    assert grader.prepare_image(data) == ("image/jpeg", data)


def test_heic_photo_becomes_jpeg():
    media_type, data = grader.prepare_image(heic())
    assert media_type == "image/jpeg" and Image.open(io.BytesIO(data)).format == "JPEG"
    assert size_of(data) == (40, 20)


def test_big_photo_is_shrunk_and_rotated(monkeypatch):
    monkeypatch.setattr(config, "IMAGE_MAX_PX", 30)
    _, data = grader.prepare_image(jpeg(60, 40, orientation=6))
    assert size_of(data) == (20, 30)


# ---------- the value rule ----------

def g(item_id, verdict="pass", value="", evidence="Image 1, last line"):
    return {"id": item_id, "verdict": verdict, "evidence": evidence, "reason": "grader's reason",
            "need": "", "learner_value": value}


@pytest.mark.parametrize("written,grader_says,verdict", [
    ("9 V", "pass", "pass"),
    ("9.15 V", "fail", "pass"),     # within 2%: Python overrides the Grader
    ("9000 mV", "pass", "pass"),
    ("8.5 V", "pass", "fail"),      # outside 2%: Python overrides the Grader
    ("-9 V", "pass", "fail"),       # sign error
    ("9 A", "pass", "fail"),        # wrong unit
])
def test_value_items_are_decided_by_python(written, grader_says, verdict):
    out = checks.apply_value_rule([g("r1", grader_says, written)], RUBRIC)[0]
    assert out["verdict"] == verdict and out["checked_by"] == "app"
    assert "9 V" not in out["reason"].replace(written, "")  # never states the correct value


def test_unreadable_or_missing_values():
    garbled = checks.apply_value_rule([g("r1", "pass", "nine-ish")], RUBRIC)[0]
    assert garbled["verdict"] == "cant_tell" and "V_th" in garbled["need"]
    missing = checks.apply_value_rule([g("r1", "pass", "")], RUBRIC)[0]
    assert missing["verdict"] == "cant_tell" and "State your final value of V_th" in missing["need"]
    not_given = checks.apply_value_rule([g("r1", "fail", "")], RUBRIC)[0]
    assert not_given["verdict"] == "fail" and "checked_by" not in not_given  # Grader's call stays


def test_method_items_are_left_to_the_grader():
    grades = [g("r3", "fail"), g("r4", "pass")]
    assert checks.apply_value_rule(grades, RUBRIC) == grades


def test_old_rubrics_without_kinds_are_untouched():
    old = [{"id": "r1", "criterion": "c", "evidence": "e"}]
    grades = [g("r1", "pass", "9 V")]
    assert checks.apply_value_rule(grades, old) == grades


# ---------- the Grader request ----------

def test_grader_prompt_covers_handwriting():
    for phrase in ("Handwritten working", "copy the relevant line(s) as written",
                   "Never guess what illegible writing says", "learner_value", "different valid approach"):
        assert phrase in grader.SYSTEM


def grades_reply(*items):
    return Grading(grades=[ItemGrade(**{"evidence": "Image 1, line 6", "reason": "r", "need": "",
                                       "learner_value": "", **i}) for i in items])


CORRECT = grades_reply({"id": "r1", "verdict": "pass", "learner_value": "9 V"},
                       {"id": "r2", "verdict": "pass", "learner_value": "6 kΩ"},
                       {"id": "r3", "verdict": "pass"}, {"id": "r4", "verdict": "pass"})
# The planted error: a sign mistake in KCL. The Grader fails the method item; the wrong final
# value is failed by Python even though the Grader (wrongly) said pass.
SIGN_ERROR = grades_reply({"id": "r1", "verdict": "pass", "learner_value": "-3 V"},
                          {"id": "r2", "verdict": "pass", "learner_value": "6 kΩ"},
                          {"id": "r3", "verdict": "fail", "reason": "KCL at a has the wrong sign for I_R2"},
                          {"id": "r4", "verdict": "pass"})


@pytest.fixture
def coach(conn, llm, fake):
    pass_all_checks(fake)
    fake.messages.defaults["examiner_task"] = solved_draft(vth="9", rth="6")
    fake.messages.defaults["task_solver"] = solution(vth="9", rth="6")
    return Coach(conn, llm)


@pytest.fixture
def mid(coach, fake, conn):
    pid = scope_to_learning(coach, fake)
    store.save_profile_data(conn, pid, SOLVED)
    m = store.get_milestones(conn, pid)[0]["id"]
    queue_lesson(fake)
    coach.request_lesson(m)
    return m


def test_correct_handwritten_solution_passes(coach, fake, conn, mid):
    fake.messages.by_agent["grader"] = [CORRECT]
    st = coach.submit_task(mid, "", [heic(), jpeg(orientation=6)])
    assert st["task_status"] == "passed"
    call = [c for _, c in fake.messages.calls if agent_of(c) == "grader"][-1]
    images = [b for b in call["messages"][0]["content"] if b["type"] == "image"]
    assert [i["source"]["media_type"] for i in images] == ["image/jpeg", "image/jpeg"]


def test_sign_error_fails_the_right_items(coach, fake, conn, mid):
    fake.messages.by_agent["grader"] = [SIGN_ERROR]
    st = coach.submit_task(mid, "", [jpeg()])
    verdicts = {x["id"]: x["verdict"] for x in st["grades"]}
    assert st["task_status"] == "failed"
    assert verdicts == {"r1": "fail", "r2": "pass", "r3": "fail", "r4": "pass"}
    r1 = next(x for x in st["grades"] if x["id"] == "r1")
    assert r1["checked_by"] == "app" and "9" not in r1["reason"].replace("-3", "")


# ---------- cost and UI ----------

def test_grading_estimate_counts_full_size_photos(coach, conn):
    pid = store.create_profile(conn)
    p = config.PRICES[config.MODELS["grader"]]
    images_in = config.EST_GRADER_IMAGES * config.EST_IMAGE_TOKENS
    assert coach._formula_costs(pid)["grader"] >= images_in * p["input"] / 1e6
    assert config.EST_IMAGE_TOKENS >= (2576 // 28) * (1932 // 28)  # a 4:3 photo at the size limit


def test_ui_accepts_heic_and_marks_app_checked_values():
    app = (config.ROOT / "ui" / "app.py").read_text(encoding="utf-8")
    assert '"heic", "heif"' in app and "value checked by the app" in app
