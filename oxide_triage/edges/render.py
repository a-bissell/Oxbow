"""Back edge: render an already-computed ``TriageResult`` through a file template.

This module receives a result object and a template name. It has no access to data sources,
the cache, or the scoring code, and it performs no arithmetic beyond number formatting. The
optional model-written rationale is disabled by default; when enabled it passes through the
same numeric guard as the refutation pass, so it cannot introduce a value either.
"""

from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from oxide_triage.refute import primary_caveat
from oxide_triage.schemas import DataStatus, ScoredCandidate, TriageResult

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
TEMPLATE_FILES = {"pi_summary": "pi_summary.md.j2", "audit": "audit.md.j2", "html": "report.html.j2"}


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
        bits.append(stab)
    bg = sc.band_gap_assessment
    if bg.effective_ev is not None:
        if bg.corrected:
            bits.append(
                f"effective gap {bg.effective_ev:.1f} eV (corrected from {bg.reported_functional} {bg.reported_ev:.1f} eV)"
            )
        else:
            bits.append(f"gap {bg.effective_ev:.1f} eV ({bg.reported_functional})")
    if r.dielectric.status == DataStatus.KNOWN and r.dielectric.e_total is not None:
        bits.append(f"dielectric constant {r.dielectric.e_total:.0f} (DFPT)")
    else:
        bits.append("dielectric constant unknown")
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


def make_env(templates_dir: Path = TEMPLATES_DIR) -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(templates_dir)),
        autoescape=lambda name: bool(name) and name.endswith(".html.j2"),  # HTML escaped, Markdown not
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )
    env.filters["fmt"] = fmt
    env.filters["pct"] = pct
    env.filters["primary_caveat"] = primary_caveat
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
