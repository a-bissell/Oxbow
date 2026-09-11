"""The held-out phrasings: written after the rules, never tuned against. The floor in the
file is the last measured rate; it may only rise. ``pytest -s`` prints the failing entries."""

from __future__ import annotations

from oxide_triage.config import load_config
from oxide_triage.heldout import load_set, report_lines, score


def test_heldout_rate_meets_recorded_floor():
    data = load_set()
    verdicts, rate = score(load_config())
    print("\n" + "\n".join(report_lines(verdicts, rate, data["floor"])))
    assert rate >= data["floor"], (
        f"held-out pass rate {rate:.0%} fell below the recorded floor {data['floor']:.0%}"
    )


def test_tuned_and_heldout_sets_do_not_share_phrasings():
    """A phrasing copied from the guard tests would be tuned, not held out."""
    import tests.test_guard as tuned

    tuned_texts = {tuned.PI_REQUEST.lower()}
    for name in dir(tuned):
        obj = getattr(tuned, name)
        marks = getattr(obj, "pytestmark", [])
        for mark in marks:
            if mark.name == "parametrize":
                for case in mark.args[1]:
                    text = case[0] if isinstance(case, tuple) else case
                    tuned_texts.add(text.lower().replace(tuned.PI_REQUEST.lower(), "").strip())
    held = {e["text"].lower() for e in load_set()["requests"]}
    assert not held & tuned_texts
