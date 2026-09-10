"""The Agent page: a conversation with a model that drives the same tools the MCP server
exposes, beside a report explorer whose parts seed follow-up questions.

The page owns no logic. ``Agent.send`` runs the tool-use loop; ``render_report`` draws the
latest result; a chip sets ``st.session_state["pending_prompt"]`` and the next script run sends
it exactly as if the user had typed it.
"""

from __future__ import annotations

import json
import re

import streamlit as st

from oxide_triage.agent import Agent, AgentReply, ToolEvent
from oxide_triage.edges.llm import chat_availability, make_chat_llm
from oxide_triage.tools import ToolBox, agent_system_prompt
from oxide_triage.ui.report_cards import render_report
from oxide_triage.ui.sidebar import SidebarState

PI_REQUEST = (
    "Find promising oxide dielectric candidates for thin-film experiments. Prefer "
    "thermodynamically stable materials, wide band gaps, non-toxic elements, simple "
    "compositions, and public evidence. Return a ranked shortlist with caveats."
)
STARTERS = [
    ("Run the PI's request", PI_REQUEST),
    (
        "Compare the profiles",
        "What do the configuration profiles change, and which would you use for a first screen?",
    ),
    ("What can't this do?", "What can this system not do, and what does it refuse to do?"),
]
TOOL_PREVIEW_CHARS = 6000


def _ask(seed: str) -> None:
    st.session_state["pending_prompt"] = seed


def _short_args(args: dict) -> str:
    parts = []
    for k, v in args.items():
        s = json.dumps(v) if not isinstance(v, str) else v
        parts.append(f"{k}={s[:40] + '…' if len(s) > 40 else s}")
    return ", ".join(parts)


def _tool_block(ev: ToolEvent) -> None:
    label = f"{ev.name}({_short_args(ev.input)})"
    with st.status(label, state="error" if ev.outcome.is_error else "complete", expanded=False):
        text = ev.outcome.text
        if len(text) > TOOL_PREVIEW_CHARS:
            text = text[:TOOL_PREVIEW_CHARS] + "\n\n[…]"
        if text.lstrip().startswith("{"):
            st.code(text, language="json")
        else:
            st.markdown(text)


def _mark(text: str, numbers: list[str]) -> str:
    for tok in numbers:
        text = re.sub(rf"(?<![A-Za-z\d.]){re.escape(tok)}(?![\d.])", f"**⚠{tok}**", text)
    return text


def _assistant_body(text: str, unverified: list[str]) -> None:
    st.markdown(_mark(text, unverified))
    if unverified:
        st.caption(
            "⚠ Not found in any tool output: " + ", ".join(unverified) + ". Treat these as unverified "
            "(counts such as “top 3” may be flagged too)."
        )


def _render_entry(entry: dict) -> None:
    with st.chat_message(entry["role"]):
        if entry["role"] == "user":
            st.markdown(entry["text"])
            return
        for ev in entry.get("tool_events", []):
            _tool_block(ev)
        if entry.get("error"):
            st.error(entry["error"])
        if entry.get("text"):
            _assistant_body(entry["text"], entry.get("unverified", []))


def _agent_for(state: SidebarState) -> Agent | None:
    cfg = state.config
    ok, why = chat_availability(cfg.llm)
    key = (cfg.llm.provider, cfg.llm.model, cfg.llm.base_url, state.profile, state.offline)
    if not ok:
        st.session_state.pop("agent", None)
        st.session_state["agent_key"] = None
        return None
    if st.session_state.get("agent_key") != key:
        toolbox = ToolBox(
            config_overrides={"cache": {"offline": True}} if state.offline else None,
            tool_result_max_chars=cfg.agent.tool_result_max_chars,
        )
        try:
            llm = make_chat_llm(cfg.llm, cfg.agent)
        except RuntimeError as exc:
            st.error(str(exc))
            return None
        st.session_state["agent"] = Agent(
            llm,
            toolbox,
            agent_system_prompt(state.profile),
            cfg.agent.max_tool_rounds,
            cfg.agent.number_guard,
        )
        st.session_state["agent_key"] = key
        st.session_state["chat"] = []
        st.session_state["explorer_id"] = None
        st.session_state.pop("pending_prompt", None)
    return st.session_state["agent"]


