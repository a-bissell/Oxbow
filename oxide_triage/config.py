"""Configuration: one YAML, named profiles, a site overrides file, environment overrides,
validated schema.

Loading order (later wins):
    config/default.yaml -> site.base -> config/profiles/<name>.yaml -> site.profiles[<name>]
                        -> env vars -> CLI/request overrides

The shipped YAML files are never written by the application. A site's edits (made on the
Admin page, or by hand) live in one generated file, ``site.yaml`` next to the cache:
``base`` holds edits to default.yaml, ``profiles.<name>`` edits to that profile file. Every
site departure from the shipped policy is recorded on the loaded ``Config`` and surfaced as a
deviation on every run, and the self-check judges the policy actually in force.

Every deviation from the base profile that a *request* introduces is recorded separately
(see ``pipeline.apply_criteria``) so it can be surfaced in the output header.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import re
from collections.abc import Collection, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, get_origin

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic.fields import FieldInfo

log = logging.getLogger(__name__)

PACKAGE_DIR = Path(__file__).resolve().parent
DATA_DIR = PACKAGE_DIR / "data"
REPO_ROOT = PACKAGE_DIR.parent
CONFIG_DIR_ENV = "OXIDE_TRIAGE_CONFIG_DIR"


def resolve_config_dir() -> Path:
    """Where the shipped YAML lives. In a checkout it is ``config/`` beside the package. An
    installed wheel has no such directory, so a deployment names it with ``OXIDE_TRIAGE_CONFIG_DIR``
    (the container image does) or runs from a directory that contains ``config/`` (a release
    unpacked from the offline archive does). The first that exists wins; a missing directory is
    reported at first use, not here, so ``--help`` and ``bundle verify`` need no configuration."""
    env = os.environ.get(CONFIG_DIR_ENV)
    if env:
        return Path(env).expanduser()
    for candidate in (REPO_ROOT / "config", Path.cwd() / "config"):
        if (candidate / "default.yaml").is_file():
            return candidate
    return REPO_ROOT / "config"


DEFAULT_CONFIG_DIR = resolve_config_dir()

SITE_CONFIG_ENV = "OXIDE_TRIAGE_SITE_CONFIG"
ADMIN_ENV = "OXIDE_TRIAGE_ADMIN"
TRUTHY = {"1", "true", "yes", "on"}

# Dotted config path -> environment variable that overrides it. The single source of truth for
# ``_env_overrides`` and for the Admin page, which disables these fields while the variable is set.
ENV_KEYS: dict[str, str] = {
    "cache.path": "OXIDE_TRIAGE_CACHE",
    "cache.offline": "OXIDE_TRIAGE_OFFLINE",
    "llm.provider": "LLM_PROVIDER",
    "llm.model": "LLM_MODEL",
    "llm.base_url": "LLM_BASE_URL",
}

# The six criteria every material class wants: thermodynamic stability, an insulating gap (a
# profile may zero it), stability against the substrate, a hazard screen, compositional
# simplicity and public literature evidence. The seventh criterion is the application's figure
# of merit and is declared by ``figure_of_merit:`` in the profile; see ``Config.criteria``.
FIXED_CRITERIA = ("stability", "band_gap", "interface", "toxicity", "simplicity", "literature")
# Property providers the figure of merit can name (implemented in sources/properties.py).
PROPERTY_PROVIDERS = ("mp_dielectric", "mp_elasticity")


class Weights(BaseModel):
    """Weights of the six fixed criteria. The figure of merit's weight lives in its own block
    (``figure_of_merit.weight``) because a profile overlay cannot delete a key it inherits from
    default.yaml, and a profile with a different figure of merit must not inherit the
    dielectric weight under the old name."""

    stability: float = Field(ge=0)
    band_gap: float = Field(ge=0)
    toxicity: float = Field(ge=0)
    simplicity: float = Field(ge=0)
    literature: float = Field(ge=0)
    interface: float = Field(
        default=0.0, ge=0
    )  # default 0 so a site file written before the criterion still loads


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


class FigureOfMeritConfig(BaseModel):
    """The one application-specific criterion. A profile declares which property it is, who
    supplies it, how it is scored and how its absence is treated; the dielectric constant of the
    oxide-dielectric profiles is one instance, not a special case in code."""

    criterion: str  # its name in weights, audit rows, caveat codes and the parser's vocabulary
    label: str  # shown to people, e.g. "dielectric constant"
    units: str = ""
    property: str  # dotted path into the provider's payload, e.g. e_total
    provider: str  # a name in PROPERTY_PROVIDERS
    method: str  # shown beside every value, as the functional is beside a band gap
    prefer: Literal["high", "low"] = "high"
    low: float = Field(ge=0)  # prefer high: score 0 at/below; prefer low: score 1 at/below
    high: float = Field(gt=0)  # prefer high: score 1 at/above; prefer low: score 0 at/above
    weight: float = Field(ge=0)
    on_missing: Literal["flag", "exclude"] = "flag"  # flag: not scored, caveated; exclude: a gate
    vocabulary: list[str] = Field(default_factory=list)  # regex alternatives the parser and guard accept
    application: str = ""  # what the profile ranks for, in the guard's out-of-scope explanation

    @field_validator("criterion")
    @classmethod
    def _criterion_name(cls, v: str) -> str:
        if not re.fullmatch(r"[a-z][a-z0-9_]*", v):
            raise ValueError("figure_of_merit.criterion must be a lowercase identifier")
        if v in FIXED_CRITERIA:
            raise ValueError(f"figure_of_merit.criterion cannot reuse the fixed criterion '{v}'")
        return v

    @field_validator("provider")
    @classmethod
    def _known_provider(cls, v: str) -> str:
        if v not in PROPERTY_PROVIDERS:
            raise ValueError(f"figure_of_merit.provider must be one of {', '.join(PROPERTY_PROVIDERS)}")
        return v

    @model_validator(mode="after")
    def _ordered(self) -> FigureOfMeritConfig:
        if self.high <= self.low:
            raise ValueError("figure_of_merit.high must exceed figure_of_merit.low")
        return self


class ToxicityConfig(BaseModel):
    table_file: str
    tier_scores: dict[int, float]
    blocklist_tiers: list[int]
    element_blocklist: list[str] = Field(default_factory=list)
    element_allowlist: list[str] = Field(default_factory=list)
    # Elements no request can unblock, whatever it says or who says it. A site changes this
    # list in its overrides file; a profile allowlist cannot reach past it either.
    never_lift: list[str] = Field(default_factory=list)


class SimplicityConfig(BaseModel):
    scores: dict[int, float]


class InterfaceConfig(BaseModel):
    """Stability of the oxide in contact with a substrate, from the convex hull of oxide plus
    substrate (Hubbard & Schlom 1996). Score 1 at no reaction, 0 at ``zero_score_at_ev_atom``."""

    substrate: str = "Si"  # an element or a hull-phase formula (Si, Ge, SrTiO3, ...)
    tolerance_ev_atom: float = Field(
        default=0.05, ge=0
    )  # a reaction inside this band is DFT noise, not a reaction
    zero_score_at_ev_atom: float = Field(default=0.20, gt=0)  # measured beyond the tolerance
    thermo_type: str = "GGA_GGA+U_R2SCAN"  # MP thermo scheme; the default matches the summary energies
    caveat_below_ev_atom: float = Field(
        default=0.02, ge=0
    )  # a reaction more exothermic than this gets a caveat


class LiteratureConfig(BaseModel):
    thin_film_saturation: int = Field(gt=0)
    total_saturation: int = Field(gt=0)
    thin_film_weight: float = Field(ge=0, le=1)


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


class RefutationConfig(BaseModel):
    """Rule-derived caveats a profile can switch on or off. The rules themselves are code
    (refute.py); this block holds the knobs a material class needs. Off by default so the
    oxide-dielectric profiles' output is unchanged."""

    # Caveat codes a profile does not want (e.g. hygroscopic_risk for a class where it is moot).
    disabled_rules: list[str] = Field(default_factory=list)
    # A competing observed polymorph within this many eV/atom of the leading phase earns a
    # phase-transformation caveat (thermal cycling). None = off.
    polymorph_window_ev_atom: float | None = Field(default=None, ge=0)
    # Flag a 4f-element oxide whose reported semi-local gap is near zero as a DFT artifact.
    f_electron_gap_check: bool = False


