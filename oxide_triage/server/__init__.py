"""Web application: a FastAPI backend over the same session, pipeline and config code the CLI
and the MCP server use, plus the built single-page front end it serves.

Nothing in this package computes a number. The agent loop (``agent.py``) turns messages into
tool calls; the tools (``tools.py``) are the same operations the MCP server exposes; the
pipeline and the session layer do the work; the front end renders result objects.
"""
