"""Checks a SPICE netlist written by an agent, before you ever see it. No API calls.

The agents write circuits as SPICE netlists in ```spice code blocks (see agents/formatting.py),
so you could paste them into a simulator later. This module only checks that a netlist is
complete and well formed; it does not simulate. A later phase can add a schematic drawing or
a simulator check of the answer key using the same parsed elements.

check(text) returns a list of problems ("" lines are fine). An empty list means it's valid.
"""
import re

# Element letter -> (description, number of nodes). Values/models come after the nodes.
ELEMENTS = {
    "R": ("resistor", 2), "C": ("capacitor", 2), "L": ("inductor", 2),
    "V": ("voltage source", 2), "I": ("current source", 2),
    "E": ("voltage-controlled voltage source", 4), "G": ("voltage-controlled current source", 4),
    "F": ("current-controlled current source", 2), "H": ("current-controlled voltage source", 2),
    "D": ("diode", 2), "M": ("MOSFET", 4),
}
MODEL_TYPES = {"D": {"D"}, "M": {"NMOS", "PMOS"}}
DOT_COMMANDS = {".model", ".op", ".dc", ".tran", ".ac", ".ic", ".end", ".options", ".temp"}
SOURCE_FUNCTIONS = {"DC", "AC", "PULSE", "SIN", "PWL", "EXP"}

# A SPICE number: 4.7k, 10meg, 1e-3, 2.2u, .5 ... Letters after the scale (e.g. "1kohm") are
# ignored by SPICE, so they're allowed. A value in braces, e.g. {R}, is an unknown/parameter.
VALUE = re.compile(r"^[+-]?(?:\d+\.?\d*|\.\d+)(?:e[+-]?\d+)?(meg|mil|[fpnumkgt])?[a-z]*$", re.IGNORECASE)
PARAM = re.compile(r"^\{[^{}]+\}$")
NODE = re.compile(r"^[A-Za-z0-9_]+$")


def value_problem(token: str) -> str:
    """'' if token is a valid SPICE value, else what's wrong with it."""
    if PARAM.match(token):
        return ""
    if "µ" in token or "μ" in token:
        return f"'{token}': SPICE doesn't read 'µ'; write 'u' (e.g. 4.7u)"
    m = VALUE.match(token)
    if not m:
        return f"'{token}' is not a SPICE value"
    # SPICE ignores case, so '1M' means 1 milli. Almost always meant as mega: ask for 'meg' or 'm'.
    num_end = len(token) - len(token.lstrip("+-0123456789.eE"))
    if token[num_end:num_end + 1] == "M" and not token[num_end:].lower().startswith("meg"):
        return f"'{token}': 'M' means milli in SPICE; write '{token[:num_end]}meg' for mega or '{token[:num_end]}m' for milli"
    return ""


def logical_lines(text: str) -> list[tuple[int, str]]:
    """(line number, text) with comments removed and '+' continuation lines joined."""
    out = []
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.split(";")[0].strip()
        if not line or line.startswith("*"):
            continue
        if line.startswith("+") and out:
            out[-1] = (out[-1][0], out[-1][1] + " " + line[1:].strip())
        else:
            out.append((n, line))
    return out


