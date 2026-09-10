"""The assistant's turn loop.

A turn takes the user's message (plus what the interface knows: the focused material, the scope
strip, a confirmation of a held-back run) and produces an assistant turn made of visible tool
calls, prose, and optional follow-up suggestions. Two drivers decide which tools to call:

* ``RulesDriver`` routes by intent with rules and narrates from the result object. It needs no
  model and no key, so the assistant works everywhere the CLI works.
* ``ClaudeDriver`` lets a Claude model orchestrate the same tools and phrase the answer. The
  model never sees a data source: it reads tool output and relays it. Any failure of the model
  falls back to the rules driver for that turn, and says so.

Both drivers emit the same events, so the front end does not know which one answered.
"""

from __future__ import annotations

import logging
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from oxide_triage.config import Config, load_config
from oxide_triage.pipeline import criteria_only
from oxide_triage.progress import Progress
from oxide_triage.refute import primary_caveat
from oxide_triage.schemas import TriageResult
from oxide_triage.server.store import (
    Conversation,
    Focus,
    Pending,
    Scope,
    SessionStore,
    Step,
    Turn,
    now_iso,
)
from oxide_triage.server.tools import TOOLS, ToolContext, ToolOutput, model_tool_definitions
from oxide_triage.session import find_candidate

log = logging.getLogger(__name__)

Emit = Callable[[dict[str, Any]], None]

DEFAULT_AGENT_MODEL = "claude-sonnet-5"
MAX_MODEL_ROUNDS = 8


class TurnRequest(BaseModel):
    text: str = ""
    focus: Focus | None = None
    scope: Scope | None = None
    confirm: str | None = None  # id of a pending action the user approved
    dismiss: str | None = None  # id of a pending action the user declined


def driver_name(config: Config) -> str:
    """Which driver a conversation gets: the model when one is configured and reachable."""
    if config.llm.provider == "anthropic" and os.environ.get("ANTHROPIC_API_KEY"):
        return "claude"
    return "rules"


def agent_model(config: Config) -> str:
    return os.environ.get("AGENT_MODEL") or config.llm.model or DEFAULT_AGENT_MODEL


# ---- executing a tool with visible steps -----------------------------------------------------


@dataclass
class TurnState:
    ctx: ToolContext
    turn: Turn
    emit: Emit
    confirmed_tool: str | None = None  # a pending tool the user just approved
    outputs: list[tuple[str, ToolOutput]] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.outputs = []


def _new_id(prefix: str) -> str:
    return f"{prefix}-{int(time.time() * 1000) % 10_000_000:07d}"


def execute(state: TurnState, name: str, args: dict[str, Any]) -> ToolOutput:
    tool = TOOLS.get(name)
    if tool is None:
        out = ToolOutput(text=f"Unknown tool {name!r}.", error=True, label=f"Unknown tool {name}")
        return out
    args = dict(args or {})
    if state.confirmed_tool == name:
        args["confirmed"] = True
    step = Step(
        tool=name,
        label=tool.description.split(".")[0] if not tool.visible else f"Running {name}",
        status="running",
        args=args,
    )
    if tool.visible:
        state.turn.steps.append(step)
        state.emit({"type": "step", "step": step.model_dump(), "index": len(state.turn.steps) - 1})
    started = time.monotonic()
    try:
        out = tool.run(state.ctx, args)
    except Exception as exc:  # a tool failure is reported, never fatal to the turn
        log.exception("tool %s failed", name)
        out = ToolOutput(
            text=f"Error: {type(exc).__name__}: {exc}", error=True, label=f"{name} failed: {exc}"
        )
    step.ms = int((time.monotonic() - started) * 1000)
    step.status = "failed" if out.error else "done"
    step.label = out.label or step.label
    if out.questions:
        step.status = "held"
    if tool.visible:
        state.emit({"type": "step", "step": step.model_dump(), "index": state.turn.steps.index(step)})
    state.outputs.append((name, out))
    turn = state.turn
    if out.result_id:
        turn.result_id = out.result_id
        if not out.questions:
            state.emit(
                {
                    "type": "result",
                    "result_id": out.result_id,
                    "previous_result_id": out.data.get("previous_result_id"),
                }
            )
    if out.explain:
        turn.explain = out.explain
        state.emit({"type": "explain", "markdown": out.explain})
    if out.focus and out.result_id:
        turn.focus = Focus(result_id=out.result_id, candidate=out.focus)
        state.emit({"type": "focus", "result_id": out.result_id, "candidate": out.focus})
    if out.questions:
        turn.pending = Pending(
            id=_new_id("pending"),
            tool=name,
            args={k: v for k, v in args.items() if k != "confirmed"},
            questions=out.questions,
        )
        state.emit({"type": "clarify", "pending": turn.pending.model_dump()})
    if out.data.get("suggestions"):
        turn.suggestions = list(out.data["suggestions"])
    return out