class SourceFetchConfig(BaseModel):
    """Per-source fetch limits. ``workers`` overrides ``candidates.fetch_workers`` for the warm's
    thread pool (0 = fetch this source sequentially); ``max_rps`` caps requests per second across
    all threads of that source's client, retries included."""

    workers: int | None = Field(default=None, ge=0, le=16)
    max_rps: float | None = Field(default=None, gt=0, le=100)
    # Circuit breaker: after this many consecutive server failures (5xx, transport errors) the
    # source is paused for pause_s and every request in that window fails at once.
    pause_after: int | None = Field(default=None, ge=1, le=1000)
    pause_s: float | None = Field(default=None, ge=0, le=3600)


class CandidatesConfig(BaseModel):
    cation_allowlist_file: str
    # Families (from the allowlist file) a query is limited to by default; empty = the whole universe.
    default_families: list[str] = Field(default_factory=list)
    max_elements_query: int = Field(ge=2, le=6)
    energy_above_hull_ceiling_ev_atom: float = Field(ge=0)
    min_reported_gap_ev: float = Field(ge=0)
    # Keep only entries Materials Project matches to an experimentally observed structure
    # (``theoretical: false``). The flag is per structure entry, not per compound, so a compound
    # whose only observed entry falls outside the hull/gap window is dropped with it; measured at
    # 25 of 1,067 all-theoretical formulas on the first live warm. ``add-material`` bypasses this.
    observed_only: bool = True
    # The formula-keyed sources (OQMD, PubChem, OpenAlex) are slow or metered, so by default the
    # warm fetches Materials Project only and a query fills the top-ranked pool on demand, then
    # re-ranks until the pool is settled. ``warm`` fetches them for every formula at warm time.
    formula_sources: Literal["on_demand", "warm"] = "on_demand"
    on_demand_pool: int = Field(default=25, ge=1, le=500)  # ranked candidates filled per query
    literature_sample_size: int = Field(ge=0, le=25)
    fetch_workers: int = Field(default=4, ge=0, le=16)  # threads per source for the warm
    fetch: dict[str, SourceFetchConfig] = Field(default_factory=dict)  # per-source overrides

    def workers_for(self, source: str) -> int:
        override = self.fetch.get(source)
        return self.fetch_workers if override is None or override.workers is None else override.workers

    def max_rps_for(self, source: str) -> float | None:
        override = self.fetch.get(source)
        return None if override is None else override.max_rps

    def breaker_for(self, source: str) -> tuple[int, float]:
        """(consecutive failures before pausing, pause seconds); shipped default 5 and 60."""
        override = self.fetch.get(source)
        after = 5 if override is None or override.pause_after is None else override.pause_after
        pause = 60.0 if override is None or override.pause_s is None else override.pause_s
        return after, pause


