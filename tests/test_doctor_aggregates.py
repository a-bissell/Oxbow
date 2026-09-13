"""The two platform-feedback aggregates in ``doctor.py``: deviation counts by code, origin and
profile, and the data-gap ledger with ``absent`` and ``not_retrieved`` kept apart. Both are
read-only reporting over append-only jsonl files, so they must survive an empty file, a missing
file and a half-written line."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from oxide_triage.config import load_config
from oxide_triage.doctor import (
    aggregate_deviations,
    aggregate_retrieval_gaps,
    read_deviation_log,
    retrieval_log_path,
)

NOW = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)


def _cfg(tmp_path):
    return load_config("default", use_env=False, overrides={"cache": {"path": str(tmp_path / "c.sqlite")}})


def _dev_entry(ts: datetime, profile: str, *devs: tuple[str, str]) -> str:
    return json.dumps(
        {
            "ts": ts.isoformat(),
            "actor": {"who": "x", "via": "cli", "how": ""},
            "profile": profile,
            "request": "r",
            "deviations": [{"code": c, "origin": o, "description": f"{c} via {o}"} for c, o in devs],
        }
    )


def _ret_entry(ts: datetime, absent: dict, nr: dict, completeness: float = 0.9) -> str:
    return json.dumps(
        {
            "ts": ts.isoformat(),
            "profile": "default",
            "request": "r",
            "completeness": completeness,
            "n_ranked": 10,
            "n_fully_retrieved": 5,
            "not_retrieved_by_criterion": nr,
            "absent_by_criterion": absent,
            "comparable": completeness >= 0.8,
        }
    )


def test_aggregates_over_missing_and_empty_files(tmp_path):
    cfg = _cfg(tmp_path)
    assert aggregate_deviations(cfg)["n_deviations"] == 0
    assert aggregate_retrieval_gaps(cfg)["n_runs"] == 0
    (tmp_path / "deviations.jsonl").write_text("")
    (tmp_path / "retrieval.jsonl").write_text("\n\n")
    assert aggregate_deviations(cfg) == {
        "window": {"since": None, "until": None},
        "n_runs": 0,
        "n_deviations": 0,
        "by_code": {},
        "by_origin": {},
        "by_profile": {},
        "by_code_and_origin": [],
    }
    gaps = aggregate_retrieval_gaps(cfg)
    assert gaps["absent_by_criterion"] == {} and gaps["not_retrieved_by_criterion"] == {}
    assert gaps["mean_completeness"] is None


def test_deviations_counted_by_code_origin_and_profile_skipping_malformed_lines(tmp_path):
    cfg = _cfg(tmp_path)
    lines = [
        _dev_entry(NOW - timedelta(days=40), "default", ("site_override", "site")),
        _dev_entry(
            NOW - timedelta(days=2), "default", ("hazard_block_lifted", "request"), ("gate_moved", "request")
        ),
        '{"ts": "2026-09-12T00:00:00+00:00", "profile": "default", "deviations": [{"code": "gate_moved"',  # cut short
        "not json at all",
        _dev_entry(
            NOW - timedelta(days=1), "exploratory", ("gate_moved", "profile"), ("gate_moved", "request")
        ),
    ]
    (tmp_path / "deviations.jsonl").write_text("\n".join(lines) + "\n")

    agg = aggregate_deviations(cfg)
    assert agg["n_runs"] == 3 and agg["n_deviations"] == 5
    assert agg["by_code"] == {"gate_moved": 3, "hazard_block_lifted": 1, "site_override": 1}
    assert agg["by_origin"] == {"request": 3, "profile": 1, "site": 1}
    assert agg["by_profile"] == {"default": 3, "exploratory": 2}
    top = agg["by_code_and_origin"][0]
    assert (top["code"], top["origin"], top["count"]) == ("gate_moved", "request", 2)
    assert top["profiles"] == ["default", "exploratory"]
    assert top["last_ts"] == (NOW - timedelta(days=1)).isoformat()
    assert top["example"] == "gate_moved via request"
    # The flat reader skips the same malformed lines and sees the same five deviations.
    assert len(read_deviation_log(cfg)) == 5

    windowed = aggregate_deviations(cfg, since=NOW - timedelta(days=30))
    assert windowed["n_runs"] == 2 and "site" not in windowed["by_origin"]
    assert windowed["window"]["since"] == (NOW - timedelta(days=30)).isoformat()


def test_window_leaves_out_entries_whose_timestamp_cannot_be_read(tmp_path):
    cfg = _cfg(tmp_path)
    undated = json.dumps(
        {"profile": "default", "deviations": [{"code": "c", "origin": "cli", "description": ""}]}
    )
    (tmp_path / "deviations.jsonl").write_text(undated + "\n")
    assert aggregate_deviations(cfg)["n_deviations"] == 1  # no window: everything counts
    assert aggregate_deviations(cfg, since=NOW - timedelta(days=1))["n_deviations"] == 0


def test_gap_ledger_keeps_absent_and_not_retrieved_apart(tmp_path):
    cfg = _cfg(tmp_path)
    lines = [
        _ret_entry(NOW - timedelta(days=3), {"dielectric": 4, "cross_check": 1}, {"literature": 6}, 0.7),
        _ret_entry(NOW - timedelta(days=1), {"dielectric": 3}, {"literature": 2, "cross_check": 5}, 0.9),
        '{"ts": "2026-09-13T00:00:00+00:00", "absent_by_criterion": {"dielectric": ',  # cut short
        json.dumps(
            {
                "ts": NOW.isoformat(),
                "absent_by_criterion": {"dielectric": "many"},
                "not_retrieved_by_criterion": [],
            }
        ),
    ]
    (tmp_path / "retrieval.jsonl").write_text("\n".join(lines) + "\n")

    gaps = aggregate_retrieval_gaps(cfg)
    assert gaps["n_runs"] == 3  # the last line is well-formed json with unusable counts: a run, no gaps
    assert gaps["n_incomparable"] == 1
    assert gaps["absent_by_criterion"] == {
        "dielectric": {"candidates": 7, "runs": 2},
        "cross_check": {"candidates": 1, "runs": 1},
    }
    assert gaps["not_retrieved_by_criterion"] == {
        "literature": {"candidates": 8, "runs": 2},
        "cross_check": {"candidates": 5, "runs": 1},
    }
    # The same criterion can be in both tables; it is never summed across them.
    assert gaps["absent_by_criterion"]["cross_check"] != gaps["not_retrieved_by_criterion"]["cross_check"]
    assert gaps["mean_completeness"] == pytest.approx((0.7 + 0.9 + 0.0) / 3, abs=1e-4)

    recent = aggregate_retrieval_gaps(cfg, since=NOW - timedelta(days=2))
    assert recent["n_runs"] == 2 and recent["absent_by_criterion"]["dielectric"] == {
        "candidates": 3,
        "runs": 1,
    }


def test_every_run_appends_to_the_retrieval_ledger_without_an_actor(tmp_path):
    from oxide_triage.actor import Actor
    from oxide_triage.pipeline import load_fixtures, run_triage

    cfg = _cfg(tmp_path)
    load_fixtures(cfg)
    who = Actor(who="jsmith", via="web", how="header")
    res = run_triage("Find oxide dielectrics for thin films.", cfg, offline=True, confirmed=True, actor=who)
    assert res.retrieval is not None
    rows = [json.loads(line) for line in retrieval_log_path(cfg).read_text().splitlines()]
    assert len(rows) == 1  # the self-check that ran on fixture load is not a query and is not in the ledger
    row = rows[0]
    assert row["ts"] == res.generated_at and row["profile"] == "default"
    assert row["completeness"] == res.retrieval.completeness
    assert row["absent_by_criterion"] == res.retrieval.absent_by_criterion
    assert row["not_retrieved_by_criterion"] == res.retrieval.not_retrieved_by_criterion
    assert "actor" not in row  # gaps are a property of the cache, not of who asked
    assert aggregate_retrieval_gaps(cfg)["n_runs"] == 1


def test_a_failed_retrieval_append_does_not_fail_the_run(tmp_path):
    from oxide_triage.pipeline import load_fixtures, run_triage

    cfg = _cfg(tmp_path)
    load_fixtures(cfg)
    (tmp_path / "retrieval.jsonl").mkdir()  # a directory where the file should be: every open fails
    res = run_triage("Find oxide dielectrics for thin films.", cfg, offline=True, confirmed=True)
    assert res.shortlist
