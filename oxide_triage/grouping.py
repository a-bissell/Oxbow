"""Polymorph grouping and tiering: post-core steps that change no score.

Grouping: one shortlist row per compound.

Runs after the deterministic core and changes no score. The core ranks *materials*
(Materials Project entries), and many oxides have several phases that each pass the gates,
so a shortlist of five can hold three rows of HfO2. Here the best-ranked phase of each
compound leads its row, the other passing phases are collapsed under it with their own
numbers, ranks are renumbered over compounds, and a caveat names the collapsed phases
because which phase a film adopts is exactly what the tool does not model.

Collapsed phases stay in the result (``TriageResult.collapsed_polymorphs``) so ``explain``
works on any material id. ``output.group_polymorphs: false`` turns the whole step off.
"""

from __future__ import annotations

from oxide_triage.schemas import Caveat, PolymorphRef, ScoredCandidate

HULL_SPREAD_WARN_EV = 0.02  # phases further apart than this in E_hull get a warning, not an info


def group_polymorphs(ranked: list[ScoredCandidate]) -> tuple[list[ScoredCandidate], list[ScoredCandidate]]:
    """Return (leaders in rank order, collapsed phases). Input order is the core's ranking."""
    leaders: list[ScoredCandidate] = []
    collapsed: list[ScoredCandidate] = []
    by_formula: dict[str, ScoredCandidate] = {}
    for sc in ranked:
        sc.rank_by_material = sc.rank
        lead = by_formula.get(sc.record.formula)
        if lead is None:
            by_formula[sc.record.formula] = sc
            leaders.append(sc)
            continue
        sc.collapsed_under = lead.record.material_id
        sc.rank = None
        lead.polymorphs.append(
            PolymorphRef(
                material_id=sc.record.material_id,
                crystal_system=sc.record.crystal_system,
                spacegroup_symbol=sc.record.spacegroup_symbol,
                energy_above_hull_ev_atom=sc.record.stability.energy_above_hull_ev_atom,
                effective_band_gap_ev=sc.band_gap_assessment.effective_ev,
                adjusted_score=sc.adjusted_score,
                rank_by_material=sc.rank_by_material,
            )
        )
        collapsed.append(sc)
    for i, sc in enumerate(leaders, start=1):
        sc.rank = i
    return leaders, collapsed


def polymorph_caveat(sc: ScoredCandidate) -> Caveat | None:
    """The caveat a leading row carries for the phases collapsed under it."""
    if not sc.polymorphs:
        return None
    lead_hull = sc.record.stability.energy_above_hull_ev_atom
    spread = 0.0
    parts = []
    for p in sc.polymorphs:
        bits = [p.spacegroup_symbol or p.crystal_system or p.material_id]
        if p.energy_above_hull_ev_atom is not None:
            bits.append(f"E_hull {p.energy_above_hull_ev_atom:.3f} eV/atom")
            if lead_hull is not None:
                spread = max(spread, abs(p.energy_above_hull_ev_atom - lead_hull))
        if p.effective_band_gap_ev is not None:
            bits.append(f"gap {p.effective_band_gap_ev:.1f} eV")
        if p.adjusted_score is not None:
            bits.append(f"score {p.adjusted_score:.2f}")
        parts.append(f"{bits[0]} ({', '.join(bits[1:])})" if len(bits) > 1 else bits[0])
    n = len(sc.polymorphs)
    lead_phase = sc.record.spacegroup_symbol or sc.record.crystal_system or "this phase"
    text = (
        f"{n} other phase{'s' if n > 1 else ''} of {sc.record.formula} passed the gates and "
        f"{'are' if n > 1 else 'is'} collapsed under this row ({lead_phase} leads): {'; '.join(parts)}. "
        "Which phase a deposited film adopts is not modelled; the numbers above are for the leading phase."
    )
    return Caveat(
        code="polymorphs_collapsed",
        severity="warning" if spread > HULL_SPREAD_WARN_EV else "info",
        text=text,
        evidence={
            "collapsed": [p.material_id for p in sc.polymorphs],
            "hull_spread_ev_atom": round(spread, 4),
        },
    )


def assign_tiers(ranked: list[ScoredCandidate], band: float) -> None:
    """Tier 1 is the leader and everyone within ``band`` of it; tier 2 starts at the first
    candidate outside that band and is measured from *its* score, and so on. Measuring from
    the tier's leader rather than chaining neighbour to neighbour keeps a tier bounded, so a
    long run of candidates 0.01 apart does not become one tier. Ranks inside a tier are kept
    for reference but the templates say they are arbitrary. ``band`` 0 gives every rank its
    own tier."""
    tier = 0
    leader_score: float | None = None
    for sc in ranked:
        score = sc.adjusted_score
        if score is None:
            sc.tier = None
            continue
        if leader_score is None or leader_score - score > band:
            tier += 1
            leader_score = score
        sc.tier = tier
