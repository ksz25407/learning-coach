"""Plain-Python rules used by the checker pipeline (no API calls here).

- quote rule: a checker flag must quote the lesson exactly, or it only counts as "minor"
- reading time and the length budget for the Pedagogy reviewer
- comparing the Quiz solver's answers with the Examiner's answer key
- the lesson status shown in the UI
- checking fenced code blocks (e.g. ```spice netlists) in anything an agent writes
"""
import re
import unicodedata

import config
import netlist

# ---------- text normalisation ----------

CITATION_MARK = re.compile(r"\[\d+\]")
SOURCE_LABEL = re.compile(r"^\s*\[Source:[^\]]*\]\s*$", re.IGNORECASE | re.MULTILINE)
SOURCES_LIST = "\n\n---\n**Sources**"  # the Teacher appends this; see agents/teacher.py

QUOTE_CHARS = {"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-", " ": " "}


def norm_text(s: str) -> str:
    """Lower-case, drop citation markers [1], Markdown emphasis and LaTeX $ delimiters,
    collapse spaces. (So a checker quoting 'V = IR' matches the lesson's '$V = IR$'.)"""
    s = unicodedata.normalize("NFKC", s or "")
    for a, b in QUOTE_CHARS.items():
        s = s.replace(a, b)
    s = CITATION_MARK.sub("", s)
    s = re.sub(r"[*_`$]", "", s)
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


CHECKER_NAMES = {"fact": "Fact-checker", "pedagogy": "Pedagogy reviewer", "block": "Netlist check"}


def checker_name(flag: dict) -> str:
    return CHECKER_NAMES.get(flag["checker"], flag["checker"])


def feedback_text(flags: list[dict]) -> str:
    """The major flags, written out for the Teacher's revision."""
    lines = []
    for i, f in enumerate(flags, 1):
        who = checker_name(f)
        lines.append(f"{i}. ({who}) Lesson text: \"{f['quote']}\"\n   Problem: {f['problem']}\n"
                     f"   Evidence: {f['evidence']}")
    return "\n".join(lines)


# ---------- fenced code blocks (netlists) ----------

# One checker per code-block tag. It gets the block's text and returns a list of problems.
# Add a tag here to check another notation; a later phase can add a simulator check too.
BLOCK_CHECKERS = {"spice": netlist.check}
FENCED_BLOCK = re.compile(r"^[ \t]*```[ \t]*([\w+-]*)[ \t]*\n(.*?)^[ \t]*```", re.MULTILINE | re.DOTALL)


def block_problems(text: str) -> list[dict]:
    """Problems in checked code blocks: [{"tag", "block" (its text), "problem"}]."""
    out = []
    for m in FENCED_BLOCK.finditer(text or ""):
        check = BLOCK_CHECKERS.get(m.group(1).lower())
        if check:
            out += [{"tag": m.group(1).lower(), "block": m.group(2), "problem": p} for p in check(m.group(2))]
    return out


def block_flags(lesson_text: str) -> list[dict]:
    """Netlist problems in a lesson, as major flags in the shared flag shape (see orchestrator).
    The quote is the offending line, so the Teacher knows where to look."""
    flags = []
    for b in block_problems(lesson_body(lesson_text)):
        m = re.match(r"line (\d+):", b["problem"])
        lines = b["block"].splitlines() or [""]
        n = int(m.group(1)) if m else 1
        quote = lines[min(n, len(lines)) - 1].strip()
        flags.append({"checker": "block", "quote": quote, "problem": f"{b['tag']} netlist: {b['problem']}",
                      "evidence": "Found by the app's netlist check (Python), not by a model.",
                      "kind": "netlist", "severity": "major", "quote_found": True})
    return flags


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