def parse(text: str) -> tuple[list[dict], dict, list[str]]:
    """Returns (elements, models {name: type}, problems)."""
    elements, models, problems = [], {}, []
    lines = logical_lines(text)
    first = lines[0][0] if lines else 0
    # A title like "Voltage divider" starts with V and looks like a broken voltage source.
    title_hint = " (if this is a title line, remove it or start it with *)"
    for n, line in lines:
        # "PULSE(0 5 0 1n)" -> keep function arguments with the function name
        tokens = re.findall(r"[A-Za-z]+\([^)]*\)|\S+", line)
        head = tokens[0]
        if head.startswith("."):
            cmd = head.lower()
            if cmd not in DOT_COMMANDS:
                problems.append(f"line {n}: '{head}' is not supported here (allowed: {', '.join(sorted(DOT_COMMANDS))})")
            elif cmd == ".model":
                m = re.match(r"^\.model\s+(\S+)\s+([A-Za-z]+)", line, re.IGNORECASE)
                if not m:
                    problems.append(f"line {n}: write .model NAME TYPE(params)")
                else:
                    models[m.group(1).lower()] = m.group(2).upper()
            continue
        letter = head[0].upper()
        if letter not in ELEMENTS:
            problems.append(f"line {n}: '{head}' is not a supported element "
                            f"(names must start with one of {''.join(ELEMENTS)}; no title line)")
            continue
        kind, n_nodes = ELEMENTS[letter]
        nodes, rest = tokens[1:1 + n_nodes], tokens[1 + n_nodes:]
        if len(nodes) < n_nodes or not rest:
            problems.append(f"line {n}: {head} ({kind}) needs {n_nodes} nodes and then "
                            f"{'a model name' if letter in MODEL_TYPES else 'a value'}"
                            + (title_hint if n == first else ""))
            continue
        bad_nodes = [x for x in nodes if not NODE.match(x)]
        if bad_nodes:
            problems.append(f"line {n}: {head} has invalid node name(s) {', '.join(bad_nodes)}"
                            + (title_hint if n == first else ""))
            continue
        elements.append({"name": head, "letter": letter, "nodes": nodes, "rest": rest, "line": n})
    return elements, models, problems


def check_element(e: dict, sources: set[str], models: dict) -> list[str]:
    n, head, letter, rest = e["line"], e["name"], e["letter"], e["rest"]
    if letter in "RCLEG":
        p = value_problem(rest[0])
        return [f"line {n}: {head}: {p}"] if p else []
    if letter in "VI":
        first = rest[0].split("(")[0].upper()
        if first in SOURCE_FUNCTIONS:
            if first == "DC":
                if len(rest) < 2:
                    return [f"line {n}: {head}: DC needs a value"]
                p = value_problem(rest[1])
                return [f"line {n}: {head}: {p}"] if p else []
            return []
        p = value_problem(rest[0])
        return [f"line {n}: {head}: {p}"] if p else []
    if letter in "FH":
        out = []
        if rest[0].lower() not in sources:
            out.append(f"line {n}: {head} is controlled by the current in '{rest[0]}', but there is no "
                       f"voltage source with that name (add a 0 V source in series to sense the current)")
        if len(rest) < 2:
            out.append(f"line {n}: {head} needs a gain after '{rest[0]}'")
        elif value_problem(rest[1]):
            out.append(f"line {n}: {head}: {value_problem(rest[1])}")
        return out
    # D and M: a model name that a .model line defines with the right type
    model = rest[0].lower()
    if model not in models:
        return [f"line {n}: {head} uses model '{rest[0]}', but there is no '.model {rest[0]} ...' line"]
    if models[model] not in MODEL_TYPES[letter]:
        want = " or ".join(sorted(MODEL_TYPES[letter]))
        return [f"line {n}: {head} needs a {want} model, but '{rest[0]}' is {models[model]}"]
    return []


def check(text: str) -> list[str]:
    """All problems in one netlist. [] means it's complete and well formed."""
    elements, models, problems = parse(text)
    if not elements:
        return problems + ["the netlist has no elements"]
    seen = {}
    for e in elements:
        key = e["name"].lower()
        if key in seen:
            problems.append(f"line {e['line']}: element name {e['name']} is used twice (also line {seen[key]})")
        seen.setdefault(key, e["line"])
    sources = {e["name"].lower() for e in elements if e["letter"] == "V"}
    for e in elements:
        problems += check_element(e, sources, models)
    # Nodes that carry current: control nodes of E and G only sense a voltage.
    nodes = {x for e in elements for x in (e["nodes"][:2] if e["letter"] in "EG" else e["nodes"])}
    if "0" not in nodes:
        problems.append("there is no ground: one node must be 0")
    return problems
