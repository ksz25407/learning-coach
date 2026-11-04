"""Plain-Python checker rules (checks.py): no API calls."""
import pytest

import checks
import config

LESSON = ("## Walls\n[Source: web]\nUse the **Wall** tool (shortcut WA)[1]. Set the "
          "Location Line to “Core Centerline”[2].\n\n## Practice\n[Source: model]\nDraw four walls."
          "\n\n---\n**Sources**\n\n- **[1]** web · x: “quote”")


# ---------- quote rule ----------

@pytest.mark.parametrize("quote", [
    "Use the Wall tool (shortcut WA).",       # emphasis and [1] marker ignored
    "set the location line to \"Core Centerline\"",  # case and curly quotes ignored
    "Draw   four walls",                        # extra spaces ignored
])
def test_quote_found(quote):
    assert checks.quote_found(quote, LESSON)


@pytest.mark.parametrize("quote", ["Use the Door tool", "", "WA", "the Sources list says x"])
def test_quote_not_found(quote):
    assert not checks.quote_found(quote, LESSON)


def test_unquoted_major_flag_becomes_minor():
    flags = [{"checker": "fact", "quote": "Draw four walls", "severity": "major"},
             {"checker": "fact", "quote": "Walls are made of cheese", "severity": "major"},
             {"checker": "pedagogy", "quote": "nope", "severity": "minor"}]
    out = checks.apply_quote_rule(flags, LESSON)
    assert [f["severity"] for f in out] == ["major", "minor", "minor"]
    assert out[1]["downgraded"] and not out[1]["quote_found"]
    assert flags[1]["severity"] == "major"  # input not changed


def test_plain_lesson_strips_app_additions():
    plain = checks.plain_lesson(LESSON)
    assert "[Source:" not in plain and "[1]" not in plain and "Sources" not in plain
    assert "Use the **Wall** tool (shortcut WA)." in plain


# ---------- length ----------

def test_reading_minutes_ignores_sources_list():
    text = "## A\n" + "word " * 400 + "\n\n---\n**Sources**\n\n" + "src " * 1000
    assert checks.reading_minutes(text) == pytest.approx(402 / config.READING_WPM)  # "##", "A" + 400


@pytest.mark.parametrize("hours,expected", [(0.5, 7.5), (1, 15), (2, 20), (None, 20)])
def test_length_budget(hours, expected):
    assert checks.length_budget(hours) == expected


# ---------- status ----------

def test_lesson_status():
    assert checks.lesson_status(True, ["pdf, web", "web"]) == "verified"
    assert checks.lesson_status(True, ["web", "model"]) == "partly_unsourced"
    assert checks.lesson_status(True, []) == "partly_unsourced"  # no sections = no source check
    assert checks.lesson_status(False, ["web"]) == "flagged"


# ---------- quiz answers ----------

def q(kind, answer, **kw):
    return {"kind": kind, "answer": answer, "choices": kw.get("choices", []), "unit": kw.get("unit", ""),
            "tolerance_pct": kw.get("tol", 0), "alternates": kw.get("alternates", [])}


@pytest.mark.parametrize("given,ok", [
    ("Core Centerline", True), ("core centerline.", True), ("CORE-CENTERLINE", True),
    ("core centreline", True),  # listed alternate
    ("Wall Centerline", False), ("", False),
])
def test_exact_term(given, ok):
    question = q("exact_term", "Core Centerline", alternates=["Core Centreline"])
    assert checks.answers_match(question, given) is ok


@pytest.mark.parametrize("given,ok", [
    ("2.2 kΩ", True), ("2200 Ω", True), ("2200", True), ("2.2k", True), ("2,200 ohms", True),
    ("2.2 kohm", True), ("2.2 kΩ", True),  # the separate Ohm sign
    ("2.22 kΩ", True),   # inside 1 %
    ("2.25 kΩ", False),  # outside 1 %
    ("2.2 kV", False),   # wrong unit
    ("about 2k", False), ("2.2 MΩ", False),
])
def test_numeric_with_units(given, ok):
    assert checks.answers_match(q("numeric", "2200", unit="Ω", tol=1), given) is ok


def test_numeric_tolerance_edge_is_inclusive():
    question = q("numeric", "100", unit="V", tol=5)
    assert checks.answers_match(question, "105 V")
    assert not checks.answers_match(question, "105.1 V")


def test_numeric_lengths_and_default_tolerance():
    assert checks.answers_match(q("numeric", "300", unit="mm"), "0.3 m")
    assert checks.answers_match(q("numeric", "300", unit="mm"), "30 cm")
    assert not checks.answers_match(q("numeric", "300", unit="mm"), "300 cm")
    assert checks.answers_match(q("numeric", "3.048", unit="m"), "10 ft")
    assert checks.answers_match(q("numeric", "4.7", unit="µF"), "4.7 uF")
    assert checks.answers_match(q("numeric", "1", unit="ms"), "1000 µs")
    # no tolerance given -> QUIZ_DEFAULT_TOLERANCE_PCT (1 %)
    assert checks.answers_match(q("numeric", "10", unit="V"), "10.09 V")


@pytest.mark.parametrize("given,ok", [
    ("B", True), ("b", True), ("(B)", True), ("B. Floor", True), ("b)", True), ("Floor", True),
    ("A", False), ("A wall", False), ("D", False), ("AMBIGUOUS", False),
])
def test_multiple_choice(given, ok):
    assert checks.answers_match(q("multiple_choice", "B", choices=["Wall", "Floor", "Roof"]), given) is ok
