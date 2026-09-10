"""Release bundle: a warmed cache packaged for a machine that will never see the network.

The deployment story of this tool is that once the cache is warmed the containers run fully
offline. A bundle makes that claim portable: a directory holding one SQLite file plus a manifest
that says what the file was built from and lets the receiving side check it arrived intact.

    build    copy the current cache (via SQLite's backup API, so a live file is safe to copy),
             stamp the release identity into its ``meta`` table, hash the result, write the
             manifest. Refuses a cache whose self-check is missing, failed or inconclusive:
             a release must never ship a cache the tool itself would refuse to serve from.
    verify   check the files are present, the hash matches and the self-check passed.
    install  verify, then copy the cache to the configured cache path. Refuses to overwrite an
             existing non-empty cache unless told to.

Nothing in here touches the network. The manifest is data about the cache, never a source of
a number: the shipped cache carries the same records, timestamps and self-check it had when it
was built, and every output still traces to those.
"""

from __future__ import annotations

import hashlib
import json
import platform
import shutil
import sqlite3
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from oxide_triage import __version__
from oxide_triage.cache import Cache
from oxide_triage.config import DEFAULT_CONFIG_DIR, Config
from oxide_triage.selfcheck import read_selfcheck

MANIFEST_NAME = "manifest.json"
CACHE_NAME = "cache.sqlite"
MANIFEST_SCHEMA = 1
RELEASE_META_KEY = "release"


class BundleError(RuntimeError):
    """A bundle that must not be built, trusted or installed, with the reason."""


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def config_files_sha256(config_dir: Path = DEFAULT_CONFIG_DIR) -> str:
    """Hash of the shipped configuration files (default.yaml and every profile), so a bundle
    records which policy its self-check was run under."""
    h = hashlib.sha256()
    paths = sorted(p for p in config_dir.rglob("*.yaml") if p.is_file())
    for p in paths:
        h.update(str(p.relative_to(config_dir)).encode())
        h.update(b"\x00")
        h.update(p.read_bytes())
        h.update(b"\n")
    return h.hexdigest()


