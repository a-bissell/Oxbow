"""The chat agent: a tool-use loop over the shared ``ToolBox``, independent of provider and of
front end (the web assistant and ``oxide-triage chat`` both drive it).

What it guarantees, and how:

* The model reaches data only through tools. ``ToolBox.call`` validates every call against the
  tool's schema and never raises; an unknown tool or bad arguments come back to the model as an
  error result it can read.
* The transcript is append-only. New turns are built in a local list and committed only when the
  turn completes, so a provider error or an interrupted turn leaves the conversation
  exactly as it was. Nothing is ever edited or truncated (thinking blocks are bound to the turn
  they were produced in).
* Every tool call gets a result. Parallel calls are answered in one results turn, in order; when
  the round cap is hit, pending calls get an error result and the model is asked once more, with
  tools disallowed, to answer in words.
* Numbers are checked. After the reply, every number in the prose is looked up in the numbers
  the tools printed (and in the user's own words). Anything else is reported as unverified; the
  front end marks it. The check flags, it does not rewrite.
* The request guard runs on the user's own words before any model sees them. A request the
  guard declines (one that would need fabricated evidence) is answered with the refusal and no
  model call; a request with a notice (an override attempt, a capability the deployment lacks)
  reaches the model with the notice prepended, so the model relays it rather than deciding
  alone whether to. Without this the guard would see only the model's paraphrase of the
  request, which is exactly what a social-engineering prompt is designed to shape.
* The confirm flag is the person's. ``ToolBox.call`` ignores ``confirmed=true`` on a call that
  has not been held for questions first, so the model cannot pre-approve a change on the
  user's behalf.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from oxide_triage.edges.llm import (
    AssistantTurn,
    ChatLLM,
    TextCallback,
    ToolResult,
    ToolResultsTurn,
    Turn,
    UserTurn,
)
from oxide_triage.guard import guard_notice
from oxide_triage.refute import allowed_numbers, unverified_numbers
from oxide_triage.schemas import GuardDecision
from oxide_triage.tools import TOOL_SPECS, GuardFn, ToolBox, ToolOutcome, ToolSpec

log = logging.getLogger(__name__)

ROUND_CAP_MESSAGE = "tool budget for this message is exhausted; answer from what you already have"
GUARD_REFUSAL = "guard_refusal"  # stop_reason of a turn the request guard answered


@dataclass
class ToolEvent:
    name: str
    input: dict[str, Any]
    outcome: ToolOutcome


@dataclass
class AgentReply:
    text: str = ""
    tool_events: list[ToolEvent] = field(default_factory=list)
    unverified_numbers: list[str] = field(default_factory=list)
    latest_result_id: str | None = None
    error: str | None = None
    stop_reason: str | None = None
    usage: dict[str, int] = field(default_factory=dict)
    guard: GuardDecision | None = None  # the guard's reading of the user's own words
    guard_notes: list[str] = field(default_factory=list)  # what was prepended for the model


ToolCallback = Callable[[ToolEvent], None]


class Agent:
    def __init__(
        self,
        llm: ChatLLM,
        toolbox: ToolBox,
        system: str,
        max_tool_rounds: int = 8,
        number_guard: str = "flag",
        specs: list[ToolSpec] | None = None,
        guard: GuardFn | None = None,
    ):
        self.llm = llm
        self.toolbox = toolbox
        self.system = system
        self.max_tool_rounds = max_tool_rounds
        self.number_guard = number_guard
        self.guard = guard
        self.specs = list(specs) if specs is not None else list(TOOL_SPECS)
        self.transcript: list[Turn] = []
        self.results: list[str] = []  # result ids produced in this conversation, oldest first
        self.usage: dict[str, int] = {}

    # ---- helpers ---------------------------------------------------------------------------

    def _run_calls(
        self, turn: AssistantTurn, on_tool: ToolCallback | None, reply: AgentReply
    ) -> ToolResultsTurn:
        results: list[ToolResult] = []
        for call in turn.tool_calls:
            outcome = self.toolbox.call(call.name, call.input)
            event = ToolEvent(call.name, call.input, outcome)
            reply.tool_events.append(event)
            if outcome.result_id:
                reply.latest_result_id = outcome.result_id
            if on_tool is not None:
                on_tool(event)
            results.append(ToolResult(call.id, call.name, outcome.text, outcome.is_error))
        return ToolResultsTurn(results)

    def _guard_corpus(self, turns: list[Turn]) -> set[float]:
        texts: list[str] = []
        for t in turns:
            if isinstance(t, UserTurn):
                texts.append(t.text)
            elif isinstance(t, ToolResultsTurn):
                texts.extend(r.text for r in t.results)
        # Never the assistant's own earlier prose: a hallucinated number must not launder itself.
        return allowed_numbers(texts)

    @staticmethod
    def _add_usage(total: dict[str, int], usage: dict[str, int]) -> None:
        for k, v in usage.items():
            total[k] = total.get(k, 0) + v

    # ---- the loop --------------------------------------------------------------------------

    def send(
        self,
        user_text: str,
        *,
        prefix: str = "",
        on_text: TextCallback | None = None,
        on_tool: ToolCallback | None = None,
    ) -> AgentReply:
        """``prefix`` is context a front end puts before the user's words (the focused
        material, the scope strip). The guard reads ``user_text`` alone."""
        reply = AgentReply()
        shown = user_text
        if self.guard is not None:
            reply.guard = decision = self.guard(user_text)
            if not decision.proceed:
                refusal = decision.refusal_message or "This request was declined by the request guard."
                turn = AssistantTurn(text=refusal, stop_reason=GUARD_REFUSAL)
                self.transcript.extend([UserTurn(prefix + user_text), turn])
                reply.text, reply.stop_reason = refusal, GUARD_REFUSAL
                if on_text is not None:
                    on_text(refusal)
                return reply
            reply.guard_notes = guard_notice(decision)
            if reply.guard_notes:
                shown = "[Request guard: " + " ".join(reply.guard_notes) + "]\n\n" + user_text
        new: list[Turn] = [UserTurn(prefix + shown)]
        try:
            turn: AssistantTurn | None = None
            for _ in range(self.max_tool_rounds):
                turn = self.llm.chat(self.system, self.transcript + new, self.specs, on_text=on_text)
                new.append(turn)
                self._add_usage(reply.usage, turn.usage)
                if turn.stop_reason == "refusal":
                    why = f" ({turn.stop_detail})" if turn.stop_detail else ""
                    turn.text = (turn.text + f"\n\n[The model declined this turn{why}.]").strip()
                    if turn.tool_calls:  # never leave a tool_use unanswered
                        new.append(self._error_results(turn, "not executed: the model declined the turn"))
                    break
                if turn.stop_reason == "max_tokens" and turn.tool_calls:
                    new.append(self._error_results(turn, "not executed: the reply was cut off by max_tokens"))
                    turn.text = (turn.text + "\n\n[Reply cut off before the tool call was complete.]").strip()
                    break
                if turn.stop_reason == "model_context_window_exceeded":
                    turn.text = (turn.text + "\n\n[Context window exceeded; start a new chat.]").strip()
                    break
                if not turn.tool_calls:
                    break
                new.append(self._run_calls(turn, on_tool, reply))
            else:
                # Round cap hit with calls pending: answer them with errors and ask for words.
                if turn is not None and turn.tool_calls:
                    new.append(self._error_results(turn, ROUND_CAP_MESSAGE))
                turn = self.llm.chat(
                    self.system, self.transcript + new, self.specs, allow_tools=False, on_text=on_text
                )
                new.append(turn)
                self._add_usage(reply.usage, turn.usage)
                if turn.tool_calls:  # a provider that ignores tool_choice=none
                    new.append(self._error_results(turn, ROUND_CAP_MESSAGE))
        except Exception as exc:  # noqa: BLE001 - reported to the user; nothing is committed
            log.warning("chat turn failed: %s", exc)
            reply.error = f"{type(exc).__name__}: {exc}"
            return reply
        # Commit only now: the transcript never holds a half-finished turn.
        self.transcript.extend(new)
        self._add_usage(self.usage, reply.usage)
        for ev in reply.tool_events:
            if ev.outcome.result_id and ev.outcome.result_id not in self.results:
                self.results.append(ev.outcome.result_id)
        assert turn is not None
        # Everything the model said this turn, including a lead-in before a tool call.
        reply.text = "\n\n".join(t.text for t in new if isinstance(t, AssistantTurn) and t.text.strip())
        reply.stop_reason = turn.stop_reason
        if self.number_guard == "flag":
            reply.unverified_numbers = unverified_numbers(reply.text, self._guard_corpus(self.transcript))
        return reply

    @staticmethod
    def _error_results(turn: AssistantTurn, message: str) -> ToolResultsTurn:
        return ToolResultsTurn([ToolResult(c.id, c.name, message, True) for c in turn.tool_calls])

    def new_conversation(self) -> None:
        self.transcript = []
        self.results = []
        self.usage = {}
