"""What the deployment can see: environment (keys masked), the site file, source reachability,
and the two append-only logs written next to the cache: ``deviations.jsonl`` (which gates a run
relaxed, and who decided) and ``retrieval.jsonl`` (which criteria went unfetched or are absent
from every public source). Shared by ``oxide-triage doctor`` and the Admin page so both report
the same thing.

The ``aggregate_*`` functions are read-only reporting for the platform team. They must never
propose, apply or auto-tune a ranking parameter: rank is a pure function of cached data plus
explicit config, and a telemetry path that adjusted defaults would break determinism and the
traceability argument with it. They inform a human, and a human changes config.

Both logs are append-only and grow without bound; rotation is left to the site (a logrotate
entry or a periodic truncate is enough, since nothing reads them back into a run).
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from oxide_triage.config import ADMIN_ENV, SITE_CONFIG_ENV, Config

ENV_VARS = (
    "MP_API_KEY",
    "OPENALEX_API_KEY",
    "OPENALEX_MAILTO",
    "LLM_PROVIDER",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
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


def retrieval_log_path(config: Config) -> Path:
    return Path(config.cache.path).with_name("retrieval.jsonl")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Every well-formed object in an append-only jsonl file, in file order. Blank and malformed
    lines (a write cut short by a crash, say) are skipped rather than failing the report."""
    if not path.is_file():
        return []
    entries: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    return entries


def _parse_ts(value: Any) -> datetime | None:
    try:
        ts = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=UTC)


def _in_window(entry: dict[str, Any], since: datetime | None, until: datetime | None) -> bool:
    """With no window every entry counts. With one, an entry whose timestamp cannot be read is
    left out: it cannot be placed in time, so counting it would misstate the window."""
    if since is None and until is None:
        return True
    ts = _parse_ts(entry.get("ts"))
    if ts is None:
        return False
    return (since is None or ts >= since) and (until is None or ts <= until)


def _window(since: datetime | None, until: datetime | None) -> dict[str, str | None]:
    return {"since": since.isoformat() if since else None, "until": until.isoformat() if until else None}


def read_deviation_log(config: Config, last_n: int = 200) -> list[dict[str, Any]]:
    """The last ``last_n`` deviations logged by runs on this cache, newest first, one row per
    deviation (a run with three deviations is three rows). Malformed lines are skipped."""
    rows: list[dict[str, Any]] = []
    for entry in _read_jsonl(deviation_log_path(config)):
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


def aggregate_deviations(
    config: Config, since: datetime | None = None, until: datetime | None = None
) -> dict[str, Any]:
    """Counts of logged deviations by ``code``, by ``origin`` and by ``profile`` over an optional
    window, plus the ``code x origin`` table that answers "which gates does this site keep
    relaxing, and who decided".

    ``origin`` is the product signal: ``request`` means a scientist works around the defaults
    ad hoc, and if it repeats the profile wants retuning for that group; ``site`` means an admin
    has already decided the shipped default is wrong here; ``profile`` and ``cli`` are expected
    and low signal. Read-only: this reports to a human, it never changes a default.
    """
    by_code: dict[str, int] = {}
    by_origin: dict[str, int] = {}
    by_profile: dict[str, int] = {}
    cross: dict[tuple[str, str], dict[str, Any]] = {}
    n_runs = 0
    n_dev = 0
    for entry in _read_jsonl(deviation_log_path(config)):
        if not _in_window(entry, since, until):
            continue
        devs = [d for d in entry.get("deviations") or [] if isinstance(d, dict)]
        if not devs:
            continue
        n_runs += 1
        profile = str(entry.get("profile") or "?")
        for d in devs:
            code, origin = str(d.get("code") or "?"), str(d.get("origin") or "?")
            n_dev += 1
            by_code[code] = by_code.get(code, 0) + 1
            by_origin[origin] = by_origin.get(origin, 0) + 1
            by_profile[profile] = by_profile.get(profile, 0) + 1
            row = cross.setdefault(
                (code, origin),
                {
                    "code": code,
                    "origin": origin,
                    "count": 0,
                    "profiles": [],
                    "last_ts": None,
                    "example": None,
                },
            )
            row["count"] += 1
            if profile not in row["profiles"]:
                row["profiles"].append(profile)
            ts = entry.get("ts")
            if isinstance(ts, str) and (row["last_ts"] is None or ts > row["last_ts"]):
                row["last_ts"] = ts
                row["example"] = d.get("description")
    desc = lambda d: dict(sorted(d.items(), key=lambda kv: (-kv[1], kv[0])))  # noqa: E731
    return {
        "window": _window(since, until),
        "n_runs": n_runs,
        "n_deviations": n_dev,
        "by_code": desc(by_code),
        "by_origin": desc(by_origin),
        "by_profile": desc(by_profile),
        "by_code_and_origin": sorted(cross.values(), key=lambda r: (-r["count"], r["code"], r["origin"])),
    }


def aggregate_retrieval_gaps(
    config: Config, since: datetime | None = None, until: datetime | None = None
) -> dict[str, Any]:
    """Per-criterion totals from the retrieval ledger over an optional window, ``absent`` and
    ``not_retrieved`` kept apart because their remedies differ. ``absent``: no permitted public
    source holds the value, so the fix is a new source or a measurement. ``not_retrieved``: this
    cache never fetched it, so the fix is ops (a rate limit, a failed warm, a paused source).
    Each criterion reports ``candidates`` (candidate-runs: the same material missing in ten runs
    counts ten) and ``runs`` (runs in which at least one candidate was missing it).
    Read-only: this reports to a human, it never changes a default.
    """

    def tally(into: dict[str, dict[str, int]], counts: Any) -> None:
        if not isinstance(counts, dict):
            return
        for crit, n in counts.items():
            if not isinstance(n, int) or n <= 0:
                continue
            row = into.setdefault(str(crit), {"candidates": 0, "runs": 0})
            row["candidates"] += n
            row["runs"] += 1

    absent: dict[str, dict[str, int]] = {}
    not_retrieved: dict[str, dict[str, int]] = {}
    n_runs = n_incomparable = 0
    completeness_sum = 0.0
    for entry in _read_jsonl(retrieval_log_path(config)):
        if not _in_window(entry, since, until):
            continue
        n_runs += 1
        tally(absent, entry.get("absent_by_criterion"))
        tally(not_retrieved, entry.get("not_retrieved_by_criterion"))
        c = entry.get("completeness")
        completeness_sum += float(c) if isinstance(c, (int, float)) else 0.0
        if entry.get("comparable") is False:
            n_incomparable += 1
    order = lambda d: dict(sorted(d.items(), key=lambda kv: (-kv[1]["candidates"], kv[0])))  # noqa: E731
    return {
        "window": _window(since, until),
        "n_runs": n_runs,
        "n_incomparable": n_incomparable,
        "mean_completeness": round(completeness_sum / n_runs, 4) if n_runs else None,
        "absent_by_criterion": order(absent),
        "not_retrieved_by_criterion": order(not_retrieved),
    }


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
                        # Prepaid credit is spent after the daily budget and keeps requests
                        # succeeding at 200 when the daily figure reads zero.
                        if (prepaid := r.headers.get("x-ratelimit-prepaid-remaining-usd")) is not None:
                            note += f", prepaid credit ${prepaid} left"
                        if not os.environ.get("OPENALEX_API_KEY"):
                            note += " (anonymous; set OPENALEX_API_KEY for the account budget)"
            except httpx.HTTPError as exc:
                note = f"unreachable: {type(exc).__name__}"
            out[name] = note
    return out
