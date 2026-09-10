"""Thin wrapper so `python -m eval.run_eval` keeps working from a source checkout.
The implementation lives in the installed package: ``oxide_triage.evaluation``."""

from __future__ import annotations

import argparse
from pathlib import Path

from oxide_triage.evaluation import run_all

__all__ = ["run_all"]

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=Path("eval/output"))
    ap.add_argument("--live-cache", action="store_true", help="evaluate the live cache instead of fixtures")
    args = ap.parse_args()
    print(run_all(args.out, use_fixtures=not args.live_cache))
