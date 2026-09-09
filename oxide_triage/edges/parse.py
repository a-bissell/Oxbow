"""Front edge: natural-language request -> validated ``Criteria``.

Two parsers, always the rule-based one first:

  * ``rule_parse``  deterministic regex/keyword parser. Handles the request vocabulary a
                    materials group actually uses (thresholds, element include/exclude,
                    "lead-free", "binaries only", "top 10", "audit view"). Always runs.
  * ``llm_parse``   optional. Asks the configured model for the same structure, validated
                    against the ``Criteria`` schema. Its output may only *fill in* fields the
                    rules left at default; anything safety-relevant the rules found (element
                    allow/exclude lists) is never overridden by the model.

Whatever is parsed is echoed back to the scientist in the output header, so an
interpretation error is visible rather than silent.
"""

from __future__ import annotations

import re
from typing import Any

from oxide_triage.config import CRITERIA, Config, HazardTable
from oxide_triage.edges.llm import LLMClient
from oxide_triage.elements import find_elements
from oxide_triage.schemas import Criteria

NUM = r"(\d+(?:\.\d+)?)"

CRITERION_WORDS = {
    "stability": r"stabilit\w*|hull",
    "band_gap": r"band ?gaps?|gap",
    "dielectric": r"dielectric|permittivit\w*|high[- ]?k|kappa",
    "toxicity": r"toxicit\w*|safety|hazard\w*|non-?toxic",
    "simplicity": r"simplicit\w*|simple composition\w*|few\w* elements",
    "literature": r"literature|evidence|published|papers?",
}


def apply_terminology(text: str, mapping: dict[str, str]) -> str:
    """Replace site-local vocabulary with canonical terms (longest keys first)."""
    for key in sorted(mapping, key=len, reverse=True):
        text = re.sub(rf"(?<!\w){re.escape(key)}(?!\w)", mapping[key], text, flags=re.I)
    return text