# Units. A unit is read as base units with powers plus a scale factor, so compound units
# work too: 'µA/V²' -> ({A: 1, V: -2}, 1e-6). Two answers match only if their base units are
# the same. Any SI prefix works on any base unit; inches and feet become metres.
SI_PREFIX = {"p": 1e-12, "n": 1e-9, "µ": 1e-6, "μ": 1e-6, "u": 1e-6, "m": 1e-3, "c": 1e-2,
             "k": 1e3, "K": 1e3, "M": 1e6, "G": 1e9}
BASE_UNITS = {"Ω", "V", "A", "W", "F", "H", "Hz", "s", "m", "g", "J", "C", "S", "N", "Pa", "VA", "Wh"}
LENGTH_UNITS = {"in": 0.0254, "inch": 0.0254, "inches": 0.0254, '"': 0.0254, "″": 0.0254,
                "ft": 0.3048, "foot": 0.3048, "feet": 0.3048, "'": 0.3048, "′": 0.3048}
NUMBER = re.compile(r"^\s*([-+]?(?:\d[\d,]*\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)\s*(.*?)\s*$")
SUPERSCRIPTS = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹⁻", "0123456789-")
UNIT_TERM = re.compile(r"^([^\d^\-]+)\^?(-?\d+)?$")  # 'mA', 'V^2', 'V2', 's^-1'
MINUS_SIGNS = {"−": "-", "–": "-"}  # Unicode minus and en dash, as models sometimes write them


def _clean_unit(u: str) -> str:
    u = re.sub(r"(?i)ohms?\b", "Ω", u.strip())
    u = u.replace("Ω", "Ω")  # the Ohm sign -> Greek capital omega
    return {"hz": "Hz", "HZ": "Hz", "sec": "s", "volts": "V", "volt": "V", "amps": "A",
            "amp": "A", "watts": "W", "watt": "W"}.get(u, u)


def _unit_term(term: str) -> tuple[dict, float] | None:
    """One factor of a unit, e.g. 'mA' or 'V^2' -> ({base: power}, scale)."""
    m = UNIT_TERM.match(term)
    if not m:
        return None
    sym, power = m.group(1), int(m.group(2) or 1)
    if sym in LENGTH_UNITS:
        base, scale = "m", LENGTH_UNITS[sym]
    elif sym in BASE_UNITS:  # exact match first: 'm' is metres, 'Pa' is pascals
        base, scale = sym, 1.0
    elif sym[0] in SI_PREFIX and sym[1:] in BASE_UNITS:
        base, scale = sym[1:], SI_PREFIX[sym[0]]
    else:
        return None
    return {base: power}, scale ** power


def parse_unit(unit: str) -> tuple[dict, float] | None:
    """'µA/V²' -> ({'A': 1, 'V': -2}, 1e-6). '' -> ({}, 1.0). None if it can't be read."""
    u = _clean_unit(unit).translate(SUPERSCRIPTS)
    if u == "":
        return {}, 1.0
    if u.count("/") > 1:
        return None
    dims, scale = {}, 1.0
    num, _, den = u.partition("/")
    for side, sign in ((num, 1), (den, -1)):
        for term in re.split(r"[·*⋅ ]+", side.strip()):
            if term in ("", "1"):  # '1/s'
                continue
            got = _unit_term(term)
            if got is None:
                return None
            for base, power in got[0].items():
                dims[base] = dims.get(base, 0) + sign * power
            scale *= got[1] ** sign
    return {b: p for b, p in dims.items() if p != 0}, scale


def unit_factor(unit: str, expected_unit: str) -> tuple[tuple, float] | None:
    """(base units, factor to base) for `unit`, or None if it can't be read. Base units are a
    sorted tuple of (base, power), so 'V/V' and no unit are both ().
    No unit is read as the expected unit. A bare prefix ('2.2k') is that prefix on the
    expected unit; a bare 'm' means milli unless metres are expected ('5m' for 5 mΩ)."""
    expected = parse_unit(expected_unit)
    u = _clean_unit(unit)
    if u == "":
        got = expected
    elif u in SI_PREFIX and expected and expected[0] and not (u == "m" and expected[0] == {"m": 1}):
        got = expected[0], SI_PREFIX[u] * expected[1]
    else:
        got = parse_unit(u)
    if got is None:
        return None
    return tuple(sorted(got[0].items())), got[1]


