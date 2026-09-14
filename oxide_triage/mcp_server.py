"""MCP server: use the triage system from any MCP-capable app (Claude Desktop, Claude Cowork,
Cursor, ...).

    oxide-triage-mcp                     # stdio, for desktop apps
    oxide-triage-mcp --transport http    # streamable HTTP on :8765/mcp, for the container

Architecture note. When the system is driven this way, the client's model is the front edge: it
turns the scientist's words into tool calls. Every guarantee of the system lives *inside* the
tools, not in the client's prompt: the request guard still bins the text, the deterministic core
still ranks, the fixture banner and deviations still print, model output at the refutation edge
is still validated. A client model cannot obtain a number, a rank or a citation that the tool did
not compute. Follow-up tools (`explain`, `rerun`) work from stored result objects, so a
conversation about a shortlist never re-derives anything.

The tool bodies live in ``oxide_triage.tools``; the web assistant and ``oxide-triage chat``
drive the same ``ToolBox``. This module only registers them with the MCP framework, which derives each tool's
schema from the wrapper's signature.

Clarify-before-run protocol: `triage` and `rerun` return the clarification questions instead of a
shortlist when the request changes something material (lifting a hazard block, moving a gate,
zeroing a criterion) and `confirmed` is false. The client asks the human, then calls again with
`confirmed=true`.
"""

from __future__ import annotations

import argparse
import json
import os
from contextvars import ContextVar
from typing import Any

from mcp.server.mcpserver import Context, MCPServer

from oxide_triage.actor import Actor, local_actor, web_actor
from oxide_triage.session import ResultStore
from oxide_triage.tools import INSTRUCTIONS, SPECS_BY_NAME, ToolBox

server = MCPServer(
    name="oxide-triage",
    title="Oxbow: oxide dielectric triage",
    instructions=INSTRUCTIONS,
    version="0.2.1",
)
# Who the deviation log names for a call depends on the transport. Over stdio the client is
# a process of the same OS user, so that user is the actor. Over HTTP the process user is the
# service account and says nothing about the caller, so the name comes from the header an
# authenticating proxy sets (server.actor_header), or the call is logged as unattributed.
_transport = "stdio"
_current_actor: ContextVar[Actor | None] = ContextVar("actor", default=None)


def _actor_for(ctx: Context | None) -> Actor:
    if _transport != "http":
        return local_actor("mcp")
    headers = getattr(ctx, "headers", None) or {}
    from oxide_triage.config import load_config

    return web_actor(headers, load_config("default").server.actor_header, via="mcp")


_toolbox = ToolBox(ResultStore(capacity=100), actor=_current_actor.get)


def _describe(name: str) -> str:
    return SPECS_BY_NAME[name].description


@server.tool(description=_describe("profiles"))
def profiles() -> str:
    return _toolbox.profiles()


@server.tool(description=_describe("parse_request"))
def parse_request(request: str, profile: str = "default") -> dict[str, Any]:
    return _toolbox.parse_request(request, profile)


@server.tool(description=_describe("triage"))
def triage(
    request: str,
    profile: str = "default",
    template: str | None = None,
    confirmed: bool = False,
    ctx: Context | None = None,
) -> str:
    token = _current_actor.set(_actor_for(ctx))
    try:
        return _toolbox.triage(request, profile, template, confirmed)
    finally:
        _current_actor.reset(token)


@server.tool(description=_describe("explain"))
def explain(result_id: str, candidate: str) -> str:
    return _toolbox.explain(result_id, candidate)


@server.tool(description=_describe("compare"))
def compare(result_id: str, candidates: list[str]) -> str:
    return _toolbox.compare(result_id, candidates)


@server.tool(description=_describe("list_candidates"))
def list_candidates(result_id: str, section: str = "shortlist", limit: int = 25) -> str:
    return _toolbox.list_candidates(result_id, section, limit)


@server.tool(description=_describe("rerun"))
def rerun(
    result_id: str,
    changes: dict[str, Any],
    template: str | None = None,
    confirmed: bool = False,
    ctx: Context | None = None,
) -> str:
    token = _current_actor.set(_actor_for(ctx))
    try:
        return _toolbox.rerun(result_id, changes, template, confirmed)
    finally:
        _current_actor.reset(token)


@server.tool(description=_describe("add_material"))
def add_material(formula: str, profile: str = "default") -> dict[str, Any]:
    return _toolbox.add_material(formula, profile)


@server.tool(description=_describe("cache_status"))
def cache_status(profile: str = "default") -> dict[str, Any]:
    return _toolbox.cache_status(profile)


@server.tool(description=_describe("selfcheck"))
def selfcheck(profile: str = "default") -> dict[str, Any]:
    return _toolbox.selfcheck(profile)


@server.resource("oxide-triage://result/{result_id}", mime_type="application/json")
def result_resource(result_id: str) -> str:
    result = _toolbox.store.get(result_id)
    return (
        result.model_dump_json(indent=2)
        if result
        else json.dumps({"error": f"unknown result_id {result_id}"})
    )


@server.resource("oxide-triage://scope", mime_type="text/plain")
def scope_resource() -> str:
    from oxide_triage.schemas import SCOPE_LIMITATION

    return SCOPE_LIMITATION


def main() -> None:
    ap = argparse.ArgumentParser(description="Oxide triage MCP server")
    ap.add_argument(
        "--transport", choices=["stdio", "http"], default=os.environ.get("MCP_TRANSPORT", "stdio")
    )
    ap.add_argument("--host", default=os.environ.get("MCP_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("MCP_PORT", "8765")))
    args = ap.parse_args()
    global _transport
    _transport = args.transport
    if args.transport == "http":
        server.run(transport="streamable-http", host=args.host, port=args.port)
    else:
        server.run(transport="stdio")


if __name__ == "__main__":  # pragma: no cover
    main()
