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
interpretation error is visible rather than silent. The converse is printed too: every
clause of the request that no rule consumed and that is not the triage ask itself comes
back as "not acted on", so a request that was only partly honoured never reads as if it had
been honoured in full. That list is the general answer to phrasings the rules do not know;
adding a rule per phrasing is not.
"""

from __future__ import annotations

import re
from collections.abc import Collection
from functools import lru_cache
from typing import Any

from oxide_triage.config import Config, HazardTable
from oxide_triage.edges.llm import LLMClient
from oxide_triage.elements import find_elements
from oxide_triage.schemas import Criteria

NUM = r"(\d+(?:\.\d+)?)"

# Words a request uses to name a criterion ("prioritise the dielectric constant"). The fixed
# six are here; the figure of merit's entry comes from the profile (``criterion_words``).
FIXED_CRITERION_WORDS = {
    "stability": r"stabilit\w*|hull",
    "band_gap": r"band ?gaps?|gap",
    "toxicity": r"toxicit\w*|safety|hazard\w*|non-?toxic",
    "simplicity": r"simplicit\w*|simple composition\w*|few\w* elements",
    "literature": r"literature|evidence|published|papers?",
    "interface": r"interface|substrate (?:reaction|compatib\w*)|react\w* with the substrate",
}
DEFAULT_FOM_WORDS = ("dielectric", r"permittivit\w*", r"high[- ]?k", "kappa")
# The shipped table: the fixed six plus the default profile's dielectric constant, in the order
# the criteria are weighted.
CRITERION_WORDS = {
    "stability": FIXED_CRITERION_WORDS["stability"],
    "band_gap": FIXED_CRITERION_WORDS["band_gap"],
    "dielectric": "|".join(DEFAULT_FOM_WORDS),
    **{k: v for k, v in FIXED_CRITERION_WORDS.items() if k not in ("stability", "band_gap")},
}


def criterion_words(fom: Any) -> dict[str, str]:
    """``CRITERION_WORDS`` for a profile: its figure of merit's vocabulary under its criterion name."""
    words = "|".join(fom.vocabulary) if fom.vocabulary else re.escape(fom.criterion.replace("_", " "))
    return {
        "stability": FIXED_CRITERION_WORDS["stability"],
        "band_gap": FIXED_CRITERION_WORDS["band_gap"],
        fom.criterion: words,
        **{k: v for k, v in FIXED_CRITERION_WORDS.items() if k not in ("stability", "band_gap")},
    }


# A clause is understood if a rule consumed part of it or it is the triage ask itself: the
# subject, a thing the tool does, or one of the criteria it ranks on. Element names are not
# on this list on purpose: "skip anything with lead" names an element and is still not acted
# on if no rule read the "skip".
_UNDERSTOOD = re.compile(
    r"\b(find|search|look for|identify|screen\w*|triage|rank\w*|shortlist\w*|suggest\w*|recommend\w*|"
    r"candidates?|materials?|oxides?|dielectrics?|permittivit\w*|high[- ]?k|k[- ]values?|"
    r"gate[- ]?(?:oxides?|stacks?|dielectrics?)|thin[- ]films?|films?|ald|sputter\w*|deposit\w*|"
    r"(?:band ?)?gaps?|hull|thermodynamic\w*|stab(?:le|ility)|toxic\w*|non-?toxic|hazard\w*|safe\w*|"
    r"simple|simplicity|compositions?|elements?|literature|evidence|published|papers?|public|"
    r"promising|prefer\w*|priorit\w*|weight\w*|thresholds?|limits?|gates?|"
    r"explain|why|compare|compar\w*|versus|vs\.?|differ\w*|favou?r\w*|better|best|"
    r"re-?run|again|instead|profiles?|conservative|exploratory|default|"
    r"caveats?|uncertain\w*|confidence|missing|data|sources?|scores?|excluded?|"
    r"json|audit|summary|brief|html|shortlist|top|list|show|give|return|tell)\b",
    re.I,
)


