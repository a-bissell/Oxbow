"""Who made a request. Recorded with every deviation so the log is attributable, not just
descriptive: "the hazard block was lifted for lead at 14:02" is a fact about the run; "by
jsmith, through the web app, as named by the proxy" is a fact a site can act on.

The tool does not authenticate anyone. It takes the name the environment already has: the
operating-system user for the command line, the MCP server and the terminal chat; for the
web app, a header set by the authenticating reverse proxy the deployment notes ask for
(``server.actor_header``, X-Forwarded-User by default). With no proxy the web actor is
recorded as unattributed, and says so, rather than trusting anything the browser sends.
"""

from __future__ import annotations

import getpass
from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel

Via = Literal["cli", "chat", "mcp", "web", "api"]


class Actor(BaseModel):
    who: str
    via: Via
    how: str  # where the name came from, in words a site admin can check

    def __str__(self) -> str:
        return f"{self.who} ({self.via}; {self.how})"


def local_actor(via: Via) -> Actor:
    """The operating-system user running the process."""
    try:
        who = getpass.getuser()
    except Exception:  # no passwd entry (some containers); still say who ran it
        who = "unknown"
    return Actor(who=who, via=via, how="operating-system user of the process")


def web_actor(headers: Mapping[str, str], header: str, via: Via = "web") -> Actor:
    """The user named by the reverse proxy's header, or an explicit 'unattributed'. Used by the
    web app and by the MCP server over HTTP, which is the same situation: a network caller
    the process itself cannot name."""
    # Header names are case-insensitive; a plain dict of them is not.
    value = next((v for k, v in headers.items() if k.lower() == header.lower()), "").strip()
    if value:
        return Actor(who=value[:120], via=via, how=f"{header} header set by the reverse proxy")
    return Actor(
        who="unattributed",
        via=via,
        how=f"no {header} header: no authenticating proxy in front of the server",
    )


UNATTRIBUTED = Actor(who="unattributed", via="api", how="the caller passed no actor")