def parse_quantity(text: str, expected_unit: str) -> tuple[float, tuple] | None:
    """'2.2 kΩ' -> (2200.0, (('Ω', 1),)). None if it isn't a number (+ optional unit)."""
    text = text or ""
    for a, b in MINUS_SIGNS.items():
        text = text.replace(a, b)
    m = NUMBER.match(text)
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


def _match_choice(question: dict, given: str) -> bool:
    key = mc_letter(question["answer"], question.get("choices", []))
    return key is not None and mc_letter(given, question.get("choices", [])) == key


def _match_numeric(question: dict, given: str) -> bool:
    tol = question.get("tolerance_pct") or config.QUIZ_DEFAULT_TOLERANCE_PCT
    return numbers_match(question["answer"], given, question.get("unit", ""), tol)


def _match_term(question: dict, given: str) -> bool:
    accepted = [question["answer"], *question.get("alternates", [])]
    return norm_term(given) != "" and norm_term(given) in {norm_term(a) for a in accepted}


# One marking rule per question kind. To add a kind later (e.g. "phasor" for AC answers like
# '5∠30° V'), write its rule here and add the kind to the Examiner's Question schema.
ANSWER_RULES = {
    "multiple_choice": _match_choice,
    "numeric": _match_numeric,
    "exact_term": _match_term,
}


def answers_match(question: dict, given: str) -> bool:
    """Does the given answer agree with the answer key? Unknown kinds never match."""
    rule = ANSWER_RULES.get(question["kind"])
    return rule is not None and rule(question, given)


# ---------- Phase 5: verifying a solved problem's answer key ----------

def _quantity_key(name: str) -> str:
    """'V_{th}', 'Vth', '$V_th$' -> 'vth'."""
    return re.sub(r"[\s_{}$\\]", "", name or "").lower()


def compare_key(rubric: list[dict], solution: dict) -> list[dict]:
    """Compare each final-value rubric item with the blind solver's value for it.
    Returns [{"id", "quantity", "key", "solver", "match"}] ("solver" is None if it gave none)."""
    given = {_quantity_key(q): v for q, v in solution.get("values", {}).items()}
    out = []
    for r in rubric:
        if r.get("kind") != "value":
            continue
        key = f"{r['value']} {r['unit']}".strip()
        got = given.get(_quantity_key(r["quantity"]))
        tol = r.get("tolerance_pct") or config.TASK_DEFAULT_TOLERANCE_PCT
        out.append({"id": r["id"], "quantity": r["quantity"], "key": key, "solver": got,
                    "match": got is not None and numbers_match(key, got, r["unit"], tol)})
    return out


def key_problems(comparison: list[dict], solution: dict) -> list[str]:
    """What to tell the Examiner when the blind solver doesn't confirm the key ([] = verified).
    It isn't told who is right: the key, the problem statement, or both may be wrong."""
    problems = []
    if solution.get("ambiguous"):
        problems.append(f"a blind solver found the problem ambiguous or incomplete: {solution.get('note') or '(no reason)'}")
    for c in comparison:
        if c["solver"] is None:
            problems.append(f"a blind solver could not give a value for {c['quantity']}; make sure the task asks "
                            f"for it by that name and gives everything needed")
        elif not c["match"]:
            problems.append(f"a blind solver got {c['quantity']} = {c['solver']}, but your key says {c['key']}; "
                            f"recheck the calculation and that the problem states every value and assumption")
    return problems


# ---------- Phase 4: exam gate ----------

