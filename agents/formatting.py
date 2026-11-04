"""Formatting rules shared by every agent whose text you read (the app renders Markdown + LaTeX).

MATH is for any subject with formulas. CIRCUITS only applies when the subject involves electric
circuits; it is phrased conditionally so other subjects ignore it (nothing is hard-coded per
subject). Netlists are checked by netlist.py before you see them.
"""

MATH = r"""Formatting (the app shows Markdown and renders LaTeX):
- Write maths in LaTeX: inline as $...$, a displayed equation as $$...$$ on its own lines.
  Use LaTeX for symbols, subscripts and fractions ($V_{GS}$, $\frac{R_2}{R_1+R_2}$, $\Omega$),
  not plain text like V_GS or R2/(R1+R2).
- Write a dollar sign that is money as \$ (e.g. \$20), so it isn't read as maths.
"""

CIRCUITS = r"""Circuits (only when the subject involves electric circuits): give each circuit as a
SPICE netlist in a code block tagged spice, so it is unambiguous and can be simulated:
```spice
* comments start with *
V1 in 0 DC 5
R1 in out 1k
R2 out 0 2.2k
```
- One element per line: name, nodes, value. Ground is node 0. No title line.
- Name the nodes you refer to in the text ("the voltage at node out", $V_{out}$).
- SPICE value suffixes: f p n u m k meg. "M" means milli, so write 1meg for 1 MΩ, and
  write u, not µ.
- Dependent sources: E (VCVS) and G (VCCS): name n+ n- nc+ nc- gain. F (CCCS) and H (CCVS):
  name n+ n- Vsense gain, where Vsense is a voltage source carrying the controlling current
  (add a 0 V source in series if it is only there to sense it).
- Diodes: D1 anode cathode DMOD, plus a line .model DMOD D(IS=1e-14). MOSFETs: M1 d g s b NMOD,
  plus .model NMOD NMOS(VTO=1 KP=200u). Give the course's model values (e.g. V_t, k_n) in the
  .model line and in the text.
- An unknown value the learner must find is written in braces, e.g. R2 out 0 {R}.
"""

ALL = MATH + "\n" + CIRCUITS
