"""Resolve profile config + parsed request criteria into the effective ranking settings.

Every place the request moves a knob away from the profile is recorded as a ``Deviation``
so the output header can show it. Element allowlisting (e.g. "include lead") is the
canonical example: permitted, logged, loud.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from oxide_triage.config import CRITERIA, Config, HazardTable
from oxide_triage.schemas import Criteria, Deviation


@dataclass(frozen=True)
class Effective:
    weights: dict[str, float]
    max_energy_above_hull: float
    min_band_gap: float
    max_elements: int
    blocked_elements: frozenset[str]
    allowed_despite_tier: frozenset[str]
    include_elements: frozenset[str] = field(default_factory=frozenset)
    exclude_elements: frozenset[str] = field(default_factory=frozenset)
    top_k: int = 5
    on_missing_stability: str = "exclude"
    on_missing_band_gap: str = "exclude"


def _fmt(value: object) -> str:
    if isinstance(value, bool) or value is None:
        return str(value)
    if isinstance(value, int | float):
        return f"{value:g}"
    if isinstance(value, list):
        return "[" + ", ".join(_fmt(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{" + ", ".join(f"{k}: {_fmt(v)}" for k, v in value.items()) + "}"
    return str(value)


def _blocked_by_tier(table: HazardTable, tiers: list[int]) -> set[str]:
    return {el for el, t in table.tiers.items() if t in tiers}


def blocked_by_policy(config: Config, table: HazardTable) -> frozenset[str]:
    """Elements the active profile blocks before any request is considered. The never-lift
    list is blocked whatever the profile's allowlist says."""
    tox = config.toxicity
    return frozenset(
        (_blocked_by_tier(table, tox.blocklist_tiers) | set(tox.element_blocklist))
        - set(tox.element_allowlist)
        | set(tox.never_lift)
    )


def never_liftable(config: Config) -> frozenset[str]:
    """Elements a request cannot unblock; the guard declines the request instead."""
    return frozenset(config.toxicity.never_lift)


def resolve(config: Config, criteria: Criteria, table: HazardTable) -> tuple[Effective, list[Deviation]]:
    deviations: list[Deviation] = []
    tox = config.toxicity

    # ---- site-level deviations: the site file changed the shipped ranking policy ------
    policy = config.policy_overrides()
    if policy:
        changes = "; ".join(f"{o.key} {_fmt(o.shipped)} -> {_fmt(o.value)}" for o in policy)
        deviations.append(
            Deviation(
                code="site_override",
                description=(
                    f"Site configuration overrides the shipped policy for profile "
                    f"'{config.profile_name}': {changes}."
                ),
                origin="site",
            )
        )

    # ---- profile-level deviations from the shipped default policy ------------------
    # The never-lift list outranks a profile or site allowlist too: an element on both stays
    # blocked, and the deviation says so rather than listing it as permitted.
    never = set(tox.never_lift)
    profile_allow = set(tox.element_allowlist) - never
    if overreach := sorted(set(tox.element_allowlist) & never):
        deviations.append(
            Deviation(
                code="allowlist_never_lift",
                description=(
                    f"Profile '{config.profile_name}' lists {', '.join(overreach)} in both element_allowlist "
                    "and never_lift; never_lift wins and they stay blocked."
                ),
                origin="profile",
            )
        )
    if profile_allow:
        deviations.append(
            Deviation(
                code="profile_element_allowlist",
                description=(
                    f"Profile '{config.profile_name}' permits hazard-tier elements: "
                    f"{', '.join(sorted(profile_allow))}. Their hazard basis is still shown in caveats."
                ),
                origin="profile",
            )
        )

    # ---- request-level deviations ---------------------------------------------------
    policy_blocked = blocked_by_policy(config, table)
    # The guard declines a request that names one of these; if criteria arrive another way
    # (a rerun, a client), the allowance is dropped here so the gate still holds.
    request_allow = {e for e in criteria.allow_elements if e in policy_blocked and e not in never}
    if request_allow:
        deviations.append(
            Deviation(
                code="request_element_allowlist",
                description=(
                    f"Request lifted the hazard block for: {', '.join(sorted(request_allow))}. "
                    "This is a configuration change, not a safety override; hazard caveats remain."
                ),
                origin="request",
            )
        )

    max_hull = config.gates.max_energy_above_hull_ev_atom
    if (
        criteria.max_energy_above_hull_ev_atom is not None
        and criteria.max_energy_above_hull_ev_atom != max_hull
    ):
        deviations.append(
            Deviation(
                code="request_hull_threshold",
                description=(
                    f"Energy-above-hull threshold changed from {max_hull:g} to "
                    f"{criteria.max_energy_above_hull_ev_atom:g} eV/atom by the request."
                ),
                origin="request",
            )
        )
        max_hull = criteria.max_energy_above_hull_ev_atom

    min_gap = config.gates.min_band_gap_ev
    if criteria.min_band_gap_ev is not None and criteria.min_band_gap_ev != min_gap:
        deviations.append(
            Deviation(
                code="request_gap_threshold",
                description=(
                    f"Minimum effective band gap changed from {min_gap:g} to "
                    f"{criteria.min_band_gap_ev:g} eV by the request."
                ),
                origin="request",
            )
        )
        min_gap = criteria.min_band_gap_ev

    max_el = config.gates.max_elements
    if criteria.max_elements is not None and criteria.max_elements != max_el:
        deviations.append(
            Deviation(
                code="request_max_elements",
                description=f"Maximum distinct elements changed from {max_el} to {criteria.max_elements}.",
                origin="request",
            )
        )
        max_el = criteria.max_elements

    weights = config.weights.normalized()
    if criteria.weight_overrides:
        merged = dict(config.weights.model_dump())
        for k, v in criteria.weight_overrides.items():
            if k in CRITERIA and v >= 0:
                merged[k] = v
        total = sum(merged.values())
        if total > 0:
            weights = {k: v / total for k, v in merged.items()}
            deviations.append(
                Deviation(
                    code="request_weight_overrides",
                    description="Criterion weights changed by the request: "
                    + ", ".join(f"{k}={v:g}" for k, v in sorted(criteria.weight_overrides.items())),
                    origin="request",
                )
            )

    blocked = set(policy_blocked) | set(criteria.exclude_elements)
    allowed = (profile_allow | request_allow) - never  # the invariant, stated once where it matters
    blocked -= allowed

    effective = Effective(
        weights=weights,
        max_energy_above_hull=max_hull,
        min_band_gap=min_gap,
        max_elements=max_el,
        blocked_elements=frozenset(blocked),
        allowed_despite_tier=frozenset(allowed),
        include_elements=frozenset(criteria.include_elements),
        exclude_elements=frozenset(criteria.exclude_elements),
        top_k=criteria.top_k or config.output.top_k,
        on_missing_stability=config.gates.on_missing_stability,
        on_missing_band_gap=config.gates.on_missing_band_gap,
    )
    return effective, deviations