def rule_parse(text: str, table: HazardTable) -> Criteria:
    t = text
    notes: list[str] = []
    kw: dict[str, Any] = {}

    # ---- top_k ------------------------------------------------------------------------
    if m := re.search(r"\btop[- ](\d{1,2})\b", t, re.I) or re.search(
        r"\b(\d{1,2})\s+(candidates?|materials?|compounds?|oxides?|entries|results)\b", t, re.I
    ):
        k = int(m.group(1))
        if 1 <= k <= 50:
            kw["top_k"] = k
            notes.append(f"shortlist length {k}")

    # ---- band gap threshold -----------------------------------------------------------
    if m := re.search(
        rf"\b(?:band ?)?gaps?\s+(?:of\s+)?(?:above|over|at least|greater than|larger than|exceeding|>=?|minimum|min)\s*{NUM}\s*eV",
        t,
        re.I,
    ) or re.search(
        rf"\b{NUM}\s*eV\s+(?:or (?:more|wider|larger)|minimum|min|and up|\+)\s*(?:band ?)?gap", t, re.I
    ):
        kw["min_band_gap_ev"] = float(m.group(1))
        notes.append(f"minimum effective band gap {m.group(1)} eV")
    elif re.search(r"\bwide(?:r)?\s+(?:band ?)?gaps?\b", t, re.I):
        notes.append("'wide band gap' -> profile band-gap threshold and preference curve unchanged")

    # ---- hull threshold ---------------------------------------------------------------
    if m := re.search(
        rf"(?:energy above hull|e_?hull|hull distance|above the hull|metastab\w+)[^.]{{0,25}}?"
        rf"(?:below|under|within|up to|at most|<=?|less than)\s*{NUM}\s*(meV|eV)",
        t,
        re.I,
    ):
        val = float(m.group(1)) / (1000.0 if m.group(2).lower() == "mev" else 1.0)
        kw["max_energy_above_hull_ev_atom"] = val
        notes.append(f"energy-above-hull threshold {val:g} eV/atom")
    elif re.search(r"\b(?:only|strictly)\s+(?:on[- ]hull|ground[- ]state|stable)\b", t, re.I):
        kw["max_energy_above_hull_ev_atom"] = 0.0
        notes.append("'only on-hull/ground-state' -> energy above hull must be 0")

    # ---- composition size -------------------------------------------------------------
    if re.search(r"\bbinar(?:y|ies)\s+(?:oxides?\s+)?only\b|\bonly\s+binar(?:y|ies)\b", t, re.I):
        kw["max_elements"] = 2
        notes.append("binaries only -> at most 2 distinct elements")
    elif m := re.search(
        r"\b(?:up to|at most|max(?:imum)?(?: of)?|no more than)\s+(\d)\s+(?:distinct\s+)?elements?\b", t, re.I
    ):
        kw["max_elements"] = int(m.group(1))
        notes.append(f"at most {m.group(1)} distinct elements")
    elif re.search(r"\b(?:include|allow|consider)\s+(?:quaternar(?:y|ies)|four[- ]element)\b", t, re.I):
        kw["max_elements"] = 4
        notes.append("quaternaries allowed -> at most 4 distinct elements")

    # ---- element lists ----------------------------------------------------------------
    include: list[str] = []
    exclude: list[str] = []
    allow: list[str] = []

    for m in re.finditer(r"\b([A-Za-z]+)[- ]based\b", t):
        include.extend(find_elements(m.group(1)))
    for m in re.finditer(
        r"\b(?:only|restrict(?:ed)? to|containing|must (?:contain|include))\s+([^.;]{1,40}?)\s+(?:oxides?|compounds?|materials?)\b",
        t,
        re.I,
    ):
        include.extend(find_elements(m.group(1)))

    for m in re.finditer(r"\b([A-Za-z]+)-free\b", t):
        exclude.extend(find_elements(m.group(1)))
    for m in re.finditer(
        r"\b(?:no|without|exclude|excluding|avoid|avoiding|not?\s+containing|free of)\s+([^.;]{1,50})",
        t,
        re.I,
    ):
        exclude.extend(find_elements(m.group(1)))

    for m in re.finditer(
        r"\b(?:include|allow|permit|consider|keep|accept|add|don'?t (?:exclude|block|filter)|do not (?:exclude|block|filter))\s+([^.;]{1,60})",
        t,
        re.I,
    ):
        for sym in find_elements(m.group(1)):
            if table.lookup(sym)[0] >= 1:
                allow.append(sym)

    include = _dedupe([e for e in include if e not in exclude])
    exclude = _dedupe(exclude)
    allow = _dedupe([e for e in allow if e not in exclude])
    if include:
        kw["include_elements"] = include
        notes.append("must contain: " + ", ".join(include))
    if exclude:
        kw["exclude_elements"] = exclude
        notes.append("must not contain: " + ", ".join(exclude))
    if allow:
        kw["allow_elements"] = allow
        notes.append("hazard block lifted for: " + ", ".join(allow) + " (configuration deviation)")

    # ---- weights ----------------------------------------------------------------------
    overrides: dict[str, float] = {}
    for crit, words in CRITERION_WORDS.items():
        if re.search(
            rf"\b(?:prioriti[sz]e|emphasi[sz]e|weight\w*\s+(?:up|heavily|more)|focus on|most important(?:ly)?)\b[^.;]{{0,30}}?\b(?:{words})\b",
            t,
            re.I,
        ):
            overrides[crit] = 0.4
        if re.search(
            rf"\b(?:ignore|de-?emphasi[sz]e|don'?t care about|do not care about|downweight)\b[^.;]{{0,30}}?\b(?:{words})\b",
            t,
            re.I,
        ):
            overrides[crit] = 0.0
    if overrides:
        kw["weight_overrides"] = overrides
        notes.append("weight overrides: " + ", ".join(f"{k}={v:g}" for k, v in sorted(overrides.items())))

    # ---- output template --------------------------------------------------------------
    if re.search(
        r"\b(audit|technical view|full breakdown|score breakdown|every component|all thresholds)\b", t, re.I
    ):
        kw["output_template"] = "audit"
    elif re.search(r"\b(json|machine[- ]readable|structured output)\b", t, re.I):
        kw["output_template"] = "json"
    elif re.search(r"\b(summary|brief|one screen|plain language|for the PI|non-technical)\b", t, re.I):
        kw["output_template"] = "pi_summary"

    kw["interpretation_notes"] = notes
    return Criteria.model_validate(kw)


