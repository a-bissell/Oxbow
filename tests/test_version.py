"""One version number, in one place.

v0.2.7 was tagged with the bump applied to pyproject.toml alone, so the package, the web app's
OpenAPI metadata and the MCP server all still announced 0.2.6 while the release called itself
0.2.7. Nothing failed: the bundle manifest takes its version from the tag, so the release built
and passed its offline smoke test with the mismatch inside it. These tests fail instead."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from oxide_triage import __version__

PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


@pytest.fixture(scope="module")
def pyproject() -> dict:
    if not PYPROJECT.is_file():  # installed without the source tree beside it
        pytest.skip("pyproject.toml is not next to the package")
    return tomllib.loads(PYPROJECT.read_text())


def test_pyproject_reads_the_version_from_the_package(pyproject):
    """No second copy to forget: the build reads __version__ rather than restating it."""
    project = pyproject["project"]
    assert "version" not in project, (
        "pyproject.toml pins the version itself; it must stay dynamic so there is one source"
    )
    assert "version" in project.get("dynamic", [])
    attr = pyproject["tool"]["setuptools"]["dynamic"]["version"]["attr"]
    assert attr == "oxide_triage.__version__"


def test_the_version_is_a_release_number():
    parts = __version__.split(".")
    assert len(parts) == 3 and all(p.isdigit() for p in parts), __version__


def test_the_mcp_server_announces_the_package_version():
    """It used to carry its own literal, which drifted a release behind."""
    mcp_server = pytest.importorskip("oxide_triage.mcp_server")
    assert mcp_server.server.version == __version__


def test_the_web_app_announces_the_package_version():
    pytest.importorskip("fastapi")
    from oxide_triage.server.app import create_app

    assert create_app(offline=True).version == __version__
