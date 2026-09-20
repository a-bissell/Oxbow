"""Attributed facts and source links. No inference from model or user prose."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from urllib.parse import quote, unquote, urlsplit

from oxide_triage.schemas import DataStatus, Provenance, ScoredCandidate


def doi_url(identifier: str | None) -> str | None:
    """Accept a bare DOI or a DOI resolver URL; reject malformed identifiers."""
    if not identifier:
        return None
    value = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", identifier.strip(), flags=re.I)
    if re.search(r"%(?![0-9A-Fa-f]{2})", value):
        return None
    try:
        value = unquote(value, errors="strict")
    except UnicodeDecodeError:
        return None
    if not re.fullmatch(r"10\.\d{4,9}/[^\s<>\"?#]+", value, re.I):
        return None
    return "https://doi.org/" + quote(value, safe="/")


def source_url(value: str | None) -> str | None:
    if not value:
        return None
    if re.match(r"^(?:10\.|https?://(?:dx\.)?doi\.org/)", value, re.I):
        return doi_url(value)
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme in {"http", "https"}
            and parsed.hostname
            and not parsed.username
            and not re.search(r'[\s<>"\\]', value)
        ):
            return value
    except ValueError:
        pass
    return None


@dataclass(frozen=True)
class GroundedFact:
    candidate_id: str
    formula: str
    property: str
    value: float
    unit: str
    provenance: Provenance

    def render(self) -> str:
        source = self.provenance.source
        if self.provenance.source_id:
            source += f" {self.provenance.source_id}"
        if self.provenance.url:
            source += f" ({self.provenance.url})"
        return (
            f"{self.formula} ({self.candidate_id}): {self.property} = "
            f"{self.value!r}{(' ' + self.unit) if self.unit else ''} [source: {source}]."
        )


def statements(sc: ScoredCandidate) -> list[GroundedFact]:
    """Only known, attributed values; computed gap corrections remain separately labelled."""
    r = sc.record
    out = []
    for record, prop, value, unit in (
        (
            r.band_gap,
            f"reported band gap ({r.band_gap.functional or 'unknown functional'})",
            r.band_gap.value_ev,
            "eV",
        ),
        (r.stability, "energy above hull", r.stability.energy_above_hull_ev_atom, "eV/atom"),
        (r.figure_of_merit, r.figure_of_merit.label, r.figure_of_merit.value, r.figure_of_merit.units),
    ):
        if (
            record.status == DataStatus.KNOWN
            and value is not None
            and math.isfinite(value)
            and record.provenance
        ):
            out.append(
                GroundedFact(
                    r.material_id, r.formula, prop, value, unit, record.provenance.model_copy(deep=True)
                )
            )
    return out


def candidate_sources(sc: ScoredCandidate) -> list[tuple[str, str]]:
    r = sc.record
    out = []
    for label, rec in (
        ("band gap", r.band_gap),
        ("stability", r.stability),
        (r.figure_of_merit.label, r.figure_of_merit),
        ("cross-check", r.cross_check),
    ):
        if rec.status == DataStatus.KNOWN and rec.provenance:
            url = source_url(rec.provenance.url)
            if url:
                out.append((label, url))
    return out


def source_summary(sc: ScoredCandidate) -> str:
    by_url: dict[str, list[str]] = {}
    for label, url in candidate_sources(sc):
        by_url.setdefault(url, []).append(label)
    links = [f"[{'/'.join(labels)}]({url})" for url, labels in by_url.items()]
    text = "Property sources: " + (" · ".join(links) if links else "no usable source links retrieved.")
    literature = [doi_url(w.doi) for w in sc.record.literature.sample_works]
    links = [f"[paper {i + 1}]({url})" for i, url in enumerate(dict.fromkeys(u for u in literature if u))][:3]
    text += " Literature matches (property support not established): " + (
        " · ".join(links) if links else "no usable paper links retrieved."
    )
    return text