class OutputConfig(BaseModel):
    default_template: Literal["pi_summary", "audit", "json", "html"]
    top_k: int = Field(ge=1, le=50)
    verbosity: Literal["terse", "normal", "verbose"]
    group_polymorphs: bool = True  # one row per compound; other passing phases collapse under it
    tie_band: float = Field(
        default=0.04, ge=0.0, le=1.0
    )  # candidates within this of a tier's leader share the tier


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


class RetrievalConfig(BaseModel):
    """How complete a cache has to be before its ranking is treated as comparable.

    Missing data lowers a candidate's score, so when retrieval is patchy the ordering partly
    reflects which fetches finished rather than which materials are better. These floors decide
    when to say so and when to stop serving a ranking altogether.
    """

    min_completeness_warn: float = Field(default=0.95, ge=0, le=1)  # below this, warn on every result
    min_completeness_serve: float = Field(default=0.0, ge=0, le=1)  # below this, refuse to rank (0 = never)


DEFAULT_SELFCHECK_REQUEST = (
    "Find promising oxide dielectric candidates for thin-film experiments. Prefer "
    "thermodynamically stable materials, wide band gaps, non-toxic elements, simple "
    "compositions, and public evidence. Return a ranked shortlist with caveats."
)


class SelfCheckConfig(BaseModel):
    enabled: bool = True
    on_failure: Literal["block", "warn"] = "block"
    # The unconstrained request the known-answer check runs, under the active profile.
    request: str = DEFAULT_SELFCHECK_REQUEST
    workhorses: list[str] = Field(default_factory=lambda: ["HfO2", "ZrO2", "Al2O3", "Ta2O5"])
    leaders: list[str] = Field(default_factory=lambda: ["HfO2"])
    # A second, wider profile the workhorses must also surface under; null skips that half.
    wide_profile: str | None = "exploratory"
    # Windows for the rank rules. On real data the default top five are the perovskite high-k
    # candidates (SrHfO3, LaAlO3, LaScO3, ...) and the workhorses sit just behind them, so the
    # windows are wide enough to pass a correct ranking and still catch a broken fetch or gate.
    leaders_top_n: int = Field(default=10, ge=1)
    wide_top_n: int = Field(default=25, ge=1)
    min_workhorses_in_wide_top: int = Field(default=2, ge=0)
    # A known-answer check on a half-retrieved cache tests the cache, not the ranker. Below this
    # completeness the check reports `insufficient_data` instead of a misleading FAIL.
    min_retrieval_completeness: float = Field(default=0.9, ge=0, le=1)


