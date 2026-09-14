"""FastAPI application: the JSON and event-stream API the front end talks to, plus the built
front end itself. Run with ``oxide-triage serve``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from oxide_triage import __version__
from oxide_triage.actor import web_actor
from oxide_triage.bundle import read_release
from oxide_triage.cache import Cache
from oxide_triage.config import (
    DEFAULT_CONFIG_DIR,
    ENV_KEYS,
    Config,
    SiteOverrides,
    admin_enabled,
    cations_for_families,
    config_layers,
    env_locked,
    list_profiles,
    load_cation_families,
    load_config,
    load_site_overrides,
    preview_config,
    save_site_overrides,
    site_config_path,
)
from oxide_triage.doctor import aggregate_deviations, aggregate_retrieval_gaps, mask
from oxide_triage.edges.render import render
from oxide_triage.pipeline import add_material, load_fixtures, run_acquisition, warm_cache
from oxide_triage.selfcheck import read_selfcheck, run_selfcheck
from oxide_triage.server.agent import TurnRequest, agent_model, driver_name, run_turn
from oxide_triage.server.auth import install_auth
from oxide_triage.server.jobs import JobRunner
from oxide_triage.server.store import SessionStore
from oxide_triage.session import explain_candidate
from oxide_triage.sources.assemble import DataLayer

log = logging.getLogger(__name__)

UI_DIST = Path(__file__).resolve().parent.parent / "ui" / "dist"

# Seconds between keepalive comments on a turn's event stream while nothing else is sent.
SSE_KEEPALIVE_S = 15.0

GREETINGS = [
    "This band gap isn’t going to tunnel itself. Where should we start?",
    "On the lookout for a stable perovskite?",
    "Hull-hugging, wide-gap, non-toxic. Pick two? No, all three.",
    "Al2O3 is fine. Let’s find something better.",
    "Which oxides should go on the bench first?",
    "Ranked on public data, argued against by design. Ask away.",
]


class AppState:
    def __init__(self, config_dir: Path = DEFAULT_CONFIG_DIR, offline: bool | None = None):
        self.config_dir = config_dir
        self.offline = offline
        base = self.load_config("default")
        self.session_root = Path(base.cache.path).parent / "sessions"
        self.store = SessionStore(self.session_root)
        self.jobs = JobRunner()
        self._universe: tuple[str, list[frozenset[str]]] | None = None  # (fingerprint, cations per material)
        self._lock = threading.Lock()
        self._turn_locks: dict[str, threading.Lock] = {}
        # The first status call needs the universe; compute it now, off the request path, so the
        # first page load and the deployment's health check do not pay for it.
        threading.Thread(target=self._warm_universe, name="warm-universe", daemon=True).start()

    def _warm_universe(self) -> None:
        try:
            self.universe()
        except Exception as exc:  # an empty or missing cache is a normal first-run state
            log.info("universe not warmed: %s", exc)

    def load_config(self, profile: str = "default") -> Config:
        return load_config(profile, config_dir=self.config_dir)

    def turn_lock(self, cid: str) -> threading.Lock:
        with self._lock:
            return self._turn_locks.setdefault(cid, threading.Lock())

    # ---- universe -------------------------------------------------------------------

    def universe(self) -> list[frozenset[str]]:
        """Cation sets of every material in the cache, memoised on the cache fingerprint.

        Read from the cached summaries only: the count needs each material's elements, not
        the assembled record, and assembling every record (hull arithmetic included) took
        tens of seconds on a live cache, which was the first page load's wait."""
        cfg = self.load_config("default")
        cache = Cache(cfg.cache.path)
        try:
            fp = cache.fingerprint()
            with self._lock:
                if self._universe and self._universe[0] == fp:
                    return self._universe[1]
            layer = DataLayer.from_config(cfg, cache=cache, offline=True)
            try:
                sets: list[frozenset[str]] = []
                for mid in layer.universe_ids():
                    doc, _ = layer.mp.summary(mid)
                    if doc is not None:
                        sets.append(frozenset(str(e) for e in doc.get("elements", []) if e != "O"))
            finally:
                layer.close()
        finally:
            cache.close()
        with self._lock:
            self._universe = (fp, sets)
        return sets

    def families_payload(self) -> dict[str, Any]:
        fams = load_cation_families(self.load_config("default").candidates.cation_allowlist_file)
        try:
            sets = self.universe()
        except Exception as exc:  # an empty or missing cache is a normal first-run state
            log.info("universe unavailable: %s", exc)
            sets = []
        out = []
        for f in fams:
            cats = set(f.cations)
            out.append(
                {
                    "id": f.id,
                    "name": f.name,
                    "rationale": f.rationale,
                    "cations": f.cations,
                    "n_any": sum(1 for s in sets if s & cats),
                    "n_only": sum(1 for s in sets if s and s <= cats),
                }
            )
        return {"families": out, "n_universe": len(sets)}

    def scope_count(self, families: list[str]) -> dict[str, int]:
        fams = load_cation_families(self.load_config("default").candidates.cation_allowlist_file)
        sets = self.universe()
        if not families or set(families) >= {f.id for f in fams}:
            return {"n_in_scope": len(sets), "n_universe": len(sets)}
        allowed = cations_for_families(fams, families)
        return {"n_in_scope": sum(1 for s in sets if s <= allowed), "n_universe": len(sets)}