# ---- deterministic narration ---------------------------------------------------------------


def narrate_result(
    result: TriageResult, rid: str, rerun_of: str | None, store: SessionStore
) -> tuple[str, list[str]]:
    if result.needs_confirmation:
        return "Before I run this, please confirm:\n" + "\n".join(f"- {q}" for q in result.clarifications), []
    if not result.guard.proceed:
        return result.warnings[0], []
    if not result.shortlist:
        return result.warnings[0] if result.warnings else "No candidates passed the gates.", [
            "Rerun with the band-gap gate at 3 eV",
            "Show what was excluded",
        ]
    n_pass = len(result.shortlist) + len(result.ranked_beyond_shortlist)
    n_scope = result.n_candidates_considered
    parts: list[str] = []
    if result.fixture_data:
        parts.append("This cache holds synthetic fixture data, so every number is illustrative.")
    scope_bit = ""
    if result.scope and result.scope.families and result.scope.n_in_scope < result.scope.n_universe:
        scope_bit = f" ({result.scope.n_in_scope} of {result.scope.n_universe} in the cache are in scope)"
    parts.append(
        f"{n_pass} of {n_scope} candidates passed the gates under the {result.profile_name} profile{scope_bit}."
    )
    top = ", ".join(
        f"{s.record.formula} ({s.adjusted_score:.3f})" if s.adjusted_score is not None else s.record.formula
        for s in result.shortlist
    )
    parts.append(f"Shortlist: {top}.")
    first = result.shortlist[0]
    cav = primary_caveat(first)
    if cav is not None:
        parts.append(f"Main caveat on {first.record.formula}: {cav.text}")
    devs = [d.description for d in result.deviations]
    if devs:
        parts.append("Deviations from the shipped policy: " + " ".join(devs))
    partial = [s.record.formula for s in result.shortlist if s.missing_criteria]
    if partial:
        parts.append("Ranked on partial data: " + ", ".join(partial) + ".")
    if rerun_of:
        prev = store.get_result(rerun_of)
        if prev is not None:
            parts.append(describe_diff(prev, result))
    suggestions = [f"Why is {first.record.formula} first?"]
    if len(result.shortlist) > 1:
        suggestions.append(f"Compare {first.record.formula} with {result.shortlist[1].record.formula}")
    suggestions.append("Show what was excluded")
    suggestions.append(f"Rerun with a shortlist of {len(result.shortlist) + 5}")
    return " ".join(parts), suggestions


def describe_diff(prev: TriageResult, new: TriageResult) -> str:
    old_rank = {s.record.material_id: s.rank for s in prev.shortlist + prev.ranked_beyond_shortlist}
    new_rank = {s.record.material_id: s.rank for s in new.shortlist + new.ranked_beyond_shortlist}
    old_short = [s.record.material_id for s in prev.shortlist]
    new_short = [s.record.material_id for s in new.shortlist]
    names = {
        s.record.material_id: s.record.formula
        for s in prev.shortlist + prev.ranked_beyond_shortlist + new.shortlist + new.ranked_beyond_shortlist
    }
    entered = [names[m] for m in new_short if m not in old_short]
    left = [names[m] for m in old_short if m not in new_short]
    moved = []
    for m in new_short:
        if m in old_short and old_rank.get(m) != new_rank.get(m):
            moved.append(f"{names[m]} {old_rank[m]} → {new_rank[m]}")
    bits = []
    if entered:
        bits.append("entered: " + ", ".join(entered))
    if left:
        bits.append("left: " + ", ".join(left))
    if moved:
        bits.append("moved: " + ", ".join(moved))
    return (
        "Compared with the previous result, "
        + ("; ".join(bits) if bits else "the shortlist is unchanged")
        + "."
    )