def git_commit(repo_root: Path | None = None) -> str | None:
    """The current commit, or None outside a checkout (an installed wheel, a container)."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_root) if repo_root else None,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None if out.returncode == 0 else None


def read_release(cache: Cache) -> dict[str, Any] | None:
    """The release identity stamped into a cache by ``build``, if any."""
    raw = cache.get_meta(RELEASE_META_KEY)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _copy_sqlite(src_path: Path, dst_path: Path) -> None:
    """Copy through the backup API: consistent even if another process holds the source open,
    and the destination is a fresh single file (no journal or WAL side files to forget)."""
    dst_path.parent.mkdir(parents=True, exist_ok=True)
    if dst_path.exists():
        dst_path.unlink()
    src = sqlite3.connect(str(src_path))
    try:
        dst = sqlite3.connect(str(dst_path))
        try:
            src.backup(dst)
            dst.execute("VACUUM")
            dst.commit()
        finally:
            dst.close()
    finally:
        src.close()


def build_bundle(
    config: Config,
    out_dir: Path,
    *,
    version: str | None = None,
    commit: str | None = None,
    allow_fixture: bool = False,
    config_dir: Path = DEFAULT_CONFIG_DIR,
) -> dict[str, Any]:
    """Package the configured cache into ``out_dir``. Returns the manifest."""
    cache_path = Path(config.cache.path)
    if str(cache_path) == ":memory:" or not cache_path.is_file():
        raise BundleError(f"no cache file at {cache_path}; warm it first (oxide-triage warm-cache)")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    cache = Cache(cache_path)
    try:
        if cache.count() == 0:
            raise BundleError("the cache is empty; warm it first (oxide-triage warm-cache)")
        fixture = cache.has_fixture_data
        if fixture and not allow_fixture:
            raise BundleError(
                "the cache holds synthetic fixture data; a release built from it would carry the "
                "fixture banner on every output. Pass --allow-fixture only for a demo bundle."
            )
        sc = read_selfcheck(cache)
        if sc is None:
            raise BundleError("no self-check recorded; run oxide-triage selfcheck first")
        if sc.inconclusive:
            raise BundleError("self-check is INCONCLUSIVE (cache too sparsely retrieved); not releasable")
        if not sc.passed:
            raise BundleError("self-check FAILED; not releasable")
        sources = cache.sources_summary()
        release = {
            "name": "oxide-triage",
            "version": version or __version__,
            "commit": commit or git_commit(config_dir.parent),
            "built_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
        }
        # The identity travels inside the file too, so `doctor`, `cache-status` and the web app
        # can name the release a cache came from with no manifest beside it.
        cache.set_meta(RELEASE_META_KEY, json.dumps(release, sort_keys=True))
        selfcheck = sc.model_dump()
    finally:
        cache.close()

    cache_out = out_dir / CACHE_NAME
    _copy_sqlite(cache_path, cache_out)
    digest = sha256_file(cache_out)
    manifest: dict[str, Any] = {
        "schema": MANIFEST_SCHEMA,
        **release,
        "python": platform.python_version(),
        "fixture_data": fixture,
        "config_sha256": config_files_sha256(config_dir),
        "site_overrides_in_effect": config.site_config_path is not None
        and Path(config.site_config_path).is_file(),
        "sources": sources,
        "selfcheck": selfcheck,
        "cache": {"file": CACHE_NAME, "sha256": digest, "bytes": cache_out.stat().st_size},
    }
    (out_dir / MANIFEST_NAME).write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest


def verify_bundle(bundle_dir: Path) -> dict[str, Any]:
    """Check a bundle is complete, intact and releasable. Returns the manifest; raises
    ``BundleError`` with the first problem found."""
    bundle_dir = Path(bundle_dir)
    manifest_path = bundle_dir / MANIFEST_NAME
    if not manifest_path.is_file():
        raise BundleError(f"{manifest_path} is missing")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise BundleError(f"{manifest_path} is not valid JSON: {exc}") from exc
    if manifest.get("schema") != MANIFEST_SCHEMA:
        raise BundleError(
            f"unsupported manifest schema {manifest.get('schema')!r} (expected {MANIFEST_SCHEMA})"
        )
    entry = manifest.get("cache") or {}
    cache_path = bundle_dir / str(entry.get("file") or CACHE_NAME)
    if not cache_path.is_file():
        raise BundleError(f"{cache_path} is missing")
    expected = entry.get("sha256")
    if not expected:
        raise BundleError("manifest carries no cache checksum")
    actual = sha256_file(cache_path)
    if actual != expected:
        raise BundleError(
            f"checksum mismatch for {cache_path.name}: manifest {expected[:16]}…, file {actual[:16]}…"
        )
    sc = manifest.get("selfcheck") or {}
    if sc.get("inconclusive") or not sc.get("passed"):
        raise BundleError("manifest records a self-check that did not pass")
    # The stamped identity must agree with the manifest: a cache swapped for another release's
    # would pass the hash only if the manifest were swapped with it, and then this catches a
    # manifest edited by hand.
    cache = Cache(cache_path)
    try:
        stamped = read_release(cache)
    finally:
        cache.close()
    if stamped is None:
        raise BundleError("the cache carries no release stamp; it was not produced by `bundle build`")
    for key in ("version", "commit", "built_at"):
        if stamped.get(key) != manifest.get(key):
            raise BundleError(
                f"release {key} differs between the cache ({stamped.get(key)}) and the manifest ({manifest.get(key)})"
            )
    return manifest


def install_bundle(bundle_dir: Path, dest: Path, *, force: bool = False) -> dict[str, Any]:
    """Verify, then copy the cache to ``dest`` (the configured cache path). The manifest is
    written beside it as ``<dest>.manifest.json``. Returns the manifest."""
    manifest = verify_bundle(bundle_dir)
    dest = Path(dest)
    if str(dest) == ":memory:":
        raise BundleError("cannot install into an in-memory cache")
    if dest.exists() and not force:
        existing = Cache(dest)
        try:
            n = existing.count()
        finally:
            existing.close()
        if n:
            raise BundleError(f"{dest} already holds {n} records; pass --force to replace it")
    src = Path(bundle_dir) / str((manifest.get("cache") or {}).get("file") or CACHE_NAME)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".installing")
    shutil.copyfile(src, tmp)
    for side in (dest.with_name(dest.name + "-wal"), dest.with_name(dest.name + "-journal")):
        if side.exists():
            side.unlink()
    tmp.replace(dest)
    dest.with_name(dest.name + ".manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest


def describe(manifest: dict[str, Any]) -> str:
    """One line for humans: what release, when, from what, and what the self-check said."""
    sc = manifest.get("selfcheck") or {}
    commit = (manifest.get("commit") or "unknown")[:12]
    fixture = " SYNTHETIC FIXTURE DATA" if manifest.get("fixture_data") else ""
    return (
        f"oxide-triage {manifest.get('version')} ({commit}) built {manifest.get('built_at')}; "
        f"{sc.get('n_candidates', '?')} candidates, self-check "
        f"{'passed' if sc.get('passed') else 'FAILED'}{fixture}"
    )
