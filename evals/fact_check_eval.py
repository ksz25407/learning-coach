"""Fact-checker eval: does it catch the errors planted in known lessons, without false alarms?

Uses the REAL API (costs money). Run by hand from the project folder:

    python evals/fact_check_eval.py --model opus      # 10 tuning lessons (default model: opus)
    python evals/fact_check_eval.py --model sonnet    # compare with Sonnet
    python evals/fact_check_eval.py --holdout         # 3 held-out lessons; NOT for tuning
    python evals/fact_check_eval.py --list-errors     # rewrite evals/PLANTED_ERRORS.md (free)

Scoring (after the app's quote rule: a flag whose quote isn't in the lesson counts as minor):
    caught       a planted error with a MAJOR flag whose quote overlaps the planted quote, or
                 one of its "also_matches" lines (e.g. the other moved steps of a step-order error)
    missed       a planted error without such a flag ("as minor" if only a minor flag hit it)
    false alarm  a MAJOR flag that matches no planted error (clean lessons included)

Before calling the API it estimates the worst-case cost (input tokens counted with the API,
output = MAX_TOKENS) and stops if this month's eval spend + that would exceed
EVAL_MONTHLY_CAP_USD. Spend is logged to the app's usage_log as agent "fact_checker_eval".
"""
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import checks  # noqa: E402
import config  # noqa: E402
from agents import fact_checker  # noqa: E402
from db import store  # noqa: E402
from llm import LLM, LLMError, RefusalError, price_for, system_blocks  # noqa: E402
from sources.base import SourceChunk  # noqa: E402

EVAL_DIR = ROOT / "evals"
FIXTURES = EVAL_DIR / "fixtures"
HOLDOUT = EVAL_DIR / "holdout"
RESULTS = EVAL_DIR / "results"
ERRORS_MD = EVAL_DIR / "PLANTED_ERRORS.md"

AGENT = "fact_checker_eval"
MODELS = {"opus": config.OPUS, "sonnet": config.SONNET}
ERROR_KINDS = ("wrong_value", "wrong_name", "step_order", "subtle_claim")

PROFILES = {
    "revit": {"subject": "Revit", "goal": "Model a small off-grid house with a friend", "level": "beginner",
              "hours_per_week": 5, "deadline": "none", "practice_type": "software",
              "proof_of_skill": "file", "notes": "Revit 2026, metric units (mm)"},
    "circuits": {"subject": "Circuit analysis", "goal": "Pass the first-year circuit analysis exam",
                 "level": "some experience (EE student)", "hours_per_week": 4, "deadline": "none",
                 "practice_type": "math", "proof_of_skill": "solved_problem", "notes": "SI units"},
}


# ---------- fixtures ----------

def load_fixtures(holdout: bool = False) -> list[dict]:
    """The tuning set by default; ONLY the held-out set with holdout=True."""
    folder = HOLDOUT if holdout else FIXTURES
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(folder.glob("*.json"))]


def lesson_text(fx: dict) -> str:
    return "\n".join(fx["lesson_lines"])


def chunks_of(fx: dict) -> list[SourceChunk]:
    return [SourceChunk(**s) for s in fx["sources"]]


def fixture_problems(fx: dict) -> list[str]:
    """A planted quote must be in the lesson, or the eval could never count it as caught."""
    text = lesson_text(fx)
    out = [f"{fx['id']}: planted quote not in lesson: {q!r}"
           for e in fx["planted_errors"] for q in error_quotes(e) if not checks.quote_found(q, text)]
    out += [f"{fx['id']}: unknown error kind {e['kind']!r}" for e in fx["planted_errors"]
            if e["kind"] not in ERROR_KINDS]
    return out


# ---------- scoring ----------

def overlaps(a: str, b: str) -> bool:
    na, nb = checks.norm_text(a).strip(" .\"'"), checks.norm_text(b).strip(" .\"'")
    return min(len(na), len(nb)) >= checks.MIN_QUOTE_CHARS and (na in nb or nb in na)


