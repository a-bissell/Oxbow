"""Data layer entry point: turn cached/fetched source payloads into ``CandidateRecord``s.

This is the only place that knows the shape of each source's payload. Downstream code
sees typed records with explicit ``DataStatus`` on every field group.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any

from oxide_triage.cache import Cache
from oxide_triage.config import (
    Config,
    HazardTable,
    load_cation_allowlist,
    load_compound_aliases,
    load_hazard_table,
)
from oxide_triage.schemas import (
    BandGapRecord,
    CandidateRecord,
    CrossCheckRecord,
    DataStatus,
    DielectricRecord,
    LiteratureRecord,
    Provenance,
    StabilityRecord,
    WorkRef,
)
from oxide_triage.sources.hazards import hazard_record
from oxide_triage.sources.materials_project import THERMO_FUNCTIONAL_LABEL, MaterialsProject
from oxide_triage.sources.openalex import OpenAlex
from oxide_triage.sources.oqmd import OQMD
from oxide_triage.sources.pubchem import PubChem

log = logging.getLogger(__name__)


@dataclass
class DataLayer:
    config: Config
    cache: Cache
    mp: MaterialsProject
    oqmd: OQMD
    openalex: OpenAlex
    pubchem: PubChem
    hazard_table: HazardTable
    aliases: dict[str, list[str]]
    cations: list[str]
    warnings: list[str] = field(default_factory=list)

    @classmethod
    def from_config(cls, config: Config, cache: Cache | None = None, offline: bool | None = None) -> DataLayer:
        cache = cache or Cache(config.cache.path)
        off = config.cache.offline if offline is None else offline
        ttl = config.cache.ttl_days
        return cls(
            config=config,
            cache=cache,
            mp=MaterialsProject(cache, ttl, off),
            oqmd=OQMD(cache, ttl, off),
            openalex=OpenAlex(
                cache, ttl, off, sample_size=config.candidates.literature_sample_size,
                mailto=os.environ.get("OPENALEX_MAILTO"),
            ),
            pubchem=PubChem(cache, ttl, off),
            hazard_table=load_hazard_table(config.toxicity.table_file),
            aliases=load_compound_aliases(),
            cations=load_cation_allowlist(config.candidates.cation_allowlist_file),
        )

    @property
    def offline(self) -> bool:
        return self.mp.offline

    # ---- universe -------------------------------------------------------------------

    def universe_ids(self) -> list[str]:
        c = self.config.candidates
        ids, status = self.mp.fetch_universe(
            self.cations, c.max_elements_query, c.energy_above_hull_ceiling_ev_atom, c.min_reported_gap_ev
        )
        if ids:
            return ids
        # Offline or fetch failed: rebuild the universe from whatever summaries are cached,
        # applying the same filter locally. Say so.
        allowed = set(self.cations) | {"O"}
        rebuilt: list[str] = []
        for key in self.cache.keys(self.mp.name, "summary:"):
            hit = self.cache.get(self.mp.name, key)
            if hit is None:
                continue
            doc = hit[0]
            if doc.get("deprecated"):
                continue
            if any(el not in allowed for el in doc.get("elements", [])):
                continue
            if doc.get("nelements", 99) > c.max_elements_query:
                continue
            if (doc.get("energy_above_hull") or 0.0) > c.energy_above_hull_ceiling_ev_atom:
                continue
            if (doc.get("band_gap") or 0.0) < c.min_reported_gap_ev:
                continue
            rebuilt.append(doc["material_id"])
        if rebuilt:
            self.warnings.append(
                f"Candidate universe reconstructed from {len(rebuilt)} cached summaries "
                f"(live universe query status: {status})."
            )
        else:
            self.warnings.append(
                f"No candidates available (universe query status: {status}). "
                "Run `oxide-triage warm-cache` with MP_API_KEY set, or load fixtures for a demo."
            )
        return sorted(rebuilt)

    # ---- per-candidate assembly -----------------------------------------------------

    def build_candidates(self) -> list[CandidateRecord]:
        ids = self.universe_ids()
        if not ids:
            return []
        if not self.offline:
            self.mp.prefetch_dielectric(ids)
            task_ids = []
            for mid in ids:
                doc, _ = self.mp.summary(mid)
                if doc and (tid := MaterialsProject.band_gap_task_id(doc)):
                    task_ids.append(tid)
            self.mp.prefetch_run_types(task_ids)

        is_fixture = self.cache.has_fixture_data
        records: list[CandidateRecord] = []
        for mid in ids:
            doc, ts = self.mp.summary(mid)
            if doc is None:
                continue
            records.append(self._record(doc, ts, is_fixture))
        records.sort(key=lambda r: r.material_id)
        return records

    def _record(self, doc: dict[str, Any], ts: str | None, is_fixture: bool) -> CandidateRecord:
        mid = str(doc["material_id"])
        formula = str(doc.get("formula_pretty") or mid)
        elements = [str(e) for e in doc.get("elements", [])]
        symmetry = doc.get("symmetry") or {}
        src_note = "synthetic fixture record, NOT real data" if is_fixture else None
        mp_prov = Provenance(
            source="fixture" if is_fixture else self.mp.name,
            source_id=mid,
            retrieved_at=ts,
            url=None if is_fixture else f"https://next-gen.materialsproject.org/materials/{mid}",
            note=src_note,
        )

        e_hull = doc.get("energy_above_hull")
        stability = StabilityRecord(
            energy_above_hull_ev_atom=None if e_hull is None else float(e_hull),
            formation_energy_ev_atom=(
                None if doc.get("formation_energy_per_atom") is None else float(doc["formation_energy_per_atom"])
            ),
            is_stable=doc.get("is_stable"),
            functional=THERMO_FUNCTIONAL_LABEL,
            status=DataStatus.KNOWN if e_hull is not None else DataStatus.UNKNOWN,
            provenance=mp_prov,
        )

        gap = doc.get("band_gap")
        functional = "unknown"
        if gap is not None:
            task_id = MaterialsProject.band_gap_task_id(doc)
            if task_id:
                functional, _, _ = self.mp.run_type(task_id)
        band_gap = BandGapRecord(
            value_ev=None if gap is None else float(gap),
            functional=functional if gap is not None else None,
            is_direct=doc.get("is_gap_direct"),
            status=DataStatus.KNOWN if gap is not None else DataStatus.UNKNOWN,
            provenance=mp_prov,
        )

        diel_payload, diel_ts, _ = self.mp.dielectric(mid)
        if diel_payload and diel_payload.get("found") and diel_payload.get("e_total") is not None:
            dielectric = DielectricRecord(
                e_total=float(diel_payload["e_total"]),
                e_electronic=_opt_float(diel_payload.get("e_electronic")),
                e_ionic=_opt_float(diel_payload.get("e_ionic")),
                refractive_index=_opt_float(diel_payload.get("n")),
                status=DataStatus.KNOWN,
                provenance=mp_prov.model_copy(update={"retrieved_at": diel_ts, "note": src_note or "MP DFPT dataset"}),
            )
        else:
            dielectric = DielectricRecord(
                status=DataStatus.UNKNOWN,
                provenance=mp_prov.model_copy(
                    update={
                        "retrieved_at": diel_ts,
                        "note": "no DFPT dielectric record in MP" if diel_payload else "dielectric lookup unavailable",
                    }
                ),
            )

        oq_payload, oq_ts, _ = self.oqmd.lookup(formula)
        if oq_payload and oq_payload.get("found"):
            cross = CrossCheckRecord(
                stability_ev_atom=_opt_float(oq_payload.get("stability")),
                formation_energy_ev_atom=_opt_float(oq_payload.get("delta_e")),
                matched_formula=oq_payload.get("name"),
                status=DataStatus.KNOWN,
                provenance=Provenance(
                    source="fixture" if is_fixture else "oqmd",
                    source_id=str(oq_payload.get("entry_id")),
                    retrieved_at=oq_ts,
                    url=None if is_fixture else f"https://oqmd.org/materials/entry/{oq_payload.get('entry_id')}",
                    note=src_note,
                ),
            )
        else:
            cross = CrossCheckRecord(
                status=DataStatus.UNKNOWN,
                provenance=Provenance(
                    source="fixture" if is_fixture else "oqmd",
                    retrieved_at=oq_ts,
                    note="no OQMD entry for formula" if oq_payload else "OQMD lookup unavailable",
                ),
            )

        names = self.aliases.get(formula, [])
        lit_payload, lit_ts, _ = self.openalex.evidence(formula, names)
        if lit_payload:
            literature = LiteratureRecord(
                total_works=int(lit_payload.get("total_works", 0)),
                thin_film_works=int(lit_payload.get("thin_film_works", 0)),
                sample_works=[WorkRef(**w) for w in lit_payload.get("sample_works", [])],
                query_terms=list(lit_payload.get("query_terms", [])),
                status=DataStatus.KNOWN,
                provenance=Provenance(
                    source="fixture" if is_fixture else "openalex", retrieved_at=lit_ts, note=src_note
                ),
            )
        else:
            literature = LiteratureRecord(
                status=DataStatus.UNKNOWN,
                provenance=Provenance(source="openalex", retrieved_at=lit_ts, note="literature lookup unavailable"),
            )

        pc_payload, pc_ts, _ = self.pubchem.hazards(formula, names)
        hazard = hazard_record(elements, self.hazard_table, pc_payload, pc_ts)

        return CandidateRecord(
            material_id=mid,
            formula=formula,
            elements=elements,
            n_elements=int(doc.get("nelements") or len(elements)),
            crystal_system=symmetry.get("crystal_system"),
            spacegroup_symbol=symmetry.get("symbol"),
            theoretical=doc.get("theoretical"),
            stability=stability,
            band_gap=band_gap,
            dielectric=dielectric,
            cross_check=cross,
            literature=literature,
            hazard=hazard,
            is_fixture=is_fixture,
        )


def _opt_float(v: Any) -> float | None:
    return None if v is None else float(v)