def _dedupe(items: list[str]) -> list[str]:
    out: list[str] = []
    for i in items:
        if i not in out:
            out.append(i)
    return out


# --------------------------------------------------------------------------------------
# LLM parser (optional)
# --------------------------------------------------------------------------------------

CRITERIA_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "top_k": {"type": ["integer", "null"], "minimum": 1, "maximum": 50},
        "max_energy_above_hull_ev_atom": {"type": ["number", "null"], "minimum": 0},
        "min_band_gap_ev": {"type": ["number", "null"], "minimum": 0},
        "max_elements": {"type": ["integer", "null"], "minimum": 2, "maximum": 6},
        "include_elements": {"type": "array", "items": {"type": "string"}},
        "exclude_elements": {"type": "array", "items": {"type": "string"}},
        "allow_elements": {"type": "array", "items": {"type": "string"}},
        "weight_overrides": {
            "type": "object",
            "additionalProperties": False,
            "properties": {c: {"type": ["number", "null"], "minimum": 0} for c in CRITERIA},
        },
        "output_template": {"type": ["string", "null"], "enum": ["pi_summary", "audit", "json", None]},
        "interpretation_notes": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "top_k",
        "max_energy_above_hull_ev_atom",
        "min_band_gap_ev",
        "max_elements",
        "include_elements",
        "exclude_elements",
        "allow_elements",
        "weight_overrides",
        "output_template",
        "interpretation_notes",
    ],
}

PARSE_SYSTEM = (
    "Extract structured triage criteria from a materials scientist's request. Only set a field "
    "when the request states it explicitly; otherwise use null / empty. Element lists use "
    "chemical symbols. `allow_elements` is for hazardous elements the scientist explicitly wants "
    "included (e.g. lead). `exclude_elements` for elements to avoid. Never invent thresholds. "
    "Put one short note per interpretation in `interpretation_notes`."
)


def llm_parse(text: str, llm: LLMClient) -> Criteria | None:
    data = llm.complete_json(PARSE_SYSTEM, f"Request:\n{text}", CRITERIA_SCHEMA)
    if not data:
        return None
    cleaned: dict[str, Any] = {k: v for k, v in data.items() if v is not None}
    if "weight_overrides" in cleaned:
        cleaned["weight_overrides"] = {k: v for k, v in cleaned["weight_overrides"].items() if v is not None}
    try:
        return Criteria.model_validate(cleaned)
    except ValueError:
        return None


def merge(rules: Criteria, model: Criteria | None) -> Criteria:
    """Rules win on everything they set; the model may only fill defaults. Element allowances
    from the model are dropped: lifting a hazard block requires the deterministic parser."""
    if model is None:
        return rules
    out = rules.model_copy(deep=True)
    defaults = Criteria()
    filled: list[str] = []
    for field in (
        "top_k",
        "max_energy_above_hull_ev_atom",
        "min_band_gap_ev",
        "max_elements",
        "output_template",
    ):
        if getattr(out, field) == getattr(defaults, field) and getattr(model, field) != getattr(
            defaults, field
        ):
            setattr(out, field, getattr(model, field))
            filled.append(field)
    if not out.include_elements and model.include_elements:
        out.include_elements = [e for e in model.include_elements if e not in out.exclude_elements]
        filled.append("include_elements")
    if not out.exclude_elements and model.exclude_elements:
        out.exclude_elements = list(model.exclude_elements)
        filled.append("exclude_elements")
    if not out.weight_overrides and model.weight_overrides:
        out.weight_overrides = {k: v for k, v in model.weight_overrides.items() if k in CRITERIA}
        filled.append("weight_overrides")
    if filled:
        out.interpretation_notes.append("filled by language model (validated): " + ", ".join(filled))
        out.interpretation_notes.extend(f"model note: {n}" for n in model.interpretation_notes[:5])
    return out


def parse_request(text: str, config: Config, table: HazardTable, llm: LLMClient) -> tuple[Criteria, str]:
    canonical = apply_terminology(text, config.terminology)
    rules = rule_parse(canonical, table)
    if llm.name != "none" and config.llm.use_for.parse:
        model = llm_parse(canonical, llm)
        return merge(rules, model), f"rules+{llm.name}" if model else "rules (model parse failed)"
    return rules, "rules"
