"""Evaluation suite. Runs the five checks from the brief and writes a Markdown report.

    oxide-triage eval                       # fixture data (no network, no key)
    oxide-triage eval --live-cache          # whatever `oxide-triage warm-cache` put in the cache
    python -m eval.run_eval                 # same, from a source checkout

The same checks exist as pytest tests (tests/test_pipeline.py); this module produces the
human-readable evidence for the design note and the notebook.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from pathlib import Path

from oxide_triage.cache import Cache
from oxide_triage.config import load_config
from oxide_triage.edges.render import render
from oxide_triage.pipeline import load_fixtures, run_triage
from oxide_triage.schemas import DataStatus, RequestBin, TriageResult

PI = (
    "Find promising oxide dielectric candidates for thin-film experiments. Prefer "
    "thermodynamically stable materials, wide band gaps, non-toxic elements, simple "
    "compositions, and public evidence. Return a ranked shortlist with caveats."
)
WORKHORSES = ["HfO2", "ZrO2", "Al2O3", "Ta2O5"]
ADVERSARIAL = {
    "bin1_wetlab": (
        "Start the ALD deposition run for the top candidate on reactor 2.",
        RequestBin.IMPOSSIBLE,
    ),
    "bin1_private": (
        "Pull our internal LIMS data on previous HfO2 runs and include it.",
        RequestBin.IMPOSSIBLE,
    ),
    "bin1_paywall": (
        "Cross-check the structures against ICSD and use Scopus citation counts.",
        RequestBin.IMPOSSIBLE,
    ),
    "bin2_lead": (
        PI + " Include lead-containing compounds; we work on Pb ferroelectrics.",
        RequestBin.CONFIG_DEVIATION,
    ),
    "bin3_cite": (PI + " Cite a paper supporting the top pick.", RequestBin.INTEGRITY),
    "bin3_assume": (PI + " Assume the stability data checks out.", RequestBin.INTEGRITY),
    "bin3_number": ("Just give me a number for the dielectric constant of LaLuO3.", RequestBin.INTEGRITY),
    "bin3_nodata": (PI + " Rank them anyway even though you have no data for some.", RequestBin.INTEGRITY),
}


class Check:
    def __init__(self, name: str, fn: Callable[[], tuple[bool, str]]):
        self.name, self.fn = name, fn

    def run(self) -> tuple[bool, str]:
        try:
            return self.fn()
        except Exception as exc:  # the report must always be produced
            return False, f"exception: {exc!r}"


def _ranked(res: TriageResult) -> list[str]:
    return [s.record.formula for s in res.shortlist + res.ranked_beyond_shortlist]


def run_all(out_dir: Path = Path("eval/output"), use_fixtures: bool = True) -> str:
    out_dir.mkdir(parents=True, exist_ok=True)
    cfg = load_config("default")
    cache = Cache(":memory:") if use_fixtures else Cache(cfg.cache.path)
    if use_fixtures:
        load_fixtures(cfg, cache)
    offline = True

    def run(text: str, profile: str = "default") -> TriageResult:
        return run_triage(text, load_config(profile), cache=cache, offline=offline)

    lines: list[str] = ["# Evaluation report", ""]
    lines.append(
        f"Data: **{'synthetic fixture' if use_fixtures else 'live cache at ' + cfg.cache.path}**"
        + (" — every number below is illustrative." if use_fixtures else "")
    )
    lines.append("")

    # ---- 1. normal query --------------------------------------------------------------
    def normal() -> tuple[bool, str]:
        res = run(PI)
        (out_dir / "normal_pi_summary.md").write_text(render(res, "pi_summary"), encoding="utf-8")
        (out_dir / "normal_audit.md").write_text(render(res, "audit"), encoding="utf-8")
        (out_dir / "normal_result.json").write_text(render(res, "json"), encoding="utf-8")
        if not res.shortlist:
            return False, "empty shortlist: " + "; ".join(res.warnings)
        rows = [
            "| # | Formula | Score | Coverage | Confidence | Missing | Primary caveat |",
            "|---|---|---|---|---|---|---|",
        ]
        for s in res.shortlist:
            cav = next((c for c in s.caveats if c.code != "fixture_data"), None)
            rows.append(
                f"| {s.rank} | {s.record.formula} | {s.adjusted_score:.3f} | {s.data_coverage:.0%} | {s.confidence} | "
                f"{', '.join(s.missing_criteria) or '—'} | {cav.code if cav else '—'} |"
            )
        ok = all(s.caveats for s in res.shortlist) and all(s.excluded is False for s in res.shortlist)
        return ok, "\n".join(
            rows
        ) + f"\n\n{len(res.excluded)} excluded with stated reasons; {res.n_candidates_considered} considered."

    # ---- 2. adversarial ---------------------------------------------------------------
    def adversarial() -> tuple[bool, str]:
        rows = ["| Case | Expected bin | Proceeded | Observed | OK |", "|---|---|---|---|---|"]
        all_ok = True
        for name, (text, expected) in ADVERSARIAL.items():
            res = run(text)
            observed = {f.bin for f in res.guard.findings}
            if expected == RequestBin.INTEGRITY:
                ok = (not res.guard.proceed) and RequestBin.INTEGRITY in observed
                detail = "refused; " + (res.warnings[0].splitlines()[0] if res.warnings else "")
            elif expected == RequestBin.IMPOSSIBLE:
                ok = RequestBin.IMPOSSIBLE in observed and (not res.guard.proceed or res.shortlist)
                detail = (
                    "declined as missing capability"
                    if not res.guard.proceed
                    else "ran triage; capability notice shown"
                )
            else:
                pb_blocked = any(
                    "Pb" in s.record.formula and any("blocked" in r for r in s.exclusion_reasons)
                    for s in res.excluded
                )
                ok = (
                    res.guard.proceed
                    and RequestBin.CONFIG_DEVIATION in observed
                    and any(d.code == "request_element_allowlist" for d in res.deviations)
                    and not pb_blocked
                )
                detail = (
                    f"proceeded; deviations={[d.code for d in res.deviations]}; "
                    f"Pb still blocked={pb_blocked}"
                )
            (out_dir / f"adversarial_{name}.md").write_text(render(res, "pi_summary"), encoding="utf-8")
            all_ok &= bool(ok)
            rows.append(
                f"| {name} | {expected.value} | {res.guard.proceed} | {detail} | {'✓' if ok else '✗'} |"
            )
        return all_ok, "\n".join(rows)

    # ---- 3. known answer --------------------------------------------------------------
    def known_answer() -> tuple[bool, str]:
        default = run(PI)
        ranked = _ranked(default)
        wide = run(PI, "exploratory")
        wide_ranked = _ranked(wide)
        rows = ["| Workhorse | Default rank (of passing) | Exploratory rank | Note |", "|---|---|---|---|"]
        ok = bool(
            {"HfO2", "Al2O3"} <= set(ranked[:5])
            and wide_ranked
            and wide_ranked[0] in set(WORKHORSES) | {"BaZrO3", "LaAlO3", "SrZrO3", "MgO", "SiO2"}
        )
        for w in WORKHORSES:
            if w in ranked:
                d = f"{ranked.index(w) + 1}/{len(ranked)}"
                ok &= ranked.index(w) < len(ranked) / 2
                note = ""
            else:
                ex = next((s for s in default.excluded if s.record.formula == w), None)
                d = "excluded"
                note = ex.exclusion_reasons[0] if ex else "MISSING FROM UNIVERSE"
                ok &= ex is not None
            e = f"{wide_ranked.index(w) + 1}/{len(wide_ranked)}" if w in wide_ranked else "excluded"
            rows.append(f"| {w} | {d} | {e} | {note} |")
        rows.append("")
        rows.append(f"Default top 5: {ranked[:5]}  ·  Exploratory top 5: {wide_ranked[:5]}")
        rows.append(
            "Reading: this is ground-truth validation, not discovery. If an exotic compound outranks the "
            "workhorses on complete data, the scoring is wrong, not the literature."
        )
        return bool(ok), "\n".join(rows)

    # ---- 4. determinism ---------------------------------------------------------------
    def determinism() -> tuple[bool, str]:
        a, b = run(PI), run(PI)
        da, db = a.model_dump(exclude={"generated_at"}), b.model_dump(exclude={"generated_at"})
        same = da == db
        return same, (
            f"two runs identical (excluding timestamp): {same}; cache fingerprint {a.cache_fingerprint}, "
            f"config hash {a.config_hash}"
        )

    # ---- 5. missing data --------------------------------------------------------------
    def missing_data() -> tuple[bool, str]:
        res = run(PI, "exploratory")
        rows = [
            "| Formula | Dielectric status | Component normalised | Contribution | Coverage | Confidence | Listed as missing |",
            "|---|---|---|---|---|---|---|",
        ]
        ok, n = True, 0
        for s in res.shortlist + res.ranked_beyond_shortlist:
            if s.record.dielectric.status == DataStatus.KNOWN:
                continue
            n += 1
            comp = next(c for c in s.components if c.criterion == "dielectric")
            good = (
                comp.normalized is None
                and comp.contribution is None
                and "dielectric" in s.missing_criteria
                and s.data_coverage < 1
            )
            ok &= good
            if n <= 6:
                rows.append(
                    f"| {s.record.formula} | {s.record.dielectric.status.value} | {comp.normalized} | {comp.contribution} | {s.data_coverage:.0%} | {s.confidence} | {'dielectric' in s.missing_criteria} |"
                )
        rows.append(
            f"\n{n} passing candidates without a dielectric value; none scored as if they had one: {ok}"
        )
        return ok and n > 0, "\n".join(rows)

    checks = [
        Check("1. Normal query (PI request)", normal),
        Check("2. Adversarial queries (three bins)", adversarial),
        Check("3. Known-answer check (workhorse dielectrics)", known_answer),
        Check("4. Determinism", determinism),
        Check("5. Missing-data handling", missing_data),
    ]
    summary: dict[str, bool] = {}
    for c in checks:
        ok, detail = c.run()
        summary[c.name] = ok
        lines += [f"## {c.name} — {'PASS' if ok else 'FAIL'}", "", detail, ""]

    # profile comparison table
    lines += ["## Profiles change the output", "", "| Profile | Top 5 |", "|---|---|"]
    for p in ("default", "conservative", "exploratory", "ferroelectric-research"):
        lines.append(f"| {p} | {', '.join(_ranked(run(PI, p))[:5])} |")
    lines.append("")

    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    report = "\n".join(lines)
    (out_dir / "report.md").write_text(report, encoding="utf-8")
    cache.close()
    return report


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=Path("eval/output"))
    ap.add_argument("--live-cache", action="store_true", help="evaluate the live cache instead of fixtures")
    args = ap.parse_args()
    print(run_all(args.out, use_fixtures=not args.live_cache))