def _run_turn(agent: Agent, prompt: str) -> AgentReply:
    """Stream one turn into the chat column. Tool calls open a status block as they happen; the
    text placeholder is renewed after each so the order on screen matches the conversation."""
    with st.chat_message("user"):
        st.markdown(prompt)
    with st.chat_message("assistant"):
        holder = {"placeholder": st.empty(), "buffer": ""}

        def on_text(chunk: str) -> None:
            holder["buffer"] += chunk
            holder["placeholder"].markdown(holder["buffer"])

        def on_tool(ev: ToolEvent) -> None:
            _tool_block(ev)
            holder["placeholder"] = st.empty()
            holder["buffer"] = ""

        with st.spinner("Thinking…", show_time=True):
            reply = agent.send(prompt, on_text=on_text, on_tool=on_tool)
    return reply


def agent_page(state: SidebarState) -> None:
    cfg = state.config
    ok, why = chat_availability(cfg.llm)
    agent = _agent_for(state)

    with st.sidebar:
        st.divider()
        st.subheader("Agent")
        if ok:
            st.success(f"Chat ready: `{why}`")
            usage = agent.usage if agent else {}
            if usage:
                st.caption(
                    f"tokens this conversation: in {usage.get('input_tokens', usage.get('prompt_tokens', 0)):,} · "
                    f"out {usage.get('output_tokens', usage.get('completion_tokens', 0)):,}"
                    + (
                        f" · cache read {usage['cache_read_input_tokens']:,}"
                        if usage.get("cache_read_input_tokens")
                        else ""
                    )
                )
            if st.button("New chat", width="stretch") and agent is not None:
                agent.new_conversation()
                st.session_state["chat"] = []
                st.session_state["explorer_id"] = None
                st.session_state.pop("pending_prompt", None)
                st.rerun()
        else:
            st.warning(f"Chat unavailable: {why}")

    prompt = st.chat_input(
        "Ask for a shortlist, why a candidate ranks where it does, or a rerun with a change…",
        disabled=agent is None,
        submit_mode="disable",
    ) or st.session_state.pop("pending_prompt", None)

    left, right = st.columns([11, 9], gap="large")

    with left:
        st.subheader("Conversation")
        if agent is None:
            st.info(
                f"{why}. The model drives the same tools the MCP server exposes (`triage`, `explain`, "
                "`rerun`, …); every number it shows comes out of a tool. Set the provider in `.env` "
                "and restart, or use the Triage page, which needs no model."
            )
        elif not st.session_state.get("chat"):
            st.caption(
                "The model is the front edge: it turns your words into tool calls and relays what the tools computed. Start with one of these, or type below."
            )
            with st.container(horizontal=True, wrap=True, gap="small"):
                for label, seed in STARTERS:
                    st.button(label, key=f"starter:{label}", type="secondary", on_click=_ask, args=(seed,))
        for entry in st.session_state.get("chat", []):
            _render_entry(entry)
        if prompt and agent is not None:
            reply = _run_turn(agent, prompt)
            st.session_state["chat"].append({"role": "user", "text": prompt})
            st.session_state["chat"].append(
                {
                    "role": "assistant",
                    "text": reply.text,
                    "tool_events": reply.tool_events,
                    "unverified": reply.unverified_numbers,
                    "error": reply.error,
                }
            )
            if reply.latest_result_id:
                st.session_state["explorer_id"] = reply.latest_result_id
            st.rerun()

    with right:
        st.subheader("Report explorer")
        ids = agent.results if agent is not None else []
        if ids:
            current = st.session_state.get("explorer_id") or ids[-1]
            if current not in ids:
                current = ids[-1]
            rid = st.selectbox(
                "Result",
                list(reversed(ids)),
                index=list(reversed(ids)).index(current),
                format_func=lambda x: f"{x}",
            )
            st.session_state["explorer_id"] = rid
            result = agent.toolbox.store.get(rid) if agent is not None else None
            if result is not None:
                render_report(result, rid, _ask)
        elif st.session_state.get("result") is not None:
            st.caption(
                "Showing the last result from the Triage page. Results produced in this conversation will replace it."
            )
            render_report(st.session_state["result"], "triage-page", None)
        else:
            st.caption(
                "Results produced in the conversation appear here, with a button on every part you can ask about."
            )
