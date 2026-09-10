"""What the deployment can see: environment (keys masked), the site file, source reachability,
and the deviations log. Shared by ``oxide-triage doctor`` and the Admin page so both report the
same thing."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from oxide_triage.config import ADMIN_ENV, SITE_CONFIG_ENV, Config

ENV_VARS = (
    "MP_API_KEY",
    "OPENALEX_API_KEY",
    "OPENALEX_MAILTO",
    "LLM_PROVIDER",
    "ANTHROPIC_API_KEY",
    "LLM_BASE_URL",
    "LLM_MODEL",
    "LLM_API_KEY",
    "OXIDE_TRIAGE_CACHE",
    "OXIDE_TRIAGE_OFFLINE",
    SITE_CONFIG_ENV,
    ADMIN_ENV,
    "OXIDE_TRIAGE_RECORD_DIR",
)
_SECRET_MARKERS = ("KEY", "TOKEN", "SECRET")


def mask(name: str, value: str | None) -> str:
    """Never print a secret: keys are shown as length and last four characters."""
    if value is None:
        return "unset"
    if any(m in name for m in _SECRET_MARKERS) and value:
        return f"set ({len(value)} chars, ends ...{value[-4:]})"
    return value or "(empty)"


def env_status() -> list[tuple[str, str]]:
    return [(name, mask(name, os.environ.get(name))) for name in ENV_VARS]


def site_file_status(config: Config) -> dict[str, Any]:
    path = Path(config.site_config_path) if config.site_config_path else None
    if path is None:
        return {"path": None, "present": False, "writable": False, "overrides": 0}
    present = path.is_file()
    target = path if present else path.parent
    writable = os.access(target, os.W_OK) if target.exists() else os.access(path.parent.parent, os.W_OK)
    return {
        "path": str(path),
        "present": present,
        "writable": writable,
        "overrides": len(config.site_overrides),
    }


def deviation_log_path(config: Config) -> Path:
    return Path(config.cache.path).with_name("deviations.jsonl")


def read_deviation_log(config: Config, last_n: int = 200) -> list[dict[str, Any]]:
    """The last ``last_n`` deviations logged by runs on this cache, newest first, one row per
    deviation (a run with three deviations is three rows). Malformed lines are skipped."""
    path = deviation_log_path(config)
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        for d in entry.get("deviations") or []:
            rows.append(
                {
                    "ts": entry.get("ts"),
                    "profile": entry.get("profile"),
                    "origin": d.get("origin"),
                    "code": d.get("code"),
                    "description": d.get("description"),
                    "request": entry.get("request"),
                }
            )
    rows.reverse()
    return rows[:last_n]


def probe_sources(timeout_s: float = 15) -> dict[str, str]:
    """One cheap request per public source; returns a note per source. Network only."""
    import httpx

    probes = {
        "materials_project": (
            "https://api.materialsproject.org/heartbeat",
            {"X-API-KEY": os.environ.get("MP_API_KEY", "")},
        ),
        "oqmd": ("https://oqmd.org/oqmdapi/formationenergy?composition=HfO2&limit=1", {}),
        "openalex": (
            "https://api.openalex.org/works?per-page=1",
            {"Authorization": f"Bearer {k}"} if (k := os.environ.get("OPENALEX_API_KEY")) else {},
        ),
        "pubchem": ("https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/name/water/cids/JSON", {}),
    }
    out: dict[str, str] = {}
    with httpx.Client(timeout=timeout_s, trust_env=True) as client:
        for name, (url, headers) in probes.items():
            try:
                r = client.get(url, headers=headers)
                note = f"HTTP {r.status_code}"
                if name == "materials_project" and r.status_code in (401, 403):
                    note += " (key rejected or missing)"
                if name == "openalex":
                    if r.status_code == 401:
                        note += " (OPENALEX_API_KEY rejected)"
                    elif r.status_code == 429:
                        note += " (daily budget spent; resets midnight UTC)"
                    if (left := r.headers.get("x-ratelimit-remaining-usd")) is not None:
                        limit = r.headers.get("x-ratelimit-limit-usd", "?")
                        note += f", budget ${left} of ${limit}/day left"
                        if not os.environ.get("OPENALEX_API_KEY"):
                            note += " (anonymous; set OPENALEX_API_KEY for the account budget)"
            except httpx.HTTPError as exc:
                note = f"unreachable: {type(exc).__name__}"
            out[name] = note
    return out
