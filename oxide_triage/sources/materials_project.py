"""Materials Project client (REST, no pymatgen dependency).

Endpoints (https://api.materialsproject.org, header ``X-API-KEY``):
  * ``/materials/summary/``     thermo + electronic summary per material
  * ``/materials/dielectric/``  DFPT dielectric tensors (sparse coverage)
  * ``/materials/tasks/``       per-calculation ``run_type`` (the DFT functional)

Known domain surprises this module makes explicit rather than hiding:
  * ``band_gap`` in the summary is whatever functional MP chose for that material
    (GGA, GGA+U, increasingly r2SCAN; HSE is rare). We follow the ``electronic_structure``
    entry in ``origins`` to the task that produced the value and record its ``run_type``.
    Task ids there are opaque hashes (e.g. ``aaafsrgu``), which ``/materials/tasks/``
    accepts with or without the ``mp-`` prefix. If that lookup fails the functional is
    stored as ``unknown``, never guessed.
  * Dielectric coverage is a fraction of the database. Absence is cached as an explicit
    ``{"found": false}`` record so "unknown" has a retrieval timestamp too.
  * The thermodynamic hull in current MP mixes GGA/GGA+U/r2SCAN; the functional label on
    stability records says so.
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any

from oxide_triage.cache import Cache
from oxide_triage.sources.base import CachedSource, Http, SourceError

BASE_URL = "https://api.materialsproject.org"
THERMO_FUNCTIONAL_LABEL = "GGA/GGA+U/r2SCAN mixed hull (MP default)"
SUMMARY_FIELDS = [
    "material_id",
    "formula_pretty",
    "elements",
    "nelements",
    "symmetry",
    "energy_above_hull",
    "formation_energy_per_atom",
    "is_stable",
    "band_gap",
    "is_gap_direct",
    "is_metal",
    "theoretical",
    "deprecated",
    "origins",
    "last_updated",
]
DIELECTRIC_FIELDS = ["material_id", "e_total", "e_electronic", "e_ionic", "n"]
PAGE_SIZE = 1000

# Everything in the periodic table that is *not* an allowlisted cation and not oxygen is
# excluded server-side so we never pull hydroxides, carbonates, oxyhalides, etc.
ALL_ELEMENTS = (
    "H He Li Be B C N O F Ne Na Mg Al Si P S Cl Ar K Ca Sc Ti V Cr Mn Fe Co Ni Cu Zn Ga Ge As "
    "Se Br Kr Rb Sr Y Zr Nb Mo Tc Ru Rh Pd Ag Cd In Sn Sb Te I Xe Cs Ba La Ce Pr Nd Pm Sm Eu Gd "
    "Tb Dy Ho Er Tm Yb Lu Hf Ta W Re Os Ir Pt Au Hg Tl Pb Bi Po At Rn Fr Ra Ac Th Pa U Np Pu"
).split()


# The API caps `exclude_elements` at 60 characters (HTTP 422 otherwise; found on the first live
# warm). Send the exclusions that remove the most non-oxide chemistry server-side, in priority
# order, and rely on the local allowlist filter for the rest.
MAX_EXCLUDE_CHARS = 60
EXCLUDE_PRIORITY = (
    "H C N F S Cl Br I Se As Ag Pd Pt Ru Rh Ir Os Au Re Tc Hg Pm Po Ra Ac Pa Np Pu He Ne Ar Kr Xe Rn At Fr"
).split()


def server_side_exclusions(allowed: set[str], max_chars: int = MAX_EXCLUDE_CHARS) -> str:
    """Comma-joined exclusion list that fits the API limit, most valuable exclusions first."""
    ordered = [el for el in EXCLUDE_PRIORITY if el not in allowed]
    ordered += [el for el in ALL_ELEMENTS if el not in allowed and el not in ordered]
    out: list[str] = []
    for el in ordered:
        candidate = ",".join([*out, el])
        if len(candidate) > max_chars:
            break
        out.append(el)
    return ",".join(out)


def normalize_run_type(run_type: str | None) -> str:
    if not run_type:
        return "unknown"
    rt = str(run_type).strip()
    upper = rt.upper().replace("_", "+")
    mapping = {
        "GGA": "GGA",
        "GGA+U": "GGA+U",
        "PBE": "GGA",
        "PBE+U": "GGA+U",
        "R2SCAN": "r2SCAN",
        "SCAN": "SCAN",
        "HSE06": "HSE06",
        "HSE": "HSE06",
        "PBESOL": "PBEsol",
    }
    return mapping.get(upper, rt)


class MaterialsProject(CachedSource):
    name = "materials_project"

    def __init__(
        self,
        cache: Cache,
        ttl_days: int = 90,
        offline: bool = False,
        api_key: str | None = None,
        http: Http | None = None,
    ):
        super().__init__(cache, ttl_days, offline)
        self.api_key = api_key if api_key is not None else os.environ.get("MP_API_KEY", "")
        self.http = http or Http(user_agent="oxide-triage/0.1 (materials-project-client)")

    # ---- HTTP -----------------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        if not self.api_key:
            raise SourceError("MP_API_KEY is not set; cannot query Materials Project")
        return {"X-API-KEY": self.api_key}

    def _paged(self, path: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        skip = 0
        while True:
            page = self.http.get_json(
                f"{BASE_URL}{path}",
                params={**params, "_limit": PAGE_SIZE, "_skip": skip},
                headers=self._headers(),
            )
            if page is None:
                break
            data = page.get("data", []) if isinstance(page, dict) else page
            out.extend(data)
            if len(data) < PAGE_SIZE:
                break
            skip += PAGE_SIZE
        return out

    # ---- candidate universe ---------------------------------------------------------

    @staticmethod
    def universe_key(cations: list[str], max_elements: int, hull_ceiling: float, min_gap: float) -> str:
        spec = json.dumps(
            {"cations": sorted(cations), "max_el": max_elements, "hull": hull_ceiling, "gap": min_gap},
            sort_keys=True,
        )
        return "universe:" + hashlib.sha256(spec.encode()).hexdigest()[:12]

    def fetch_universe(
        self, cations: list[str], max_elements: int, hull_ceiling: float, min_gap: float
    ) -> tuple[list[str], str]:
        """Return (material_ids, status). Summaries are cached one row per material."""
        key = self.universe_key(cations, max_elements, hull_ceiling, min_gap)
        allowed = set(cations) | {"O"}
        excluded = server_side_exclusions(allowed)

        def fetch() -> dict[str, Any]:
            docs = self._paged(
                "/materials/summary/",
                {
                    "elements": "O",
                    "exclude_elements": excluded,
                    "nelements_min": 2,
                    "nelements_max": max_elements,
                    "energy_above_hull_max": hull_ceiling,
                    "band_gap_min": min_gap,
                    "deprecated": "false",
                    "_fields": ",".join(SUMMARY_FIELDS),
                },
            )
            ids = []
            for doc in docs:
                # The local allowlist filter is authoritative: the server-side exclusion list
                # is truncated to the API's 60-character limit.
                if any(el not in allowed for el in doc.get("elements", [])):
                    continue
                self.cache.put(self.name, f"summary:{doc['material_id']}", doc)
                ids.append(doc["material_id"])
            if not ids:
                raise SourceError("universe query returned no candidates; not caching an empty universe")
            return {"material_ids": sorted(ids), "n_returned": len(docs)}

        payload, _ts, status = self.cached(key, fetch)
        extra = self.on_demand_ids(cations, max_elements, hull_ceiling, min_gap)
        if payload is None:
            return sorted(extra), status
        return sorted(set(payload["material_ids"]) | set(extra)), status

    def fetch_by_formula(self, formula: str, allowed_elements: set[str]) -> list[str]:
        """Pull every non-deprecated MP entry for a reduced formula into the cache (online only).
        Returns the material ids stored. Used for on-demand additions to the universe."""
        if self.offline:
            raise SourceError("offline mode: cannot fetch new materials")
        docs = self._paged(
            "/materials/summary/",
            {"formula": formula, "deprecated": "false", "_fields": ",".join(SUMMARY_FIELDS)},
        )
        ids: list[str] = []
        for doc in docs:
            if any(el not in allowed_elements for el in doc.get("elements", [])):
                continue
            self.cache.put(self.name, f"summary:{doc['material_id']}", doc)
            ids.append(str(doc["material_id"]))
        self.cache.log(self.name, f"formula:{formula}", "fetched", f"{len(ids)} entries")
        return sorted(ids)

    def add_to_universe(
        self, cations: list[str], max_elements: int, hull_ceiling: float, min_gap: float, ids: list[str]
    ) -> int:
        """Record on-demand additions for these universe parameters. They live in a separate
        ``ondemand:`` row and are merged in by ``fetch_universe``, so they can never stand in for,
        or mask, the real universe query."""
        key = "ondemand:" + self.universe_key(cations, max_elements, hull_ceiling, min_gap)
        hit = self.cache.get(self.name, key)
        current = set(hit[0].get("material_ids", [])) if hit else set()
        new = [i for i in ids if i not in current]
        if new:
            self.cache.put(self.name, key, {"material_ids": sorted(current | set(new))})
        return len(new)

    def on_demand_ids(
        self, cations: list[str], max_elements: int, hull_ceiling: float, min_gap: float
    ) -> list[str]:
        key = "ondemand:" + self.universe_key(cations, max_elements, hull_ceiling, min_gap)
        hit = self.cache.get(self.name, key)
        return list(hit[0].get("material_ids", [])) if hit else []

    def refresh_functional(self, material_id: str) -> str:
        """Acquisition route for an unresolved band-gap functional: re-fetch the summary (origins
        may have been absent or stale) and the producing task's run_type. Returns the functional
        label, ``unknown`` if it still cannot be resolved."""
        if self.offline:
            return "unknown"
        page = self.http.get_json(
            f"{BASE_URL}/materials/summary/",
            params={"material_ids": material_id, "_fields": ",".join(SUMMARY_FIELDS)},
            headers=self._headers(),
        )
        docs = (page or {}).get("data", []) if isinstance(page, dict) else []
        if not docs:
            return "unknown"
        doc = docs[0]
        self.cache.put(self.name, f"summary:{material_id}", doc)
        task_id = self.band_gap_task_id(doc)
        if not task_id:
            return "unknown"
        page = self.http.get_json(
            f"{BASE_URL}/materials/tasks/",
            params={"task_ids": task_id, "_fields": "task_id,run_type"},
            headers=self._headers(),
        )
        data = (page or {}).get("data", []) if isinstance(page, dict) else []
        run_type = data[0].get("run_type") if data else None
        self.cache.put(self.name, f"task:{task_id}", {"run_type": run_type, "route": "refresh"})
        return normalize_run_type(run_type)

    def summary(self, material_id: str) -> tuple[dict[str, Any] | None, str | None]:
        hit = self.cache.get(self.name, f"summary:{material_id}")
        return (None, None) if hit is None else (hit[0], hit[1])

    # ---- dielectric -----------------------------------------------------------------

    def dielectric(self, material_id: str) -> tuple[dict[str, Any] | None, str | None, str]:
        def fetch() -> dict[str, Any]:
            page = self.http.get_json(
                f"{BASE_URL}/materials/dielectric/",
                params={"material_ids": material_id, "_fields": ",".join(DIELECTRIC_FIELDS)},
                headers=self._headers(),
            )
            data = (page or {}).get("data", []) if isinstance(page, dict) else []
            if not data:
                return {"found": False}
            doc = data[0]
            return {
                "found": True,
                "e_total": doc.get("e_total"),
                "e_electronic": doc.get("e_electronic"),
                "e_ionic": doc.get("e_ionic"),
                "n": doc.get("n"),
            }

        return self.cached(f"dielectric:{material_id}", fetch)

    def prefetch_dielectric(self, material_ids: list[str]) -> None:
        """Batch the dielectric lookups (one request per 100 ids) into the cache."""
        missing = [m for m in material_ids if self.cache.get(self.name, f"dielectric:{m}") is None]
        if self.offline or not missing:
            return
        for i in range(0, len(missing), 100):
            chunk = missing[i : i + 100]
            try:
                page = self.http.get_json(
                    f"{BASE_URL}/materials/dielectric/",
                    params={
                        "material_ids": ",".join(chunk),
                        "_fields": ",".join(DIELECTRIC_FIELDS),
                        "_limit": len(chunk),
                    },
                    headers=self._headers(),
                )
            except SourceError:
                return  # per-material fallback path will record the failure
            found = {d["material_id"]: d for d in (page or {}).get("data", [])}
            for m in chunk:
                doc = found.get(m)
                payload = (
                    {"found": False}
                    if doc is None
                    else {
                        "found": True,
                        "e_total": doc.get("e_total"),
                        "e_electronic": doc.get("e_electronic"),
                        "e_ionic": doc.get("e_ionic"),
                        "n": doc.get("n"),
                    }
                )
                self.cache.put(self.name, f"dielectric:{m}", payload)

    # ---- functional behind the band gap ---------------------------------------------

    # Origin names that identify the calculation the summary band gap came from. The live API
    # labels it ``electronic_structure`` (there is no ``band_gap`` origin); the older name is
    # kept as a fallback so synthetic fixtures and any future rename keep resolving.
    BAND_GAP_ORIGIN_NAMES = ("electronic_structure", "band_gap")

    @classmethod
    def band_gap_task_id(cls, summary_doc: dict[str, Any]) -> str | None:
        origins = summary_doc.get("origins") or []
        for name in cls.BAND_GAP_ORIGIN_NAMES:
            for origin in origins:
                if origin.get("name") == name and origin.get("task_id"):
                    return str(origin["task_id"])
        return None

    def run_type(self, task_id: str) -> tuple[str, str | None, str]:
        """Return (normalized functional, retrieved_at, status)."""

        def fetch() -> dict[str, Any]:
            page = self.http.get_json(
                f"{BASE_URL}/materials/tasks/",
                params={"task_ids": task_id, "_fields": "task_id,run_type"},
                headers=self._headers(),
            )
            data = (page or {}).get("data", []) if isinstance(page, dict) else []
            if not data:
                return {"run_type": None}
            return {"run_type": data[0].get("run_type")}

        payload, ts, status = self.cached(f"task:{task_id}", fetch)
        if payload is None:
            return "unknown", ts, status
        return normalize_run_type(payload.get("run_type")), ts, status

    def prefetch_run_types(self, task_ids: list[str]) -> None:
        missing = [t for t in task_ids if self.cache.get(self.name, f"task:{t}") is None]
        if self.offline or not missing:
            return
        for i in range(0, len(missing), 100):
            chunk = missing[i : i + 100]
            try:
                page = self.http.get_json(
                    f"{BASE_URL}/materials/tasks/",
                    params={"task_ids": ",".join(chunk), "_fields": "task_id,run_type", "_limit": len(chunk)},
                    headers=self._headers(),
                )
            except SourceError:
                return
            found = {d["task_id"]: d for d in (page or {}).get("data", [])}
            for t in chunk:
                doc = found.get(t)
                self.cache.put(
                    self.name, f"task:{t}", {"run_type": None if doc is None else doc.get("run_type")}
                )