def error_quotes(e: dict) -> list[str]:
    """The planted quote plus any other lesson lines that belong to the same error."""
    return [e["quote"], *e.get("also_matches", [])]


def hits_error(flag: dict, e: dict) -> bool:
    return any(overlaps(flag["quote"], q) for q in error_quotes(e))


def score(fx: dict, flags: list[dict]) -> dict:
    flags = checks.apply_quote_rule(flags, lesson_text(fx))
    majors = [f for f in flags if f["severity"] == "major"]
    minors = [f for f in flags if f["severity"] == "minor"]
    caught, missed, used = [], [], set()
    for e in fx["planted_errors"]:
        hits = [i for i, f in enumerate(majors) if hits_error(f, e)]
        if hits:
            caught.append(e)
            used.update(hits)
        else:
            as_minor = any(hits_error(f, e) for f in minors)
            missed.append({**e, "flagged_as_minor": as_minor})
    false_alarms = [f for i, f in enumerate(majors) if i not in used]
    return {"id": fx["id"], "planted": len(fx["planted_errors"]), "caught": caught, "missed": missed,
            "false_alarms": false_alarms, "minor_flags": minors}


# ---------- cost ----------

def setup_agent(model: str):
    """Register the eval agent: same settings as the app's Fact-checker, chosen model."""
    config.MODELS[AGENT] = model
    config.EFFORT[AGENT] = config.EFFORT["fact_checker"]
    config.MAX_TOKENS[AGENT] = config.MAX_TOKENS["fact_checker"]


def worst_case_cost(llm, fixtures: list[dict], model: str) -> float:
    """Input counted by the API (priced as a cache write, the dearest input) + MAX_TOKENS output."""
    p = price_for(model)
    total = 0.0
    for fx in fixtures:
        count = llm.client.messages.count_tokens(
            model=model, system=system_blocks(fact_checker.SYSTEM, PROFILES[fx["subject"]]),
            messages=fact_checker.build_messages(lesson_text(fx), chunks_of(fx)))
        total += count.input_tokens * p["cache_write"] + config.MAX_TOKENS[AGENT] * p["output"]
    return total / 1e6


# ---------- PLANTED_ERRORS.md ----------

def planted_errors_md() -> str:
    lines = ["# Planted errors in the Fact-checker eval", "",
             "Generated by `python evals/fact_check_eval.py --list-errors`. Do not edit by hand:",
             "edit the JSON files, then regenerate.", ""]
    for title, holdout in (("Tuning set (evals/fixtures/)", False),
                           ("Held-out set (evals/holdout/): never use for tuning", True)):
        lines += [f"## {title}", ""]
        for fx in load_fixtures(holdout):
            lines.append(f"### {fx['id']} ({fx['subject']}): {fx['milestone']['objective']}")
            lines.append("")
            if not fx["planted_errors"]:
                lines += ["Clean lesson: no planted errors (measures false alarms).", ""]
                continue
            lines += ["| Kind | Planted text | Correct version | Why |", "|---|---|---|---|"]
            for e in fx["planted_errors"]:
                planted = e["quote"]
                if e.get("also_matches"):
                    planted += " (also counts: " + " / ".join(e["also_matches"]) + ")"
                cells = [e["kind"], planted, e["correct"], e["why"]]
                lines.append("| " + " | ".join(c.replace("|", "\\|").replace("**", "") for c in cells) + " |")
            lines.append("")
    return "\n".join(lines)


# ---------- main ----------

def print_table(rows: list[dict], title: str):
    print(f"\n{title}")
    print(f"{'lesson':30} {'planted':>7} {'caught':>6} {'missed':>6} {'false alarms':>12}")
    for r in rows:
        if "error" in r:
            print(f"{r['id']:30} ERROR: {r['error']}")
            continue
        print(f"{r['id']:30} {r['planted']:>7} {len(r['caught']):>6} {len(r['missed']):>6} "
              f"{len(r['false_alarms']):>12}")
    for r in rows:
        for m in r.get("missed", []):
            minor = " (flagged only as minor)" if m["flagged_as_minor"] else ""
            print(f"  MISSED {r['id']}: [{m['kind']}] {m['quote']}{minor}")
        for f in r.get("false_alarms", []):
            print(f"  FALSE ALARM {r['id']}: \"{f['quote']}\": {f['problem']}")


