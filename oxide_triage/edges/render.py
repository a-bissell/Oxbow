"""Back edge: render an already-computed ``TriageResult`` through a file template.

This module receives a result object and a template name. It has no access to data sources,
the cache, or the scoring code, and it performs no arithmetic beyond number formatting. The
rationale is rendered deterministically from structured values. No arbitrary prose is
certified as evidence.
"""

from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from oxide_triage.evidence import doi_url, source_summary
from oxide_triage.refute import primary_caveat
from oxide_triage.schemas import DataStatus, ScoredCandidate, TriageResult

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
TEMPLATE_FILES = {
    "pi_summary": "pi_summary.md.j2",  # the plain summary: one line per candidate
    "advanced": "advanced.md.j2",  # the detailed summary: full rationale, tiers, gaps, hashes
    "audit": "audit.md.j2",
    "html": "report.html.j2",
}


def fmt(x: float | None, digits: int = 3) -> str:
    return "—" if x is None else f"{x:.{digits}f}"


def pct(x: float | None) -> str:
    return "—" if x is None else f"{round(x * 100)}%"


def rationale_line(sc: ScoredCandidate) -> str:
    """One deterministic line of plain-language rationale built from computed facts only."""
    r = sc.record
    bits: list[str] = []
    e_hull = r.stability.energy_above_hull_ev_atom
    if e_hull is not None:
        stab = "on the convex hull" if e_hull == 0 else f"{e_hull * 1000:.0f} meV/atom above hull"
        if sc.cross_source_agreement == "agree":
            stab += ", OQMD agrees"
        elif sc.cross_source_agreement == "disagree":
            stab += ", OQMD disagrees"
        elif sc.cross_source_agreement == "untested":
            stab += ", no cross-check run"
        bits.append(stab)
    bg = sc.band_gap_assessment
    if bg.effective_ev is not None:
        if bg.corrected:
            bits.append(
                f"effective gap {bg.effective_ev:.1f} eV (corrected from {bg.reported_functional} {bg.reported_ev:.1f} eV)"
            )
        else:
            bits.append(f"gap {bg.effective_ev:.1f} eV ({bg.reported_functional})")
    fom = r.figure_of_merit
    if fom.status == DataStatus.KNOWN and fom.value is not None:
        bits.append(fom.short or f"{fom.label} {fom.value:.0f} ({fom.method})")
    elif fom.status == DataStatus.NOT_RETRIEVED:
        bits.append(f"{fom.label} not retrieved")
    else:
        bits.append(f"{fom.label} unknown")
    interface = next((c for c in sc.components if c.criterion == "interface"), None)
    if interface and interface.status == DataStatus.KNOWN:
        bits.append(interface.raw_label)
    tier = r.hazard.worst_tier
    if tier == 0:
        bits.append("benign elements")
    elif tier == 1:
        bits.append(f"caution element(s): {', '.join(r.hazard.worst_elements)}")
    elif tier is not None:
        bits.append(f"hazardous element(s) permitted by config: {', '.join(r.hazard.worst_elements)}")
    lit = r.literature
    if lit.status == DataStatus.KNOWN:
        bits.append(f"{lit.thin_film_works:,} thin-film papers")
    return "; ".join(bits) + "."


def plain_line(sc: ScoredCandidate) -> str:
    """The rationale in plain words for the summary: what a PI needs to place the candidate,
    no method names, no ids. Computed facts only, like ``rationale_line``."""
    r = sc.record
    bits: list[str] = []
    e_hull = r.stability.energy_above_hull_ev_atom
    if e_hull is not None:
        mev = e_hull * 1000
        if e_hull == 0:
            stab = "on computed hull"
        elif mev < 1:
            stab = f"nearly stable ({mev:.1f} meV/atom above the hull)"
        else:
            stab = f"metastable ({mev:.0f} meV/atom above the hull)"
        if sc.cross_source_agreement == "disagree":
            stab += ", sources disagree"
        bits.append(stab)
    bg = sc.band_gap_assessment
    if bg.effective_ev is not None:
        bits.append(f"gap {'≈' if bg.corrected else ''}{bg.effective_ev:.1f} eV")  # ≈: a scaled DFT value
    fom = r.figure_of_merit
    if fom.status == DataStatus.KNOWN and fom.value is not None:
        bits.append(f"{fom.label} {fom.value:.0f}{(' ' + fom.units) if fom.units else ''}")
    else:
        bits.append(f"no {fom.label} data")
    interface = next((c for c in sc.components if c.criterion == "interface"), None)
    if interface and interface.status == DataStatus.KNOWN:
        bits.append(interface.raw_label)
    tier = r.hazard.worst_tier
    if tier is not None and tier > 0:
        bits.append(f"contains {', '.join(r.hazard.worst_elements)} (caution)")
    lit = r.literature
    if lit.status == DataStatus.KNOWN and lit.thin_film_works is not None:
        n = lit.thin_film_works
        if n >= 1000:
            bits.append("well studied")
        elif n >= 100:
            bits.append(f"some literature ({n:,} papers)")
        elif n > 0:
            bits.append(f"little literature ({n} papers)")
        else:
            bits.append("no thin-film papers found")
    return ", ".join(bits) + "."


def tie_lines(shortlist: list[ScoredCandidate], band: float) -> list[str]:
    """One sentence per tier that holds more than one shortlisted candidate."""
    out: list[str] = []
    by_tier: dict[int, list[ScoredCandidate]] = {}
    for sc in shortlist:
        if sc.tier is not None:
            by_tier.setdefault(sc.tier, []).append(sc)
    for members in by_tier.values():
        if len(members) > 1:
            ranks = [m.rank for m in members if m.rank is not None]
            names = " and ".join(
                [", ".join(m.record.formula for m in members[:-1]), members[-1].record.formula]
            )
            out.append(f"{ranks[0]}–{ranks[-1]} are effectively tied ({names}; scores within {band:g}).")
    return out


def make_env(templates_dir: Path = TEMPLATES_DIR) -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(templates_dir)),
        autoescape=lambda name: bool(name) and name.endswith(".html.j2"),  # HTML escaped, Markdown not
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )
    env.filters["doi_url"] = doi_url
    env.filters["source_summary"] = source_summary
    env.filters["fmt"] = fmt
    env.filters["pct"] = pct
    env.filters["primary_caveat"] = primary_caveat
    env.filters["plain_line"] = plain_line
    env.filters["tie_lines"] = tie_lines
    return env


def render(
    result: TriageResult,
    template: str | None = None,
    templates_dir: Path = TEMPLATES_DIR,
    **context: object,
) -> str:
    """Render ``result`` through a template. ``context`` adds optional extras a template may use
    (the HTML report accepts ``eval_summary`` and ``eval_data_label``)."""
    name = template or result.criteria.output_template or "pi_summary"
    if name == "json":
        return result.model_dump_json(indent=2)
    if name not in TEMPLATE_FILES:
        raise ValueError(f"Unknown template '{name}'. Available: {', '.join([*TEMPLATE_FILES, 'json'])}")
    env = make_env(templates_dir)
    extras = {"eval_summary": None, "eval_data_label": None, **context}
    return env.get_template(TEMPLATE_FILES[name]).render(result=result, **extras)
