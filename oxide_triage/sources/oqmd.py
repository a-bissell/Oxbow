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
from oxide_triage.sources.base import CachedSource, Http

BASE_URL = "https://oqmd.org/oqmdapi/formationenergy"
FIELDS = "name,entry_id,delta_e,stability,spacegroup"


class OQMD(CachedSource):
    name = "oqmd"

    def __init__(self, cache: Cache, ttl_days: int = 90, offline: bool = False, http: Http | None = None):
        super().__init__(cache, ttl_days, offline)
        self.http = http or Http(timeout_s=60, user_agent="oxide-triage/0.1 (oqmd-client)")

    def lookup(self, formula: str) -> tuple[dict[str, Any] | None, str | None, str]:
        """Best (lowest-stability) OQMD entry for a reduced formula, cached as one record."""

        def fetch() -> dict[str, Any]:
            page = self.http.get_json(
                BASE_URL, params={"composition": formula, "fields": FIELDS, "limit": 50}
            )
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

        return self.cached(f"formula:{formula}", fetch)
