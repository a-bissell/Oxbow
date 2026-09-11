"""Release bundles: build from a warmed cache, verify, install, and run offline from the result."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from oxide_triage.bundle import (
    CACHE_NAME,
    MANIFEST_NAME,
    BundleError,
    build_bundle,
    install_bundle,
    read_release,
    verify_bundle,
)
from oxide_triage.cache import Cache
from oxide_triage.cli import app
from oxide_triage.config import load_config
from oxide_triage.pipeline import load_fixtures, run_triage
from oxide_triage.selfcheck import read_selfcheck

PI = (
    "Find promising oxide dielectric candidates for thin-film experiments. Prefer "
    "thermodynamically stable materials, wide band gaps, non-toxic elements, simple "
    "compositions, and public evidence. Return a ranked shortlist with caveats."
)


def _config(tmp_path, name="warm.sqlite"):
    return load_config("default", use_env=False, overrides={"cache": {"path": str(tmp_path / name)}})


@pytest.fixture
def warmed(tmp_path):
    """A cache with the fixture loaded and a passing self-check, on disk like a real warm."""
    cfg = _config(tmp_path)
    cache = Cache(cfg.cache.path)
    load_fixtures(cfg, cache)
    assert read_selfcheck(cache).passed
    cache.close()
    return cfg


def test_build_writes_cache_and_manifest_with_checksum_and_stamp(warmed, tmp_path):
    out = tmp_path / "bundle"
    manifest = build_bundle(warmed, out, version="9.9.9", commit="abc123", allow_fixture=True)
    assert (out / CACHE_NAME).is_file() and (out / MANIFEST_NAME).is_file()
    on_disk = json.loads((out / MANIFEST_NAME).read_text())
    assert on_disk == manifest
    assert manifest["version"] == "9.9.9" and manifest["commit"] == "abc123"
    assert manifest["fixture_data"] is True
    assert manifest["selfcheck"]["passed"] and not manifest["selfcheck"]["inconclusive"]
    assert manifest["sources"] and manifest["cache"]["bytes"] > 0
    assert len(manifest["cache"]["sha256"]) == 64 and len(manifest["config_sha256"]) == 64
    # The copy carries the same identity inside it.
    shipped = Cache(out / CACHE_NAME)
    try:
        stamp = read_release(shipped)
        assert shipped.count() > 0 and shipped.has_fixture_data
    finally:
        shipped.close()
    assert stamp == {k: manifest[k] for k in ("name", "version", "commit", "built_at")}
    assert verify_bundle(out)["version"] == "9.9.9"


def test_build_refuses_fixture_data_unless_allowed(warmed, tmp_path):
    with pytest.raises(BundleError, match="fixture"):
        build_bundle(warmed, tmp_path / "bundle")


def test_build_refuses_empty_or_unchecked_cache(tmp_path):
    cfg = _config(tmp_path, "empty.sqlite")
    with pytest.raises(BundleError, match="warm it first"):
        build_bundle(cfg, tmp_path / "bundle")
    Cache(cfg.cache.path).close()  # creates the empty file
    with pytest.raises(BundleError, match="empty"):
        build_bundle(cfg, tmp_path / "bundle")


def test_build_refuses_a_failed_selfcheck(warmed, tmp_path):
    cache = Cache(warmed.cache.path)
    sc = read_selfcheck(cache)
    cache.set_meta("selfcheck", sc.model_copy(update={"passed": False}).model_dump_json())
    cache.close()
    with pytest.raises(BundleError, match="FAILED"):
        build_bundle(warmed, tmp_path / "bundle", allow_fixture=True)


def test_verify_detects_tampering_and_manifest_edits(warmed, tmp_path):
    out = tmp_path / "bundle"
    build_bundle(warmed, out, version="1.0.0", commit="abc", allow_fixture=True)
    # A flipped byte in the cache.
    data = bytearray((out / CACHE_NAME).read_bytes())
    data[-1] ^= 0xFF
    (out / CACHE_NAME).write_bytes(data)
    with pytest.raises(BundleError, match="checksum mismatch"):
        verify_bundle(out)
    # Rebuild, then edit the manifest's version by hand: the stamp inside the cache disagrees.
    build_bundle(warmed, out, version="1.0.0", commit="abc", allow_fixture=True)
    m = json.loads((out / MANIFEST_NAME).read_text())
    m["version"] = "2.0.0"
    (out / MANIFEST_NAME).write_text(json.dumps(m))
    with pytest.raises(BundleError, match="version differs"):
        verify_bundle(out)
    # And a missing manifest.
    (out / MANIFEST_NAME).unlink()
    with pytest.raises(BundleError, match="missing"):
        verify_bundle(out)


def test_install_then_query_offline_from_the_installed_cache(warmed, tmp_path):
    out = tmp_path / "bundle"
    build_bundle(warmed, out, version="1.0.0", commit="abc", allow_fixture=True)
    site_cfg = _config(tmp_path / "site", "cache.sqlite")
    dest = tmp_path / "site" / "cache.sqlite"
    manifest = install_bundle(out, dest)
    assert dest.is_file() and dest.with_name("cache.sqlite.manifest.json").is_file()
    assert manifest["version"] == "1.0.0"
    # The receiving site runs entirely from the file it was handed.
    res = run_triage(PI, site_cfg, offline=True)
    assert res.guard.proceed and res.shortlist and res.fixture_data
    cache = Cache(dest)
    try:
        assert read_selfcheck(cache).passed
        assert read_release(cache)["version"] == "1.0.0"
    finally:
        cache.close()
    # A second install refuses to clobber a populated cache unless forced.
    with pytest.raises(BundleError, match="--force"):
        install_bundle(out, dest)
    install_bundle(out, dest, force=True)


def test_cli_build_verify_install_round_trip(warmed, tmp_path, monkeypatch):
    runner = CliRunner()
    monkeypatch.setenv("OXIDE_TRIAGE_CACHE", warmed.cache.path)
    out = tmp_path / "bundle"
    r = runner.invoke(app, ["bundle", "build", "--out", str(out)])
    assert r.exit_code == 4 and "fixture" in r.output
    r = runner.invoke(app, ["bundle", "build", "--out", str(out), "--allow-fixture", "--version", "0.0.1"])
    assert r.exit_code == 0, r.output
    assert "sha256" in r.output and "SYNTHETIC FIXTURE DATA" in r.output
    r = runner.invoke(app, ["bundle", "verify", str(out)])
    assert r.exit_code == 0 and r.output.startswith("OK oxide-triage 0.0.1")

    dest = tmp_path / "site" / "cache.sqlite"
    monkeypatch.setenv("OXIDE_TRIAGE_CACHE", str(dest))
    r = runner.invoke(app, ["bundle", "install", str(out)])
    assert r.exit_code == 0, r.output
    assert "installed oxide-triage 0.0.1" in r.output
    r = runner.invoke(app, ["doctor"])
    assert r.exit_code == 0 and "release: 0.0.1" in r.output
    r = runner.invoke(app, ["bundle", "install", str(out)])
    assert r.exit_code == 4 and "--force" in r.output
