"""Streamlit front end. Run with:  streamlit run oxide_triage/app.py

Deliberately thin: it collects a request and a profile, calls the same ``run_triage`` the CLI
and the MCP server use, shows the rendered template, and offers the same follow-ups (explain a
candidate, rerun with a change) from the stored result. No logic lives here.
"""

from __future__ import annotations

import os

import streamlit as st

from oxide_triage.cache import Cache
from oxide_triage.config import list_profiles, load_config, load_hazard_table
from oxide_triage.edges.render import render
from oxide_triage.guard import guard_request
from oxide_triage.pipeline import criteria_only, load_fixtures, run_triage, warm_cache
from oxide_triage.scoring.settings import resolve
from oxide_triage.selfcheck import read_selfcheck
from oxide_triage.session import apply_changes, clarifications, explain_candidate

PI_REQUEST = (
    "Find promising oxide dielectric candidates for thin-film experiments. Prefer "
    "thermodynamically stable materials, wide band gaps, non-toxic elements, simple "
    "compositions, and public evidence. Return a ranked shortlist with caveats."
)
TEMPLATES = ["pi_summary", "audit", "json", "html"]

st.set_page_config(page_title="Oxide Dielectric Triage", page_icon="🧪", layout="wide")
st.title("Oxide dielectric triage assistant")
st.caption(
    "Deterministic ranking on cached public data. A language model, if configured, only parses the "
    "request and phrases caveats. The same tools are available over MCP for Claude Desktop / Cowork."
)

# ---------------------------------------------------------------------------------------------
# Sidebar: configuration and cache
# ---------------------------------------------------------------------------------------------
with st.sidebar:
    st.header("Configuration")
    profile = st.selectbox("Profile", ["default", *list_profiles()], index=0)
    config = load_config(profile)
    template = st.radio("Output", TEMPLATES, index=TEMPLATES.index(config.output.default_template))
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
            with st.spinner("Fetching from Materials Project, OQMD, OpenAlex, PubChem..."):
                s = warm_cache(config)
            st.success(f"{s['candidates']} candidates cached.")
            for w in s["warnings"]:
                st.warning(w)
            st.rerun()

# ---------------------------------------------------------------------------------------------
# Request, interpretation, clarify-before-run
# ---------------------------------------------------------------------------------------------
request = st.text_area("Request", PI_REQUEST, height=110)
criteria = criteria_only(request, config)
table = load_hazard_table(config.toxicity.table_file)
guard = guard_request(request, table)
_, deviations = resolve(config, criteria, table)
questions = clarifications(criteria, deviations, guard, config)

with st.expander("How the request is being read (front edge, validated)", expanded=bool(questions)):
    st.json(criteria.model_dump(exclude_defaults=True))
    for f in guard.findings:
        st.write(f'**{f.bin.value}** — "{f.matched_text}": {f.explanation}')

confirmed = True
if questions and guard.proceed:
    st.warning("Before running, please confirm:")
    for q in questions:
        st.write(f"- {q}")
    confirmed = st.checkbox("I confirm; run with these settings")


def show_result(result, tmpl: str) -> None:
    if not result.guard.proceed:
        st.error(result.warnings[0])
        return
    if result.selfcheck_status == "failed" and not result.shortlist:
        st.error(result.warnings[0])
        return
    if result.fixture_data:
        st.warning("SYNTHETIC FIXTURE DATA. Every number is illustrative.")
    for d in result.deviations:
        st.warning(f"Configuration deviation ({d.origin}): {d.description}")
    for f in result.guard.findings:
        if f.bin.value == "architecturally_impossible":
            st.info(f'Not possible in this deployment: "{f.matched_text}" — {f.explanation}')
    for w in result.warnings:
        st.info(w)
    text = render(result, tmpl)
    if tmpl == "json":
        st.code(text, language="json")
    elif tmpl == "html":
        st.components.v1.html(text, height=1400, scrolling=True)
    else:
        st.markdown(text)
    st.download_button(
        "Download JSON", render(result, "json"), file_name="triage_result.json", mime="application/json"
    )
    st.download_button("Download audit (Markdown)", render(result, "audit"), file_name="triage_audit.md")
    st.download_button(
        "Download HTML report", render(result, "html"), file_name="triage_report.html", mime="text/html"
    )


if st.button("Run triage", type="primary", disabled=not confirmed):
    with st.spinner("Ranking..."):
        result = run_triage(request, config, offline=offline, template=template, confirmed=True)
    st.session_state["result"] = result
    st.session_state["result_profile"] = profile

# ---------------------------------------------------------------------------------------------
# Result and follow-ups (from the stored result; nothing is re-derived)
# ---------------------------------------------------------------------------------------------
result = st.session_state.get("result")
if result is not None:
    show_result(result, template)

    st.divider()
    st.subheader("Follow up")
    col_a, col_b = st.columns(2)

    with col_a:
        st.markdown("**Explain a candidate**")
        names = [
            f"{s.record.formula} ({s.record.material_id})"
            for s in result.shortlist + result.ranked_beyond_shortlist + result.excluded
        ]
        if names:
            pick = st.selectbox("Candidate", names)
            if st.button("Explain"):
                st.markdown(explain_candidate(result, pick.split(" (")[1].rstrip(")")))

    with col_b:
        st.markdown("**Rerun with a change**")
        gap = st.number_input(
            "Minimum effective band gap (eV)",
            value=float(result.scoring.gates["min_effective_band_gap_ev"]),
            step=0.5,
        )
        hull = st.number_input(
            "Max energy above hull (eV/atom)",
            value=float(result.scoring.gates["max_energy_above_hull_ev_atom"]),
            step=0.01,
            format="%.3f",
        )
        max_el = st.number_input(
            "Max distinct elements", value=int(result.scoring.gates["max_elements"]), min_value=2, max_value=6
        )
        top_k = st.number_input(
            "Shortlist length", value=len(result.shortlist) or 5, min_value=1, max_value=50
        )
        allow = st.text_input(
            "Lift hazard block for (comma-separated symbols)", value=", ".join(result.criteria.allow_elements)
        )
        exclude = st.text_input(
            "Exclude elements (comma-separated symbols)", value=", ".join(result.criteria.exclude_elements)
        )
        if st.button("Rerun"):
            changes = {
                "min_band_gap_ev": gap,
                "max_energy_above_hull_ev_atom": hull,
                "max_elements": int(max_el),
                "top_k": int(top_k),
                "allow_elements": [e.strip() for e in allow.split(",") if e.strip()],
                "exclude_elements": [e.strip() for e in exclude.split(",") if e.strip()],
            }
            cfg = load_config(st.session_state.get("result_profile", profile))
            new_criteria, notes = apply_changes(result.criteria, changes)
            with st.spinner("Re-ranking..."):
                new = run_triage(
                    result.request_text + " [rerun: " + "; ".join(notes) + "]",
                    cfg,
                    offline=offline,
                    template=template,
                    criteria=new_criteria,
                    confirmed=True,
                )
            st.session_state["result"] = new
            st.rerun()
