"""Property providers for the application figure of merit.

The six general criteria (stability, band gap, interface, toxicity, simplicity, literature) are
wired to their sources directly. The seventh criterion is the application's figure of merit,
and its source is an adapter: anything that can answer "give me property P for material M,
with provenance and a retrieval timestamp" fits ``PropertyProvider``. The configured provider
(``figure_of_merit.provider``) is looked up in ``PROVIDERS`` by name.

Every provider keeps the fetch-layer contract of ``CachedSource.cached``: ``fetch`` returns
``(payload, retrieved_at, fetch_status)`` and the data layer maps that through ``status_for``,
so a source that holds no record (ABSENT) and a lookup that never completed (NOT_RETRIEVED)
stay distinct. A record the source holds but flags as unusable extracts to no value with a
reject reason; that is ABSENT with the reason attached, since it is a fact about the data.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:  # pragma: no cover
    from oxide_triage.config import FigureOfMeritConfig
    from oxide_triage.sources.materials_project import MaterialsProject


@dataclass(frozen=True)
class Extracted:
    value: float | None
    extras: dict[str, float] = field(default_factory=dict)
    reject_reason: str | None = None  # set when the source answered with a record it flags as unusable


class PropertyProvider(Protocol):
    name: str

    def prefetch(self, material_ids: list[str]) -> None: ...

    def fetch(self, material_id: str) -> tuple[dict[str, Any] | None, str | None, str]: ...

    def extract(self, payload: dict[str, Any] | None) -> Extracted: ...

    def describe(self, value: float, extras: dict[str, float]) -> tuple[str, str]:
        """``(display, short)``: the audit-view label and the one-line rationale fragment."""
        ...

    def dataset_note(self) -> str: ...

    def absent_note(self) -> str: ...

    def absent_reason(self) -> str: ...

    def unretrieved_reason(self, fetch_status: str) -> str: ...

    def no_route_reason(self) -> str: ...


def _opt_float(x: Any) -> float | None:
    return None if x is None else float(x)


def _path(payload: dict[str, Any], dotted: str) -> Any:
    node: Any = payload
    for part in dotted.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node


class MPDielectricProvider:
    """Materials Project's DFPT dielectric set (``/materials/dielectric/``). The cache key
    ``dielectric:{material_id}`` and the request are unchanged from the days when this was the
    only property the tool knew, so existing caches, the fixture loader and the recorded
    responses keep working."""

    name = "mp_dielectric"

    def __init__(self, mp: MaterialsProject, fom: FigureOfMeritConfig):
        self.mp = mp
        self.fom = fom

    def prefetch(self, material_ids: list[str]) -> None:
        self.mp.prefetch_dielectric(material_ids)

    def fetch(self, material_id: str) -> tuple[dict[str, Any] | None, str | None, str]:
        return self.mp.dielectric(material_id)

    def extract(self, payload: dict[str, Any] | None) -> Extracted:
        if not payload or not payload.get("found"):
            return Extracted(value=None)
        value = _opt_float(_path(payload, self.fom.property))
        extras = {
            k: v
            for k, v in (
                ("e_electronic", _opt_float(payload.get("e_electronic"))),
                ("e_ionic", _opt_float(payload.get("e_ionic"))),
                ("refractive_index", _opt_float(payload.get("n"))),
            )
            if v is not None and k != self.fom.property
        }
        return Extracted(value=value, extras=extras)

    def describe(self, value: float, extras: dict[str, float]) -> tuple[str, str]:
        electronic = extras.get("e_electronic")
        display = (
            f"{self.fom.property} = {value:.1f} ({self.fom.method}; "
            f"electronic {electronic if electronic is not None else '?'})"
        )
        return display, f"{self.fom.label} {value:.0f} ({self.fom.method})"

    def dataset_note(self) -> str:
        return f"MP {self.fom.method} dataset"

    def absent_note(self) -> str:
        return f"no {self.fom.method} dielectric record in MP"

    def absent_reason(self) -> str:
        return f"no {self.fom.method} dielectric record in MP for this material"

    def unretrieved_reason(self, fetch_status: str) -> str:
        return (
            f"MP dielectric lookup never completed here ({fetch_status}); "
            "this is a gap in the cache, not in MP"
        )

    def no_route_reason(self) -> str:
        return (
            "No public per-material dielectric source beyond Materials Project's DFPT set is wired in. "
            "The JARVIS-DFT bulk dataset (OptB88vdW dielectric tensors) is a candidate future route; "
            "until then the value stays unknown."
        )


PROVIDERS: dict[str, type] = {
    "mp_dielectric": MPDielectricProvider,
}


def make_provider(fom: FigureOfMeritConfig, mp: MaterialsProject) -> PropertyProvider:
    try:
        cls = PROVIDERS[fom.provider]
    except KeyError:
        raise ValueError(
            f"unknown figure_of_merit.provider '{fom.provider}'; known: {', '.join(sorted(PROVIDERS))}"
        ) from None
    return cls(mp, fom)
