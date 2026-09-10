"""OpenAlex literature-evidence client.

REST: ``https://api.openalex.org/works?filter=title_and_abstract.search:<query>``
Two counts per compound:
  * ``total_works``      any work mentioning the formula or a common name
  * ``thin_film_works``  the subset that also mentions a thin-film deposition term

Counts are an *evidence-strength proxy*, not a literature review. Formula-string search is
noisy for short or ambiguous formulae (CaO, BaO); the audit view shows the exact query
terms so a scientist can judge the noise.

Titles returned here are stored verbatim and passed downstream as delimited data. They are
never interpreted as instructions (see ``edges/llm.py`` and ``tests/test_injection.py``).
"""

from __future__ import annotations

import os
from typing import Any

from oxide_triage.cache import Cache
from oxide_triage.sources.base import CachedSource, Http, SourceError

BASE_URL = "https://api.openalex.org/works"
THIN_FILM_TERMS = [
    '"thin film"',
    '"thin films"',
    '"atomic layer deposition"',
    "sputtered",
    "sputtering",
    "epitaxial",
    '"pulsed laser deposition"',
    '"chemical vapor deposition"',
]


def _quote(term: str) -> str:
    term = term.strip()
    if " " in term and not term.startswith('"'):
        return f'"{term}"'
    return term


def build_queries(formula: str, aliases: list[str]) -> tuple[str, str, list[str]]:
    terms = [formula] + [a for a in aliases if a.lower() != formula.lower()]
    compound = "(" + " OR ".join(_quote(t) for t in terms) + ")"
    thin_film = compound + " AND (" + " OR ".join(THIN_FILM_TERMS) + ")"
    return compound, thin_film, terms


class OpenAlex(CachedSource):
    name = "openalex"

    def __init__(
        self,
        cache: Cache,
        ttl_days: int = 90,
        offline: bool = False,
        http: Http | None = None,
        mailto: str | None = None,
        sample_size: int = 5,
    ):
        super().__init__(cache, ttl_days, offline)
        self.http = http or Http(user_agent="oxide-triage/0.1 (openalex-client)")
        self.mailto = mailto if mailto is not None else os.environ.get("OPENALEX_MAILTO", "")
        self.sample_size = sample_size

    def _search(self, query: str, per_page: int) -> dict[str, Any]:
        params: dict[str, Any] = {
            "filter": f"title_and_abstract.search:{query}",
            "per-page": per_page,
            "select": "id,title,publication_year,doi",
            "sort": "cited_by_count:desc",
        }
        if self.mailto:
            params["mailto"] = self.mailto
        page = self.http.get_json(BASE_URL, params=params) or {}
        return page

    def evidence(self, formula: str, aliases: list[str]) -> tuple[dict[str, Any] | None, str | None, str]:
        compound_q, thin_film_q, terms = build_queries(formula, aliases)

        def fetch() -> dict[str, Any]:
            total = self._search(compound_q, per_page=1)
            films = self._search(thin_film_q, per_page=max(1, self.sample_size))
            sample = [
                {
                    "work_id": str(w.get("id", "")).replace("https://openalex.org/", ""),
                    "title": (w.get("title") or "")[:300],
                    "year": w.get("publication_year"),
                    "doi": w.get("doi"),
                }
                for w in films.get("results", [])[: self.sample_size]
            ]
            return {
                "total_works": int((total.get("meta") or {}).get("count", 0)),
                "thin_film_works": int((films.get("meta") or {}).get("count", 0)),
                "sample_works": sample,
                "query_terms": terms,
                "thin_film_terms": THIN_FILM_TERMS,
            }

        return self.cached(f"formula:{formula}", fetch)

    # ---- alternative acquisition route --------------------------------------------------

    def evidence_names_only(
        self, formula: str, aliases: list[str]
    ) -> tuple[dict[str, Any] | None, str | None, str]:
        """Second route when the formula-string search returns nothing: search on the common
        names alone (a formula token such as "LaLuO3" is often absent from abstracts that spell
        the compound out). Not applicable when no alias is known. Stored under the same key with
        ``route="names_only"`` and the terms actually used."""
        if not aliases:
            return None, None, "not_applicable"
        if self.offline:
            return None, None, "missing_offline"
        compound_q, thin_film_q, terms = build_queries(aliases[0], aliases[1:])
        try:
            total = self._search(compound_q, per_page=1)
            films = self._search(thin_film_q, per_page=max(1, self.sample_size))
        except SourceError as exc:
            self.cache.log(self.name, f"formula:{formula}", "fetch_failed", f"names_only route: {exc}")
            return None, None, "fetch_failed"
        payload = {
            "total_works": int((total.get("meta") or {}).get("count", 0)),
            "thin_film_works": int((films.get("meta") or {}).get("count", 0)),
            "sample_works": [
                {
                    "work_id": str(w.get("id", "")).replace("https://openalex.org/", ""),
                    "title": (w.get("title") or "")[:300],
                    "year": w.get("publication_year"),
                    "doi": w.get("doi"),
                }
                for w in films.get("results", [])[: self.sample_size]
            ],
            "query_terms": terms,
            "thin_film_terms": THIN_FILM_TERMS,
            "route": "names_only",
        }
        ts = self.cache.put(self.name, f"formula:{formula}", payload)
        self.cache.log(self.name, f"formula:{formula}", "fetched", "names_only route")
        return payload, ts, "fetched"
