"""OQMD (Open Quantum Materials Database) cross-check client.

REST: ``https://oqmd.org/oqmdapi/formationenergy?composition=HfO2&fields=...``
Returns ``{"data": [{"name", "entry_id", "delta_e", "stability", "spacegroup"}, ...]}``.

We use OQMD only as an *independent* stability opinion. Agreement with Materials Project
is evidence; disagreement is surfaced as a caveat. OQMD's ``stability`` is the distance to
its own convex hull in eV/atom (0 = on the hull); ``delta_e`` is the formation energy.
"""

from __future__ import annotations

from typing import Any

from oxide_triage.cache import Cache
from oxide_triage.formula import elements_of, same_stoichiometry
from oxide_triage.sources.base import CachedSource, Http, SourceError

BASE_URL = "https://oqmd.org/oqmdapi/formationenergy"
FIELDS = "name,entry_id,delta_e,stability,spacegroup"


class OQMD(CachedSource):
    name = "oqmd"

    def __init__(self, cache: Cache, ttl_days: int = 90, offline: bool = False, http: Http | None = None):
        super().__init__(cache, ttl_days, offline)
        self.http = http or Http(timeout_s=60, user_agent="oxide-triage/0.1 (oqmd-client)")

    def fetch_composition(self, formula: str) -> dict[str, Any]:
        """HTTP only (thread-safe, no cache writes): best OQMD entry for a reduced formula."""
        page = self.http.get_json(BASE_URL, params={"composition": formula, "fields": FIELDS, "limit": 50})
        entries = (page or {}).get("data", []) if isinstance(page, dict) else []
        usable = [e for e in entries if e.get("stability") is not None]
        if not usable:
            return {"found": False, "n_entries": len(entries)}
        best = min(usable, key=lambda e: float(e["stability"]))
        return {
            "found": True,
            "n_entries": len(entries),
            "entry_id": best.get("entry_id"),
            "name": best.get("name"),
            "stability": float(best["stability"]),
            "delta_e": None if best.get("delta_e") is None else float(best["delta_e"]),
            "spacegroup": best.get("spacegroup"),
        }

    def lookup(self, formula: str) -> tuple[dict[str, Any] | None, str | None, str]:
        """Best (lowest-stability) OQMD entry for a reduced formula, cached as one record."""
        return self.cached(f"formula:{formula}", lambda: self.fetch_composition(formula))

    # ---- alternative acquisition route --------------------------------------------------

    def lookup_by_chemsys(self, formula: str) -> tuple[dict[str, Any] | None, str | None, str]:
        """Second route when the composition query finds nothing: list every OQMD entry in the
        compound's chemical system and keep those with the same stoichiometry. Stores the result
        under the same ``formula:`` key with ``route="chemsys"`` so downstream code is unchanged
        and provenance can say how the match was made."""
        if self.offline:
            return None, None, "missing_offline"
        elements = elements_of(formula)
        flt = f"element_set={','.join(elements)} AND ntypes={len(elements)}"
        try:
            page = self.http.get_json(BASE_URL, params={"filter": flt, "fields": FIELDS, "limit": 200})
        except SourceError as exc:
            self.cache.log(self.name, f"formula:{formula}", "fetch_failed", f"chemsys route: {exc}")
            return None, None, "fetch_failed"
        entries = (page or {}).get("data", []) if isinstance(page, dict) else []
        usable = [
            e
            for e in entries
            if e.get("stability") is not None and same_stoichiometry(str(e.get("name", "")), formula)
        ]
        if not usable:
            payload = {"found": False, "n_entries": len(entries), "route": "chemsys", "filter": flt}
        else:
            best = min(usable, key=lambda e: float(e["stability"]))
            payload = {
                "found": True,
                "n_entries": len(entries),
                "entry_id": best.get("entry_id"),
                "name": best.get("name"),
                "stability": float(best["stability"]),
                "delta_e": None if best.get("delta_e") is None else float(best["delta_e"]),
                "spacegroup": best.get("spacegroup"),
                "route": "chemsys",
                "filter": flt,
            }
        ts = self.cache.put(self.name, f"formula:{formula}", payload)
        self.cache.log(self.name, f"formula:{formula}", "fetched", f"chemsys route: found={payload['found']}")
        return payload, ts, "fetched"