def narrate_explain(result: TriageResult, key: str) -> tuple[str, list[str]]:
    sc = find_candidate(result, key)
    if sc is None:
        return f"I could not find {key} in the latest result.", []
    r = sc.record
    n_pass = len(result.shortlist) + len(result.ranked_beyond_shortlist)
    if sc.excluded:
        text = f"{r.formula} was excluded by a gate: " + "; ".join(sc.exclusion_reasons) + "."
        sugg = ["Rerun with the band-gap gate at 3 eV", "Show what was excluded"]
        return text, sugg
    known = [c for c in sc.components if c.contribution is not None]
    weakest = min(known, key=lambda c: c.normalized or 0.0) if known else None
    strongest = max(known, key=lambda c: c.normalized or 0.0) if known else None
    parts = [
        f"{r.formula} ranks {sc.rank} of {n_pass} passing with an adjusted score of {sc.adjusted_score:.3f} "
        f"and {sc.confidence} confidence."
    ]
    if strongest and weakest and strongest is not weakest:
        parts.append(
            f"Its strongest component is {strongest.criterion} ({strongest.raw_label}, normalised "
            f"{strongest.normalized:.2f}) and its weakest is {weakest.criterion} ({weakest.raw_label}, "
            f"normalised {weakest.normalized:.2f})."
        )
    if sc.missing_criteria:
        parts.append(
            "No data for " + ", ".join(sc.missing_criteria) + ", which earns no credit and lowers confidence."
        )
    cav = primary_caveat(sc)
    if cav is not None:
        parts.append(f"Main caveat: {cav.text}")
    sugg = []
    if result.shortlist and result.shortlist[0].record.material_id != r.material_id:
        sugg.append(f"Compare {r.formula} with {result.shortlist[0].record.formula}")
    if weakest is not None:
        sugg.append(f"Rerun with {weakest.criterion} weight 0")
    sugg.append("Show what was excluded")
    return " ".join(parts), sugg


# ---- rules driver ----------------------------------------------------------------------------

_EXPLAIN = re.compile(
    r"\b(why|explain|what about|tell me about|how (did|does|come)|details? (on|for|about)|walk me through)\b",
    re.I,
)
_COMPARE = re.compile(r"\b(compare|versus|vs\.?|against|difference between|differ)\b", re.I)
_EXCLUDED = re.compile(r"\b(excluded|left out|didn'?t make|did not make|failed|rejected|cut)\b", re.I)
_BEYOND = re.compile(
    r"\b(beyond|next \d+|ranks? \d+|more candidates|show more|below the shortlist|runners?.up)\b", re.I
)
_MODIFIER = re.compile(
    r"\b(rerun|re-run|again|instead|change|set|make it|what if|without|with|lift|allow|permit|include|exclude|only|"
    r"top \d+|shortlist|gap|hull|weight|prioriti[sz]e|drop|remove|add|widen|narrow|relax|tighten|limit)\b",
    re.I,
)
_MPID = re.compile(r"\bmp-\d+\b", re.I)
_CHANGE_FIELDS = (
    "top_k",
    "max_energy_above_hull_ev_atom",
    "min_band_gap_ev",
    "max_elements",
    "include_elements",
    "exclude_elements",
    "allow_elements",
    "weight_overrides",
    "families",
)


def _mentioned_candidates(text: str, result: TriageResult | None, terminology: dict[str, str]) -> list[str]:
    if result is None:
        return []
    lowered = text.lower()
    for word, canon in terminology.items():
        if re.search(rf"\b{re.escape(word.lower())}\b", lowered) and re.match(r"^[A-Z][A-Za-z0-9]*$", canon):
            lowered = lowered.replace(word.lower(), canon.lower())
    known: dict[str, str] = {}
    for sc in result.shortlist + result.ranked_beyond_shortlist + result.excluded:
        known.setdefault(sc.record.formula.lower(), sc.record.formula)
        known.setdefault(sc.record.material_id.lower(), sc.record.material_id)
    found: list[str] = []
    for tok in re.findall(r"[A-Za-z][A-Za-z0-9\-]*", lowered):
        if tok in known and known[tok] not in found:
            found.append(known[tok])
    for m in _MPID.findall(text):
        if m.lower() in known and known[m.lower()] not in found:
            found.append(known[m.lower()])
    # keep the order they appear in the text
    found.sort(key=lambda k: lowered.find(k.lower()))
    return found


