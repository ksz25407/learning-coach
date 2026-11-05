"""The Fact-checker eval script, offline (fake client, temp database)."""
import json
import sys
from pathlib import Path

import pytest

import config

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "evals"))
import fact_check_eval as ev  # noqa: E402

from tests.conftest import fact_flags  # noqa: E402


@pytest.fixture(autouse=True)
def restore_config():
    """main() registers the eval agent in config; undo that after each test."""
    saved = {d: dict(getattr(config, d)) for d in ("MODELS", "EFFORT", "MAX_TOKENS")}
    yield
    for d, v in saved.items():
        getattr(config, d).clear()
        getattr(config, d).update(v)


# ---------- fixtures ----------

def test_tuning_set_shape():
    fixtures = ev.load_fixtures()
    assert len(fixtures) == 12  # Phase 5: + diode clamper, MOSFET regions (netlists, LaTeX)
    assert sum(1 for fx in fixtures if not fx["planted_errors"]) == 3
    kinds = {e["kind"] for fx in fixtures for e in fx["planted_errors"]}
    assert kinds == set(ev.ERROR_KINDS)  # all four kinds of planted error
    assert {fx["subject"] for fx in fixtures} == {"revit", "circuits"}


def test_holdout_set_shape_and_separation():
    holdout = ev.load_fixtures(holdout=True)
    assert len(holdout) == 3
    assert sum(1 for fx in holdout if not fx["planted_errors"]) == 1
    assert not {fx["id"] for fx in holdout} & {fx["id"] for fx in ev.load_fixtures()}  # never mixed


@pytest.mark.parametrize("holdout", [False, True])
def test_every_planted_quote_is_in_its_lesson(holdout):
    for fx in ev.load_fixtures(holdout):
        assert ev.fixture_problems(fx) == []


def test_planted_errors_md_is_up_to_date():
    assert ev.ERRORS_MD.read_text(encoding="utf-8") == ev.planted_errors_md()


# ---------- scoring ----------

def flag(quote, severity="major"):
    return {"checker": "fact", "quote": quote, "problem": "p", "evidence": "e", "kind": "source",
            "severity": severity}


def test_score_caught_missed_false_alarm():
    fx = next(f for f in ev.load_fixtures() if f["id"] == "circuits_voltage_divider")
    result = ev.score(fx, [
        flag("Vout = 12 × 5 / 15 = 6 V."),                     # caught (overlapping quote)
        flag("Connecting it raises Vout slightly", "minor"),     # only minor -> missed
        flag("Two resistors in series across Vin."),             # false alarm
        flag("A sentence that is not in the lesson"),           # unquoted -> minor, not a false alarm
    ])
    assert [e["quote"] for e in result["caught"]] == ["Vout = 12 × 5 / 15 = 6 V"]
    assert [m["flagged_as_minor"] for m in result["missed"]] == [True]
    assert [f["quote"] for f in result["false_alarms"]] == ["Two resistors in series across Vin."]


def test_step_order_flag_on_another_moved_step_counts_as_caught():
    """Sonnet flagged the floor lesson's step 5 ('the list never ends with Finish Edit Mode'):
    that is the planted step-order error, not a false alarm."""
    fx = next(f for f in ev.load_fixtures() if f["id"] == "revit_floors")
    result = ev.score(fx, [flag("5. On the Draw panel, use **Pick Walls** and click each exterior wall.")])
    assert len(result["caught"]) == 1 and result["false_alarms"] == []
    # ...but a flag on an unrelated line still is one.
    result = ev.score(fx, [flag("1. Open the **Level 1** floor plan.")])
    assert result["caught"] == [] and len(result["false_alarms"]) == 1


def test_partial_quote_counts_as_caught():
    fx = next(f for f in ev.load_fixtures() if f["id"] == "revit_walls")
    result = ev.score(fx, [flag("shortcut WL"), flag("Draw the outside walls counter-clockwise")])
    assert len(result["caught"]) == 2 and result["false_alarms"] == []


# ---------- main(): cost cap and a full offline run ----------

def test_cost_cap_stops_before_any_check(conn, fake, capsys):
    # The fake count_tokens says 999,999 input tokens -> far above the $3 cap.
    assert ev.main(["--model", "opus"], conn=conn, client=fake) == 1
    assert [kind for kind, _ in fake.messages.calls] == ["count_tokens"] * len(ev.load_fixtures())  # no parse() call
    assert "Stopped before calling the API" in capsys.readouterr().out


def test_cap_counts_earlier_eval_spend(conn, fake, monkeypatch):
    fake.messages.count_tokens = lambda **kw: type("C", (), {"input_tokens": 1000})()
    store_ = ev.store
    store_.log_usage(conn, None, ev.AGENT, "m", 1, 1, 0, 0, config.EVAL_MONTHLY_CAP_USD - 0.01)
    assert ev.main(["--model", "sonnet"], conn=conn, client=fake) == 1
    assert fake.messages.calls == []


def test_full_offline_run_writes_results(conn, fake, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(config, "EVAL_MONTHLY_CAP_USD", 1e9)
    monkeypatch.setattr(ev, "RESULTS", tmp_path)
    fake.messages.defaults["fact_checker"] = fact_flags()  # finds nothing -> every error missed
    assert ev.main(["--model", "sonnet", "--holdout"], conn=conn, client=fake) == 0

    checks = [c for kind, c in fake.messages.calls if kind == "parse"]
    assert len(checks) == 3 and all(c["model"] == config.SONNET for c in checks)
    [out] = list(tmp_path.glob("*-sonnet-holdout.json"))
    result = json.loads(out.read_text(encoding="utf-8"))
    assert result["set"] == "held-out" and result["model_requested"] == config.SONNET
    assert result["totals"]["planted"] == 4 and result["totals"]["missed"] == 4
    assert conn.execute("SELECT COUNT(*) FROM usage_log WHERE agent = ?", (ev.AGENT,)).fetchone()[0] == 3
    assert "TOTAL: caught 0/4" in capsys.readouterr().out