class AcquisitionConfig(BaseModel):
    enabled: bool = True
    budget: int = Field(default=200, ge=0)  # max route attempts per pass
    planner: Literal["ladder", "llm"] = "ladder"


class ServerConfig(BaseModel):
    """The web server. Nothing here affects ranking; excluded from the config hash."""

    # Header the authenticating reverse proxy sets with the signed-in user's name. Recorded
    # as the actor of every deviation a web request causes. With no proxy, the actor is
    # logged as unattributed; the server never trusts a name the browser itself sends.
    actor_header: str = "X-Forwarded-User"


class AgentConfig(BaseModel):
    """The in-app chat agent (the web assistant, `oxide-triage chat`). It drives the same tools
    the MCP server exposes; nothing here affects ranking, so it is excluded from the config
    hash."""

    model: str | None = None  # assistant model; None = the provider's chat default (see make_chat_llm)
    max_tool_rounds: int = Field(default=8, ge=1, le=32)  # tool-call rounds per user message
    number_guard: Literal["flag", "off"] = "flag"  # flag numbers absent from every tool output
    max_tokens: int = Field(default=16000, ge=256)  # per model reply
    timeout_s: float = 300  # adaptive thinking can pause longer than llm.timeout_s
    tool_result_max_chars: int = Field(default=20000, ge=1000)  # tool output shown to the model


class SiteOverride(BaseModel):
    """One leaf the site file changed relative to the shipped configuration."""

    key: str  # dotted path, e.g. gates.min_band_gap_ev
    shipped: Any = None
    value: Any = None