class RulesDriver:
    name = "rules"

    def run(self, state: TurnState, req: TurnRequest, config: Config) -> None:
        ctx, turn = state.ctx, state.turn
        text = req.text.strip()
        rid = ctx.conv.latest_result_id
        latest = ctx.store.get_result(rid) if rid else None
        mentioned = _mentioned_candidates(text, latest, config.terminology)
        focus_key = req.focus.candidate if req.focus else None

        if latest is not None and (
            len(mentioned) >= 2 or (focus_key and mentioned and _COMPARE.search(text))
        ):
            keys = mentioned if len(mentioned) >= 2 else [focus_key, *mentioned]
            out = execute(state, "compare", {"candidates": keys[:3], "result_id": rid})
            turn.text = out.text.splitlines()[-1] if not out.error else out.text
            turn.suggestions = [f"Why is {k} ranked where it is?" for k in keys[:2]]
            return
        if (
            latest is not None
            and (mentioned or focus_key)
            and (_EXPLAIN.search(text) or focus_key and not _MODIFIER.search(text))
        ):
            key = mentioned[0] if mentioned else focus_key
            out = execute(state, "explain", {"candidate": key, "result_id": rid})
            if out.error:
                turn.text = out.text
                return
            turn.text, turn.suggestions = narrate_explain(latest, key)  # type: ignore[arg-type]
            return
        if latest is not None and _EXCLUDED.search(text) and not _MODIFIER.search(text):
            out = execute(state, "list_candidates", {"section": "excluded", "limit": 15, "result_id": rid})
            turn.text = (
                f"{len(latest.excluded)} candidates were excluded by a gate; the excluded tab lists each with its reason. "
                "The first few: " + "; ".join(out.text.splitlines()[1:6]).replace("- ", "")
            )
            turn.suggestions = ["Rerun with the band-gap gate at 3 eV", "Rerun with up to 4 elements"]
            return
        if latest is not None and _BEYOND.search(text) and not _MODIFIER.search(text):
            out = execute(state, "list_candidates", {"section": "beyond", "limit": 20, "result_id": rid})
            turn.text = (
                f"{len(latest.ranked_beyond_shortlist)} candidates passed the gates but sit below the shortlist. "
                + out.text.replace("\n", " ")
            )
            turn.suggestions = [f"Rerun with a shortlist of {len(latest.shortlist) + 10}"]
            return

        # A modification of the latest result, or a new request.
        if latest is not None and text:
            parsed = criteria_only(text, config)
            changes = {k: v for k in _CHANGE_FIELDS if (v := getattr(parsed, k)) not in (None, [], {})}
            if changes and (_MODIFIER.search(text) or len(text.split()) <= 14):
                out = execute(state, "rerun", {"result_id": rid, "changes": changes})
                self._narrate_run(state, out, rerun_of=rid)
                return
        if not text:
            turn.text = "Ask a question, or pick one of the suggestions."
            return
        out = execute(state, "triage", {"request": text})
        self._narrate_run(state, out, rerun_of=None)

    def _narrate_run(self, state: TurnState, out: ToolOutput, rerun_of: str | None) -> None:
        turn = state.turn
        if out.result_id is None:
            turn.text = out.text
            return
        result = state.ctx.store.get_result(out.result_id)
        if result is None:
            turn.text = out.text
            return
        turn.text, turn.suggestions = narrate_result(result, out.result_id, rerun_of, state.ctx.store)


# ---- Claude driver ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are the assistant inside Oxide Triage, a tool that ranks oxide dielectric candidates for \
thin-film experiments from cached public data (Materials Project, OQMD, OpenAlex, PubChem). The ranking is \
deterministic and happens inside your tools; you decide which tool to call and you phrase the answer.

