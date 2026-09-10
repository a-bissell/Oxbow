"""Element-level hazard screen from the versioned table shipped in ``data/``."""

from __future__ import annotations

from typing import Any

from oxide_triage.config import HazardTable
from oxide_triage.schemas import DataStatus, HazardRecord, Provenance


def hazard_record(
    elements: list[str],
    table: HazardTable,
    pubchem_payload: dict[str, Any] | None = None,
    pubchem_retrieved_at: str | None = None,
) -> HazardRecord:
    cations = [el for el in elements if el != "O"]
    tiers: dict[str, int] = {}
    basis: dict[str, str] = {}
    for el in cations:
        tier, why, in_table = table.lookup(el)
        tiers[el] = tier
        basis[el] = why if in_table else "not in hazard table (default tier applied)"
    worst = max(tiers.values()) if tiers else 0
    worst_elements = sorted(el for el, t in tiers.items() if t == worst) if tiers else []

    rec = HazardRecord(
        element_tiers=tiers,
        element_basis=basis,
        worst_tier=worst,
        worst_elements=worst_elements,
        table_version=table.version,
        status=DataStatus.KNOWN,
        provenance=Provenance(source="element_table", source_id=f"element_hazards.yaml@{table.version}"),
    )
    if pubchem_payload is None:
        rec.pubchem_status = DataStatus.UNKNOWN
    elif pubchem_payload.get("found"):
        rec.pubchem_cid = pubchem_payload.get("cid")
        rec.ghs_hazard_codes = list(pubchem_payload.get("ghs_codes") or [])
        rec.pubchem_status = DataStatus.KNOWN
    else:
        rec.pubchem_status = DataStatus.NOT_APPLICABLE  # sought; PubChem has no record
    if pubchem_retrieved_at and rec.provenance:
        rec.provenance.note = f"PubChem lookup {pubchem_retrieved_at}"
    return rec
