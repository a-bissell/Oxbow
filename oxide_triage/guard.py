"""Request guard: sort a request into bins *before* any model sees it.

Bin 1  architecturally impossible   wetlab triggers, private lab data, paywalled sources.
        There is no tool for these in the deployment. The response says the capability
        does not exist; it does not say "I have been told to decline".
Bin 2  configuration deviation      "include lead" looks like bypassing a safety filter but
        is a legitimate config change. Proceed; surface it in the header; log it.
Bin 3  evidence-integrity attack    "cite a paper supporting this", "assume the data checks
        out", "just give me a number", "rank these even though you have no data". Refuse,
        because complying would fabricate evidence.

The guard is rule-based on purpose. Refusal behaviour must not depend on a model.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from oxide_triage.config import HazardTable
from oxide_triage.elements import find_elements
from oxide_triage.schemas import GuardDecision, GuardFinding, RequestBin


@dataclass(frozen=True)
class Rule:
    code: str
    pattern: re.Pattern[str]
    explanation: str


def _r(code: str, pattern: str, explanation: str) -> Rule:
    return Rule(code, re.compile(pattern, re.I | re.S), explanation)


IMPOSSIBLE_RULES: list[Rule] = [
    _r(
        "wetlab_trigger",
        r"\b(run|start|trigger|schedule|queue|launch|execute|begin|kick off|initiate|book|reserve)\b"
        r"[^.]{0,60}?\b(deposition|ald|sputter\w*|anneal\w*|synthesis|experiment\w*|growth run|"
        r"reactor|chamber|furnace|wetlab|wet lab|tool time)\b",
        "This deployment has no interface to laboratory equipment or scheduling systems. "
        "It cannot start, queue or book any physical process.",
    ),
    _r(
        "wetlab_trigger",
        r"\b(deposit|synthesi[sz]e|grow|anneal|sputter|fabricate|make)\b[^.]{0,40}?\b(the |a |some )?"
        r"(samples?|films?|wafers?|batch|it|them|these)\b",
        "This deployment has no interface to laboratory equipment. It cannot deposit, grow or "
        "synthesise anything.",
    ),
    _r(
        "private_data",
        r"\b(our|the|my|internal|private|in-house|unpublished|confidential|proprietary)\b[^.]{0,30}?"
        r"\b(lims|eln|lab notebooks?|internal (database|data|results|runs)|private (data|database)|"
        r"unpublished (data|results)|shared drive|sharepoint|group data|previous runs|run logs?)\b",
        "This deployment is not connected to any private or internal data store (LIMS, ELN, "
        "shared drives). It reads only cached public sources.",
    ),
    _r(
        "paywalled_source",
        r"\b(icsd|scopus|web of science|springer ?materials|reaxys|scifinder|sci-?hub|"
        r"paywall\w*|closed[- ]access|subscription database)\b",
        "Paywalled and closed sources (ICSD, Scopus, Web of Science, SpringerMaterials, ...) are "
        "not part of this deployment. Only public APIs are cached.",
    ),
]

INTEGRITY_RULES: list[Rule] = [
    # "cite ... a paper/reference" is a request to produce evidence, whatever follows.
    _r(
        "fabricate_citation",
        r"\bcite\b[^.]{0,30}?\b(papers?|references?|citations?|sources?|studies|study|publications?)\b",
        "A citation is evidence. This tool only reports literature records it actually retrieved; "
        "it cannot produce a reference to support a conclusion.",
    ),
    # "find/give me a paper that supports the top pick" — the object is the tool's own result.
    _r(
        "fabricate_citation",
        r"\b(find|give|provide|add|include|make up|invent|generate)\b[^.]{0,30}?"
        r"\b(a |some |any |one )?(papers?|references?|citations?|sources?|studies|study|publications?)\b"
        r"[^.]{0,20}?\b(support\w*|prov(?:e|es|ing)|confirm\w*|back\w*|justif\w*)\b[^.]{0,20}?"
        r"\b(the |this |that |your |our |its |my )?(top|pick|choice|rank\w*|result|conclusion|claim|"
        r"recommendation|shortlist|answer|candidates?|number|value)\b",
        "A citation is evidence. This tool only reports literature records it actually retrieved; "
        "it cannot produce a reference to support a conclusion.",
    ),
    _r(
        "fabricate_citation",
        r"\b(make up|invent|fabricate)\b[^.]{0,30}?\b(references?|citations?|data|numbers?|values?|results?)\b",
        "This tool cannot invent references, data or numbers.",
    ),
    _r(
        "assume_data_valid",
        r"\b(assume|pretend|take it|suppose)\b[^.]{0,40}?\b(stability|data|values?|numbers?|results?|"
        r"band ?gaps?|dielectric)\b[^.]{0,40}?\b(check(s|ed)? out|are (fine|correct|ok|valid|right)|"
        r"is (fine|correct|ok|valid|right)|verified|confirmed|reliable)\b",
        "Whether data checks out is a fact about the sources, not an instruction the tool can "
        "accept. Cross-source agreement and missing values are always reported as found.",
    ),
    _r(
        "number_without_data",
        r"\b(just|simply)\b[^.]{0,15}?\b(give|tell|hand)\b[^.]{0,15}?\b(me|us)\b[^.]{0,15}?"
        r"\b(a|the|one|some)\b[^.]{0,10}?\b(number|value|figure|estimate|dielectric constant|band ?gap)\b",
        "A number without a source is a fabrication. Missing values are reported as unknown.",
    ),
    _r(
        "number_without_data",
        r"\b(estimate|guess|fill in|make up|infer|extrapolate)\b[^.]{0,30}?\b(the |any |all )?"
        r"(missing|unknown|absent)\b[^.]{0,20}?\b(values?|data|numbers?|dielectric|constants?)\b",
        "Missing values stay missing. The tool does not estimate or infer data it did not retrieve.",
    ),
    _r(
        "rank_without_data",
        r"\b(rank|score|shortlist|order)\b[^.]{0,40}?\b(anyway|regardless|even (though|if|when)|"
        r"despite|without)\b[^.]{0,40}?\b(no|missing|lack\w*|absent|any) (data|evidence|values?)\b",
        "Ranking on absent data would present a fabrication as a result. Candidates with missing "
        "data are scored on what exists and the gaps are listed explicitly.",
    ),
    # "gaps" alone is the domain's word for band gaps; only uncertainty vocabulary counts here.
    _r(
        "hide_uncertainty",
        r"\b(drop|remove|hide|suppress|omit|leave out|skip|don'?t (show|include|mention|list))\b"
        r"[^.]{0,20}?\b(the )?(caveats?|uncertaint\w*|missing[- ]data|data gaps|warnings?|limitations?|"
        r"confidence labels?)\b",
        "Caveats and data gaps are part of the result, not decoration. The PI summary already "
        "compresses them to one line per candidate.",
    ),
]

ALLOW_RE = re.compile(
    r"\b(include|allow|permit|consider|keep|accept|add|don'?t (exclude|block|filter)|do not (exclude|block|filter)|"
    r"with|containing|based)\b[^.]{0,50}",
    re.I,
)
# Mirrors the exclude-phrase regex in edges/parse.py: an element named here is being
# excluded, not allowed, even though it may also fall inside an ALLOW_RE match (e.g.
# "with no cadmium").
EXCLUDE_RE = re.compile(
    r"\b(?:no|without|exclude|excluding|avoid|avoiding|not?\s+containing|free of)\s+([^.;]{1,50})",
    re.I,
)
TRIAGE_INTENT_RE = re.compile(
    r"\b(find|candidates?|shortlist|rank\w*|oxides?|dielectrics?|materials?|screen\w*|triage|"
    r"suggest\w*|recommend\w*|promising|band ?gap|stable|compositions?)\b",
    re.I,
)


def _hazard_allowances(
    text: str, table: HazardTable, blocked: frozenset[str] | None = None
) -> list[GuardFinding]:
    findings: list[GuardFinding] = []
    seen: set[str] = set()
    negated: set[str] = set()
    for m in EXCLUDE_RE.finditer(text):
        negated.update(find_elements(m.group(1)))
    for m in ALLOW_RE.finditer(text):
        for sym in find_elements(m.group(0)):
            if sym in negated:
                continue
            tier, basis, _ = table.lookup(sym)
            is_blocked = (sym in blocked) if blocked is not None else tier >= 2
            if is_blocked and sym not in seen:
                seen.add(sym)
                findings.append(
                    GuardFinding(
                        bin=RequestBin.CONFIG_DEVIATION,
                        code="hazard_element_allowance",
                        matched_text=m.group(0).strip()[:80],
                        explanation=(
                            f"{sym} is hazard tier {tier} ({basis}). Permitting it is a configuration "
                            "change: the run proceeds, the deviation is shown in the header and logged, "
                            "and the hazard caveat stays attached to every affected candidate."
                        ),
                    )
                )
    return findings


def guard_request(text: str, table: HazardTable, blocked: frozenset[str] | None = None) -> GuardDecision:
    """``blocked`` is the set of elements the active profile blocks; when omitted, tier-2
    elements are assumed blocked (the shipped default)."""
    findings: list[GuardFinding] = []

    for rule in INTEGRITY_RULES:
        m = rule.pattern.search(text)
        if m:
            findings.append(
                GuardFinding(
                    bin=RequestBin.INTEGRITY,
                    code=rule.code,
                    matched_text=m.group(0).strip()[:120],
                    explanation=rule.explanation,
                )
            )
    for rule in IMPOSSIBLE_RULES:
        m = rule.pattern.search(text)
        if m:
            findings.append(
                GuardFinding(
                    bin=RequestBin.IMPOSSIBLE,
                    code=rule.code,
                    matched_text=m.group(0).strip()[:120],
                    explanation=rule.explanation,
                )
            )
    findings.extend(_hazard_allowances(text, table, blocked))

    integrity = [f for f in findings if f.bin == RequestBin.INTEGRITY]
    impossible = [f for f in findings if f.bin == RequestBin.IMPOSSIBLE]
    has_triage_intent = bool(TRIAGE_INTENT_RE.search(text))

    if integrity:
        lines = [
            "This request cannot be completed as asked because it would require fabricating evidence:",
            *[f'  - "{f.matched_text}": {f.explanation}' for f in integrity],
            "",
            "What the tool can do: rank on the data that exists, list every criterion with no data "
            "behind it, and show the literature records it actually retrieved.",
        ]
        return GuardDecision(proceed=False, findings=findings, refusal_message="\n".join(lines))

    if impossible and not has_triage_intent:
        lines = [
            "This deployment does not have the capability the request needs:",
            *[f'  - "{f.matched_text}": {f.explanation}' for f in impossible],
            "",
            "It can triage candidate oxides from cached public data (Materials Project, OQMD, "
            "OpenAlex, PubChem) and return a ranked shortlist with caveats.",
        ]
        return GuardDecision(proceed=False, findings=findings, refusal_message="\n".join(lines))

    return GuardDecision(proceed=True, findings=findings)