def grade_quiz(questions: list[dict], answers: dict[str, str]) -> tuple[float | None, list[dict]]:
    """Mark your answers ({question id: answer}). Returns (score 0-1, one result per question).
    Score is None for a quiz with no questions."""
    results = []
    for q in questions:
        given = (answers.get(q["id"]) or "").strip()
        results.append({"id": q["id"], "given": given, "correct": bool(given) and answers_match(q, given)})
    if not questions:
        return None, results
    return sum(r["correct"] for r in results) / len(questions), results


def quiz_passed(score: float | None) -> bool:
    return score is not None and score >= config.QUIZ_PASS_PCT - 1e-9


VERDICTS = ("pass", "fail", "cant_tell")


def apply_evidence_rule(grades: list[dict]) -> list[dict]:
    """A "pass" must name specific visible evidence. A pass without it becomes "cant_tell"."""
    out = []
    for g in grades:
        g = dict(g)
        if g["verdict"] == "pass" and len((g.get("evidence") or "").strip()) < 3:
            g["verdict"] = "cant_tell"
            g["reason"] = f"No specific evidence named, so this can't count as a pass. {g.get('reason', '')}".strip()
            g["need"] = g.get("need") or "Show this item clearly in an image or in your text."
            g["no_evidence"] = True
        out.append(g)
    return out


def apply_value_rule(grades: list[dict], rubric: list[dict]) -> list[dict]:
    """Final-value items are decided by Python, like quiz answers: the Grader only reads your
    final value off the page (learner_value), and the tolerance check is done here.
    - value read and parsable: pass if within the allowed error, else fail;
    - value written but not a number with a readable unit: cant_tell, asking you to write it clearly;
    - no value read: the Grader's verdict stays, except that a "pass" becomes cant_tell.
    The reason never states the correct value (you may resubmit). Marked checked_by="app"."""
    items = {r["id"]: r for r in rubric}
    out = []
    for g in grades:
        r = items.get(g["id"])
        if not r or r.get("kind") != "value":
            out.append(g)
            continue
        g = dict(g)
        written = (g.get("learner_value") or "").strip()
        tol = r.get("tolerance_pct") or config.TASK_DEFAULT_TOLERANCE_PCT
        key = f"{r['value']} {r['unit']}".strip()
        if written and parse_quantity(written, r["unit"]) is not None:
            ok = numbers_match(key, written, r["unit"], tol)
            g.update(verdict="pass" if ok else "fail", checked_by="app", need="",
                     evidence=g.get("evidence") or f"final value written: {written}",
                     reason=(f"Your final value ({written}) is within the allowed {tol:g}%." if ok else
                             f"Your final value ({written}) is not within {tol:g}% of the correct value."))
        elif written:
            g.update(verdict="cant_tell", checked_by="app",
                     reason=f"Couldn't read '{written}' as a number with a unit.",
                     need=f"Write your final value of {r['quantity']} clearly as a number and unit (e.g. 6.4 V).")
        elif g["verdict"] == "pass":
            g.update(verdict="cant_tell", checked_by="app",
                     reason="The grader found no final value for this quantity.",
                     need=f"State your final value of {r['quantity']} clearly, with its unit.")
        out.append(g)
    return out


def effective_grades(grades: list[dict], overrides: list[dict]) -> list[dict]:
    """The grader's verdicts with your overrides applied (the newest override per item wins)."""
    latest = {}
    for o in overrides:  # oldest first
        latest[o["item_id"]] = o
    out = []
    for g in grades:
        o = latest.get(g["id"])
        out.append({**g, "verdict": o["new_verdict"], "override": o} if o else dict(g))
    return out


def task_status(rubric: list[dict], grades: list[dict] | None) -> str:
    """passed: every rubric item passes. failed: at least one fails.
    need_more: no fails, but some item is "cant_tell" or wasn't graded."""
    if not grades:
        return "need_more"
    by_id = {g["id"]: g["verdict"] for g in grades}
    verdicts = [by_id.get(item["id"], "cant_tell") for item in rubric]
    if "fail" in verdicts:
        return "failed"
    if all(v == "pass" for v in verdicts):
        return "passed"
    return "need_more"
