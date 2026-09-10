"""Band gap correction. DFT (PBE/GGA) gaps underestimate experiment by roughly 30-50 %.

The strategy is a config option, the functional behind every value is recorded, and a
corrected value is always labelled as corrected. Nothing here is a hidden constant.
"""

from __future__ import annotations

from oxide_triage.config import BandGapConfig
from oxide_triage.schemas import BandGapAssessment, BandGapRecord, DataStatus


def is_hybrid(functional: str | None, hybrids: list[str]) -> bool:
    if not functional:
        return False
    f = functional.upper()
    return any(f.startswith(h.upper()) for h in hybrids)


def assess_band_gap(record: BandGapRecord, cfg: BandGapConfig) -> BandGapAssessment:
    strategy = cfg.correction.strategy
    reported = record.value_ev
    functional = record.functional

    if record.status != DataStatus.KNOWN or reported is None:
        return BandGapAssessment(
            reported_ev=None,
            reported_functional=functional,
            effective_ev=None,
            corrected=False,
            correction_strategy=strategy,
            correction_note="no band gap value available",
        )

    hybrid = is_hybrid(functional, cfg.hybrid_functionals)
    label = functional or "unknown"

    def scaled(reason: str) -> BandGapAssessment:
        factor = cfg.correction.scalar_factor
        return BandGapAssessment(
            reported_ev=reported,
            reported_functional=functional,
            effective_ev=round(reported * factor, 3),
            corrected=True,
            correction_strategy=strategy,
            correction_note=(
                f"CORRECTED: {label} value {reported:.2f} eV x {factor:g} = "
                f"{reported * factor:.2f} eV ({reason})"
            ),
        )

    def unchanged(reason: str) -> BandGapAssessment:
        return BandGapAssessment(
            reported_ev=reported,
            reported_functional=functional,
            effective_ev=reported,
            corrected=False,
            correction_strategy=strategy,
            correction_note=f"{label} value used as reported ({reason})",
        )

    if strategy == "none":
        return unchanged("correction strategy = none")
    if hybrid:
        return unchanged("hybrid functional; no correction applied")
    if strategy == "scalar_factor":
        return scaled("semi-local functional; scalar correction")
    # hse_preferred and no hybrid value available
    if cfg.correction.fallback == "scalar_factor":
        return scaled("no hybrid value available; fell back to scalar correction")
    return unchanged("no hybrid value available; fallback = none")
