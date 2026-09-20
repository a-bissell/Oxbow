"""The default profiles must rank exactly as they did before the figure-of-merit seam.

``tests/golden/fom_baseline.json`` was captured on the last commit that hard-coded the
dielectric criterion. Every number, status, caveat code and label string in it must be
reproduced, except explicitly revised interface display wording and rationale. The config hash is deliberately not part of the snapshot: the YAML shape changed.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

GOLDEN = Path(__file__).parent / "golden"
sys.path.insert(0, str(GOLDEN))

from capture_baseline import BASELINE, capture  # noqa: E402


def _ranking_contract(value):
    """Preserve the historical numeric baseline; new wording has dedicated output tests."""
    if isinstance(value, list):
        return [_ranking_contract(v) for v in value]
    if isinstance(value, dict):
        return {
            k: _ranking_contract(v)
            for k, v in value.items()
            if k != "rationale" and not (k == "raw_label" and value.get("criterion") == "interface")
        }
    return value


@pytest.fixture(scope="module")
def snapshot():
    return _ranking_contract(capture())


@pytest.fixture(scope="module")
def baseline():
    return _ranking_contract(json.loads(BASELINE.read_text(encoding="utf-8")))


def test_baseline_profiles_are_all_present(baseline, snapshot):
    assert set(baseline["profiles"]) <= set(snapshot["profiles"])


@pytest.mark.parametrize("profile", ["default", "conservative", "exploratory", "ferroelectric-research"])
def test_ranking_is_unchanged(baseline, snapshot, profile):
    want, got = baseline["profiles"][profile], snapshot["profiles"][profile]
    assert got["weights"] == want["weights"]
    assert list(got["weights"]) == list(want["weights"]), "weight key order changed"
    assert [c["material_id"] for c in got["ranked"]] == [c["material_id"] for c in want["ranked"]]
    for g, w in zip(got["ranked"], want["ranked"], strict=True):
        assert g == w, f"{profile}: {w['formula']} differs"
    assert got["excluded"] == want["excluded"]
    assert got["collapsed"] == want["collapsed"]
    assert got["n_candidates_considered"] == want["n_candidates_considered"]
    assert got["deviations"] == want["deviations"]


def test_request_weight_override_is_unchanged(baseline, snapshot):
    assert snapshot["variant"] == baseline["variant"]
