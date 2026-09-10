"""Interface thermodynamics from public hull data, without pymatgen.

The question is the one Hubbard and Schlom asked in 1996 (J. Mater. Res. 11, 2757): will this
oxide react with the substrate it is deposited on? The answer is in the convex hull of the
oxide's elements plus the substrate's. Mix the oxide with the substrate in atom fraction ``x``;
the hull energy at that composition is the lowest-energy combination of stable phases that adds
up to it, a small linear programme. If that energy is below the unreacted mixture, the pair
wants to react, and the phases in the minimising combination are the products. The reaction
energy reported is the most negative value over ``x``. Zero means the oxide is stable in contact
with the substrate; HfO2, Al2O3, Y2O3 and LaAlO3 come out at zero, Ta2O5 and TiO2 do not, which
is the published result and why the gate-dielectric survivors are what they are.

Bulk thermodynamics only: no kinetics, no interlayer, no epitaxy. Formation energies are per
atom relative to the elements, so elements sit at zero.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache

import numpy as np
from scipy.optimize import linprog

_TOKEN = re.compile(r"([A-Z][a-z]?)(\d*\.?\d*)")


def parse_formula(formula: str) -> dict[str, float]:
    """Element counts from a reduced formula. Parentheses are flattened (``Hf(SiO)2`` -> the
    multiplier is dropped), which is acceptable here because MP's ``formula_pretty`` for stable
    phases is written out; counts only need to be proportional."""
    out: dict[str, float] = {}
    for el, n in _TOKEN.findall(formula.replace("(", "").replace(")", "")):
        out[el] = out.get(el, 0.0) + (float(n) if n else 1.0)
    return out


def fraction_vector(formula: str, elements: list[str]) -> np.ndarray:
    comp = parse_formula(formula)
    total = sum(comp.values())
    return np.array([comp.get(el, 0.0) / total for el in elements])


@dataclass
class InterfaceOutcome:
    reaction_energy_ev_atom: float  # <= 0; 0 means stable in contact
    x_substrate: float | None  # atom fraction of substrate at the most exothermic point
    products: list[str] = field(default_factory=list)  # hull phases at that point
    n_phases: int = 0


def hull_energy(
    phases: dict[str, float], elements: list[str], target: np.ndarray
) -> tuple[float, dict[str, float]] | None:
    """Lowest energy per atom of any combination of ``phases`` with composition ``target``,
    and the atom fractions of the phases that achieve it. None if the target cannot be formed."""
    names = list(phases)
    a = np.array([fraction_vector(f, elements) for f in names]).T  # elements x phases
    c = np.array([phases[f] for f in names])
    res = linprog(c, A_eq=a, b_eq=target, bounds=(0, None), method="highs")
    if not res.success:
        return None
    weights = {f: float(w) for f, w in zip(names, res.x, strict=True) if w > 1e-6}
    return float(res.fun), weights


def interface_reaction(
    formula: str,
    phases: dict[str, float],
    substrate: str = "Si",
    steps: int = 19,
) -> InterfaceOutcome | None:
    """Memoised front door: the answer is a pure function of the hull, and every polymorph
    of a compound and every candidate build asks for the same one."""
    return _interface_reaction(formula, tuple(sorted(phases.items())), substrate, steps)


@lru_cache(maxsize=4096)
def _interface_reaction(
    formula: str,
    phase_items: tuple[tuple[str, float], ...],
    substrate: str,
    steps: int,
) -> InterfaceOutcome | None:
    """Most exothermic reaction of ``formula`` with ``substrate`` against the hull ``phases``
    (formula -> formation energy per atom, elements at 0).

    The reference for the oxide is the hull energy *at its own composition*, not the
    candidate's own energy: a metastable polymorph's excess above the hull is the stability
    criterion's business, and counting it here as a "reaction with the substrate" would charge
    it twice. So every polymorph of a compound gets the same answer, which is right for a bulk
    thermodynamic question about the composition."""
    elements = sorted(set(parse_formula(formula)) | set(parse_formula(substrate)))
    phases = dict(phase_items)
    for el in elements:
        phases.setdefault(el, 0.0)
    e_sub = phases.get(substrate)
    if e_sub is None:
        return None  # a compound substrate must itself be a hull phase
    f_ox = fraction_vector(formula, elements)
    f_sub = fraction_vector(substrate, elements)
    ref = hull_energy(phases, elements, f_ox)
    if ref is None:
        return None
    e_ox = ref[0]
    worst, at, products = 0.0, None, []
    for x in np.linspace(0.05, 0.95, steps):
        result = hull_energy(phases, elements, (1 - x) * f_ox + x * f_sub)
        if result is None:
            continue
        e_hull, weights = result
        rxn = e_hull - ((1 - x) * e_ox + x * e_sub)
        if rxn < worst - 1e-6:
            worst, at, products = rxn, float(x), sorted(weights, key=weights.get, reverse=True)
    return InterfaceOutcome(
        reaction_energy_ev_atom=round(min(worst, 0.0), 4),
        x_substrate=None if at is None else round(at, 2),
        products=[p for p in products if p != substrate and p != formula],
        n_phases=len(phases),
    )