class NewConversation(BaseModel):
    profile: str = "default"


class OverlayBody(BaseModel):
    base: dict[str, Any] = Field(default_factory=dict)
    profiles: dict[str, dict[str, Any]] = Field(default_factory=dict)


class JobBody(BaseModel):
    kind: str
    args: dict[str, Any] = Field(default_factory=dict)


def _sse(event: dict[str, Any]) -> str:
    return f"data: {json.dumps(event, default=str)}\n\n"


def create_app(config_dir: Path = DEFAULT_CONFIG_DIR, offline: bool | None = None) -> FastAPI:
    state = AppState(config_dir=config_dir, offline=offline)
    app = FastAPI(title="Oxbow", version=__version__, docs_url="/api/docs", openapi_url="/api/openapi.json")
    app.state.triage = state
    # A public deployment sets OXBOW_PASSWORD; everything below then sits behind the login
    # page except /api/health, which a load balancer polls.
    install_auth(app)

    @app.get("/api/health", include_in_schema=False)
    def health() -> dict[str, bool]:
        """Liveness only: nothing about the cache or the configuration, and never gated."""
        return {"ok": True}

    # ---- status and reference data --------------------------------------------------

    @app.get("/api/status")
    def status() -> dict[str, Any]:
        cfg = state.load_config("default")
        cache = Cache(cfg.cache.path)
        try:
            summary = cache.sources_summary()
            fixture = cache.has_fixture_data
            sc = read_selfcheck(cache)
            release = read_release(cache)
        finally:
            cache.close()
        profiles = []
        for name in ["default", *list_profiles(state.config_dir)]:
            c = state.load_config(name)
            profiles.append(
                {
                    "name": name,
                    "description": c.description.strip(),
                    "top_k": c.output.top_k,
                    "gates": c.gates.model_dump(),
                    "weights": c.normalized_weights(),
                    "figure_of_merit": c.figure_of_merit.model_dump(),
                    "default_families": c.candidates.default_families,
                }
            )
        fam = state.families_payload()
        return {
            "cache": {
                "path": cfg.cache.path,
                "sources": summary,
                "fixture_data": fixture,
                "empty": not summary,
                "offline": bool(cfg.cache.offline if state.offline is None else state.offline),
                "release": release,
            },
            "selfcheck": None
            if sc is None
            else {
                "passed": sc.passed,
                "inconclusive": sc.inconclusive,
                "checked_at": sc.checked_at,
                "details": sc.details,
            },
            "profiles": profiles,
            "families": fam["families"],
            "n_universe": fam["n_universe"],
            "llm": {
                "provider": cfg.llm.provider,
                "model": cfg.llm.model,
                "driver": driver_name(cfg),
                "agent_model": agent_model(cfg),
            },
            "admin_editable": admin_enabled(),
            "greetings": GREETINGS,
            "suggested_requests": [
                {
                    "label": "The PI's request",
                    "text": "Find promising oxide dielectric candidates for thin-film experiments. Prefer thermodynamically stable materials, wide band gaps, non-toxic elements, simple compositions, and public evidence. Return a ranked shortlist with caveats.",
                },
                {
                    "label": "Lead-free perovskites",
                    "text": "Lead-free perovskite oxides with a large dielectric constant, top 8.",
                },
                {
                    "label": "Hafnium or zirconium binaries",
                    "text": "Hafnium- or zirconium-based binaries only, top 5.",
                },
                {
                    "label": "Metastable phases",
                    "text": "Include metastable phases within 60 meV of the hull and prioritize the dielectric constant.",
                },
                {
                    "label": "Thermal barrier coatings",
                    "text": (
                        "Find promising oxide candidates for thermal barrier coatings on a superalloy with an "
                        "alumina bond-coat scale. Prefer low thermal conductivity, thermodynamically stable, "
                        "non-toxic elements, simple compositions, and public evidence. Return a ranked "
                        "shortlist with caveats."
                    ),
                    "profile": "thermal-barrier",
                },
            ],
            "scope_limitation": (
                "Stability against the substrate is bulk hull thermodynamics only; deposition feasibility, "
                "reaction kinetics, film morphology and hygroscopic degradation are not modelled."
            ),
        }

    @app.get("/api/universe/scope")
    def universe_scope(families: str = "") -> dict[str, int]:
        ids = [f for f in families.split(",") if f]
        try:
            return state.scope_count(ids)
        except Exception:
            return {"n_in_scope": 0, "n_universe": 0}

    # ---- conversations ---------------------------------------------------------------

    @app.get("/api/conversations")
    def conversations() -> list[dict[str, Any]]:
        return state.store.list_conversations()

    @app.post("/api/conversations")
    def new_conversation(body: NewConversation) -> dict[str, Any]:
        cfg = state.load_config(body.profile)
        conv = state.store.new_conversation(profile=body.profile, driver=driver_name(cfg))
        return conv.model_dump()

    @app.get("/api/conversations/{cid}")
    def get_conversation(cid: str) -> dict[str, Any]:
        conv = state.store.get_conversation(cid)
        if conv is None:
            raise HTTPException(404, "no such conversation")
        data = conv.model_dump()
        data.pop("model_messages", None)
        return data

    @app.delete("/api/conversations/{cid}")
    def delete_conversation(cid: str) -> dict[str, bool]:
        return {"deleted": state.store.delete_conversation(cid)}

    @app.post("/api/conversations/{cid}/turns")
    async def post_turn(cid: str, req: TurnRequest, request: Request) -> StreamingResponse:
        conv = state.store.get_conversation(cid)
        if conv is None:
            raise HTTPException(404, "no such conversation")
        actor = web_actor(request.headers, state.load_config("default").server.actor_header)
        lock = state.turn_lock(cid)
        if not lock.acquire(blocking=False):
            raise HTTPException(409, "a turn is already running for this conversation")
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

        def emit(ev: dict[str, Any]) -> None:
            loop.call_soon_threadsafe(queue.put_nowait, ev)

        def work() -> None:
            try:
                run_turn(
                    state.store,
                    conv,
                    req,
                    emit,
                    config_dir=state.config_dir,
                    offline=state.offline,
                    actor=actor,
                )
            except Exception as exc:  # last resort; run_turn handles its own failures
                log.exception("turn failed")
                emit({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
            finally:
                lock.release()
                emit(None)  # type: ignore[arg-type]

        threading.Thread(target=work, name=f"turn-{cid}", daemon=True).start()

        async def events() -> AsyncIterator[str]:
            # A turn can be silent for a minute or more while the model argues against each
            # candidate. Proxies and load balancers close an idle stream (60 s is a common
            # default), so send an SSE comment at intervals; clients ignore comment lines.
            while True:
                try:
                    ev = await asyncio.wait_for(queue.get(), timeout=SSE_KEEPALIVE_S)
                except TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                if ev is None:
                    yield _sse({"type": "end"})
                    return
                yield _sse(ev)

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # ---- results ----------------------------------------------------------------------

    @app.get("/api/results/{rid}")
    def get_result(rid: str) -> Any:
        result = state.store.get_result(rid)
        if result is None:
            raise HTTPException(404, "no such result")
        return json.loads(result.model_dump_json())

    @app.get("/api/results/{rid}/explain/{candidate}", response_class=PlainTextResponse)
    def get_explain(rid: str, candidate: str) -> str:
        result = state.store.get_result(rid)
        if result is None:
            raise HTTPException(404, "no such result")
        return explain_candidate(result, candidate)

    @app.get("/api/results/{rid}/render/{template}")
    def get_render(rid: str, template: str) -> Any:
        result = state.store.get_result(rid)
        if result is None:
            raise HTTPException(404, "no such result")
        if template not in {"pi_summary", "advanced", "audit", "json", "html"}:
            raise HTTPException(400, "template must be pi_summary, advanced, audit, json or html")
        text = render(result, template)
        media = {"json": "application/json", "html": "text/html"}.get(template, "text/markdown")
        ext = {"json": "json", "html": "html"}.get(template, "md")
        return PlainTextResponse(
            text,
            media_type=media,
            headers={"Content-Disposition": f'attachment; filename="triage_{rid}_{template}.{ext}"'},
        )

    # ---- admin --------------------------------------------------------------------------

    def _overlay_payload(profile: str) -> dict[str, Any]:
        try:
            layers = config_layers(profile, config_dir=state.config_dir)
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        shipped = Config.model_validate(layers.shipped())
        site = load_site_overrides(layers.site_path)
        return {
            "profile": profile,
            "profiles": ["default", *list_profiles(state.config_dir)],
            "effective": json.loads(layers.effective.model_dump_json()),
            "shipped": json.loads(shipped.model_dump_json()),
            "overlay": {"base": site.base, "profiles": site.profiles},
            "overlay_path": str(layers.site_path) if layers.site_path else None,
            "editable": admin_enabled() and layers.site_path is not None,
            "env_locked": {path: var for path in ENV_KEYS if (var := env_locked(path))},
            "site_overrides": [o.model_dump() for o in layers.effective.site_overrides],
        }

    @app.get("/api/admin/config")
    def admin_config(profile: str = "default") -> dict[str, Any]:
        return _overlay_payload(profile)

    @app.put("/api/admin/overlay")
    def put_overlay(body: OverlayBody) -> dict[str, Any]:
        if not admin_enabled():
            raise HTTPException(403, "editing is disabled: set OXIDE_TRIAGE_ADMIN=1 and restart the server")
        path = site_config_path(state.config_dir)
        if path is None:
            raise HTTPException(
                422, "the site file is disabled (OXIDE_TRIAGE_SITE_CONFIG=off or an in-memory cache)"
            )
        try:
            site = SiteOverrides(base=body.base, profiles=body.profiles)
            for name in ["default", *list_profiles(state.config_dir)]:
                preview_config(name, site, state.config_dir)  # raises on a value a profile cannot take
        except Exception as exc:
            raise HTTPException(422, f"rejected: {exc}") from exc
        save_site_overrides(path, site)
        return {"saved": str(path), "overlay": {"base": site.base, "profiles": site.profiles}}

    @app.get("/api/admin/environment")
    def admin_environment() -> dict[str, Any]:
        cfg = state.load_config("default")
        keys = ("MP_API_KEY", "OPENALEX_API_KEY", "ANTHROPIC_API_KEY", "LLM_API_KEY")
        return {
            "keys": {k: (mask(k, os.environ.get(k)) if os.environ.get(k) else None) for k in keys},
            "llm": {
                "provider": cfg.llm.provider,
                "model": cfg.llm.model,
                "base_url": cfg.llm.base_url,
                "driver": driver_name(cfg),
                "agent_model": agent_model(cfg),
            },
            "cache_path": cfg.cache.path,
            "offline": bool(cfg.cache.offline if state.offline is None else state.offline),
            "config_dir": str(state.config_dir),
            "session_root": str(state.session_root),
            "site_config_path": cfg.site_config_path,
            "admin_editable": admin_enabled(),
        }

    @app.get("/api/admin/deviations")
    def admin_deviations(limit: int = 100) -> list[dict[str, Any]]:
        path = Path(state.load_config("default").cache.path).with_name("deviations.jsonl")
        if not path.is_file():
            return []
        lines = path.read_text(encoding="utf-8").splitlines()[-limit:]
        out = []
        for line in reversed(lines):
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out

    def _since(days: int | None) -> datetime | None:
        if days is None or days <= 0:
            return None
        return datetime.now(UTC) - timedelta(days=days)

    @app.get("/api/admin/deviations/summary")
    def admin_deviations_summary(days: int | None = None) -> dict[str, Any]:
        """Deviation counts by code, origin and profile over the last ``days`` (all time when
        unset). Read-only reporting for the platform team; nothing here changes a default."""
        return aggregate_deviations(state.load_config("default"), since=_since(days))

    @app.get("/api/admin/retrieval/summary")
    def admin_retrieval_summary(days: int | None = None) -> dict[str, Any]:
        """The data-gap ledger: per-criterion ``absent`` (no public source holds it) and
        ``not_retrieved`` (this cache never fetched it) totals over the last ``days``. Read-only."""
        return aggregate_retrieval_gaps(state.load_config("default"), since=_since(days))

    @app.post("/api/admin/jobs")
    def start_job(body: JobBody) -> dict[str, Any]:
        cfg = state.load_config(str(body.args.get("profile") or "default"))
        online_ok = not (cfg.cache.offline if state.offline is None else state.offline)
        if body.kind in {"warm", "add_material", "fill_gaps"} and not admin_enabled():
            # Fetching spends the public sources' budgets; the demo fixture and the self-check do not.
            raise HTTPException(
                403, "cache operations are disabled: set OXIDE_TRIAGE_ADMIN=1 and restart the server"
            )

        def fn(job: Any) -> Any:
            if body.kind == "fixtures":
                return {"loaded": load_fixtures(cfg)}
            if body.kind == "selfcheck":
                cache = Cache(cfg.cache.path)
                try:
                    return run_selfcheck(cfg, cache, offline=True).model_dump()
                finally:
                    cache.close()
            if not online_ok:
                raise RuntimeError("deployment is offline; this job needs network access")
            if body.kind == "warm":
                if not os.environ.get("MP_API_KEY"):
                    raise RuntimeError("MP_API_KEY is not set")
                return warm_cache(cfg)
            if body.kind == "add_material":
                return add_material(str(body.args.get("formula") or ""), cfg)
            if body.kind == "fill_gaps":
                return run_acquisition(cfg).summary()
            raise ValueError(f"unknown job kind {body.kind}")

        try:
            job = state.jobs.start(body.kind, fn, body.args)
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(409, str(exc)) from exc
        return job.model_dump()

    @app.get("/api/admin/jobs/current")
    def current_job() -> Any:
        job = state.jobs.snapshot()
        return job.model_dump() if job else None

    # ---- front end -----------------------------------------------------------------------

    if (UI_DIST / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=str(UI_DIST / "assets")), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str, request: Request) -> Any:
        if path.startswith("api/"):
            raise HTTPException(404)
        index = UI_DIST / "index.html"
        if index.is_file():
            candidate = UI_DIST / path
            if path and candidate.is_file():
                return FileResponse(str(candidate))
            return FileResponse(str(index))
        return HTMLResponse(
            "<h1>Oxbow</h1><p>The front end has not been built. Run <code>npm install &amp;&amp; npm run build</code> "
            "in <code>ui/</code>, or use the API at <a href='/api/docs'>/api/docs</a>.</p>",
            status_code=200,
        )

    return app
