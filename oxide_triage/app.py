"""Streamlit front end. Run with:  streamlit run oxide_triage/app.py

Deliberately thin: it collects a request and a profile, calls the same ``run_triage`` the CLI
uses, and shows the rendered template. No logic lives here.
"""

from __future__ import annotations

import os

import streamlit as st

from oxide_triage.cache import Cache
from oxide_triage.config import list_profiles, load_config
from oxide_triage.edges.render import render
from oxide_triage.pipeline import criteria_only, load_fixtures, run_triage, warm_cache

PI_REQUEST = (
    "Find promising oxide dielectric candidates for thin-film experiments. Prefer "
    "thermodynamically stable materials, wide band gaps, non-toxic elements, simple "
    "compositions, and public evidence. Return a ranked shortlist with caveats."
)

st.set_page_config(page_title="Oxide Dielectric Triage", page_icon="🧪", layout="wide")
st.title("Oxide dielectric triage assistant")
st.caption(
    "Deterministic ranking on cached public data. A language model, if configured, only parses the request and phrases caveats."
)

with st.sidebar:
    st.header("Configuration")
    profile = st.selectbox("Profile", ["default", *list_profiles()], index=0)
    config = load_config(profile)
    template = st.radio(
        "Output",
        ["pi_summary", "audit", "json"],
        index=["pi_summary", "audit", "json"].index(config.output.default_template),
    )
    offline = st.toggle(
        "Offline (cache only)", value=config.cache.offline or os.environ.get("OXIDE_TRIAGE_OFFLINE") == "1"
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
    cache.close()
    if fixture:
        st.warning("Cache contains SYNTHETIC fixture data. Outputs are illustrative only.")
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
            with st.spinner("Fetching from Materials Project, OQMD, OpenAlex, PubChem..."):
                s = warm_cache(config)
            st.success(f"{s['candidates']} candidates cached.")
            for w in s["warnings"]:
                st.warning(w)
            st.rerun()

request = st.text_area("Request", PI_REQUEST, height=110)
with st.expander("How the request is being read (front edge, validated)", expanded=False):
    st.json(criteria_only(request, config).model_dump(exclude_defaults=True))

if st.button("Run triage", type="primary"):
    with st.spinner("Ranking..."):
        result = run_triage(request, config, offline=offline, template=template)
    if not result.guard.proceed:
        st.error(result.warnings[0])
    else:
        if result.fixture_data:
            st.warning("SYNTHETIC FIXTURE DATA. Every number is illustrative.")
        for d in result.deviations:
            st.warning(f"Configuration deviation ({d.origin}): {d.description}")
        for f in result.guard.findings:
            if f.bin.value == "architecturally_impossible":
                st.info(f'Not possible in this deployment: "{f.matched_text}" — {f.explanation}')
        text = render(result, template)
        if template == "json":
            st.code(text, language="json")
        else:
            st.markdown(text)
        st.download_button(
            "Download JSON", render(result, "json"), file_name="triage_result.json", mime="application/json"
        )
        st.download_button("Download audit (Markdown)", render(result, "audit"), file_name="triage_audit.md")
