"""Plain-Python rules used by the checker pipeline (no API calls here).

- quote rule: a checker flag must quote the lesson exactly, or it only counts as "minor"
- reading time and the length budget for the Pedagogy reviewer
- comparing the Quiz solver's answers with the Examiner's answer key
- the lesson status shown in the UI
"""
import re
import unicodedata

import config

# ---------- text normalisation ----------

CITATION_MARK = re.compile(r"\[\d+\]")
SOURCE_LABEL = re.compile(r"^\s*\[Source:[^\]]*\]\s*$", re.IGNORECASE | re.MULTILINE)
SOURCES_LIST = "\n\n---\n**Sources**"  # the Teacher appends this; see agents/teacher.py

QUOTE_CHARS = {"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-", " ": " "}


def norm_text(s: str) -> str:
    """Lower-case, drop citation markers [1] and Markdown emphasis, collapse spaces."""
    s = unicodedata.normalize("NFKC", s or "")
    for a, b in QUOTE_CHARS.items():
        s = s.replace(a, b)
    s = CITATION_MARK.sub("", s)
    s = re.sub(r"[*_`]", "", s)
    return " ".join(s.lower().split())


def lesson_body(text: str) -> str:
    """The lesson without the Sources list at the end."""
    return text.split(SOURCES_LIST)[0]


def plain_lesson(text: str) -> str:
    """The lesson as the learner reads it, minus the app's additions: no Sources list,
    no [Source: ...] labels, no [n] citation markers. Used for revisions and quiz agents."""
    body = SOURCE_LABEL.sub("", lesson_body(text))
    body = CITATION_MARK.sub("", body)
    return re.sub(r"\n{3,}", "\n\n", body).strip()


# ---------- the quote rule ----------

MIN_QUOTE_CHARS = 4


def quote_found(quote: str, lesson_text: str) -> bool:
    """True if the quote appears in the lesson (ignoring case, spacing, emphasis, [n] markers)."""
    q = norm_text(quote).strip(" .\"'")
    return len(q) >= MIN_QUOTE_CHARS and q in norm_text(lesson_text)


def apply_quote_rule(flags: list[dict], lesson_text: str) -> list[dict]:
    """Mark each flag with quote_found; a flag whose quote isn't in the lesson becomes minor."""
    out = []
    for f in flags:
        found = quote_found(f.get("quote", ""), lesson_text)
        f = {**f, "quote_found": found}
        if not found and f["severity"] == "major":
            f["severity"] = "minor"
            f["downgraded"] = True
        out.append(f)
    return out


def feedback_text(flags: list[dict]) -> str:
    """The major flags, written out for the Teacher's revision."""
    lines = []
    for i, f in enumerate(flags, 1):
        who = "Fact-checker" if f["checker"] == "fact" else "Pedagogy reviewer"
        lines.append(f"{i}. ({who}) Lesson text: \"{f['quote']}\"\n   Problem: {f['problem']}\n"
                     f"   Evidence: {f['evidence']}")
    return "\n".join(lines)


# ---------- length ----------

def reading_minutes(text: str) -> float:
    return len(plain_lesson(text).split()) / config.READING_WPM


def length_budget(est_hours: float | None) -> float:
    """Minutes of reading the lesson may take."""
    if not est_hours:
        return float(config.READING_CAP_MIN)
    return min(est_hours * 60 * config.READING_SHARE, config.READING_CAP_MIN)


# ---------- lesson status ----------

def lesson_status(passed: bool, section_labels: list[str]) -> str:
    """verified: passed and every section is source-backed.
    partly_unsourced: passed, but some sections (or the whole lesson) come from model knowledge.
    flagged: still has major problems after the revision rounds."""
    if not passed:
        return "flagged"
    if not section_labels or any("model" in label for label in section_labels):
        return "partly_unsourced"
    return "verified"


# ---------- quiz answers ----------

def norm_term(s: str) -> str:
    """'Core  Centerline.' -> 'corecenterline' (case, spaces and punctuation ignored)."""
    s = unicodedata.normalize("NFKC", s or "").lower()
    return "".join(ch for ch in s if ch.isalnum())


def letter(i: int) -> str:
    return chr(ord("A") + i)


