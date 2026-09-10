"""Data layer entry point: turn cached/fetched source payloads into ``CandidateRecord``s.

This is the only place that knows the shape of each source's payload. Downstream code
sees typed records with explicit ``DataStatus`` on every field group.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
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
    HazardRecord,
    LiteratureRecord,
    Provenance,
    StabilityRecord,
    WorkRef,
)
from oxide_triage.sources.base import Http, SourceError, status_for
from oxide_triage.sources.hazards import hazard_record
from oxide_triage.sources.materials_project import THERMO_FUNCTIONAL_LABEL, MaterialsProject
from oxide_triage.sources.openalex import OpenAlex
from oxide_triage.sources.oqmd import OQMD
from oxide_triage.sources.pubchem import PubChem


def _why(status: DataStatus, absent_note: str, unretrieved_note: str) -> str:
    """Pick the note that explains *why* a value is missing."""
    return absent_note if status is DataStatus.ABSENT else unretrieved_note


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
    def from_config(
        cls,
        config: Config,
        cache: Cache | None = None,
        offline: bool | None = None,
        http: Any | None = None,
    ) -> DataLayer:
        """``http`` lets every client share one transport, e.g. a ``ReplayHttp`` over recorded
        responses in tests; by default each client builds its own live ``Http``."""
        cache = cache or Cache(config.cache.path)
        off = config.cache.offline if offline is None else offline
        ttl = config.cache.ttl_days
        c = config.candidates

        def live(name: str, **kw: Any) -> Any:
            """One rate-capped Http per source unless a shared transport was supplied."""
            return http if http is not None else Http(max_rps=c.max_rps_for(name), **kw)

        return cls(
            config=config,
            cache=cache,
            mp=MaterialsProject(
                cache,
                ttl,
                off,
                http=live("materials_project", user_agent="oxide-triage/0.1 (materials-project-client)"),
            ),
            oqmd=OQMD(
                cache, ttl, off, http=live("oqmd", timeout_s=60, user_agent="oxide-triage/0.1 (oqmd-client)")
            ),
            openalex=OpenAlex(
                cache,
                ttl,
                off,
                http=live("openalex", user_agent="oxide-triage/0.1 (openalex-client)"),
                sample_size=c.literature_sample_size,
                mailto=os.environ.get("OPENALEX_MAILTO"),
                api_key=os.environ.get("OPENALEX_API_KEY"),
            ),
            pubchem=PubChem(
                cache, ttl, off, http=live("pubchem", user_agent="oxide-triage/0.1 (pubchem-client)")
            ),
            hazard_table=load_hazard_table(config.toxicity.table_file),
            aliases=load_compound_aliases(),
            cations=load_cation_allowlist(config.candidates.cation_allowlist_file),
        )

    @property
    def offline(self) -> bool:
        return self.mp.offline

    def close(self) -> None:
        """Release the HTTP clients (a shared transport is closed once)."""
        seen: set[int] = set()
        for client in (self.mp, self.oqmd, self.openalex, self.pubchem):
            http = getattr(client, "http", None)
            if http is not None and id(http) not in seen and hasattr(http, "close"):
                seen.add(id(http))
                http.close()

    # ---- universe -------------------------------------------------------------------

    def universe_ids(self) -> list[str]:
        c = self.config.candidates
        ids, status = self.mp.fetch_universe(
            self.cations,
            c.max_elements_query,
            c.energy_above_hull_ceiling_ev_atom,
            c.min_reported_gap_ev,
            c.observed_only,
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
            if c.observed_only and doc.get("theoretical") is True:
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

    # ---- concurrent prefetch of formula-keyed sources -------------------------------

    @property
    def formula_sources(self) -> set[str]:
        return {self.oqmd.name, self.openalex.name, self.pubchem.name}

    @property
    def warm_fetches_formula_sources(self) -> bool:
        return self.config.candidates.formula_sources == "warm"

    def formula_sources_for_warm(self) -> set[str]:
        """Sources fetched for every formula during a warm: all three formula-keyed sources under
        ``candidates.formula_sources: warm``, none under ``on_demand`` (the query path fills the
        top-ranked pool instead, see ``fill_formula_sources``)."""
        return self.formula_sources if self.warm_fetches_formula_sources else set()

    def prefetch_formula_sources(
        self, formulas: list[str], workers: int = 4, sources: set[str] | None = None
    ) -> dict[str, int]:
        """Fetch OQMD, OpenAlex and PubChem records (or the subset in ``sources``) for every
        formula not yet cached, with a bounded thread pool per source. Threads do HTTP only; the
        main thread writes the cache (SQLite connections are not shared across threads).
        Polymorphs share a formula, so each formula is fetched once. Failures leave no row; the
        sequential path retries them once."""
        if self.offline or not formulas or workers <= 0:
            return {}
        want = sources if sources is not None else {self.oqmd.name, self.openalex.name, self.pubchem.name}
        # per-source pool sizes; a source set to 0 workers is left to the sequential path
        c = self.config.candidates
        pool_size = {src: (c.workers_for(src) if src in c.fetch else workers) for src in want}
        want = {src for src in want if pool_size[src] > 0}
        jobs: list[tuple[str, str, Callable[[], dict]]] = []
        for f in dict.fromkeys(formulas):  # dedupe, keep order: polymorphs share a formula
            names = self.aliases.get(f, [])
            if self.oqmd.name in want and self.cache.get(self.oqmd.name, f"formula:{f}") is None:
                jobs.append((self.oqmd.name, f, lambda f=f: self.oqmd.fetch_composition(f)))
            if self.openalex.name in want and self.cache.get(self.openalex.name, f"formula:{f}") is None:
                jobs.append((self.openalex.name, f, lambda f=f, n=names: self.openalex.fetch_evidence(f, n)))
            if self.pubchem.name in want and self.cache.get(self.pubchem.name, f"formula:{f}") is None:
                jobs.append((self.pubchem.name, f, lambda f=f, n=names: self.pubchem.fetch_hazards(f, n)))
        if not jobs:
            return {}
        counts = {"fetched": 0, "failed": 0}
        started = time.monotonic()
        limits = ", ".join(
            f"{src} x{pool_size[src]}" + (f" @{c.max_rps_for(src):g}/s" if c.max_rps_for(src) else "")
            for src in sorted(want)
        )
        log.info(
            "prefetch: %d requests across %d formulas (workers per source: %s)",
            len(jobs),
            len(formulas),
            limits,
        )
        # one pool per source so a slow source cannot starve the others
        by_source: dict[str, list[tuple[str, str, Callable[[], dict]]]] = {}
        for job in jobs:
            by_source.setdefault(job[0], []).append(job)
        pools = {
            src: ThreadPoolExecutor(max_workers=pool_size[src], thread_name_prefix=src) for src in by_source
        }
        try:
            futures = {}
            for src, src_jobs in by_source.items():
                for source, formula, fn in src_jobs:
                    futures[pools[src].submit(fn)] = (source, formula)
            done = 0
            for fut in as_completed(futures):
                source, formula = futures[fut]
                done += 1
                try:
                    payload = fut.result()
                except SourceError as exc:
                    counts["failed"] += 1
                    self.cache.log(source, f"formula:{formula}", "fetch_failed", str(exc))
                except Exception as exc:  # a bad response shape must not kill the warm
                    counts["failed"] += 1
                    self.cache.log(
                        source, f"formula:{formula}", "fetch_failed", f"{type(exc).__name__}: {exc}"
                    )
                else:
                    self.cache.put(source, f"formula:{formula}", payload)
                    self.cache.log(source, f"formula:{formula}", "fetched")
                    counts["fetched"] += 1
                if done % 100 == 0 or done == len(futures):
                    elapsed = time.monotonic() - started
                    eta = elapsed / done * (len(futures) - done)
                    log.info(
                        "  prefetch %d/%d  ok %d  failed %d  elapsed %dm%02ds  eta %dm%02ds",
                        done,
                        len(futures),
                        counts["fetched"],
                        counts["failed"],
                        elapsed // 60,
                        elapsed % 60,
                        eta // 60,
                        eta % 60,
                    )
        finally:
            for pool in pools.values():
                pool.shutdown(wait=True)
        return counts

    # ---- per-candidate assembly -----------------------------------------------------

    def build_candidates(self) -> list[CandidateRecord]:
        ids = self.universe_ids()
        if not ids:
            return []
        if not self.offline:
            self.mp.prefetch_dielectric(ids)
            task_ids = []
            formulas: list[str] = []
            for mid in ids:
                doc, _ = self.mp.summary(mid)
                if doc and (tid := MaterialsProject.band_gap_task_id(doc)):
                    task_ids.append(tid)
                if doc and doc.get("formula_pretty") and doc["formula_pretty"] not in formulas:
                    formulas.append(str(doc["formula_pretty"]))
            self.mp.prefetch_run_types(task_ids)
            self.prefetch_formula_sources(
                formulas,
                workers=self.config.candidates.fetch_workers,
                sources=self.formula_sources_for_warm(),
            )

        is_fixture = self.cache.has_fixture_data
        records: list[CandidateRecord] = []
        total = len(ids)
        started = time.monotonic()
        if not self.offline:
            warmed = ", ".join(sorted(self.formula_sources_for_warm()))
            log.info(
                "universe: %d candidates; assembling records (%s)",
                total,
                f"per-formula sources warmed: {warmed}"
                if warmed
                else "formula sources on demand at query time",
            )
        for i, mid in enumerate(ids, 1):
            doc, ts = self.mp.summary(mid)
            if doc is None:
                continue
            records.append(self._record(doc, ts, is_fixture))
            if not self.offline and (i % 25 == 0 or i == total):
                elapsed = time.monotonic() - started
                eta = elapsed / i * (total - i)
                log.info(
                    "  %d/%d %s  elapsed %dm%02ds  eta %dm%02ds",
                    i,
                    total,
                    records[-1].formula,
                    elapsed // 60,
                    elapsed % 60,
                    eta // 60,
                    eta % 60,
                )
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
                None
                if doc.get("formation_energy_per_atom") is None
                else float(doc["formation_energy_per_atom"])
            ),
            is_stable=doc.get("is_stable"),
            functional=THERMO_FUNCTIONAL_LABEL,
            # the summary doc was retrieved; a null field is the source's answer, not a gap
            status=DataStatus.KNOWN if e_hull is not None else DataStatus.ABSENT,
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
            status=DataStatus.KNOWN if gap is not None else DataStatus.ABSENT,
            provenance=mp_prov,
        )

        diel_payload, diel_ts, diel_fetch = self.mp.dielectric(mid)
        diel_found = bool(
            diel_payload and diel_payload.get("found") and diel_payload.get("e_total") is not None
        )
        diel_status = status_for(diel_fetch, diel_found)
        if diel_found and diel_payload is not None:
            dielectric = DielectricRecord(
                e_total=float(diel_payload["e_total"]),
                e_electronic=_opt_float(diel_payload.get("e_electronic")),
                e_ionic=_opt_float(diel_payload.get("e_ionic")),
                refractive_index=_opt_float(diel_payload.get("n")),
                status=DataStatus.KNOWN,
                provenance=mp_prov.model_copy(
                    update={"retrieved_at": diel_ts, "note": src_note or "MP DFPT dataset"}
                ),
            )
        else:
            dielectric = DielectricRecord(
                status=diel_status,
                provenance=mp_prov.model_copy(
                    update={
                        "retrieved_at": diel_ts,
                        "note": _why(
                            diel_status,
                            "no DFPT dielectric record in MP for this material",
                            f"MP dielectric lookup never completed here ({diel_fetch}); "
                            "this is a gap in the cache, not in MP",
                        ),
                    }
                ),
            )

        warm = self.warm_fetches_formula_sources
        cross = self._cross_check(formula, is_fixture, fetch=warm)

        names = self.aliases.get(formula, [])
        literature = self._literature(formula, names, is_fixture, fetch=warm)
        hazard = self._hazard(elements, formula, names, fetch=warm)

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

    # ---- formula-keyed records (OQMD, OpenAlex, PubChem) ------------------------------
    #
    # Each helper reads the cache and, with ``fetch=True``, goes to the source when nothing is
    # cached. ``fetch=False`` never leaves the cache: that is candidate assembly under
    # ``candidates.formula_sources: on_demand``, where the warm has not touched these sources
    # and the query path fills the top-ranked pool afterwards (``fill_formula_sources``).

    def _on_demand_note(self, what: str) -> str:
        return (
            f"{what} not fetched at warm; queried on demand for the top "
            f"{self.config.candidates.on_demand_pool} ranked candidates at query time"
        )

    def _cross_check(self, formula: str, is_fixture: bool, fetch: bool) -> CrossCheckRecord:
        src_note = "synthetic fixture record, NOT real data" if is_fixture else None
        if fetch:
            oq_payload, oq_ts, oq_fetch = self.oqmd.lookup(formula)
        else:
            oq_payload, oq_ts, oq_fetch = self.oqmd.peek(f"formula:{formula}")
        oq_status = status_for(oq_fetch, bool(oq_payload and oq_payload.get("found")))
        if oq_status is DataStatus.KNOWN and oq_payload is not None:
            # OQMD reports `stability` <= 0 for phases on its hull (depth below the competing
            # phases); MP's energy_above_hull is >= 0. Clamp so the two are comparable.
            raw_stab = _opt_float(oq_payload.get("stability"))
            clamped = None if raw_stab is None else max(0.0, raw_stab)
            clamp_note = (
                f"OQMD stability {raw_stab:.3f} eV/atom (negative = below competing phases) taken as on-hull"
                if raw_stab is not None and raw_stab < 0
                else None
            )
            return CrossCheckRecord(
                stability_ev_atom=clamped,
                formation_energy_ev_atom=_opt_float(oq_payload.get("delta_e")),
                matched_formula=oq_payload.get("name"),
                status=DataStatus.KNOWN,
                provenance=Provenance(
                    source="fixture" if is_fixture else "oqmd",
                    source_id=str(oq_payload.get("entry_id")),
                    retrieved_at=oq_ts,
                    url=None
                    if is_fixture
                    else f"https://oqmd.org/materials/entry/{oq_payload.get('entry_id')}",
                    note=_join_notes(
                        src_note,
                        f"matched via chemical-system query ({oq_payload.get('filter')})"
                        if oq_payload.get("route") == "chemsys"
                        else None,
                        clamp_note,
                    ),
                ),
            )
        else:
            return CrossCheckRecord(
                status=oq_status,
                provenance=Provenance(
                    source="fixture" if is_fixture else "oqmd",
                    retrieved_at=oq_ts,
                    note=_why(
                        oq_status,
                        "no OQMD entry for this formula: stability rests on one source",
                        self._on_demand_note("OQMD cross-check")
                        if oq_fetch == "not_fetched" and not self.warm_fetches_formula_sources
                        else f"OQMD was never successfully queried for this formula here ({oq_fetch}); "
                        "no cross-check was attempted, so agreement is untested rather than absent",
                    ),
                ),
            )

    def _hazard(self, elements: list[str], formula: str, names: list[str], fetch: bool) -> HazardRecord:
        """Element-table screen (always known) plus PubChem's compound record when available."""
        if fetch:
            pc_payload, pc_ts, pc_fetch = self.pubchem.hazards(formula, names)
        else:
            pc_payload, pc_ts, pc_fetch = self.pubchem.peek(f"formula:{formula}")
        rec = hazard_record(elements, self.hazard_table, pc_payload, pc_ts, pc_fetch)
        if pc_fetch == "not_fetched" and not self.warm_fetches_formula_sources and rec.provenance:
            rec.provenance.note = self._on_demand_note("PubChem compound record")
        return rec

    def _literature(self, formula: str, names: list[str], is_fixture: bool, fetch: bool) -> LiteratureRecord:
        """Literature record from the cache; ``fetch=True`` goes to OpenAlex when nothing is
        cached (warm mode, and the on-demand fill), ``fetch=False`` never leaves the cache."""
        src_note = "synthetic fixture record, NOT real data" if is_fixture else None
        if fetch:
            lit_payload, lit_ts, lit_fetch = self.openalex.evidence(formula, names)
        else:
            lit_payload, lit_ts, lit_fetch = self.openalex.evidence_cached(formula)
        if lit_payload:
            return LiteratureRecord(
                total_works=int(lit_payload.get("total_works", 0)),
                thin_film_works=int(lit_payload.get("thin_film_works", 0)),
                sample_works=[WorkRef(**w) for w in lit_payload.get("sample_works", [])],
                query_terms=list(lit_payload.get("query_terms", [])),
                status=DataStatus.KNOWN,
                provenance=Provenance(
                    source="fixture" if is_fixture else "openalex",
                    retrieved_at=lit_ts,
                    note=_join_notes(
                        src_note,
                        "counts from common-name search only (formula string returned nothing)"
                        if lit_payload.get("route") == "names_only"
                        else None,
                    ),
                ),
            )
        # A literature hole is almost never a fact about OpenAlex: an empty result still comes
        # back as a payload with zero works. Reaching here means we did not get an answer.
        status = status_for(lit_fetch, False)
        if fetch or self.warm_fetches_formula_sources or lit_fetch != "not_fetched":
            note = _why(
                status,
                "OpenAlex holds no work matching this formula or its names",
                f"OpenAlex was never successfully queried for this formula here ({lit_fetch})",
            )
        else:
            note = self._on_demand_note("Literature counts")
        return LiteratureRecord(
            status=status,
            provenance=Provenance(source="openalex", retrieved_at=lit_ts, note=note),
        )

    # ---- on-demand fill (query time) --------------------------------------------------

    @staticmethod
    def needs_formula_sources(record: CandidateRecord) -> bool:
        """True when any formula-keyed source was never successfully queried for this record."""
        return (
            record.cross_check.status is DataStatus.NOT_RETRIEVED
            or record.literature.status is DataStatus.NOT_RETRIEVED
            or record.hazard.pubchem_status is DataStatus.NOT_RETRIEVED
        )

    def fill_formula_sources(
        self, records: list[CandidateRecord]
    ) -> tuple[list[CandidateRecord], dict[str, int]]:
        """Fetch OQMD, OpenAlex and PubChem for a small set of already-ranked candidates and return
        them with those records rebuilt. Cached formulas cost nothing; polymorphs share one fetch
        per source; the alternative routes of the acquisition ladder are applied inline where the
        first query answers with nothing (chemical-system match for OQMD, common-name search for
        OpenAlex). Offline, records come back unchanged. The caller re-ranks and, because a
        cross-check can lower a score, repeats until the top of the ranking is settled."""
        if self.offline:
            return records, {"skipped_offline": len(records)}
        todo = [r for r in records if self.needs_formula_sources(r)]
        formulas = list(dict.fromkeys(r.formula for r in todo))
        counts = (
            self.prefetch_formula_sources(
                formulas, workers=self.config.candidates.fetch_workers, sources=self.formula_sources
            )
            if formulas
            else {}
        )
        out: list[CandidateRecord] = []
        for r in records:
            if not self.needs_formula_sources(r):
                out.append(r)
                continue
            names = self.aliases.get(r.formula, [])
            cross = r.cross_check
            if cross.status is DataStatus.NOT_RETRIEVED:
                cross = self._cross_check(r.formula, r.is_fixture, fetch=True)
                if cross.status is DataStatus.ABSENT:
                    payload, _, _ = self.oqmd.lookup_by_chemsys(r.formula)
                    if payload and payload.get("found"):
                        cross = self._cross_check(r.formula, r.is_fixture, fetch=False)
            lit = r.literature
            if lit.status is DataStatus.NOT_RETRIEVED:
                lit = self._literature(r.formula, names, r.is_fixture, fetch=True)
                if lit.status is DataStatus.KNOWN and lit.total_works == 0 and names:
                    payload, _, _ = self.openalex.evidence_names_only(r.formula, names)
                    if payload:
                        lit = self._literature(r.formula, names, r.is_fixture, fetch=False)
            hazard = r.hazard
            if hazard.pubchem_status is DataStatus.NOT_RETRIEVED:
                hazard = self._hazard(r.elements, r.formula, names, fetch=True)
            out.append(r.model_copy(update={"cross_check": cross, "literature": lit, "hazard": hazard}))
        counts["resolved"] = sum(1 for r in out if not self.needs_formula_sources(r))
        return out, counts


def _join_notes(*notes: str | None) -> str | None:
    parts = [n for n in notes if n]
    return "; ".join(parts) if parts else None


def _opt_float(v: Any) -> float | None:
    return None if v is None else float(v)
