"""Phase 5: the SPICE netlist check (netlist.py) and the fenced-block hook in checks.py."""
import pytest

import checks
import netlist

DIVIDER = """* voltage divider
V1 in 0 DC 5
R1 in out 1k
R2 out 0 2.2k
"""

DEPENDENT = """V1 in 0 10
R1 in a 2k
Vsense a b 0
R2 b 0 4k
F1 0 c Vsense 3
H1 d 0 Vsense 500
E1 e 0 a 0 2
G1 0 f a b 1m
R3 c 0 1k
R4 d 0 1k
R5 e 0 1k
R6 f 0 1k
"""

CLAMP = """V1 in 0 SIN(0 5 1k)
C1 in out 1u
D1 0 out DMOD
R1 out 0 100k
.model DMOD D(IS=1e-14)
.tran 10u 5m
"""

MOSFET = """VDD dd 0 DC 5
VG g 0 DC 2
RD dd d 10k
M1 d g 0 0 NMOD L=1u W=10u
+ AD=0
.model NMOD NMOS(VTO=1 KP=200u)
.op
"""


@pytest.mark.parametrize("text", [DIVIDER, DEPENDENT, CLAMP, MOSFET])
def test_valid_netlists(text):
    assert netlist.check(text) == []


def test_unknown_value_in_braces_is_allowed():
    assert netlist.check("V1 in 0 5\nR1 in out {R}\nR2 out 0 1k") == []


@pytest.mark.parametrize("text,expect", [
    ("R1 a 0 1k\nR2 a b 1M\nR3 b 0 1k", "'M' means milli"),
    ("R1 a 0 4.7µ", "write 'u'"),
    ("R1 a 0 big", "not a SPICE value"),
    ("R1 a b 1k\nR2 b a 2k", "no ground"),
    ("V1 a 0 5\nR1 a", "needs 2 nodes"),
    ("Voltage divider\nV1 a 0 5\nR1 a 0 1k", "if this is a title line"),  # 'V' looks like a source
    ("Bridge circuit\nV1 a 0 5\nR1 a 0 1k", "no title line"),  # 'B' is no element at all
    ("V1 a 0 5\nR1 a 0 1k\nF1 0 b Vx 2\nR2 b 0 1k", "no voltage source with that name"),
    ("V1 a 0 5\nR1 a 0 1k\nH1 b 0 R1 2\nR2 b 0 1k", "no voltage source with that name"),
    ("V1 a 0 5\nD1 a 0 DX", "no '.model DX"),
    ("V1 a 0 5\nM1 a a 0 0 DX\n.model DX D", "needs a NMOS or PMOS model"),
    ("V1 a 0 5\nR1 a 0 1k\nR1 a 0 2k", "used twice"),
    (".subckt foo a b\nR1 a 0 1k", "'.subckt' is not supported"),
    ("* only a comment", "no elements"),
])
def test_problems_are_reported(text, expect):
    problems = netlist.check(text)
    assert any(expect in p for p in problems), problems


def test_1meg_and_lowercase_m_are_fine():
    assert netlist.check("V1 a 0 5\nR1 a b 1meg\nR2 b 0 1m\nR3 b 0 1Meg") == []


def test_control_nodes_of_e_and_g_dont_count_as_ground():
    # the only 0 is a control node of E1: nothing carries current to ground
    problems = netlist.check("V1 a b 5\nR1 a b 1k\nE1 c d a 0 2\nR2 c d 1k")
    assert any("no ground" in p for p in problems)


def test_problem_lines_point_at_the_netlist_line():
    problems = netlist.check("V1 a 0 5\n\n* note\nR1 a 0 1M")
    assert problems[0].startswith("line 4:")


# ---------- fenced blocks ----------

def fenced(body, tag="spice"):
    return f"Find $V_{{out}}$.\n\n```{tag}\n{body}```\n\nMore text."


def test_block_problems_only_checks_spice_blocks():
    assert checks.block_problems(fenced(DIVIDER)) == []
    assert checks.block_problems(fenced("this is not spice", tag="text")) == []
    assert checks.block_problems("no blocks at all") == []
    got = checks.block_problems(fenced("R1 a b 1M\n"))
    assert got and got[0]["tag"] == "spice" and any("'M' means milli" in g["problem"] for g in got)


def test_block_flags_are_major_and_quote_the_bad_line():
    lesson = "## Divider\n[Source: model]\n" + fenced("V1 in 0 5\nR1 in out 1M\nR2 out 0 1k\n")
    flags = checks.block_flags(lesson)
    assert len(flags) == 1
    f = flags[0]
    assert f["severity"] == "major" and f["checker"] == "block" and f["quote"] == "R1 in out 1M"
    assert "Netlist check" in checks.feedback_text(flags)


def test_block_flags_ignore_the_sources_list():
    lesson = "## A\nText." + checks.SOURCES_LIST + "\n" + fenced("R1 a b 1M\n")
    assert checks.block_flags(lesson) == []
