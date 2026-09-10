"""Configuration: one YAML, named profiles, environment overrides, validated schema.

Loading order (later wins):
    config/default.yaml  ->  config/profiles/<name>.yaml  ->  env vars  ->  CLI/request overrides

Every deviation from the base profile that a *request* introduces is recorded separately
(see ``pipeline.apply_criteria``) so it can be surfaced in the output header.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

PACKAGE_DIR = Path(__file__).resolve().parent
DATA_DIR = PACKAGE_DIR / "data"
REPO_ROOT = PACKAGE_DIR.parent
DEFAULT_CONFIG_DIR = REPO_ROOT / "config"

CRITERIA = ("stability", "band_gap", "dielectric", "toxicity", "simplicity", "literature")


class Weights(BaseModel):
    stability: float = Field(ge=0)
    band_gap: float = Field(ge=0)
    dielectric: float = Field(ge=0)
    toxicity: float = Field(ge=0)
    simplicity: float = Field(ge=0)
    literature: float = Field(ge=0)

    def normalized(self) -> dict[str, float]:
        raw = self.model_dump()
        total = sum(raw.values())
        if total <= 0:
            raise ValueError("At least one criterion weight must be positive")
        return {k: v / total for k, v in raw.items()}


class Gates(BaseModel):
    max_energy_above_hull_ev_atom: float = Field(ge=0)
    min_band_gap_ev: float = Field(ge=0)
    max_elements: int = Field(ge=2, le=6)
    on_missing_stability: Literal["exclude", "flag"] = "exclude"
    on_missing_band_gap: Literal["exclude", "flag"] = "exclude"


class BandGapCorrection(BaseModel):
    strategy: Literal["none", "scalar_factor", "hse_preferred"]
    scalar_factor: float = Field(gt=0)
    fallback: Literal["scalar_factor", "none"] = "scalar_factor"


class BandGapPreference(BaseModel):
    ideal_ev: float = Field(gt=0)


class BandGapConfig(BaseModel):
    correction: BandGapCorrection
    hybrid_functionals: list[str]
    preference: BandGapPreference


class StabilityConfig(BaseModel):
    zero_score_at_ev_atom: float = Field(gt=0)
    cross_check_tolerance_ev_atom: float = Field(gt=0)
    agreement_bonus: float = Field(ge=0)
    disagreement_penalty: float = Field(ge=0)


class DielectricConfig(BaseModel):
    low: float = Field(ge=0)
    high: float = Field(gt=0)

    @model_validator(mode="after")
    def _ordered(self) -> DielectricConfig:
        if self.high <= self.low:
            raise ValueError("dielectric.high must exceed dielectric.low")
        return self


class ToxicityConfig(BaseModel):
    table_file: str
    tier_scores: dict[int, float]
    blocklist_tiers: list[int]
    element_blocklist: list[str] = Field(default_factory=list)
    element_allowlist: list[str] = Field(default_factory=list)


class SimplicityConfig(BaseModel):
    scores: dict[int, float]


class LiteratureConfig(BaseModel):
    thin_film_saturation: int = Field(gt=0)
    total_saturation: int = Field(gt=0)
    thin_film_weight: float = Field(ge=0, le=1)
    # When OpenAlex counts are fetched. on_demand: at query time, for the top-ranked candidates
    # only (OpenAlex meters a small daily budget). warm: for every formula during warm-cache.
    # never: literature stays unknown unless already cached (fixtures, earlier warms).
    fetch: Literal["on_demand", "warm", "never"] = "on_demand"
    on_demand_pool: int = Field(default=25, ge=1, le=500)  # ranked candidates fetched per query


class MissingDataConfig(BaseModel):
    policy: Literal["no_credit", "renormalize"] = "no_credit"
    penalty: float = Field(ge=0, le=1)
    confidence_thresholds: dict[str, float]

    @field_validator("confidence_thresholds")
    @classmethod
    def _has_levels(cls, v: dict[str, float]) -> dict[str, float]:
        if "high" not in v or "medium" not in v or v["high"] < v["medium"]:
            raise ValueError("confidence_thresholds needs high >= medium")
        return v


class SourceFetchConfig(BaseModel):
    """Per-source fetch limits. ``workers`` overrides ``candidates.fetch_workers`` for the warm's
    thread pool (0 = fetch this source sequentially); ``max_rps`` caps requests per second across
    all threads of that source's client, retries included."""

    workers: int | None = Field(default=None, ge=0, le=16)
    max_rps: float | None = Field(default=None, gt=0, le=100)


