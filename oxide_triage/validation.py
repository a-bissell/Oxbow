"""Retrospective interface benchmark against a published classification.

Hubbard and Schlom (1996) and the workhorse cases informed tolerance and assessment-band
choices (see docs/ranking-decisions.md), so this is not independent validation. It reports
agreement on the existing cases; hard, soft, borderline and secondary groups remain distinct.
No parameters or classifications are changed by this report.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from oxide_triage.cache import Cache
from oxide_triage.config import DATA_DIR, Config
from oxide_triage.scoring.hull import interface_reaction, parse_formula
from oxide_triage.sources.materials_project import MaterialsProject

DEFAULT_SET = DATA_DIR / "validation" / "hubbard_schlom_1996.yaml"

HARD_GROUPS = {"proven_stable": "stable", "unstable": "reacts"}
SOFT_GROUPS = {"not_shown_unstable": "stable"}
REPORT_ONLY = ("borderline", "secondary")


@dataclass
class ValidationRow:
    formula: str
    group: str
    expected: str | None  # stable | reacts | None
    confidence: str
    predicted: str | None  # stable | marginal | reacts | None (no hull)
    reaction_energy_ev_atom: float | None
    products: list[str]
    agrees: bool | None  # None when not scored
    note: str | None = None


@dataclass
class ValidationReport:
    citation: str
    substrate: str
    tolerance_ev_atom: float
    rows: list[ValidationRow] = field(default_factory=list)
    hard_total: int = 0
    hard_agree: int = 0
    soft_total: int = 0
    soft_agree: int = 0
    missing: list[str] = field(default_factory=list)  # no hull in the cache (offline) or fetch failed

    @property
    def passed(self) -> bool:
        return (
            self.hard_total > 0
            and self.hard_agree == self.hard_total
            and not any(r.group in HARD_GROUPS and r.predicted is None for r in self.rows)
        )

    @property
    def disagreements(self) -> list[ValidationRow]:
        return [r for r in self.rows if r.agrees is False]


def load_set(path: Path = DEFAULT_SET) -> dict[str, Any]:
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _predict(e: float | None, tol: float) -> str | None:
    """stable: inside the tolerance (DFT noise). marginal: inside twice the tolerance, the band
    the literature itself argues over for ZrO2. reacts: beyond that. The scoring ramp reaches
    zero further out, but where the *score* reaches zero is a preference; where a reaction is
    distinguishable from noise is not, and only the second belongs in a validation."""
    if e is None:
        return None
    if e >= -tol:
        return "stable"
    if e >= -2 * tol:
        return "marginal"
    return "reacts"


def validate_interface(
    config: Config,
    cache: Cache,
    offline: bool | None = None,
    path: Path = DEFAULT_SET,
    mp: MaterialsProject | None = None,
) -> ValidationReport:
    data = load_set(path)
    ic = config.interface
    substrate = str(data.get("substrate") or ic.substrate)
    offline = config.cache.offline if offline is None else offline
    mp = mp or MaterialsProject(cache, config.cache.ttl_days, offline)
    report = ValidationReport(
        citation=str(data.get("citation", "")), substrate=substrate, tolerance_ev_atom=ic.tolerance_ev_atom
    )
    sub_elements = set(parse_formula(substrate))

    def hull_for(formula: str) -> dict[str, float] | None:
        elements = sorted(set(parse_formula(formula)) | sub_elements)
        if offline:
            payload, _, _ = mp.stable_phases_cached(elements)
        else:
            payload, _, _ = mp.stable_phases(elements, ic.thermo_type)
        if not payload or not payload.get("phases"):
            return None
        return dict(payload["phases"])

    for group in (*HARD_GROUPS, *SOFT_GROUPS, *REPORT_ONLY):
        for entry in data.get(group, []) or []:
            formula = str(entry["formula"])
            expected = HARD_GROUPS.get(group) or SOFT_GROUPS.get(group)
            if group in REPORT_ONLY:
                exp = entry.get("expected")
                expected = {"stable": "stable", "unstable": "reacts"}.get(str(exp)) if exp else None
            phases = hull_for(formula)
            outcome = interface_reaction(formula, phases, substrate) if phases else None
            e = outcome.reaction_energy_ev_atom if outcome else None
            predicted = _predict(e, ic.tolerance_ev_atom)
            agrees: bool | None = None
            if predicted is not None and expected is not None and group not in REPORT_ONLY:
                # "stable" expected: stable agrees, marginal agrees (inside the band the paper's
                # own successors put ZrO2 in), reacts disagrees. "reacts" expected: only reacts agrees.
                agrees = predicted != "reacts" if expected == "stable" else predicted == "reacts"
            elif predicted is not None and expected is not None:
                agrees = predicted != "reacts" if expected == "stable" else predicted == "reacts"
            row = ValidationRow(
                formula=formula,
                group=group,
                expected=expected,
                confidence=str(entry.get("confidence", "")),
                predicted=predicted,
                reaction_energy_ev_atom=e,
                products=list(outcome.products) if outcome else [],
                agrees=agrees,
                note=entry.get("note") or entry.get("source"),
            )
            report.rows.append(row)
            if phases is None:
                report.missing.append(formula)
            if group in HARD_GROUPS and predicted is not None:
                report.hard_total += 1
                report.hard_agree += bool(agrees)
            elif group in SOFT_GROUPS and predicted is not None:
                report.soft_total += 1
                report.soft_agree += bool(agrees)
    return report


def render_markdown(report: ValidationReport) -> str:
    lines = [
        f"Retrospective benchmark: {report.citation}; substrate {report.substrate}; the tool's tolerance "
        f"{report.tolerance_ev_atom:g} eV/atom (a reaction inside it counts as none).",
        "",
        "Not independent validation: these literature cases informed parameter and assessment-band choices.",
        "",
        "| Oxide | Paper says | Group | Tool: E_rxn (eV/atom) | Tool says | Products | Agrees |",
        "|---|---|---|---|---|---|---|",
    ]
    label = {"stable": "stable", "reacts": "unstable"}
    for r in report.rows:
        e = "no hull cached" if r.reaction_energy_ev_atom is None else f"{r.reaction_energy_ev_atom:+.3f}"
        agrees = "—" if r.agrees is None else ("yes" if r.agrees else "**no**")
        conf = f" ({r.confidence})" if r.confidence and r.confidence != "confirmed" else ""
        lines.append(
            f"| {r.formula} | {label.get(r.expected or '', '—')}{conf} | {r.group} | {e} | {r.predicted or '—'} | "
            f"{', '.join(r.products) or '—'} | {agrees} |"
        )
    lines.append("")
    lines.append(
        f"Hard assertions (proven stable, unstable): {report.hard_agree} of {report.hard_total} agree. "
        f"Soft assertions (not shown unstable): {report.soft_agree} of {report.soft_total} agree."
        + (f" No hull cached for: {', '.join(report.missing)}." if report.missing else "")
    )
    if report.disagreements:
        lines.append("")
        lines.append("Disagreements, named:")
        for r in report.disagreements:
            lines.append(
                f"- {r.formula}: the paper says {label.get(r.expected or '', '?')}; the hull says "
                f"{r.reaction_energy_ev_atom:+.3f} eV/atom"
                + (f" forming {', '.join(r.products)}" if r.products else "")
                + (f". {r.note}" if r.note else ".")
            )
    return "\n".join(lines)