@lru_cache(maxsize=32)
def understood_re(vocabulary: tuple[str, ...]) -> re.Pattern[str]:
    """``_UNDERSTOOD`` plus a profile's figure-of-merit words, so a clause naming that
    property ("low thermal conductivity") is the ask itself and not reported as unread."""
    if not vocabulary:
        return _UNDERSTOOD
    return re.compile(_UNDERSTOOD.pattern[:-4] + "|" + "|".join(vocabulary) + r")\b", re.I)


# Clauses that read as reasons or courtesies rather than asks.
_ASIDE = re.compile(
    r"^\s*(?:because|since|as|so that|they'?re|they are|it'?s|it is|we'?re|we are|that'?s|"
    r"thanks|thank you|please|cheers)\b",
    re.I,
)
_CLAUSE_SPLIT = re.compile(
    r"[.;!?\n]+|,\s+(?:and|then|also|but|plus)\s+|\s+(?:and|and then|and also|but also|then)\s+", re.I
)
# A substrate named in the request. The interface criterion is computed against the
# configured substrate; a request cannot change it yet, so the ask is reported, not applied.
_SUBSTRATE = re.compile(
    r"\bon\s+(?:a\s+|an\s+|the\s+)?(germanium|ge|gaas|gallium arsenide|gan|gallium nitride|sic|"
    r"silicon carbide|srtio3|strontium titanate|sapphire|glass|quartz|graphene|mos2|inp|diamond|"
    r"silicon|si)\b(?:\s+(?:substrates?|wafers?))?",
    re.I,
)
_SUBSTRATE_ALIASES = {
    "si": "Si",
    "silicon": "Si",
    "ge": "Ge",
    "germanium": "Ge",
    "srtio3": "SrTiO3",
    "strontium titanate": "SrTiO3",
}


def unhandled_clauses(
    text: str, consumed: list[tuple[int, int]], understood: re.Pattern[str] = _UNDERSTOOD
) -> list[str]:
    """Clauses of ``text`` that no rule consumed and that are not the triage ask itself."""
    out: list[str] = []
    pos = 0
    for part in _CLAUSE_SPLIT.split(text):
        start = text.find(part, pos)
        end = start + len(part)
        pos = end
        clause = part.strip(" ,\t")
        if len(clause.split()) < 3 or _ASIDE.match(clause):
            continue
        if any(a < end and b > start for a, b in consumed):
            continue
        if understood.search(clause):
            continue
        out.append(clause)
    return out


def apply_terminology(text: str, mapping: dict[str, str]) -> str:
    """Replace site-local vocabulary with canonical terms (longest keys first)."""
    for key in sorted(mapping, key=len, reverse=True):
        text = re.sub(rf"(?<!\w){re.escape(key)}(?!\w)", mapping[key], text, flags=re.I)
    return text