class CandidatesConfig(BaseModel):
    cation_allowlist_file: str
    max_elements_query: int = Field(ge=2, le=6)
    energy_above_hull_ceiling_ev_atom: float = Field(ge=0)
    min_reported_gap_ev: float = Field(ge=0)
    literature_sample_size: int = Field(ge=0, le=25)
    fetch_workers: int = Field(default=4, ge=0, le=16)  # threads per source for the warm
    fetch: dict[str, SourceFetchConfig] = Field(default_factory=dict)  # per-source overrides

    def workers_for(self, source: str) -> int:
        override = self.fetch.get(source)
        return self.fetch_workers if override is None or override.workers is None else override.workers

    def max_rps_for(self, source: str) -> float | None:
        override = self.fetch.get(source)
        return None if override is None else override.max_rps


class OutputConfig(BaseModel):
    default_template: Literal["pi_summary", "audit", "json", "html"]
    top_k: int = Field(ge=1, le=50)
    verbosity: Literal["terse", "normal", "verbose"]


class CacheConfig(BaseModel):
    path: str
    ttl_days: int = Field(ge=0)
    offline: bool


class LLMUseFor(BaseModel):
    parse: bool = True
    refute: bool = True
    rationale: bool = False


class LLMConfig(BaseModel):
    provider: Literal["none", "anthropic", "openai_compatible"]
    model: str | None = None
    base_url: str | None = None
    timeout_s: float = 60
    use_for: LLMUseFor = Field(default_factory=LLMUseFor)


class SelfCheckConfig(BaseModel):
    enabled: bool = True
    on_failure: Literal["block", "warn"] = "block"
    workhorses: list[str] = Field(default_factory=lambda: ["HfO2", "ZrO2", "Al2O3", "Ta2O5"])
    leaders: list[str] = Field(default_factory=lambda: ["HfO2", "Al2O3"])
    min_workhorses_in_wide_top10: int = Field(default=3, ge=0)


class AcquisitionConfig(BaseModel):
    enabled: bool = True
    budget: int = Field(default=200, ge=0)  # max route attempts per pass
    planner: Literal["ladder", "llm"] = "ladder"


class AgentConfig(BaseModel):
    """The in-app chat agent (Streamlit Agent page, `oxide-triage chat`). It drives the same
    tools the MCP server exposes; nothing here affects ranking, so it is excluded from the
    config hash."""

    max_tool_rounds: int = Field(default=8, ge=1, le=32)  # tool-call rounds per user message
    number_guard: Literal["flag", "off"] = "flag"  # flag numbers absent from every tool output
    max_tokens: int = Field(default=16000, ge=256)  # per model reply
    timeout_s: float = 300  # adaptive thinking can pause longer than llm.timeout_s
    tool_result_max_chars: int = Field(default=20000, ge=1000)  # tool output shown to the model


class Config(BaseModel):
    profile_name: str
    description: str = ""
    weights: Weights
    gates: Gates
    band_gap: BandGapConfig
    stability: StabilityConfig
    dielectric: DielectricConfig
    toxicity: ToxicityConfig
    simplicity: SimplicityConfig
    literature: LiteratureConfig
    missing_data: MissingDataConfig
    candidates: CandidatesConfig
    output: OutputConfig
    terminology: dict[str, str] = Field(default_factory=dict)
    cache: CacheConfig
    llm: LLMConfig
    selfcheck: SelfCheckConfig = Field(default_factory=SelfCheckConfig)
    acquisition: AcquisitionConfig = Field(default_factory=AcquisitionConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)

    def config_hash(self) -> str:
        """Stable hash of everything that affects ranking (excludes cache path / LLM / agent)."""
        relevant = self.model_dump(
            exclude={"cache", "llm", "description", "output", "selfcheck", "acquisition", "agent"}
        )
        blob = json.dumps(relevant, sort_keys=True, default=str)
        return hashlib.sha256(blob.encode()).hexdigest()[:16]


