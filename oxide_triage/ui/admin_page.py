"""The Admin page: the site's configuration, edited against the shipped policy.

Nothing here touches the shipped YAML. Edits go to the site overrides file (``site.yaml`` next
to the cache); every value shows what it would fall back to and where that comes from; a
pending-changes panel validates the result and shows the ranking hash before and after, so an
admin can see whether a change alters results before saving. Saving re-runs the self-check
when the policy moved. Editing and the operations need OXIDE_TRIAGE_ADMIN=1; otherwise the page
is a read-only view. There is no per-user authentication: whoever can reach the port can edit.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import streamlit as st
from pydantic import ValidationError

from oxide_triage.cache import Cache
from oxide_triage.config import (
    ADMIN_ENV,
    SITE_CONFIG_ENV,
    ConfigLayers,
    SiteOverrides,
    admin_enabled,
    config_layers,
    diff_layer,
    list_profiles,
    load_config,
    load_hazard_table,
    preview_config,
    save_site_overrides,
)
from oxide_triage.doctor import env_status, probe_sources, read_deviation_log, site_file_status
from oxide_triage.pipeline import load_fixtures, run_acquisition, warm_cache
from oxide_triage.selfcheck import read_selfcheck, run_selfcheck
from oxide_triage.ui.admin_form import fmt_value, render_section
from oxide_triage.ui.sidebar import SidebarState

RANKING_SECTIONS = [
    "weights",
    "gates",
    "band_gap",
    "stability",
    "dielectric",
    "toxicity",
    "simplicity",
    "missing_data",
    "literature",
]
RUNTIME_SECTIONS = [
    "description",
    "candidates",
    "retrieval",
    "output",
    "terminology",
    "cache",
    "llm",
    "selfcheck",
    "acquisition",
    "agent",
]
SECTION_NOTES = {
    "weights": "Relative; normalised to sum 1 at load time.",
    "gates": "A candidate failing any gate is excluded with the reason stated.",
    "band_gap": "DFT gaps underestimate experiment; corrected values are always labelled.",
    "missing_data": "no_credit: an unknown criterion earns nothing and can never help a candidate.",
    "literature": "on_demand fetches OpenAlex counts at query time for the top-ranked pool only.",
    "candidates": "What the warm pulls from Materials Project, and the per-source fetch limits.",
    "retrieval": "When a partly retrieved cache warns on every result, and when it refuses to rank.",
    "cache": "cache.path is fixed by the environment; offline may be forced by OXIDE_TRIAGE_OFFLINE.",
    "llm": "Provider, model and base URL are overridden by LLM_* variables when set (always in Docker).",
    "agent": "Limits of the chat agent on the Agent page; nothing here affects ranking.",
}


def _scope_label(scope: str) -> str:
    return "Base (all profiles)" if scope == "base" else scope


def _bump(kind: str, section: str | None = None) -> None:
    gens = st.session_state.setdefault("admin:gen", {})
    key = "all" if section is None else section
    gens[key] = gens.get(key, 0) + 1
    if kind == "all":
        gens["all"] = gens.get("all", 0) + 1


def _keygen(scope: str, section: str):
    gens = st.session_state.setdefault("admin:gen", {})

    def key(path: str) -> str:
        return f"admin:{scope}:{gens.get('all', 0)}:{section}:{gens.get(section, 0)}:{path}"

    return key


def _site_from(layers: ConfigLayers) -> SiteOverrides:
    base = dict(layers.layers)["site.base"]
    site_profile = dict(layers.layers)["site.profile"]
    profiles = {layers.profile: site_profile} if site_profile else {}
    return SiteOverrides(base=base, profiles=profiles)


def _current_site(site_path: Path | None) -> SiteOverrides:
    from oxide_triage.config import load_site_overrides

    return load_site_overrides(site_path)


def _mtime(path: Path | None) -> int | None:
    return path.stat().st_mtime_ns if path is not None and path.exists() else None


def _validate(new_site: SiteOverrides, profiles: list[str]) -> tuple[dict[str, tuple[str, str]], list[str]]:
    """Hash before/after per profile, and validation errors (empty when valid)."""
    hashes: dict[str, tuple[str, str]] = {}
    errors: list[str] = []
    for name in profiles:
        try:
            after = preview_config(name, new_site).config_hash()
        except ValidationError as exc:
            for err in exc.errors():
                errors.append(f"{name}: {'.'.join(str(x) for x in err['loc'])}: {err['msg']}")
            continue
        before = config_layers(name).effective.config_hash()
        hashes[name] = (before, after)
    return hashes, errors


def _write(site_path: Path, new_site: SiteOverrides) -> None:
    if new_site.is_empty():
        if site_path.exists():
            site_path.unlink()
    else:
        save_site_overrides(site_path, new_site)


@st.fragment
def _editor(state: SidebarState, scope: str, editable: bool, site_path: Path | None) -> None:
    profile = "default" if scope == "base" else scope
    layers = config_layers(profile)
    reference = layers.reference_for(scope)
    current = layers.current_for(scope)
    table = load_hazard_table(state.config.toxicity.table_file)
    edited: dict[str, Any] = {}

    def origin_for(section: str):
        # Origins are shown relative to the scope: for Base, anything from the profile file or a
        # profile section of the site file is not in play.
        def f(path: str) -> str:
            o = layers.origin_of(path)
            if scope == "base" and o in {"profile", "site.profile"}:
                return "default"
            return {"default": "default.yaml", "profile": f"profile {profile}"}.get(o, o)

        return f

    def render_tab(sections: list[str]) -> None:
        for section in sections:
            with st.expander(section.replace("_", " "), expanded=section == "weights"):
                if note := SECTION_NOTES.get(section):
                    st.caption(note)
                edited.update(
                    render_section(
                        section,
                        current,
                        reference,
                        origin_for(section),
                        keygen=_keygen(scope, section),
                        disabled=not editable,
                        table=table,
                    )
                )
                if editable:
                    st.button(
                        "Revert unsaved edits in this section",
                        key=f"admin:revert:{scope}:{section}",
                        type="tertiary",
                        on_click=_bump,
                        args=("section", section),
                    )

    tab_ranking, tab_runtime = st.tabs(["Ranking policy", "Runtime"])
    with tab_ranking:
        render_tab(RANKING_SECTIONS)
    with tab_runtime:
        render_tab(RUNTIME_SECTIONS)

    if not editable:
        return

    st.divider()
    st.subheader("Pending changes")
    try:
        pending = diff_layer(reference, edited)
    except (ValidationError, ValueError) as exc:
        st.error(f"A table has an invalid row: {exc}")
        return
    if not pending:
        st.caption(
            "No unsaved changes for this scope. Values equal to what they would fall back to are not stored."
        )
    site_now = _current_site(site_path)
    new_site = site_now.model_copy(deep=True)
    if scope == "base":
        new_site.base = pending
    else:
        new_site.profiles = {**new_site.profiles, scope: pending}
    # Re-validate the object so stripped keys and unknown paths surface the same way as on load.
    new_site = SiteOverrides.model_validate(new_site.model_dump())
    affected = ["default", *list_profiles()] if scope == "base" else [scope]
    hashes, errors = _validate(new_site, affected)
    if pending:
        ref_flat = {}
        from oxide_triage.config import flatten_leaves

        ref_flat = flatten_leaves(reference)
        st.dataframe(
            [
                {"key": k, "falls back to": fmt_value(ref_flat.get(k)), "new value": fmt_value(v)}
                for k, v in flatten_leaves(pending).items()
            ],
            hide_index=True,
            width="stretch",
        )
    for err in errors:
        st.error(err)
    if hashes:
        moved = {p: h for p, h in hashes.items() if h[0] != h[1]}
        if moved:
            st.warning(
                "Ranking hash changes for: "
                + ", ".join(f"{p} ({b} → {a})" for p, (b, a) in moved.items())
                + ". Results for these profiles will differ and carry a site deviation; the self-check runs after saving."
            )
        elif pending:
            st.info(
                "The ranking hash does not change: these are runtime settings, and results stay identical."
            )
    cols = st.columns([2, 2, 2, 3])
    stored_mtime = st.session_state.get("admin:mtime")
    if cols[0].button(
        "Save to site file", key="admin:save", type="primary", disabled=bool(errors) or not pending
    ):
        if _mtime(site_path) != stored_mtime:
            st.error(
                "The site file changed on disk since this page loaded. Reload the page and apply your edits again."
            )
            return
        assert site_path is not None
        _write(site_path, new_site)
        if any(b != a for b, a in hashes.values()):
            cfg = load_config(profile)
            cache = Cache(cfg.cache.path)
            try:
                if cache.count():
                    sc = run_selfcheck(cfg, cache)
                    st.session_state["admin:last_selfcheck"] = sc.model_dump()
            finally:
                cache.close()
        _bump("all")
        st.session_state["admin:mtime"] = _mtime(site_path)
        st.rerun(scope="app")
    cols[1].button("Revert all unsaved edits", key="admin:revert-all", on_click=_bump, args=("all",))
    scope_has_saved = bool(site_now.base) if scope == "base" else bool(site_now.profiles.get(scope))
    if cols[2].button(
        "Remove saved overrides for this scope", key="admin:remove", disabled=not scope_has_saved
    ):
        assert site_path is not None
        cleared = site_now.model_copy(deep=True)
        if scope == "base":
            cleared.base = {}
        else:
            cleared.profiles = {k: v for k, v in cleared.profiles.items() if k != scope}
        _write(site_path, cleared)
        _bump("all")
        st.session_state["admin:mtime"] = _mtime(site_path)
        st.rerun(scope="app")
    cols[3].download_button(
        "Download site.yaml (with pending changes)",
        new_site.to_yaml(),
        file_name="site.yaml",
        mime="application/yaml",
        key="admin:download",
    )


def _operations(state: SidebarState, admin: bool) -> None:
    config = state.config
    cache = Cache(config.cache.path)
    try:
        summary = cache.sources_summary()
        fixture = cache.has_fixture_data
        sc = read_selfcheck(cache)
        n = cache.count()
    finally:
        cache.close()
    st.markdown(
        f"**Cache** `{config.cache.path}` · {n} rows · fixture data: {fixture} · offline: {config.cache.offline}"
    )
    if summary:
        st.dataframe(
            [{"source": k, "rows": v["n"], "newest": v["newest"]} for k, v in summary.items()],
            hide_index=True,
            width="stretch",
        )
    if sc is None:
        st.info("Self-check not yet run on this cache.")
    elif sc.passed:
        st.success(f"Self-check passed {sc.checked_at}: " + "; ".join(sc.details))
    else:
        st.error("Self-check FAILED: " + "; ".join(sc.details))
    if last := st.session_state.pop("admin:last_selfcheck", None):
        st.info("Self-check re-run after the last save: " + ("passed" if last["passed"] else "FAILED"))

    if admin:
        c1, c2, c3, c4 = st.columns(4)
        if c1.button("Load demo fixture", key="admin:op:fixture"):
            n = load_fixtures(config)
            st.success(f"Loaded {n} synthetic materials.")
            st.rerun(scope="app")
        if c2.button("Warm cache (live)", key="admin:op:warm"):
            if not os.environ.get("MP_API_KEY"):
                st.error("MP_API_KEY is not set.")
            else:
                with st.spinner("Fetching from Materials Project, OQMD, PubChem..."):
                    s = warm_cache(config)
                st.success(f"{s['candidates']} candidates cached.")
                for w in s["warnings"]:
                    st.warning(w)
                st.rerun(scope="app")
        if c3.button("Run self-check", key="admin:op:selfcheck"):
            cache = Cache(config.cache.path)
            try:
                res = run_selfcheck(config, cache)
            finally:
                cache.close()
            (st.success if res.passed else st.error)("; ".join(res.details))
        if c4.button("Fill data gaps (online)", key="admin:op:fill"):
            cache = Cache(config.cache.path)
            try:
                with st.spinner("Trying alternative routes for gaps..."):
                    report = run_acquisition(config, cache)
                if report is None:
                    st.warning("Acquisition did not run (disabled in config, or cache is offline).")
                else:
                    st.json(report.summary())
                    run_selfcheck(config, cache)
            finally:
                cache.close()
    else:
        st.caption(f"Operations need {ADMIN_ENV}=1.")

    st.subheader("Deviations log")
    st.caption(
        "Every run whose configuration departed from the shipped policy (request, profile or site), newest first. "
        "The self-check's own runs appear here too."
    )
    rows = read_deviation_log(config)
    if rows:
        st.dataframe(rows, hide_index=True, width="stretch", height=300)
    else:
        st.caption("No deviations logged on this cache.")

    st.subheader("Environment")
    st.dataframe([{"variable": k, "value": v} for k, v in env_status()], hide_index=True, width="stretch")
    if admin and not config.cache.offline and st.button("Probe public sources", key="admin:op:probe"):
        with st.spinner("Probing..."):
            for name, note in probe_sources().items():
                st.text(f"{name}: {note}")


def _profiles_overview(site_path: Path | None) -> None:
    rows = []
    for name in ["default", *list_profiles()]:
        cfg = config_layers(name).effective
        g = cfg.gates
        rows.append(
            {
                "profile": name,
                "E_hull ≤": g.max_energy_above_hull_ev_atom,
                "gap ≥": g.min_band_gap_ev,
                "elements ≤": g.max_elements,
                "blocked tiers": fmt_value(cfg.toxicity.blocklist_tiers),
                "allowed": fmt_value(cfg.toxicity.element_allowlist),
                "top_k": cfg.output.top_k,
                "weights": fmt_value(cfg.weights.model_dump()),
                "config hash": cfg.config_hash(),
                "site edits": len(cfg.site_overrides),
                "policy edits": len(cfg.policy_overrides()),
            }
        )
    st.dataframe(rows, hide_index=True, width="stretch")
    st.subheader("Site file")
    if site_path is None:
        st.caption("Disabled.")
    elif site_path.exists():
        st.code(site_path.read_text(encoding="utf-8"), language="yaml")
    else:
        st.caption(f"`{site_path}` does not exist yet; nothing is overridden.")


def admin_page(state: SidebarState) -> None:
    admin = admin_enabled()
    status = site_file_status(state.config)
    site_path = Path(status["path"]) if status["path"] else None
    if "admin:mtime" not in st.session_state:
        st.session_state["admin:mtime"] = _mtime(site_path)

    st.subheader("Site configuration")
    if not admin:
        st.info(
            f"Read-only: set `{ADMIN_ENV}=1` in the environment to edit settings and run operations. "
            "There is no per-user login; with the flag set, anyone who can reach this port can change the site policy."
        )
    if site_path is None:
        st.warning(
            f"The site overrides file is disabled ({SITE_CONFIG_ENV} is off, or the cache is in memory). Settings are read-only."
        )
    else:
        st.caption(
            f"Shipped policy: `config/default.yaml` and `config/profiles/`. Site overrides: `{site_path}` "
            f"({'present' if status['present'] else 'absent'}). Environment variables win over both."
        )
    editable = admin and site_path is not None

    profiles = ["default", *list_profiles()]
    scope = (
        st.segmented_control(
            "Scope", ["base", *profiles], default="base", key="admin:scope", format_func=_scope_label
        )
        or "base"
    )
    st.caption(
        "Base edits apply to every profile (as if default.yaml were edited); a profile scope edits only that profile. "
        "A value equal to what it falls back to is not stored."
    )

    tab_settings, tab_ops, tab_profiles = st.tabs(["Settings", "Operations", "Profiles"])
    with tab_settings:
        _editor(state, scope, editable, site_path)
    with tab_ops:
        _operations(state, admin)
    with tab_profiles:
        _profiles_overview(site_path)
