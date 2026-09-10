"""PubChem compound-level hazard lookup (PUG REST + PUG View).

  * name -> CID:  ``/rest/pug/compound/name/{name}/cids/JSON``
  * GHS:          ``/rest/pug_view/data/compound/{cid}/JSON?heading=GHS+Classification``

Only the GHS hazard statement codes (H3xx etc.) are extracted. They feed *caveats*, not the
score: the element-level table is the scoring basis because it is complete and versioned,
whereas PubChem records exist for some oxides and not others.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

from oxide_triage.cache import Cache
from oxide_triage.sources.base import CachedSource, Http

PUG = "https://pubchem.ncbi.nlm.nih.gov/rest/pug"
PUG_VIEW = "https://pubchem.ncbi.nlm.nih.gov/rest/pug_view"
H_CODE = re.compile(r"\bH[2-4]\d{2}[A-Za-z]?\b")


def extract_h_codes(ghs_json: Any) -> list[str]:
    """Walk the nested PUG View JSON and collect distinct GHS H-codes, sorted."""
    found: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                if k == "String" and isinstance(v, str):
                    found.update(H_CODE.findall(v))
                else:
                    walk(v)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(ghs_json)
    return sorted(found)


class PubChem(CachedSource):
    name = "pubchem"

    def __init__(self, cache: Cache, ttl_days: int = 90, offline: bool = False, http: Http | None = None):
        super().__init__(cache, ttl_days, offline)
        self.http = http or Http(user_agent="oxide-triage/0.1 (pubchem-client)")

    def hazards(self, formula: str, names: list[str]) -> tuple[dict[str, Any] | None, str | None, str]:
        candidates = [n for n in names if n] or [formula]

        def fetch() -> dict[str, Any]:
            cid: int | None = None
            used_name: str | None = None
            for nm in candidates:
                page = self.http.get_json(f"{PUG}/compound/name/{quote(nm)}/cids/JSON")
                cids = ((page or {}).get("IdentifierList") or {}).get("CID") or []
                if cids:
                    cid, used_name = int(cids[0]), nm
                    break
            if cid is None:
                return {"found": False, "names_tried": candidates}
            ghs = self.http.get_json(
                f"{PUG_VIEW}/data/compound/{cid}/JSON", params={"heading": "GHS Classification"}
            )
            return {
                "found": True,
                "cid": cid,
                "matched_name": used_name,
                "ghs_codes": extract_h_codes(ghs) if ghs else [],
                "ghs_section_present": ghs is not None,
            }

        return self.cached(f"formula:{formula}", fetch)
