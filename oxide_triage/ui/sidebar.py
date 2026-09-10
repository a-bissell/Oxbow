"""The sidebar shared by every page: profile, output template, offline toggle, cache state."""

from __future__ import annotations

import os
from dataclasses import dataclass

import streamlit as st

from oxide_triage.cache import Cache
from oxide_triage.config import Config, list_profiles, load_config
from oxide_triage.pipeline import load_fixtures, warm_cache
from oxide_triage.selfcheck import read_selfcheck

TEMPLATES = ["pi_summary", "audit", "json", "html"]


@dataclass
class SidebarState:
    profile: str
    config: Config
    template: str
    offline: bool


def render_sidebar() -> SidebarState:
    with st.sidebar:
        st.header("Configuration")
        profile = st.selectbox("Profile", ["default", *list_profiles()], index=0)
        config = load_config(profile)
        template = st.radio("Output", TEMPLATES, index=TEMPLATES.index(config.output.default_template))
        offline = st.toggle(
            "Offline (cache only)",
            value=config.cache.offline or os.environ.get("OXIDE_TRIAGE_OFFLINE") == "1",
        )
        st.markdown(
            f"**Language model:** `{config.llm.provider}`"
            + (f" · `{config.llm.model}`" if config.llm.model else "")
        )
        st.markdown(f"_{config.description.strip()}_")

        st.divider()
        st.subheader("Cache")
        cache = Cache(config.cache.path)
        summary = cache.sources_summary()
        fixture = cache.has_fixture_data
        selfcheck = read_selfcheck(cache)
        cache.close()
        if fixture:
            st.warning("Cache contains SYNTHETIC fixture data. Outputs are illustrative only.")
        if selfcheck is None:
            st.info("Self-check not yet run on this cache.")
        elif selfcheck.passed:
            st.success(f"Self-check passed {selfcheck.checked_at}")
        else:
            st.error("Self-check FAILED: " + "; ".join(selfcheck.details))
        if summary:
            for src, info in summary.items():
                st.text(f"{src}: {info['n']} rows, newest {info['newest']}")
        else:
            st.info("Cache is empty. Warm it (needs MP_API_KEY) or load the demo fixture.")
        c1, c2 = st.columns(2)
        if c1.button("Load demo fixture"):
            n = load_fixtures(config)
            st.success(f"Loaded {n} synthetic materials.")
            st.rerun()
        if c2.button("Warm cache (live)"):
            if not os.environ.get("MP_API_KEY"):
                st.error("MP_API_KEY is not set.")
            else:
                with st.spinner(
                    "Fetching from Materials Project, OQMD, PubChem (literature is fetched per query)..."
                ):
                    s = warm_cache(config)
                st.success(f"{s['candidates']} candidates cached.")
                for w in s["warnings"]:
                    st.warning(w)
                st.rerun()
    return SidebarState(profile=profile, config=config, template=template, offline=offline)