def rule_parse(
    text: str,
    table: HazardTable,
    blocked: frozenset[str] | None = None,
    substrate: str = "Si",
    words: dict[str, str] | None = None,
    vocabulary: Collection[str] = (),
) -> Criteria:
    """``words`` is the criterion vocabulary (``criterion_words(config.figure_of_merit)``;
    the shipped table when omitted) and ``vocabulary`` the figure of merit's own terms."""
    t = text
    notes: list[str] = []
    kw: dict[str, Any] = {}
    consumed: list[tuple[int, int]] = []  # spans a rule read and acted on

    def hit(m: re.Match[str] | None) -> re.Match[str] | None:
        if m is not None:
            consumed.append(m.span())
        return m

    # ---- top_k ------------------------------------------------------------------------
    if m := re.search(r"\btop[- ](\d{1,2})\b", t, re.I) or re.search(
        r"\b(\d{1,2})\s+(candidates?|materials?|compounds?|oxides?|entries|results)\b", t, re.I
    ):
        k = int(m.group(1))
        if 1 <= k <= 50:
            kw["top_k"] = k
            notes.append(f"shortlist length {k}")
            hit(m)

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
        hit(m)
    elif hit(re.search(r"\bwide(?:r)?\s+(?:band ?)?gaps?\b", t, re.I)):
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
        hit(m)
    elif hit(
        re.search(
            r"\b(?:only|strictly)\s+(?:on[- ]hull|ground[- ]state|(?:thermodynamically\s+)?stable)\b", t, re.I
        )
    ):
        kw["max_energy_above_hull_ev_atom"] = 0.0
        notes.append("'only on-hull/ground-state' -> energy above hull must be 0")

    # ---- composition size -------------------------------------------------------------
    if hit(re.search(r"\bbinar(?:y|ies)\s+(?:oxides?\s+)?only\b|\bonly\s+binar(?:y|ies)\b", t, re.I)):
        kw["max_elements"] = 2
        notes.append("binaries only -> at most 2 distinct elements")
    elif m := re.search(
        r"\b(?:up to|at most|max(?:imum)?(?: of)?|no more than)\s+(\d)\s+(?:distinct\s+)?elements?\b", t, re.I
    ):
        n = int(m.group(1))
        hit(m)
        if 2 <= n <= 6:
            kw["max_elements"] = n
            notes.append(f"at most {n} distinct elements")
        else:
            notes.append(f"ignored 'at most {n} elements': supported range is 2-6")
    elif hit(re.search(r"\b(?:include|allow|consider)\s+(?:quaternar(?:y|ies)|four[- ]element)\b", t, re.I)):
        kw["max_elements"] = 4
        notes.append("quaternaries allowed -> at most 4 distinct elements")

    # ---- element lists ----------------------------------------------------------------
    include: list[str] = []
    exclude: list[str] = []
    allow: list[str] = []

    def found(m: re.Match[str], group: int = 1) -> list[str]:
        syms = find_elements(m.group(group))
        if syms:
            hit(m)
        return syms

    for m in re.finditer(r"\b([A-Za-z]+)[- ]based\b", t):
        include.extend(found(m))
    for m in re.finditer(
        r"\b(?:only|restrict(?:ed)? to|containing|must (?:contain|include))\s+([^.;]{1,40}?)\s+(?:oxides?|compounds?|materials?)\b",
        t,
        re.I,
    ):
        include.extend(found(m))

    for m in re.finditer(r"\b([A-Za-z]+)-free\b", t):
        exclude.extend(found(m))
    for m in re.finditer(
        r"\b(?:no|without|exclude|excluding|avoid|avoiding|not?\s+containing|free of|skip|omit|leave out|"
        r"steer clear of|stay away from)\s+([^.;]{1,50})",
        t,
        re.I,
    ):
        exclude.extend(found(m))

    for m in re.finditer(
        r"\b(?:includ(?:e|ing)|allow(?:ing)?|permit(?:ting)?|consider(?:ing)?|keep(?:ing)?|accept(?:ing)?|add(?:ing)?|"
        r"unblock(?:ing)?|don'?t (?:exclude|block|filter)|do not (?:exclude|block|filter)|"
        r"(?:lift|remove|drop|relax)\s+the\s+(?:block|restrictions?|ban|filter|blocklist)\s+(?:on|for|against))\s+([^.;]{1,60})",
        t,
        re.I,
    ):
        for sym in found(m):
            is_blocked = (sym in blocked) if blocked is not None else table.lookup(sym)[0] >= 2
            if is_blocked:
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
    for crit, crit_words in (words or CRITERION_WORDS).items():
        if hit(
            re.search(
                rf"\b(?:prioriti[sz]e|emphasi[sz]e|weight\w*\s+(?:up|heavily|more)|focus on|most important(?:ly)?)\b[^.;]{{0,30}}?\b(?:{crit_words})\b",
                t,
                re.I,
            )
        ):
            overrides[crit] = 0.4
        if hit(
            re.search(
                rf"\b(?:ignore|de-?emphasi[sz]e|don'?t care about|do not care about|downweight)\b[^.;]{{0,30}}?\b(?:{crit_words})\b",
                t,
                re.I,
            )
        ):
            overrides[crit] = 0.0
    if overrides:
        kw["weight_overrides"] = overrides
        notes.append("weight overrides: " + ", ".join(f"{k}={v:g}" for k, v in sorted(overrides.items())))

    # ---- output template --------------------------------------------------------------
    if hit(
        re.search(
            r"\b(audit|technical view|full breakdown|score breakdown|every component|all thresholds)\b",
            t,
            re.I,
        )
    ):
        kw["output_template"] = "audit"
    elif hit(re.search(r"\b(json|machine[- ]readable|structured output)\b", t, re.I)):
        kw["output_template"] = "json"
    elif hit(re.search(r"\bhtml\b|\bweb report\b|\bprintable report\b", t, re.I)):
        kw["output_template"] = "html"
    elif hit(re.search(r"\b(summary|brief|one screen|plain language|for the PI|non-technical)\b", t, re.I)):
        kw["output_template"] = "pi_summary"

    # ---- what was asked for and not done ------------------------------------------------
    unhandled: list[str] = []
    for m in _SUBSTRATE.finditer(t):
        name = m.group(1).lower()
        canonical = _SUBSTRATE_ALIASES.get(name, m.group(1))
        if canonical.lower() != substrate.lower():
            hit(m)
            unhandled.append(
                f'"{m.group(0).strip()}": the interface criterion is computed against {substrate} in this '
                "configuration; a request cannot change the substrate yet"
            )
    unhandled.extend(f'"{c}"' for c in unhandled_clauses(t, consumed, understood_re(tuple(vocabulary))))
    kw["unhandled"] = unhandled

    kw["interpretation_notes"] = notes
    try:
        return Criteria.model_validate(kw)
    except ValueError as exc:  # a value slipped past the range checks above: drop it, say so
        bad = {str(e["loc"][0]) for e in exc.errors()} if hasattr(exc, "errors") else set()
        for k in bad:
            kw.pop(k, None)
        kw["interpretation_notes"] = notes + [f"ignored out-of-range value(s) for: {', '.join(sorted(bad))}"]
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


