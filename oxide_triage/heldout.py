"""Score the held-out request phrasings against the request path.

The rules in ``guard.py`` and ``edges/parse.py`` were written against the phrasings in
``tests/test_guard.py``; those pass by construction. ``data/heldout_requests.yaml`` holds
phrasings written afterwards and never tuned against. This module runs each one through the
same guard and rule parser the pipeline uses (no model, so the answer is deterministic and
needs no key) and compares what the tool would do with what the file says an honest outcome
is. The evaluation prints both pass rates; the difference is the honest measure of how far the
rules generalise beyond their own tests.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from oxide_triage.config import DATA_DIR, Config, load_hazard_table
from oxide_triage.edges.parse import apply_terminology, criterion_words, rule_parse
from oxide_triage.guard import guard_request, scope_vocabulary
from oxide_triage.schemas import RequestBin
from oxide_triage.scoring.settings import blocked_by_policy, never_liftable

DEFAULT_SET = DATA_DIR / "heldout_requests.yaml"

# Guard bins as the file names them.
_REASON_OF_BIN = {
    RequestBin.INTEGRITY: "integrity",
    RequestBin.IMPOSSIBLE: "impossible",
    RequestBin.OVERRIDE: "override",
    RequestBin.OUT_OF_SCOPE: "out_of_scope",
    RequestBin.HAZARD_POLICY: "never_lift",
}


@dataclass
class Assessment:
    """What the tool would do with a request, in the terms the held-out file uses."""

    runs: bool
    acknowledged: bool
    lifted: list[str]
    reasons: set[str] = field(default_factory=set)
    unhandled: list[str] = field(default_factory=list)


def assess_request(text: str, config: Config) -> Assessment:
    """The request path up to, but not including, the run: guard, then the rule parser."""
    table = load_hazard_table(config.toxicity.table_file)
    blocked = blocked_by_policy(config, table)
    guard = guard_request(text, table, blocked, never_liftable(config), scope=scope_vocabulary(config))
    reasons = {_REASON_OF_BIN[f.bin] for f in guard.findings if f.bin in _REASON_OF_BIN}
    lifted: list[str] = []
    unhandled: list[str] = []
    if guard.proceed:
        criteria = rule_parse(
            apply_terminology(text, config.terminology),
            table,
            blocked,
            substrate=config.interface.substrate,
            words=criterion_words(config.figure_of_merit),
            vocabulary=config.figure_of_merit.vocabulary,
        )
        lifted = list(criteria.allow_elements)
        unhandled = list(criteria.unhandled)
        if unhandled:
            reasons.add("unsupported")
    # A refusal names its reason; a run acknowledges what the guard flagged as not done and
    # what the parser left unread. (Configuration deviations are honoured, so they are not
    # an acknowledgement.)
    acknowledged = (
        (not guard.proceed)
        or any(f.bin in (RequestBin.OVERRIDE, RequestBin.IMPOSSIBLE) for f in guard.findings)
        or bool(unhandled)
    )
    return Assessment(
        runs=guard.proceed, acknowledged=acknowledged, lifted=lifted, reasons=reasons, unhandled=unhandled
    )


@dataclass
class Verdict:
    id: str
    text: str
    passed: bool
    expected: dict[str, Any]
    observed: Assessment
    mismatches: list[str]
    tuned_after: str | None = None  # the rule change made for this phrasing, if any


def load_set(path: Path = DEFAULT_SET) -> dict[str, Any]:
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def judge(entry: dict[str, Any], observed: Assessment) -> list[str]:
    """Every field the entry states must match; fields it leaves out are not judged."""
    bad: list[str] = []
    if observed.runs != entry["runs"]:
        bad.append(f"runs: expected {entry['runs']}, got {observed.runs}")
    if observed.acknowledged != entry["acknowledged"]:
        bad.append(f"acknowledged: expected {entry['acknowledged']}, got {observed.acknowledged}")
    if "lifted" in entry and sorted(observed.lifted) != sorted(entry["lifted"]):
        bad.append(f"lifted: expected {entry['lifted']}, got {observed.lifted}")
    if "reason" in entry and entry["acknowledged"]:
        wanted = entry["reason"] if isinstance(entry["reason"], list) else [entry["reason"]]
        if not observed.reasons.intersection(wanted):
            bad.append(f"reason: expected one of {wanted}, got {sorted(observed.reasons) or 'none'}")
    return bad


def score(config: Config, path: Path = DEFAULT_SET) -> tuple[list[Verdict], float]:
    data = load_set(path)
    verdicts: list[Verdict] = []
    for entry in data["requests"]:
        observed = assess_request(entry["text"], config)
        mismatches = judge(entry, observed)
        verdicts.append(
            Verdict(
                id=entry["id"],
                text=entry["text"],
                passed=not mismatches,
                expected={
                    k: v for k, v in entry.items() if k in ("runs", "acknowledged", "lifted", "reason")
                },
                observed=observed,
                mismatches=mismatches,
                tuned_after=entry.get("tuned_after"),
            )
        )
    rate = sum(v.passed for v in verdicts) / len(verdicts) if verdicts else 0.0
    return verdicts, rate


def report_lines(verdicts: list[Verdict], rate: float, floor: float) -> list[str]:
    lines = [
        f"Held-out request phrasings: {sum(v.passed for v in verdicts)}/{len(verdicts)} "
        f"({rate:.0%}) against a floor of {floor:.0%}.",
    ]
    untouched = [v for v in verdicts if not v.tuned_after]
    if len(untouched) < len(verdicts):
        lines.append(
            f"Entries no rule was widened for after they were seen: "
            f"{sum(v.passed for v in untouched)}/{len(untouched)}; the other "
            f"{len(verdicts) - len(untouched)} pass after a vocabulary or rule change made for their kind of phrasing."
        )
    failing = [v for v in verdicts if not v.passed]
    if failing:
        lines.append("Not handled as the file expects:")
        for v in failing:
            lines.append(f'  - {v.id}: "{v.text}"')
            for m in v.mismatches:
                lines.append(f"      {m}")
            if v.observed.unhandled:
                lines.append(f"      not acted on: {'; '.join(v.observed.unhandled)}")
    return lines
