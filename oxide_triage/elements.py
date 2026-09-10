"""Element name/symbol helpers used by the guard and the rule-based request parser."""

from __future__ import annotations

import re

SYMBOL_BY_NAME: dict[str, str] = {
    "hydrogen": "H",
    "lithium": "Li",
    "beryllium": "Be",
    "boron": "B",
    "carbon": "C",
    "nitrogen": "N",
    "oxygen": "O",
    "fluorine": "F",
    "sodium": "Na",
    "magnesium": "Mg",
    "aluminum": "Al",
    "aluminium": "Al",
    "silicon": "Si",
    "phosphorus": "P",
    "sulfur": "S",
    "sulphur": "S",
    "chlorine": "Cl",
    "potassium": "K",
    "calcium": "Ca",
    "scandium": "Sc",
    "titanium": "Ti",
    "vanadium": "V",
    "chromium": "Cr",
    "manganese": "Mn",
    "iron": "Fe",
    "cobalt": "Co",
    "nickel": "Ni",
    "copper": "Cu",
    "zinc": "Zn",
    "gallium": "Ga",
    "germanium": "Ge",
    "arsenic": "As",
    "selenium": "Se",
    "bromine": "Br",
    "rubidium": "Rb",
    "strontium": "Sr",
    "yttrium": "Y",
    "zirconium": "Zr",
    "niobium": "Nb",
    "molybdenum": "Mo",
    "technetium": "Tc",
    "ruthenium": "Ru",
    "rhodium": "Rh",
    "palladium": "Pd",
    "silver": "Ag",
    "cadmium": "Cd",
    "indium": "In",
    "tin": "Sn",
    "antimony": "Sb",
    "tellurium": "Te",
    "iodine": "I",
    "cesium": "Cs",
    "caesium": "Cs",
    "barium": "Ba",
    "lanthanum": "La",
    "cerium": "Ce",
    "praseodymium": "Pr",
    "neodymium": "Nd",
    "promethium": "Pm",
    "samarium": "Sm",
    "europium": "Eu",
    "gadolinium": "Gd",
    "terbium": "Tb",
    "dysprosium": "Dy",
    "holmium": "Ho",
    "erbium": "Er",
    "thulium": "Tm",
    "ytterbium": "Yb",
    "lutetium": "Lu",
    "hafnium": "Hf",
    "tantalum": "Ta",
    "tungsten": "W",
    "rhenium": "Re",
    "osmium": "Os",
    "iridium": "Ir",
    "platinum": "Pt",
    "gold": "Au",
    "mercury": "Hg",
    "thallium": "Tl",
    "lead": "Pb",
    "bismuth": "Bi",
    "polonium": "Po",
    "radium": "Ra",
    "actinium": "Ac",
    "thorium": "Th",
    "protactinium": "Pa",
    "uranium": "U",
    "neptunium": "Np",
    "plutonium": "Pu",
}
SYMBOLS: frozenset[str] = frozenset(SYMBOL_BY_NAME.values())

_NAME_RE = re.compile(r"\b(" + "|".join(sorted(SYMBOL_BY_NAME, key=len, reverse=True)) + r")\b", re.I)
# Bare symbols are ambiguous in prose ("In", "As", "I", "W", "K", "O" ...) so only accept
# them when the token is capitalised exactly as a symbol and not a common English word.
_AMBIGUOUS = {"In", "As", "I", "W", "K", "O", "At", "Be", "He", "No", "Am", "Pa", "Ac", "Re", "Sn", "Er", "V"}
_SYMBOL_RE = re.compile(r"(?<![A-Za-z])(" + "|".join(sorted(SYMBOLS, key=len, reverse=True)) + r")(?![a-z])")


# "lead" is also an English verb. Treat it as the element only when it is not used as one.
_LEAD_VERB_AFTER = re.compile(
    r"^\s+(to|the|in|on|a|an|us|them|towards?|away|from|with|into|by|off|up|out|through|our|your)\b", re.I
)
_LEAD_VERB_BEFORE = re.compile(
    r"\b(could|can|may|might|will|would|shall|should|to|that|which|who|they|we|you|it|and)\s+$", re.I
)


def _is_verb_lead(text: str, start: int, end: int) -> bool:
    return bool(_LEAD_VERB_AFTER.match(text[end:])) or bool(_LEAD_VERB_BEFORE.search(text[:start]))


def find_elements(text: str) -> list[str]:
    """Return element symbols mentioned by name or unambiguous symbol, in order of appearance."""
    found: list[tuple[int, str]] = []
    for m in _NAME_RE.finditer(text):
        if m.group(1).lower() == "lead" and _is_verb_lead(text, m.start(), m.end()):
            continue
        found.append((m.start(), SYMBOL_BY_NAME[m.group(1).lower()]))
    for m in _SYMBOL_RE.finditer(text):
        sym = m.group(1)
        if sym in _AMBIGUOUS:
            continue
        found.append((m.start(), sym))
    out: list[str] = []
    for _, sym in sorted(found):
        if sym not in out and sym != "O":
            out.append(sym)
    return out
