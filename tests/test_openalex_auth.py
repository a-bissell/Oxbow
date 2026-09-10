"""The OpenAlex key travels in a header, never in a URL, and never into a recording."""

from __future__ import annotations

from oxide_triage.cache import Cache
from oxide_triage.sources.base import Recorder
from oxide_triage.sources.openalex import OpenAlex


class _Http:
    def __init__(self, recorder=None):
        self.calls = []
        self.recorder = recorder

    def get_json(self, url, params=None, headers=None):
        self.calls.append((url, dict(params or {}), dict(headers or {})))
        if self.recorder is not None:
            self.recorder.save(url, params, {"meta": {"count": 3}, "results": []})
        return {"meta": {"count": 3}, "results": []}


def test_key_goes_in_header_not_params(monkeypatch):
    monkeypatch.delenv("OPENALEX_API_KEY", raising=False)
    http = _Http()
    oa = OpenAlex(Cache(":memory:"), 90, False, http=http, api_key="sk-test-123", mailto="a@b")
    oa.fetch_evidence("HfO2", [])
    assert http.calls, "no requests made"
    for url, params, headers in http.calls:
        assert headers == {"Authorization": "Bearer sk-test-123"}
        assert "sk-test-123" not in url and "sk-test-123" not in str(params)
        assert params.get("mailto") == "a@b"


def test_no_key_means_no_auth_header(monkeypatch):
    monkeypatch.delenv("OPENALEX_API_KEY", raising=False)
    http = _Http()
    OpenAlex(Cache(":memory:"), 90, False, http=http, mailto="").fetch_evidence("HfO2", [])
    assert all(headers == {} for _, _, headers in http.calls)


def test_key_read_from_env_and_never_recorded(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENALEX_API_KEY", "sk-env-456")
    http = _Http(recorder=Recorder(tmp_path))
    OpenAlex(Cache(":memory:"), 90, False, http=http, mailto="").fetch_evidence("HfO2", [])
    assert http.calls[0][2] == {"Authorization": "Bearer sk-env-456"}
    for f in tmp_path.rglob("*.json"):
        assert "sk-env-456" not in f.read_text()
