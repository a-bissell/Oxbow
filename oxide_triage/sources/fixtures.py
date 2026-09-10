"""Load the synthetic development fixture into a cache.

The fixture (``data/fixtures/fixture_cache.json``) is a hand-written approximation of what
the public sources return for ~35 well-known oxides. It exists so the deterministic core,
templates, front end and tests run with no network and no API key.

IT IS NOT REAL DATA. Loading it sets a ``fixture_loaded`` flag in the cache; every record,
every provenance note and every rendered output then carries a fixture banner. The real
cache is built with ``oxide-triage warm-cache`` against the live sources.
"""

from __future__ import annotations

import json
from pathlib import Path

from oxide_triage.cache import FIXTURE_FLAG, Cache
from oxide_triage.config import DATA_DIR, Config, load_cation_allowlist
from oxide_triage.sources.materials_project import MaterialsProject

FIXTURE_PATH = DATA_DIR / "fixtures" / "fixture_cache.json"
FIXTURE_TS = "fixture"  # deliberately not a timestamp: never expires, never mistaken for live


def load_fixture(cache: Cache, config: Config, path: Path = FIXTURE_PATH) -> int:
    data = json.loads(path.read_text(encoding="utf-8"))
    materials = data["materials"]
    c = config.candidates
    cations = load_cation_allowlist(c.cation_allowlist_file)
    allowed = set(cations) | {"O"}
    mp = "materials_project"
    universe: list[str] = []

    for m in materials:
        mid = m["material_id"]
        summary = {
            k: m.get(k)
            for k in (
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
            )
        }
        cache.put(mp, f"summary:{mid}", summary, FIXTURE_TS)
        task_id = MaterialsProject.band_gap_task_id(summary)
        if task_id:
            cache.put(mp, f"task:{task_id}", {"run_type": m.get("run_type")}, FIXTURE_TS)
        diel = m.get("dielectric")
        cache.put(
            mp,
            f"dielectric:{mid}",
            {"found": False} if diel is None else {"found": True, **diel},
            FIXTURE_TS,
        )
        formula = m["formula_pretty"]
        oq = m.get("oqmd")
        # Formula-keyed records are shared between polymorphs; never let a polymorph without
        # a cross-check overwrite one that has it.
        if oq is not None or cache.get("oqmd", f"formula:{formula}") is None:
            cache.put(
                "oqmd",
                f"formula:{formula}",
                {"found": False, "n_entries": 0} if oq is None else {"found": True, "n_entries": 1, **oq},
                FIXTURE_TS,
            )
        lit = m.get("literature")
        if lit is not None:
            cache.put("openalex", f"formula:{formula}", lit, FIXTURE_TS)
        pc = m.get("pubchem")
        cache.put(
            "pubchem",
            f"formula:{formula}",
            {"found": False, "names_tried": [formula]} if pc is None else {"found": True, **pc},
            FIXTURE_TS,
        )
        # Mirror the server-side universe filter so the fixture path equals the live path.
        if (
            not summary.get("deprecated")
            and all(el in allowed for el in summary["elements"])
            and summary["nelements"] <= c.max_elements_query
            and (summary["energy_above_hull"] or 0.0) <= c.energy_above_hull_ceiling_ev_atom
            and (summary["band_gap"] or 0.0) >= c.min_reported_gap_ev
            and not (c.observed_only and summary.get("theoretical") is True)
        ):
            universe.append(mid)

    key = MaterialsProject.universe_key(
        cations,
        c.max_elements_query,
        c.energy_above_hull_ceiling_ev_atom,
        c.min_reported_gap_ev,
        c.observed_only,
    )
    cache.put(mp, key, {"material_ids": sorted(universe), "n_returned": len(materials)}, FIXTURE_TS)
    cache.set_meta(FIXTURE_FLAG, "1")
    cache.set_meta("fixture_version", str(data.get("version", "unversioned")))
    return len(materials)
