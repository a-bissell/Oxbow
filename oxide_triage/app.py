"""Streamlit front end. Run with:  streamlit run oxide_triage/app.py

Two pages over the same deterministic core the CLI and the MCP server use:

* **Triage** — a form: request in, rendered template out, explain / rerun follow-ups.
* **Agent** — a conversation. A language model drives the same tools the MCP server exposes;
  the report it produces is shown as cards whose parts seed follow-up questions. Needs
  LLM_PROVIDER=anthropic (and ANTHROPIC_API_KEY) or openai_compatible.

Deliberately thin: no logic lives in the UI.
"""

from __future__ import annotations

from functools import partial

import streamlit as st

from oxide_triage.ui.agent_page import agent_page
from oxide_triage.ui.sidebar import render_sidebar
from oxide_triage.ui.triage_page import triage_page

st.set_page_config(page_title="Oxide Dielectric Triage", page_icon="🧪", layout="wide")
st.title("Oxide dielectric triage assistant")
st.caption(
    "Deterministic ranking on cached public data. A language model, if configured, only parses the "
    "request, phrases caveats, and (on the Agent page) drives the same tools that are available over "
    "MCP for Claude Desktop / Cowork."
)

state = render_sidebar()
st.navigation(
    [
        st.Page(partial(triage_page, state), title="Triage", icon="🧪", default=True),
        st.Page(partial(agent_page, state), title="Agent", icon="💬", url_path="agent"),
    ]
).run()