Rules that do not bend:
- Numbers, ranks, citations and caveats come only from tool output. Relay them as given. Never adjust, extend, \
estimate, round differently or invent a value. If a tool did not provide something, say so.
- The user sees the structured result on a canvas beside this chat, so do not repeat whole tables. Answer in two to \
five sentences of plain prose, name materials by formula, then call suggest_followups with two to four short \
follow-ups the user might click.
- When a tool returns NEEDS CONFIRMATION, put the questions to the user in your own words and stop. Do not call the \
tool again until the user agrees; when they do, call the same tool again with confirmed=true.
- If a result says SYNTHETIC FIXTURE DATA, say so plainly.
- The system cannot trigger lab actions, read private data or use paywalled sources. Deposition feasibility, film \
morphology, substrate compatibility and hygroscopic handling are not modelled; do not speculate about them.
- Use explain for "why is X ranked there", compare for how two materials differ, rerun for "what if" changes to the \
latest result, list_candidates before talking about what was excluded, and triage for a new question. Do not run a \
new triage when a rerun of the latest result answers the question.
- Lines in square brackets at the top of a user message come from the interface (the focused material, the scope \
strip, a confirmation), not from the user; treat them as context."""


class ClaudeDriver:
    name = "claude"

    def __init__(self, model: str):
        self.model = model

    def _context_line(self, state: TurnState, req: TurnRequest) -> str:
        bits: list[str] = []
        if req.focus:
            bits.append(f"focused on {req.focus.candidate} in result {req.focus.result_id}")
        if req.scope:
            sc = req.scope
            scope_bits = [f"profile {sc.profile}"] if sc.profile else []
            if sc.families:
                scope_bits.append("families " + "+".join(sc.families))
            if sc.top_k:
                scope_bits.append(f"top {sc.top_k}")
            if sc.min_band_gap_ev is not None:
                scope_bits.append(f"gap ≥ {sc.min_band_gap_ev:g} eV")
            if sc.max_energy_above_hull_ev_atom is not None:
                scope_bits.append(f"hull ≤ {sc.max_energy_above_hull_ev_atom:g} eV/atom")
            if scope_bits:
                bits.append("scope strip: " + ", ".join(scope_bits) + " (applied automatically to triage)")
        if state.confirmed_tool:
            bits.append(
                f"the user confirmed the pending {state.confirmed_tool} call: call it again with the same arguments and confirmed=true"
            )
        if state.ctx.conv.latest_result_id:
            bits.append(f"latest result_id {state.ctx.conv.latest_result_id}")
        return f"[Interface: {'; '.join(bits)}]\n\n" if bits else ""

    def run(self, state: TurnState, req: TurnRequest, config: Config) -> None:
        import anthropic

        client = anthropic.Anthropic(timeout=config.llm.timeout_s or 120)
        conv, turn, emit = state.ctx.conv, state.turn, state.emit
        messages: list[dict[str, Any]] = list(conv.model_messages)
        user_text = req.text.strip() or ("Yes, go ahead." if state.confirmed_tool else "")
        messages.append({"role": "user", "content": self._context_line(state, req) + user_text})
        text_parts: list[str] = []
        response = None
        for _ in range(MAX_MODEL_ROUNDS):
            with client.messages.stream(
                model=self.model,
                max_tokens=8000,
                system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
                tools=model_tool_definitions(),
                messages=messages,
            ) as stream:
                for delta in stream.text_stream:
                    text_parts.append(delta)
                    emit({"type": "text", "delta": delta})
                response = stream.get_final_message()
            messages.append(
                {
                    "role": "assistant",
                    "content": [b.model_dump(mode="json", exclude_none=True) for b in response.content],
                }
            )
            if response.stop_reason == "refusal":
                text_parts.append("\n\nThe model declined to answer this request.")
                break
            tool_uses = [b for b in response.content if b.type == "tool_use"]
            if not tool_uses:
                break
            results = []
            for tu in tool_uses:
                out = execute(state, tu.name, dict(tu.input or {}))
                results.append(
                    {"type": "tool_result", "tool_use_id": tu.id, "content": out.text, "is_error": out.error}
                )
            messages.append({"role": "user", "content": results})
            if text_parts and not text_parts[-1].endswith("\n"):
                text_parts.append("\n\n")
                emit({"type": "text", "delta": "\n\n"})
        conv.model_messages = messages
        turn.text = "".join(text_parts).strip()
        if response is not None and response.stop_reason == "max_tokens":
            turn.text += "\n\n(The answer was cut off at the length limit.)"


# ---- the turn ---------------------------------------------------------------------------------


def _progress_emitter(emit: Emit) -> Callable[[Progress], None]:
    def fn(p: Progress) -> None:
        emit({"type": "progress", "stage": p.stage, "message": p.message, "done": p.done, "total": p.total})

    return fn


def run_turn(
    store: SessionStore,
    conv: Conversation,
    req: TurnRequest,
    emit: Emit,
    config_loader: Callable[[str], Config] = load_config,
    offline: bool | None = None,
) -> Turn:
    """Execute one user turn and return the assistant turn; both are appended to ``conv``."""
    profile = (req.scope.profile if req.scope and req.scope.profile else None) or conv.profile
    config = config_loader(profile)
    conv.profile = profile
    user = Turn(id=_new_id("u"), role="user", text=req.text, focus=req.focus, scope=req.scope)
    conv.turns.append(user)
    if not conv.title and req.text.strip():
        conv.title = req.text.strip()[:72]
    turn = Turn(id=_new_id("a"), role="assistant")
    ctx = ToolContext(
        store=store,
        conv=conv,
        profile=profile,
        scope=req.scope,
        progress=_progress_emitter(emit),
        offline=offline,
        config_loader=config_loader,
    )
    state = TurnState(ctx=ctx, turn=turn, emit=emit)

    pending = _find_pending(conv, req.confirm or req.dismiss)
    if req.dismiss and pending is not None:
        turn.text = "Understood, I have not run it. Change the request or the scope and ask again."
        _finish(store, conv, turn, emit)
        return turn
    if req.confirm and pending is not None:
        state.confirmed_tool = pending.tool
        if not req.text.strip():
            user.text = "Yes, run it."

    driver: str = conv.driver if conv.driver in {"rules", "claude"} else driver_name(config)
    if driver == "claude" and driver_name(config) != "claude":
        driver = "rules"
    conv.driver = driver
    try:
        if driver == "claude":
            ClaudeDriver(agent_model(config)).run(state, req, config)
        else:
            if state.confirmed_tool and pending is not None:
                out = execute(state, pending.tool, dict(pending.args))
                RulesDriver()._narrate_run(state, out, rerun_of=None)
            else:
                RulesDriver().run(state, req, config)
    except Exception as exc:  # model or transport failure: answer with rules and say so
        log.warning("driver %s failed: %s", driver, exc)
        if driver == "claude":
            turn.steps.clear()
            turn.error = f"The model was unavailable ({type(exc).__name__}); this answer came from the rule-based assistant."
            emit({"type": "notice", "message": turn.error})
            try:
                if state.confirmed_tool and pending is not None:
                    out = execute(state, pending.tool, dict(pending.args))
                    RulesDriver()._narrate_run(state, out, rerun_of=None)
                else:
                    RulesDriver().run(state, req, config)
            except Exception as exc2:
                log.exception("rules driver failed after model failure")
                turn.error = f"{turn.error} Then the rule-based assistant failed too: {exc2}"
                turn.text = (
                    turn.text or "Something went wrong while answering. The details are in the server log."
                )
        else:
            log.exception("rules driver failed")
            turn.error = f"{type(exc).__name__}: {exc}"
            turn.text = (
                turn.text or "Something went wrong while answering. The details are in the server log."
            )
    _finish(store, conv, turn, emit)
    return turn


def _find_pending(conv: Conversation, pid: str | None) -> Pending | None:
    if not pid:
        return None
    for t in reversed(conv.turns):
        if t.role == "assistant" and t.pending and t.pending.id == pid:
            return t.pending
    return None


def _finish(store: SessionStore, conv: Conversation, turn: Turn, emit: Emit) -> None:
    turn.created_at = now_iso()
    conv.turns.append(turn)
    store.save_conversation(conv)
    emit({"type": "turn", "turn": turn.model_dump()})
