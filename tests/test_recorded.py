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
from oxide_triage.config import load_config
from oxide_triage.schemas import DataStatus
from oxide_triage.selfcheck import run_selfcheck
from oxide_triage.sources.assemble import DataLayer
from oxide_triage.sources.base import Http, Recorder, ReplayHttp, SourceError, request_key

RECORDED = Path(__file__).parent / "recorded"


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
    known_diel = sum(r.dielectric.status == DataStatus.KNOWN for r in records)
    assert 0 < known_diel < len(records), "dielectric coverage should be partial on real data"
    check = run_selfcheck(cfg, cache)
    assert check.passed, check.details