def criteria_schema(criteria: Collection[str]) -> dict[str, Any]:
    """The JSON schema the model's parse must satisfy; ``criteria`` names the weights it may set."""
    return {
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
                "properties": {c: {"type": ["number", "null"], "minimum": 0} for c in criteria},
            },
            # anyOf rather than a null inside the enum: the Anthropic schema grammar rejects the latter.
            "output_template": {
                "anyOf": [{"type": "string", "enum": ["pi_summary", "audit", "json"]}, {"type": "null"}]
            },
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


def llm_parse(text: str, llm: LLMClient, criteria: Collection[str]) -> Criteria | None:
    data = llm.complete_json(PARSE_SYSTEM, f"Request:\n{text}", criteria_schema(criteria))
    if not data:
        return None
    cleaned: dict[str, Any] = {k: v for k, v in data.items() if v is not None}
    if "weight_overrides" in cleaned:
        cleaned["weight_overrides"] = {k: v for k, v in cleaned["weight_overrides"].items() if v is not None}
    try:
        return Criteria.model_validate(cleaned)
    except ValueError:
        return None


def merge(rules: Criteria, model: Criteria | None, criteria: Collection[str]) -> Criteria:
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
        out.exclude_elements = [e for e in model.exclude_elements if e not in out.include_elements]
        filled.append("exclude_elements")
    if not out.weight_overrides and model.weight_overrides:
        out.weight_overrides = {k: v for k, v in model.weight_overrides.items() if k in criteria}
        filled.append("weight_overrides")
    if filled:
        out.interpretation_notes.append("filled by language model (validated): " + ", ".join(filled))
        # Model text reaches the output header here, labelled and clipped: never as a value.
        out.interpretation_notes.extend(f"model note: {str(n)[:120]}" for n in model.interpretation_notes[:5])
    return out


def parse_request(
    text: str,
    config: Config,
    table: HazardTable,
    llm: LLMClient,
    blocked: frozenset[str] | None = None,
) -> tuple[Criteria, str]:
    """``blocked``: elements the active profile blocks; only allowances for those are recorded."""
    canonical = apply_terminology(text, config.terminology)
    rules = rule_parse(
        canonical,
        table,
        blocked,
        substrate=config.interface.substrate,
        words=criterion_words(config.figure_of_merit),
        vocabulary=config.figure_of_merit.vocabulary,
    )
    if llm.name != "none" and config.llm.use_for.parse:
        model = llm_parse(canonical, llm, config.criteria())
        return merge(rules, model, config.criteria()), (
            f"rules+{llm.name}" if model else "rules (model parse failed)"
        )
    return rules, "rules"