def totals(rows: list[dict]) -> dict:
    ok = [r for r in rows if "error" not in r]
    return {"lessons": len(rows), "failed_calls": len(rows) - len(ok),
            "planted": sum(r["planted"] for r in ok), "caught": sum(len(r["caught"]) for r in ok),
            "missed": sum(len(r["missed"]) for r in ok),
            "false_alarms": sum(len(r["false_alarms"]) for r in ok),
            "false_alarms_on_clean_lessons": sum(len(r["false_alarms"]) for r in ok if r["planted"] == 0)}


def main(argv=None, conn=None, client=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # Ω and ∥ on Windows consoles
    except AttributeError:
        pass
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--model", choices=MODELS, default="opus")
    ap.add_argument("--holdout", action="store_true", help="run ONLY the held-out lessons")
    ap.add_argument("--list-errors", action="store_true", help="rewrite PLANTED_ERRORS.md and exit")
    args = ap.parse_args(argv)

    if args.list_errors:
        ERRORS_MD.write_text(planted_errors_md(), encoding="utf-8")
        print(f"Wrote {ERRORS_MD}")
        return 0

    fixtures = load_fixtures(args.holdout)
    problems = [p for fx in fixtures for p in fixture_problems(fx)]
    if problems:
        print("Fixture problems:\n" + "\n".join(problems))
        return 2

    model = MODELS[args.model]
    setup_agent(model)
    conn = conn or store.connect(config.DB_PATH)
    llm = LLM(conn, client=client)

    estimate = worst_case_cost(llm, fixtures, model)
    spent = store.month_spend(conn, agent=AGENT)
    print(f"Eval spend this month: ${spent:.2f}. This run, worst case: ${estimate:.2f}. "
          f"Cap: ${config.EVAL_MONTHLY_CAP_USD:.2f}.")
    if spent + estimate > config.EVAL_MONTHLY_CAP_USD:
        print("Stopped before calling the API: this run could go over EVAL_MONTHLY_CAP_USD "
              "(config.py).")
        return 1

    first_id = conn.execute("SELECT COALESCE(MAX(id), 0) FROM usage_log").fetchone()[0]
    rows = []
    for fx in fixtures:
        print(f"Checking {fx['id']}...", flush=True)
        try:
            flags = fact_checker.check(llm, PROFILES[fx["subject"]], lesson_text(fx), chunks_of(fx),
                                       agent=AGENT)
            rows.append(score(fx, flags))
        except (LLMError, RefusalError) as e:
            rows.append({"id": fx["id"], "planted": len(fx["planted_errors"]), "error": str(e)})

    used = conn.execute("SELECT COALESCE(SUM(cost_usd), 0), GROUP_CONCAT(DISTINCT model) FROM usage_log"
                        " WHERE id > ? AND agent = ?", (first_id, AGENT)).fetchone()
    set_name = "held-out" if args.holdout else "tuning"
    print_table(rows, f"Fact-checker eval, {set_name} set, {model}")
    t = totals(rows)
    print(f"\nTOTAL: caught {t['caught']}/{t['planted']}, missed {t['missed']}, "
          f"false alarms {t['false_alarms']} ({t['false_alarms_on_clean_lessons']} on clean lessons). "
          f"Cost ${used[0]:.4f}.")

    RESULTS.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = RESULTS / f"{stamp}-{args.model}{'-holdout' if args.holdout else ''}.json"
    out.write_text(json.dumps({
        "timestamp": stamp, "set": set_name, "model_requested": model, "models_answered": used[1],
        "effort": config.EFFORT[AGENT], "cost_usd": round(used[0], 4), "totals": t, "lessons": rows,
    }, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Saved {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