def mc_letter(given: str, choices: list[str]) -> str | None:
    """The letter the solver picked: 'B', 'b)', '(B)', 'B. Wall tool' or the choice text itself."""
    g = (given or "").strip()
    m = re.match(r"^\(?([A-Za-z])\s*(?:[).:\-]|$)", g)
    if m and ord(m.group(1).upper()) - ord("A") < max(len(choices), 1):
        return m.group(1).upper()
    for i, c in enumerate(choices):
        if norm_term(g) == norm_term(c):
            return letter(i)
    return None


# Units: SI prefixes on any base unit below, plus a few length units for Revit.
SI_PREFIX = {"p": 1e-12, "n": 1e-9, "µ": 1e-6, "μ": 1e-6, "u": 1e-6, "m": 1e-3, "c": 1e-2,
             "k": 1e3, "K": 1e3, "M": 1e6, "G": 1e9}
BASE_UNITS = {"Ω", "V", "A", "W", "F", "H", "Hz", "s", "m", "g", "J", "C", "S", "N", "Pa", "VA", "Wh"}
LENGTH_UNITS = {"in": 0.0254, "inch": 0.0254, "inches": 0.0254, '"': 0.0254, "″": 0.0254,
                "ft": 0.3048, "foot": 0.3048, "feet": 0.3048, "'": 0.3048, "′": 0.3048}
NUMBER = re.compile(r"^\s*([-+]?(?:\d[\d,]*\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)\s*(.*?)\s*$")


def _clean_unit(u: str) -> str:
    u = re.sub(r"(?i)ohms?\b", "Ω", u.strip())
    u = u.replace("Ω", "Ω")  # the Ohm sign -> Greek capital omega
    return {"hz": "Hz", "HZ": "Hz", "sec": "s", "volts": "V", "volt": "V", "amps": "A",
            "amp": "A", "watts": "W", "watt": "W"}.get(u, u)


def unit_factor(unit: str, expected_unit: str) -> tuple[str, float] | None:
    """(base unit, factor to base) for `unit`, or None if it can't be read.
    A bare prefix ('2.2k') or no unit at all is read as the expected unit."""
    u = _clean_unit(unit)
    if u == "":
        u = _clean_unit(expected_unit)
        if u == "":
            return "", 1.0
    if u in LENGTH_UNITS:
        return "m", LENGTH_UNITS[u]
    if u in BASE_UNITS:
        return u, 1.0
    if u in SI_PREFIX:  # '2.2k' with expected 'Ω'
        base = unit_factor(expected_unit, "")
        return (base[0], SI_PREFIX[u] * base[1]) if base else None
    if u[0] in SI_PREFIX and u[1:] in BASE_UNITS:
        return u[1:], SI_PREFIX[u[0]]
    return None


def parse_quantity(text: str, expected_unit: str) -> tuple[float, str] | None:
    """'2.2 kΩ' -> (2200.0, 'Ω'). None if it isn't a number (+ optional unit)."""
    m = NUMBER.match(text or "")
    if not m:
        return None
    try:
        value = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    unit = unit_factor(m.group(2), expected_unit)
    if unit is None:
        return None
    return value * unit[1], unit[0]


def numbers_match(key: str, given: str, unit: str, tolerance_pct: float) -> bool:
    k, g = parse_quantity(key, unit), parse_quantity(given, unit)
    if k is None or g is None or k[1] != g[1]:
        return False
    tol = abs(k[0]) * (tolerance_pct or 0) / 100
    return abs(k[0] - g[0]) <= tol + 1e-9 * max(abs(k[0]), 1e-30)


def answers_match(question: dict, given: str) -> bool:
    """Does the solver's answer agree with the answer key?"""
    kind = question["kind"]
    if kind == "multiple_choice":
        key = mc_letter(question["answer"], question.get("choices", []))
        return key is not None and mc_letter(given, question.get("choices", [])) == key
    if kind == "numeric":
        tol = question.get("tolerance_pct") or config.QUIZ_DEFAULT_TOLERANCE_PCT
        return numbers_match(question["answer"], given, question.get("unit", ""), tol)
    if kind == "exact_term":
        accepted = [question["answer"], *question.get("alternates", [])]
        return norm_term(given) != "" and norm_term(given) in {norm_term(a) for a in accepted}
    return False
