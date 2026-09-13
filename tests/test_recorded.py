"""Response recording and replay.

Two layers:
  * unit tests of the recorder/replayer round trip (always run);
  * a live-shape regression test that replays real responses recorded with
    ``oxide-triage warm-cache --record tests/recorded`` through the whole data layer and
    self-check. It is skipped until recordings exist, and becomes the permanent evidence that the
    clients agree with the real APIs once the first live warm has been captured.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from oxide_triage.cache import Cache
from oxide_triage.config import load_config, load_hazard_table
from oxide_triage.schemas import Criteria, DataStatus
from oxide_triage.scoring.core import rank, retrieval_completeness
from oxide_triage.scoring.settings import resolve
from oxide_triage.selfcheck import run_selfcheck
from oxide_triage.sources.assemble import DataLayer
from oxide_triage.sources.base import Http, Recorder, ReplayHttp, SourceError, request_key

RECORDED = Path(__file__).parent / "recorded"
TABLE = load_hazard_table(load_config("default", use_env=False).toxicity.table_file)


class CannedTransport(Http):
    """An Http whose network call is replaced; exercises the recorder hook in the real class."""

    def __init__(self, canned: dict[str, Any], recorder: Recorder):
        super().__init__(recorder=recorder)
        self.canned = canned

    def _get_json(self, url, params=None, headers=None):
        try:
            return self.canned[url]
        except KeyError as exc:
            raise SourceError("nope") from exc


def test_request_key_ignores_volatile_params_and_order():
    a = request_key("https://x/y", {"b": 1, "a": 2, "mailto": "me@x"})
    b = request_key("https://x/y", {"a": 2, "b": 1})
    assert a == b and a != request_key("https://x/y", {"a": 3, "b": 1})


def test_record_then_replay_roundtrip(tmp_path):
    rec = Recorder(tmp_path)
    http = CannedTransport({"https://api.example.org/v": {"data": [1, 2]}}, rec)
    assert http.get_json("https://api.example.org/v", {"q": "x", "mailto": "a@b"}) == {"data": [1, 2]}
    files = list(tmp_path.rglob("*.json"))
    assert len(files) == 1 and files[0].parent.name == "api.example.org"
    text = files[0].read_text()
    assert "mailto" not in text and "X-API-KEY" not in text  # never records keys or volatile params
    replay = ReplayHttp(tmp_path)
    assert len(replay) == 1
    assert replay.get_json("https://api.example.org/v", {"q": "x"}) == {"data": [1, 2]}
    with pytest.raises(SourceError):
        replay.get_json("https://api.example.org/other", {})


def test_recorder_disabled_by_default(monkeypatch):
    monkeypatch.delenv("OXIDE_TRIAGE_RECORD_DIR", raising=False)
    assert Http().recorder is None
    monkeypatch.setenv("OXIDE_TRIAGE_RECORD_DIR", "/tmp/somewhere")
    assert Http().recorder is not None and Http().recorder.dir == Path("/tmp/somewhere")


def _has_recordings() -> bool:
    return RECORDED.exists() and any(RECORDED.rglob("*.json"))


@pytest.mark.skipif(not _has_recordings(), reason="no recorded live responses under tests/recorded/")
def test_live_shape_replay_warm_and_selfcheck(monkeypatch):
    """Replays a recorded live warm through the real clients. Fails loudly if a field the code
    depends on is missing from the real API shape, so the fixture can never be the only evidence."""
    monkeypatch.setenv("MP_API_KEY", "recorded")
    cfg = load_config("default", use_env=False)
    cache = Cache(":memory:")
    layer = DataLayer.from_config(cfg, cache=cache, offline=False, http=ReplayHttp(RECORDED))
    records = layer.build_candidates()
    assert records, layer.warnings
    formulas = {r.formula for r in records}
    assert {"HfO2", "ZrO2", "Al2O3"} <= formulas, "workhorses missing from the live universe"
    assert not cache.has_fixture_data
    for r in records:
        assert r.stability.status == DataStatus.KNOWN and r.band_gap.status == DataStatus.KNOWN
        assert r.band_gap.functional is not None
    functionals = {r.band_gap.functional for r in records}
    assert functionals - {"unknown"}, "no band-gap functional resolved from the tasks endpoint"
    known_diel = sum(r.figure_of_merit.status == DataStatus.KNOWN for r in records)
    assert 0 < known_diel < len(records), "dielectric coverage should be partial on real data"
    # The recorded warm is incomplete: OpenAlex stopped on its daily budget and OQMD rate-limited
    # partway through, so most candidates have no literature counts and no cross-check. That must
    # surface as an *inconclusive* self-check naming the reason, never as a ranking failure — a
    # known-answer test cannot validate ranks built on data that was never fetched.
    check = run_selfcheck(cfg, cache, http=ReplayHttp(RECORDED))
    assert check.retrieval_completeness is not None
    if check.retrieval_completeness < cfg.selfcheck.min_retrieval_completeness:
        assert check.inconclusive, check.details
        assert not check.passed  # inconclusive is not a pass
        assert "INCONCLUSIVE" in check.details[0] and "retrieval completeness" in check.details[0]
    else:
        assert check.passed, check.details
        assert not check.inconclusive


@pytest.mark.skipif(not _has_recordings(), reason="no recorded live responses under tests/recorded/")
def test_replayed_gaps_are_not_retrieved_not_absent(monkeypatch):
    """The distinction the recording exists to prove: a source that never answered leaves
    NOT_RETRIEVED, not ABSENT. Collapsing the two is what let an incomplete warm look like a
    universe of genuinely data-poor materials."""
    monkeypatch.setenv("MP_API_KEY", "recorded")
    cfg = load_config("default", use_env=False)
    cache = Cache(":memory:")
    layer = DataLayer.from_config(cfg, cache=cache, offline=False, http=ReplayHttp(RECORDED))
    records = layer.build_candidates()
    if not records:
        pytest.skip("recordings predate the current universe query parameters; re-record the warm")

    # MP answered for every candidate, so its own fields are never NOT_RETRIEVED.
    assert all(r.stability.status == DataStatus.KNOWN for r in records)
    assert not any(r.figure_of_merit.status == DataStatus.NOT_RETRIEVED for r in records), (
        "MP answered the dielectric query for every candidate; a null result is ABSENT"
    )
    # OQMD and OpenAlex did not finish, so their gaps must be attributed to the cache.
    unretrieved_xcheck = [r for r in records if r.cross_check.status == DataStatus.NOT_RETRIEVED]
    assert unretrieved_xcheck, "the recording is known to be missing most OQMD lookups"
    note = unretrieved_xcheck[0].cross_check.provenance.note or ""
    # under on-demand formula sources the warm leaves OQMD unfetched by design; either way the
    # note attributes the hole to this cache, never to OQMD
    assert "never successfully queried" in note or "not fetched at warm" in note

    result = retrieval_completeness(rank(records, cfg, resolve(cfg, Criteria(), TABLE)[0])[0], cfg)
    assert not result.comparable
    assert "INCOMPLETE RETRIEVAL" in result.note
    assert result.not_retrieved_by_criterion, result.note


@pytest.mark.skipif(not _has_recordings(), reason="no recorded live responses under tests/recorded/")
def test_thermal_barrier_replay_warm_and_selfcheck(monkeypatch):
    """The second material class through the same recorded transport: the elasticity route,
    absent-at-the-source workhorses, and the profile's own known answer."""
    monkeypatch.setenv("MP_API_KEY", "recorded")
    cfg = load_config("thermal-barrier", use_env=False)
    cache = Cache(":memory:")
    layer = DataLayer.from_config(cfg, cache=cache, offline=False, http=ReplayHttp(RECORDED))
    records = layer.build_candidates()
    if not records:
        pytest.skip("recordings predate the thermal-barrier universe query parameters; re-record the warm")
    by = {r.formula: r for r in records}
    assert {"ZrO2", "HfO2", "La2Zr2O7", "SrZrO3"} <= set(by), "workhorses missing from the live universe"
    known = [r for r in records if r.figure_of_merit.status == DataStatus.KNOWN]
    assert 0 < len(known) < len(records), "elastic-tensor coverage should be partial on real data"
    assert all(r.figure_of_merit.criterion == "thermal_conductivity" for r in records)
    assert all(r.figure_of_merit.units == "W/(m·K)" for r in known)

    def values(formula: str) -> list[float]:
        return [
            r.figure_of_merit.value
            for r in records
            if r.formula == formula and r.figure_of_merit.value is not None
        ]

    assert values("ZrO2") and all(v < 2.0 for v in values("ZrO2"))  # a low-conductivity oxide
    assert values("Al2O3") and all(v > 2.0 for v in values("Al2O3"))  # a good conductor scores low
    # MP answered for every candidate: no elastic tensor is ABSENT, never NOT_RETRIEVED.
    assert by["La2Zr2O7"].figure_of_merit.status == DataStatus.ABSENT
    assert not any(r.figure_of_merit.status == DataStatus.NOT_RETRIEVED for r in records)
    assert all(r.interface.substrate == "Al2O3" for r in records if r.interface.status == DataStatus.KNOWN)
    check = run_selfcheck(cfg, cache, http=ReplayHttp(RECORDED))
    assert check.profile == "thermal-barrier"
    if (
        check.retrieval_completeness is not None
        and check.retrieval_completeness < cfg.selfcheck.min_retrieval_completeness
    ):
        assert check.inconclusive, check.details
    else:
        assert check.passed, check.details
        assert any(d.startswith("La2Zr2O7") and "unverifiable" in d for d in check.details) or any(
            d.startswith("La2Zr2O7") and "rank" in d for d in check.details
        )