class Config(BaseModel):
    profile_name: str
    description: str = ""
    weights: Weights
    gates: Gates
    band_gap: BandGapConfig
    stability: StabilityConfig
    figure_of_merit: FigureOfMeritConfig
    toxicity: ToxicityConfig
    simplicity: SimplicityConfig
    literature: LiteratureConfig
    interface: InterfaceConfig = Field(default_factory=InterfaceConfig)
    missing_data: MissingDataConfig
    refutation: RefutationConfig = Field(default_factory=RefutationConfig)
    retrieval: RetrievalConfig = Field(default_factory=RetrievalConfig)
    candidates: CandidatesConfig
    output: OutputConfig
    terminology: dict[str, str] = Field(default_factory=dict)
    cache: CacheConfig
    llm: LLMConfig
    selfcheck: SelfCheckConfig = Field(default_factory=SelfCheckConfig)
    acquisition: AcquisitionConfig = Field(default_factory=AcquisitionConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    # Provenance, filled by the loader; excluded from dumps and therefore from the hash.
    site_overrides: list[SiteOverride] = Field(default_factory=list, exclude=True)
    site_config_path: str | None = Field(default=None, exclude=True)

    def criteria(self) -> tuple[str, ...]:
        """The seven criterion names in the order the weights are reported: the six fixed ones
        with the figure of merit in the third slot, where the dielectric criterion always was."""
        return (
            "stability",
            "band_gap",
            self.figure_of_merit.criterion,
            "toxicity",
            "simplicity",
            "literature",
            "interface",
        )

    def criterion_weights(self) -> dict[str, float]:
        """Raw weights of all seven criteria, keyed by name, in ``criteria()`` order."""
        w = self.weights
        return {
            "stability": w.stability,
            "band_gap": w.band_gap,
            self.figure_of_merit.criterion: self.figure_of_merit.weight,
            "toxicity": w.toxicity,
            "simplicity": w.simplicity,
            "literature": w.literature,
            "interface": w.interface,
        }

    def normalized_weights(self) -> dict[str, float]:
        raw = self.criterion_weights()
        total = sum(raw.values())
        if total <= 0:
            raise ValueError("At least one criterion weight must be positive")
        return {k: v / total for k, v in raw.items()}

    def config_hash(self) -> str:
        """Stable hash of everything that affects ranking (excludes cache path / LLM / agent)."""
        relevant = self.model_dump(
            exclude={"cache", "llm", "description", "output", "selfcheck", "acquisition", "agent", "server"}
        )
        blob = json.dumps(relevant, sort_keys=True, default=str)
        return hashlib.sha256(blob.encode()).hexdigest()[:16]

    def policy_overrides(self) -> list[SiteOverride]:
        """Site overrides that change the ranking policy (shown as a deviation on every run)."""
        return [o for o in self.site_overrides if is_policy_key(o.key)]


# --------------------------------------------------------------------------------------
# Schema walking: the leaf paths of Config, used by the site layer and the Admin page
# --------------------------------------------------------------------------------------


def leaf_fields(model: type[BaseModel] = Config, prefix: str = "") -> dict[str, FieldInfo]:
    """Dotted path -> FieldInfo for every leaf of ``model``. Nested models are descended;
    dict- and list-typed fields are leaves. Loader-internal fields (``exclude=True``) are skipped."""
    out: dict[str, FieldInfo] = {}
    for name, info in model.model_fields.items():
        if info.exclude:
            continue
        path = f"{prefix}.{name}" if prefix else name
        ann = info.annotation
        if isinstance(ann, type) and issubclass(ann, BaseModel):
            out.update(leaf_fields(ann, path))
        else:
            out[path] = info
    return out


LEAF_PATHS: frozenset[str] = frozenset(leaf_fields(Config))
DICT_PATHS: frozenset[str] = frozenset(
    p for p, f in leaf_fields(Config).items() if get_origin(f.annotation) is dict
)
READ_ONLY_PATHS: frozenset[str] = frozenset(
    {
        "profile_name",
        "toxicity.table_file",
        "candidates.cation_allowlist_file",
        "cache.path",
        # Renaming the criterion or switching its source is a profile decision, not a site edit:
        # caveat codes, cache keys and the parser vocabulary all hang off it.
        "figure_of_merit.criterion",
        "figure_of_merit.provider",
        "figure_of_merit.property",
    }
)
# What counts as ranking *policy*: a site change here prints a deviation on every result.
POLICY_SECTIONS: frozenset[str] = frozenset(
    {
        "weights",
        "gates",
        "band_gap",
        "stability",
        "figure_of_merit",
        "toxicity",
        "simplicity",
        "missing_data",
        "refutation",
        "literature",
    }
)
POLICY_EXTRA: frozenset[str] = frozenset(
    {
        "candidates.max_elements_query",
        "candidates.energy_above_hull_ceiling_ev_atom",
        "candidates.min_reported_gap_ev",
    }
)


def is_policy_key(path: str) -> bool:
    return path.split(".", 1)[0] in POLICY_SECTIONS or path in POLICY_EXTRA


def flatten_leaves(
    data: dict[str, Any], leaf_paths: Collection[str] = LEAF_PATHS, prefix: str = ""
) -> dict[str, Any]:
    """Flatten a config layer to dotted leaf paths, stopping at dict-typed leaves (so
    ``terminology`` is one entry, not one per alias). Paths the schema does not know are kept,
    so a caller can reject them."""
    out: dict[str, Any] = {}
    for key, value in data.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if path in leaf_paths or not isinstance(value, dict):
            out[path] = value
        else:
            out.update(flatten_leaves(value, leaf_paths, path))
    return out


def _set_path(target: dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    node = target
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value


def _pop_path(target: dict[str, Any], path: str) -> bool:
    """Remove ``path`` from a nested dict, pruning emptied parents. True if it was present."""
    parts = path.split(".")
    node = target
    trail: list[dict[str, Any]] = []
    for part in parts[:-1]:
        nxt = node.get(part)
        if not isinstance(nxt, dict):
            return False
        trail.append(node)
        node = nxt
    if parts[-1] not in node:
        return False
    del node[parts[-1]]
    for parent, part in zip(reversed(trail), reversed(parts[:-1]), strict=True):
        if not parent[part]:
            del parent[part]
        else:
            break
    return True


def diff_layer(reference: dict[str, Any], edited: dict[str, Any]) -> dict[str, Any]:
    """The nested dict of leaves where ``edited`` differs from ``reference`` (whole dicts for
    dict-typed leaves; read-only paths dropped). This is what the site file stores."""
    ref = flatten_leaves(reference)
    out: dict[str, Any] = {}
    for path, value in flatten_leaves(edited).items():
        if path in READ_ONLY_PATHS:
            continue
        if path not in ref or ref[path] != value:
            _set_path(out, path, copy.deepcopy(value))
    return out


# --------------------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------------------


def deep_merge(
    base: dict[str, Any],
    override: dict[str, Any],
    replace_at: Collection[str] = (),
    _prefix: str = "",
) -> dict[str, Any]:
    """Recursive merge, ``override`` winning. A dict at a dotted path in ``replace_at`` is
    assigned whole instead of merged, which is how a site layer deletes an entry."""
    out = copy.deepcopy(base)
    for key, value in override.items():
        path = f"{_prefix}.{key}" if _prefix else str(key)
        if isinstance(value, dict) and isinstance(out.get(key), dict) and path not in replace_at:
            out[key] = deep_merge(out[key], value, replace_at, path)
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


def _env_flag(name: str) -> bool | None:
    """True/False for a set boolean variable, None when unset or empty."""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return None
    return raw.strip().lower() in TRUTHY


def admin_enabled() -> bool:
    """Editing on the Admin page and its operations are enabled by OXIDE_TRIAGE_ADMIN."""
    return bool(_env_flag(ADMIN_ENV))


def env_locked(path: str) -> str | None:
    """The environment variable currently overriding ``path``, if any."""
    var = ENV_KEYS.get(path)
    if var is None:
        return None
    raw = os.environ.get(var)
    return var if raw is not None and raw != "" else None


def _env_overrides() -> dict[str, Any]:
    """Environment variables that a container admin sets without editing YAML."""
    out: dict[str, Any] = {}
    for path, var in ENV_KEYS.items():
        raw = os.environ.get(var)
        if raw is None or raw == "":
            continue
        value: Any = raw
        if path == "cache.offline":
            value = _env_flag(var)
        elif path == "llm.provider":
            value = raw.strip().lower()
        _set_path(out, path, value)
    return out


# --------------------------------------------------------------------------------------
# Site overrides: the one file the application writes
# --------------------------------------------------------------------------------------

SITE_HEADER = (
    "# Site configuration overrides, written by the Admin page (oxide-triage).\n"
    "# `base` holds edits to config/default.yaml (every profile); `profiles.<name>` holds edits to\n"
    "# that profile. Delete a key to restore the shipped value. Keys read from the environment\n"
    "# (OXIDE_TRIAGE_CACHE, OXIDE_TRIAGE_OFFLINE, LLM_*) win over anything here.\n"
)
_SITE_STRIPPED = ("profile_name", "cache.path")
# Keys a site file may hold from before the dielectric criterion became the configurable figure
# of merit. They are moved, not rejected, so an Admin-written site.yaml keeps loading.
_LEGACY_PATHS = {
    "weights.dielectric": "figure_of_merit.weight",
    "dielectric.low": "figure_of_merit.low",
    "dielectric.high": "figure_of_merit.high",
}


def _clean_layer(layer: dict[str, Any], name: str) -> dict[str, Any]:
    layer = copy.deepcopy(layer)
    for path in _SITE_STRIPPED:
        if _pop_path(layer, path):
            log.warning("%s: '%s' cannot be set in the site file; ignored", name, path)
    for old, new in _LEGACY_PATHS.items():
        value = flatten_leaves(layer).get(old)
        if value is not None and _pop_path(layer, old):
            _set_path(layer, new, value)
            log.warning("%s: '%s' is now '%s'; moved", name, old, new)
    unknown = [p for p in flatten_leaves(layer) if p not in LEAF_PATHS]
    if unknown:
        raise ValueError(f"{name}: unknown configuration key '{unknown[0]}'")
    return layer


class SiteOverrides(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    base: dict[str, Any] = Field(default_factory=dict)
    profiles: dict[str, dict[str, Any]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _clean(self) -> SiteOverrides:
        self.base = _clean_layer(self.base, "site.base")
        self.profiles = {
            n: _clean_layer(layer or {}, f"site.profiles.{n}") for n, layer in self.profiles.items()
        }
        return self

    def layer_for(self, profile: str) -> dict[str, Any]:
        return self.profiles.get(profile, {})

    def is_empty(self) -> bool:
        return not self.base and not any(self.profiles.values())

    def to_yaml(self) -> str:
        data = self.model_dump()
        data["profiles"] = {n: layer for n, layer in data["profiles"].items() if layer}
        return SITE_HEADER + yaml.safe_dump(data, sort_keys=False, allow_unicode=True)


def site_config_path(config_dir: Path = DEFAULT_CONFIG_DIR) -> Path | None:
    """Where the site file lives: OXIDE_TRIAGE_SITE_CONFIG, else ``site.yaml`` next to the cache
    named by OXIDE_TRIAGE_CACHE or default.yaml. ``off`` (or empty) disables it; no file for an
    in-memory cache."""
    raw = os.environ.get(SITE_CONFIG_ENV)
    if raw is not None:
        raw = raw.strip()
        if raw == "" or raw.lower() in {"off", "none", "0"}:
            return None
        return Path(raw)
    cache_path = os.environ.get("OXIDE_TRIAGE_CACHE") or str(
        _read_yaml(config_dir / "default.yaml").get("cache", {}).get("path", "")
    )
    if not cache_path or cache_path == ":memory:":
        return None
    return Path(cache_path).with_name("site.yaml")


def load_site_overrides(path: Path | None) -> SiteOverrides:
    if path is None or not path.is_file():
        return SiteOverrides()
    return SiteOverrides.model_validate(_read_yaml(path))


def save_site_overrides(path: Path, overrides: SiteOverrides) -> None:
    """Atomic write (temp file + rename) so a reader never sees a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(overrides.to_yaml(), encoding="utf-8")
    os.replace(tmp, path)


class _Auto:
    def __repr__(self) -> str:  # pragma: no cover
        return "AUTO"


AUTO = _Auto()
LAYER_NAMES = ("default", "site.base", "profile", "site.profile", "env", "overrides")
_SITE_LAYERS = frozenset({"site.base", "site.profile"})


@dataclass(frozen=True)
class ConfigLayers:
    """The layers behind one effective Config, in merge order, with provenance helpers."""

    profile: str
    site_path: Path | None
    layers: tuple[tuple[str, dict[str, Any]], ...]
    effective: Config

    def merge(self, names: Iterable[str]) -> dict[str, Any]:
        wanted = set(names)
        out: dict[str, Any] = {}
        for name, layer in self.layers:
            if name in wanted:
                out = deep_merge(out, layer, DICT_PATHS if name in _SITE_LAYERS else ())
        return out

    def shipped(self) -> dict[str, Any]:
        return self.merge(("default", "profile"))

    def with_site(self) -> dict[str, Any]:
        return self.merge(("default", "site.base", "profile", "site.profile"))

    def origin_of(self, path: str) -> str:
        """Name of the last layer that sets ``path``."""
        origin = "default"
        for name, layer in self.layers:
            if path in flatten_leaves(layer):
                origin = name
        return origin

    def reference_for(self, scope: str) -> dict[str, Any]:
        """What the scope's edits are measured against: shipped default.yaml for ``base``;
        default + site.base + the profile file for a profile scope."""
        if scope == "base":
            return self.merge(("default",))
        return self.merge(("default", "site.base", "profile"))

    def current_for(self, scope: str) -> dict[str, Any]:
        if scope == "base":
            return self.merge(("default", "site.base"))
        return self.with_site()


def config_layers(
    profile: str | None = None,
    config_dir: Path = DEFAULT_CONFIG_DIR,
    overrides: dict[str, Any] | None = None,
    use_env: bool = True,
    site_config: Path | None | _Auto = AUTO,
) -> ConfigLayers:
    """Load every layer and the effective Config. ``site_config``: AUTO discovers the site file
    (only when ``use_env`` is true), None loads the shipped configuration only, a Path is always
    read."""
    if use_env:
        load_dotenv()
    name = profile or "default"
    if not (config_dir / "default.yaml").is_file():
        raise FileNotFoundError(
            f"no configuration at {config_dir}: set {CONFIG_DIR_ENV} to the directory holding "
            "default.yaml and profiles/, or run from a directory that contains config/"
        )
    default = _read_yaml(config_dir / "default.yaml")
    prof: dict[str, Any] = {}
    if name != "default":
        profile_path = config_dir / "profiles" / f"{name}.yaml"
        if not profile_path.exists():
            available = ", ".join(list_profiles(config_dir)) or "(none)"
            raise FileNotFoundError(f"Unknown profile '{name}'. Available: {available}")
        prof = _read_yaml(profile_path)
    if isinstance(site_config, _Auto):
        site_path = site_config_path(config_dir) if use_env else None
    else:
        site_path = site_config
    site = load_site_overrides(site_path)
    layers = (
        ("default", default),
        ("site.base", site.base),
        ("profile", prof),
        ("site.profile", site.layer_for(name)),
        ("env", _env_overrides() if use_env else {}),
        ("overrides", overrides or {}),
    )
    merged: dict[str, Any] = {}
    for lname, layer in layers:
        merged = deep_merge(merged, layer, DICT_PATHS if lname in _SITE_LAYERS else ())
    cfg = Config.model_validate(merged)
    result = ConfigLayers(profile=name, site_path=site_path, layers=layers, effective=cfg)
    shipped = flatten_leaves(result.shipped())
    with_site = flatten_leaves(result.with_site())
    cfg.site_overrides = [
        SiteOverride(key=k, shipped=shipped.get(k), value=v)
        for k, v in with_site.items()
        if shipped.get(k) != v
    ]
    cfg.site_config_path = str(site_path) if site_path is not None else None
    return result


def load_config(
    profile: str | None = None,
    config_dir: Path = DEFAULT_CONFIG_DIR,
    overrides: dict[str, Any] | None = None,
    use_env: bool = True,
    site_config: Path | None | _Auto = AUTO,
) -> Config:
    return config_layers(profile, config_dir, overrides, use_env, site_config).effective


def preview_config(profile: str, site: SiteOverrides, config_dir: Path = DEFAULT_CONFIG_DIR) -> Config:
    """The Config a given site overrides object would produce for ``profile`` (shipped files plus
    the site layers, no environment). Used by the Admin page to validate and to compare hashes
    before anything is written."""
    name = profile or "default"
    merged = _read_yaml(config_dir / "default.yaml")
    merged = deep_merge(merged, site.base, DICT_PATHS)
    if name != "default":
        merged = deep_merge(merged, _read_yaml(config_dir / "profiles" / f"{name}.yaml"))
    merged = deep_merge(merged, site.layer_for(name), DICT_PATHS)
    return Config.model_validate(merged)


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


class CationFamily(BaseModel):
    id: str
    name: str
    cations: list[str]
    rationale: str = ""


def load_cation_families(filename: str = "cation_allowlist.yaml") -> list[CationFamily]:
    """Families group the allowlist's cations for the assistant's scope control. A file without
    a ``families`` key yields one family holding every cation, so scoping degrades to 'all'."""
    raw = _read_yaml(DATA_DIR / filename)
    fams = [CationFamily.model_validate(f) for f in raw.get("families", []) or []]
    if not fams:
        fams = [CationFamily(id="all", name="All cations", cations=list(raw.get("cations", [])))]
    return fams


def cations_for_families(families: list[CationFamily], selected: list[str]) -> frozenset[str]:
    """Cations covered by the selected family ids; an empty selection means every family."""
    chosen = {f.id for f in families} if not selected else set(selected)
    return frozenset(c for f in families if f.id in chosen for c in f.cations)


def load_compound_aliases(filename: str = "compound_aliases.yaml") -> dict[str, list[str]]:
    raw = _read_yaml(DATA_DIR / filename)
    return {k: list(v) for k, v in raw.items()}