# --------------------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------------------


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def _read_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a mapping at top level")
    return data


def list_profiles(config_dir: Path = DEFAULT_CONFIG_DIR) -> list[str]:
    profiles_dir = config_dir / "profiles"
    if not profiles_dir.exists():
        return []
    return sorted(p.stem for p in profiles_dir.glob("*.yaml"))


def load_dotenv(paths: list[Path] | None = None) -> list[str]:
    """Load ``KEY=VALUE`` lines from ``.env`` files into the environment without overriding
    variables that are already set. No dependency; quotes and ``export`` prefixes are tolerated.
    Looks in the current directory and the repository root. Returns the keys it set."""
    candidates = paths if paths is not None else [Path.cwd() / ".env", REPO_ROOT / ".env"]
    loaded: list[str] = []
    seen: set[Path] = set()
    for path in candidates:
        try:
            path = path.resolve()
        except OSError:
            continue
        if path in seen or not path.is_file():
            continue
        seen.add(path)
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[len("export ") :]
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip()
            if not key or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
                continue
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            if key not in os.environ and value != "":
                os.environ[key] = value
                loaded.append(key)
    return loaded


def _env_overrides() -> dict[str, Any]:
    """Environment variables that a container admin sets without editing YAML."""
    out: dict[str, Any] = {}
    cache: dict[str, Any] = {}
    if path := os.environ.get("OXIDE_TRIAGE_CACHE"):
        cache["path"] = path
    if (off := os.environ.get("OXIDE_TRIAGE_OFFLINE")) is not None and off != "":
        cache["offline"] = off.strip().lower() in {"1", "true", "yes", "on"}
    if cache:
        out["cache"] = cache
    llm: dict[str, Any] = {}
    if provider := os.environ.get("LLM_PROVIDER"):
        llm["provider"] = provider.strip().lower()
    if model := os.environ.get("LLM_MODEL"):
        llm["model"] = model
    if base_url := os.environ.get("LLM_BASE_URL"):
        llm["base_url"] = base_url
    if llm:
        out["llm"] = llm
    return out


def load_config(
    profile: str | None = None,
    config_dir: Path = DEFAULT_CONFIG_DIR,
    overrides: dict[str, Any] | None = None,
    use_env: bool = True,
) -> Config:
    if use_env:
        load_dotenv()
    data = _read_yaml(config_dir / "default.yaml")
    if profile and profile != "default":
        profile_path = config_dir / "profiles" / f"{profile}.yaml"
        if not profile_path.exists():
            available = ", ".join(list_profiles(config_dir)) or "(none)"
            raise FileNotFoundError(f"Unknown profile '{profile}'. Available: {available}")
        data = deep_merge(data, _read_yaml(profile_path))
    if use_env:
        data = deep_merge(data, _env_overrides())
    if overrides:
        data = deep_merge(data, overrides)
    return Config.model_validate(data)


# --------------------------------------------------------------------------------------
# Curated data tables shipped with the package
# --------------------------------------------------------------------------------------


class HazardTable(BaseModel):
    version: str
    default_tier: int
    tiers: dict[str, int]
    basis: dict[str, str]

    def lookup(self, element: str) -> tuple[int, str, bool]:
        """Return (tier, basis, in_table)."""
        if element in self.tiers:
            return self.tiers[element], self.basis[element], True
        return self.default_tier, "not in hazard table", False


def load_hazard_table(filename: str = "element_hazards.yaml") -> HazardTable:
    raw = _read_yaml(DATA_DIR / filename)
    elements = raw.get("elements", {})
    return HazardTable(
        version=str(raw.get("version", "unversioned")),
        default_tier=int(raw.get("default_tier", 1)),
        tiers={el: int(v["tier"]) for el, v in elements.items()},
        basis={el: str(v.get("basis", "")) for el, v in elements.items()},
    )


def load_cation_allowlist(filename: str = "cation_allowlist.yaml") -> list[str]:
    raw = _read_yaml(DATA_DIR / filename)
    return list(raw.get("cations", []))


def load_compound_aliases(filename: str = "compound_aliases.yaml") -> dict[str, list[str]]:
    raw = _read_yaml(DATA_DIR / filename)
    return {k: list(v) for k, v in raw.items()}
