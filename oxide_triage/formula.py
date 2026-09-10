"""Minimal chemical-formula parsing for stoichiometry matching across databases.

Different databases write the same compound differently (``Y3Al5O12`` vs ``Al5Y3O12``,
``HfO2`` vs ``Hf1O2``). Acquisition routes that search by chemical system need to recognise
the same stoichiometry regardless of spelling. No pymatgen dependency.
"""

from __future__ import annotations

import re
from math import gcd

_TOKEN = re.compile(r"([A-Z][a-z]?)(\d*\.?\d*)")


def parse_formula(formula: str) -> dict[str, float]:
    """``"Y3Al5O12"`` -> ``{"Y": 3, "Al": 5, "O": 12}``. Parentheses are expanded one level."""
    s = formula.replace(" ", "")

    def expand(m: re.Match[str]) -> str:
        inner, mult = m.group(1), float(m.group(2) or 1)
        return "".join(f"{el}{(float(n) if n else 1.0) * mult:g}" for el, n in _TOKEN.findall(inner))

    s = re.sub(r"\(([^()]*)\)(\d*\.?\d*)", expand, s)
    out: dict[str, float] = {}
    pos = 0
    for m in _TOKEN.finditer(s):
        if m.start() != pos:
            raise ValueError(f"cannot parse formula {formula!r}")
        pos = m.end()
        el, n = m.group(1), m.group(2)
        out[el] = out.get(el, 0.0) + (float(n) if n else 1.0)
    if pos != len(s) or not out:
        raise ValueError(f"cannot parse formula {formula!r}")
    return out


def reduced(composition: dict[str, float]) -> dict[str, int]:
    """Integer composition divided by the gcd. Non-integer inputs are scaled to integers first."""
    scale = 1
    for v in composition.values():
        while abs(v * scale - round(v * scale)) > 1e-6 and scale < 1000:
            scale *= 10
    ints = {k: int(round(v * scale)) for k, v in composition.items()}
    g = 0
    for v in ints.values():
        g = gcd(g, v)
    g = g or 1
    return {k: v // g for k, v in ints.items()}


def same_stoichiometry(a: str, b: str) -> bool:
    try:
        return reduced(parse_formula(a)) == reduced(parse_formula(b))
    except ValueError:
        return False


def elements_of(formula: str) -> list[str]:
    return sorted(parse_formula(formula))
