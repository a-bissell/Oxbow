"""Suite-wide fixtures."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_site_config(monkeypatch):
    """A developer's data/site.yaml must never leak into the tests; site tests opt in by passing
    ``site_config=`` explicitly or setting the variable themselves."""
    monkeypatch.setenv("OXIDE_TRIAGE_SITE_CONFIG", "off")
