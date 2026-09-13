"""Capture the ranking baseline used by tests/test_fom_regression.py.

The snapshot is the offline PI request over the synthetic fixture under every shipped profile:
the order, every number and every string the scoring core and the refutation pass produce.
It was first captured before the dielectric criterion became the configurable figure of merit,
so the test proves that the default profiles rank exactly as they did.

    python tests/golden/capture_baseline.py --update     # re-capture on purpose
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from oxide_triage.cache import Cache
from oxide_triage.config import list_profiles, load_config
from oxide_triage.edges.llm import NullLLM
from oxide_triage.pipeline import load_fixtures, run_triage

BASELINE = Path(__file__).with_name("fom_baseline.json")
PI = (
    "Find promising oxide dielectric candidates for thin-film experiments. Prefer "
    "thermodynamically stable materials, wide band gaps, non-toxic elements, simple "
    "compositions, and public evidence. Return a ranked shortlist with caveats."
)
VARIANT = PI + " Prioritise the dielectric constant."
# Profiles that existed when the baseline was captured. A profile added later is not part of
# the regression contract and is captured only if it is named here.
PROFILES = ("default", "conservative", "exploratory", "ferroelectric-research")


def _fom_record(record: Any) -> Any:
    return getattr(record, "figure_of_merit", None) or getattr(record, "dielectric", None)


def _candidate(s: Any) -> dict[str, Any]:
    fom = _fom_record(s.record)
    return {
        "material_id": s.record.material_id,
        "formula": s.record.formula,
        "rank": s.rank,
        "raw_score": s.raw_score,
        "adjusted_score": s.adjusted_score,
        "data_coverage": s.data_coverage,
        "confidence": s.confidence,
        "tier": s.tier,
        "rationale": s.rationale,
        "missing_criteria": list(s.missing_criteria),
        "absent_criteria": list(s.absent_criteria),
        "not_retrieved_criteria": list(s.not_retrieved_criteria),
        "components": [
            {
                "criterion": c.criterion,
                "weight": c.weight,
                "normalized": c.normalized,
                "contribution": c.contribution,
                "status": c.status.value,
                "raw_label": c.raw_label,
                "notes": list(c.notes),
            }
            for c in s.components
        ],
        "gates": [g.gate for g in s.gates],
        "caveats": [c.code for c in s.caveats],
        "fom_status": fom.status.value if fom is not None else None,
        "fom_provenance_note": (fom.provenance.note if fom is not None and fom.provenance else None),
        "polymorphs": [p.material_id for p in s.polymorphs],
    }


def capture() -> dict[str, Any]:
    out: dict[str, Any] = {"request": PI, "profiles": {}}
    for profile in PROFILES:
        if profile not in list_profiles() and profile != "default":
            continue
        cfg = load_config(profile, use_env=False)
        cache = Cache(":memory:")
        load_fixtures(cfg, cache)
        res = run_triage(PI, cfg, cache=cache, offline=True, llm=NullLLM(), skip_selfcheck=True)
        ranked = res.shortlist + res.ranked_beyond_shortlist
        out["profiles"][profile] = {
            "weights": res.scoring.weights,
            "ranked": [_candidate(s) for s in ranked],
            "excluded": [
                {"material_id": s.record.material_id, "reasons": list(s.exclusion_reasons)}
                for s in res.excluded
            ],
            "collapsed": [s.record.material_id for s in res.collapsed_polymorphs],
            "n_candidates_considered": res.n_candidates_considered,
            "deviations": [d.code for d in res.deviations],
        }
    cfg = load_config("default", use_env=False)
    cache = Cache(":memory:")
    load_fixtures(cfg, cache)
    res = run_triage(VARIANT, cfg, cache=cache, offline=True, llm=NullLLM(), skip_selfcheck=True)
    out["variant"] = {
        "request": VARIANT,
        "weight_overrides": dict(res.criteria.weight_overrides),
        "weights": res.scoring.weights,
        "ranked": [s.record.material_id for s in res.shortlist + res.ranked_beyond_shortlist],
    }
    # A JSON round trip so the in-memory structure compares equal to the file.
    return json.loads(json.dumps(out, sort_keys=False))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--update", action="store_true", help="overwrite the checked-in baseline")
    args = ap.parse_args()
    snap = capture()
    if args.update or not BASELINE.exists():
        BASELINE.write_text(json.dumps(snap, indent=1) + "\n", encoding="utf-8")
        print(f"wrote {BASELINE}")
    else:
        same = snap == json.loads(BASELINE.read_text(encoding="utf-8"))
        print("baseline matches" if same else "baseline DIFFERS (run with --update to accept)")


if __name__ == "__main__":
    main()
