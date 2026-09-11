"""The web assistant's turn loop.

A turn takes the user's message plus what the interface knows (the focused material, the scope
strip, a confirmation of a held-back run) and produces an assistant turn made of visible tool
calls, prose and follow-up suggestions. Two drivers decide which tools to call:

* ``RulesDriver`` routes by intent with rules and narrates from the result object. It needs no
  model and no key, so the assistant works everywhere the CLI works.
* ``ModelDriver`` runs the shared ``oxide_triage.agent.Agent`` (Anthropic tool use, or an
  OpenAI-compatible local model) over the same tools, with its number guard. If the model fails
  mid-turn the rules driver answers that turn and says so.

Both go through ``WebToolBox``, the shared ``ToolBox`` with visible steps: every call is emitted
to the front end as it starts and as it finishes, and the outcome is read for what the canvas
needs (the result id, the focused candidate, the questions of a held run).
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel
from pydantic import Field as PField

from oxide_triage.actor import Actor
from oxide_triage.agent import GUARD_REFUSAL, Agent
from oxide_triage.config import DEFAULT_CONFIG_DIR, Config, load_config
from oxide_triage.edges.llm import (
    AssistantTurn,
    ToolCall,
    ToolResult,
    ToolResultsTurn,
    UserTurn,
    chat_availability,
    make_chat_llm,
)
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
from oxide_triage.session import find_candidate
from oxide_triage.tools import (
    TOOL_SPECS,
    ToolBox,
    ToolOutcome,
    ToolSpec,
    _Args,
    agent_system_prompt,
    make_guard,
)

log = logging.getLogger(__name__)

Emit = Callable[[dict[str, Any]], None]


class TurnRequest(BaseModel):
    text: str = ""
    focus: Focus | None = None
    scope: Scope | None = None
    confirm: str | None = None  # id of a pending action the user approved
    dismiss: str | None = None  # id of a pending action the user declined


# ---- the web-only tool and the front end's rules ---------------------------------------------


class SuggestArgs(_Args):
    suggestions: list[str] = PField(max_length=4, description="Two to four short follow-up questions.")


SUGGEST_SPEC = ToolSpec(
    "suggest_followups",
    (
        "Attach two to four short follow-up questions the user might click next (each under 60 characters, "
        "phrased as the user would say them, about this result). Call it once at the end of your answer."
    ),
    SuggestArgs,
)
WEB_SPECS: list[ToolSpec] = [*TOOL_SPECS, SUGGEST_SPEC]

UI_RULES = (
    "You are answering inside the web app. The user sees the structured result on a canvas beside this "
    "chat (shortlist cards, the excluded list, every component of a focused candidate), so do not repeat "
    "whole tables: answer in two to five sentences of plain prose, name materials by formula, then call "
    "suggest_followups. Lines in square brackets at the top of a user message come from the interface "
    "(the focused material, the scope strip, a confirmation), not from the user; treat them as context. "
    "The scope strip's values are applied to triage automatically; do not pass them again. Use explain for "
    "'why is X ranked there', compare for how two materials differ, rerun for a 'what if' change to the "
    "latest result, list_candidates before talking about what was excluded, and triage only for a new "
    "question. Deposition feasibility, film morphology, substrate compatibility and hygroscopic handling "
    "are not modelled; do not speculate about them."
)


def driver_name(config: Config) -> str:
    """Which driver a conversation gets: the model when one is configured and reachable."""
    return "model" if chat_availability(config.llm)[0] else "rules"


def agent_model(config: Config) -> str | None:
    ok, why = chat_availability(config.llm)
    return why if ok else None


# ---- the toolbox with visible steps ----------------------------------------------------------


@dataclass
class TurnState:
    store: SessionStore
    conv: Conversation
    turn: Turn
    emit: Emit
    profile: str
    scope: Scope | None = None
    offline: bool | None = None
    confirmed_tool: str | None = None  # a pending tool the user just approved
    config_dir: Path = DEFAULT_CONFIG_DIR
    outcomes: list[tuple[str, ToolOutcome]] = field(default_factory=list)
    actor: Actor | None = None  # the person behind this turn, as the proxy named them


def _new_id(prefix: str) -> str:
    return f"{prefix}-{int(time.time() * 1000) % 10_000_000:07d}"


def _scope_overrides(scope: Scope | None) -> dict[str, Any] | None:
    if scope is None:
        return None
    out: dict[str, Any] = {}
    for k in ("families", "top_k", "min_band_gap_ev", "max_energy_above_hull_ev_atom", "max_elements"):
        v = getattr(scope, k)
        if v is not None:
            out[k] = v
    return out or None


class WebToolBox(ToolBox):
    """The shared tools, with each call shown in the conversation as it runs."""

    def __init__(self, state: TurnState, config: Config):
        super().__init__(
            store=state.store,
            config_overrides={"cache": {"offline": True}} if state.offline else None,
            tool_result_max_chars=config.agent.tool_result_max_chars,
            request_overrides=_scope_overrides(state.scope),
            progress=_progress_emitter(state.emit),
            actor=state.actor,
        )
        self.state = state
        self.profile = state.profile
        self.config_dir = state.config_dir

    def _config(self, profile: str | None) -> Config:
        # The conversation's profile wins unless the model deliberately named another one.
        name = profile if profile and profile != "default" else self.profile
        return load_config(name, config_dir=self.config_dir, overrides=self.config_overrides)

    def _confirmation_allowed(self, name: str, args: dict[str, Any]) -> bool:
        return self.state.confirmed_tool == name

    def call(self, name: str, raw_input: dict[str, Any] | None) -> ToolOutcome:
        st = self.state
        args = dict(raw_input or {})
        if name == SUGGEST_SPEC.name:
            try:
                st.turn.suggestions = [s[:80] for s in SuggestArgs.model_validate(args).suggestions]
            except Exception as exc:  # noqa: BLE001 - the model gets the failure as data
                return ToolOutcome(name, f"Invalid arguments for {name}: {exc}", True)
            return ToolOutcome(name, "ok")
        # The confirm flag belongs to the confirm button, never to the model: whatever the
        # model passed is dropped, and the flag is set only for the call the person approved.
        args.pop("confirmed", None)
        if st.confirmed_tool == name:
            args["confirmed"] = True
        step = Step(tool=name, label=f"Running {name}", status="running", args=args)
        st.turn.steps.append(step)
        index = len(st.turn.steps) - 1
        st.emit({"type": "step", "step": step.model_dump(), "index": index})
        started = time.monotonic()
        outcome = super().call(name, args)
        step.ms = int((time.monotonic() - started) * 1000)
        self._absorb(step, name, args, outcome)
        st.emit({"type": "step", "step": step.model_dump(), "index": index})
        st.outcomes.append((name, outcome))
        return outcome

    def _absorb(self, step: Step, name: str, args: dict[str, Any], out: ToolOutcome) -> None:
        """Read the outcome for the front end: label, status, result, focus, questions."""
        st, turn = self.state, self.state.turn
        step.status = "failed" if out.is_error else "done"
        result = out.result
        if name in {"triage", "rerun"} and result is not None and out.result_id:
            n_pass = len(result.shortlist) + len(result.ranked_beyond_shortlist)
            if result.needs_confirmation:
                step.status = "held"
                step.label = "Needs confirmation before running"
                turn.pending = Pending(
                    id=_new_id("pending"),
                    tool=name,
                    args={k: v for k, v in args.items() if k != "confirmed"},
                    questions=list(result.clarifications),
                )
                st.emit({"type": "clarify", "pending": turn.pending.model_dump()})
                return
            st.conv.result_ids.append(out.result_id)
            turn.result_id = out.result_id
            if not result.guard.proceed:
                step.status, step.label = "failed", "Request declined"
            elif not result.shortlist:
                step.status, step.label = "failed", "No shortlist served"
            elif name == "triage":
                step.label = f"Ranked {result.n_candidates_considered} candidates, {n_pass} passed the gates"
            else:
                changes = ", ".join(f"{k}={v!r}" for k, v in dict(args.get("changes") or {}).items())
                step.label = f"Reran with {changes} · {n_pass} passed"
            st.emit(
                {
                    "type": "result",
                    "result_id": out.result_id,
                    "previous_result_id": args.get("result_id") if name == "rerun" else None,
                }
            )
            return
        if name == "explain":
            rid = str(args.get("result_id") or "")
            res = st.store.get_result(rid)
            sc = find_candidate(res, str(args.get("candidate") or "")) if res else None
            if out.is_error or sc is None or res is None:
                step.status, step.label = "failed", f"No candidate {args.get('candidate')!r} in result {rid}"
                return
            step.label = f"Explained {sc.record.formula} from result {rid} · nothing re-computed"
            turn.result_id = rid
            turn.explain = out.text
            turn.focus = Focus(result_id=rid, candidate=sc.record.material_id)
            st.emit({"type": "explain", "markdown": out.text})
            st.emit({"type": "focus", "result_id": rid, "candidate": sc.record.material_id})
            return
        if name == "compare":
            ok = out.text.startswith("# Comparison")
            keys = [str(k) for k in (args.get("candidates") or [])]
            step.status = "done" if ok else "failed"
            step.label = ("Compared " + " and ".join(keys)) if ok else "Comparison failed"
            if ok:
                turn.result_id = str(args.get("result_id") or turn.result_id or "") or None
                turn.explain = out.text
                st.emit({"type": "explain", "markdown": out.text})
            return
        labels = {
            "list_candidates": f"Listed {args.get('section', 'shortlist')} of result {args.get('result_id', '')}",
            "parse_request": "Read the request without running it",
            "profiles": "Listed the profiles",
            "cache_status": "Checked the cache",
            "selfcheck": "Ran the self-check",
            "add_material": f"Added {args.get('formula', '')} to the universe",
        }
        step.label = labels.get(name, f"Ran {name}")
        if out.is_error:
            step.label = f"{name} failed"
            step.detail = out.text[:200]


# ---- deterministic narration ---------------------------------------------------------------


def narrate_result(result: TriageResult, rerun_of: str | None, store: SessionStore) -> tuple[str, list[str]]:
    if result.needs_confirmation:
        return "Before I run this, please confirm:\n" + "\n".join(f"- {q}" for q in result.clarifications), []
    if not result.guard.proceed:
        return result.warnings[0], []
    if not result.shortlist:
        return (
            result.warnings[0] if result.warnings else "No candidates passed the gates.",
            ["Rerun with the band-gap gate at 3 eV", "Show what was excluded"],
        )
    n_pass = len(result.shortlist) + len(result.ranked_beyond_shortlist)
    parts: list[str] = []
    if result.fixture_data:
        parts.append("This cache holds synthetic fixture data, so every number is illustrative.")
    scope_bit = ""
    if result.scope and result.scope.families and result.scope.n_in_scope < result.scope.n_universe:
        scope_bit = f" ({result.scope.n_in_scope} of {result.scope.n_universe} in the cache are in scope)"
    parts.append(
        f"{n_pass} of {result.n_candidates_considered} candidates passed the gates under the "
        f"{result.profile_name} profile{scope_bit}."
    )
    parts.append("Shortlist: " + describe_tiers(result.shortlist) + ".")
    first = result.shortlist[0]
    shared = [c.code for c in result.run_notes]
    cav = primary_caveat(first, shared)
    if cav is not None:
        parts.append(f"Main caveat on {first.record.formula}: {cav.text}")
    if result.run_notes:
        parts.append("Shared by every shortlisted candidate: " + " ".join(c.text for c in result.run_notes))
    devs = [d.description for d in result.deviations]
    if devs:
        parts.append("Deviations from the shipped policy: " + " ".join(devs))
    if result.not_acted_on:
        parts.append("Parts of your request I did not act on: " + " ".join(result.not_acted_on))
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


def describe_tiers(rows: list[Any]) -> str:
    """ "tier 1 (within 0.04, order arbitrary): A (0.958), B (0.949); tier 2: C (0.88)"; or a
    plain list when tiering is off."""
    if not rows or rows[0].tier is None:
        return ", ".join(
            f"{s.record.formula} ({s.adjusted_score:.3f})"
            if s.adjusted_score is not None
            else s.record.formula
            for s in rows
        )
    out: list[str] = []
    tiers = sorted({s.tier for s in rows if s.tier is not None})
    for t in tiers:
        members = [s for s in rows if s.tier == t]
        names = ", ".join(
            f"{s.record.formula} ({s.adjusted_score:.3f})"
            if s.adjusted_score is not None
            else s.record.formula
            for s in members
        )
        label = f"tier {t}"
        if len(members) > 1:
            label += " (effectively tied, order arbitrary)"
        out.append(f"{label}: {names}")
    return "; ".join(out)


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
    moved = [
        f"{names[m]} {old_rank[m]} → {new_rank[m]}"
        for m in new_short
        if m in old_short and old_rank.get(m) != new_rank.get(m)
    ]
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
        return (
            f"{r.formula} was excluded by a gate: " + "; ".join(sc.exclusion_reasons) + ".",
            ["Rerun with the band-gap gate at 3 eV", "Show what was excluded"],
        )
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
    cav = primary_caveat(sc, [c.code for c in result.run_notes])
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
    found.sort(key=lambda k: lowered.find(k.lower()))
    return found


class RulesDriver:
    name = "rules"

    def run(self, state: TurnState, box: WebToolBox, req: TurnRequest, config: Config) -> None:
        turn = state.turn
        text = req.text.strip()
        rid = state.conv.latest_result_id
        latest = state.store.get_result(rid) if rid else None
        mentioned = _mentioned_candidates(text, latest, config.terminology)
        focus_key = req.focus.candidate if req.focus else None

        if (
            latest is not None
            and rid
            and (len(mentioned) >= 2 or (focus_key and mentioned and _COMPARE.search(text)))
        ):
            keys = mentioned if len(mentioned) >= 2 else [focus_key, *mentioned]
            out = box.call("compare", {"candidates": keys[:3], "result_id": rid})
            turn.text = out.text.splitlines()[-1] if not out.is_error else out.text
            turn.suggestions = [f"Why is {k} ranked where it is?" for k in keys[:2]]
            return
        if (
            latest is not None
            and rid
            and (mentioned or focus_key)
            and (_EXPLAIN.search(text) or focus_key and not _MODIFIER.search(text))
        ):
            key = mentioned[0] if mentioned else focus_key
            out = box.call("explain", {"candidate": key, "result_id": rid})
            if out.is_error or out.text.startswith("No candidate"):
                turn.text = out.text
                return
            turn.text, turn.suggestions = narrate_explain(latest, key)  # type: ignore[arg-type]
            return
        if latest is not None and rid and _EXCLUDED.search(text) and not _MODIFIER.search(text):
            out = box.call("list_candidates", {"section": "excluded", "limit": 15, "result_id": rid})
            turn.text = (
                f"{len(latest.excluded)} candidates were excluded by a gate; the excluded tab lists each with its reason. "
                "The first few: " + "; ".join(out.text.splitlines()[1:6]).replace("- ", "")
            )
            turn.suggestions = ["Rerun with the band-gap gate at 3 eV", "Rerun with up to 4 elements"]
            return
        if latest is not None and rid and _BEYOND.search(text) and not _MODIFIER.search(text):
            out = box.call("list_candidates", {"section": "beyond", "limit": 20, "result_id": rid})
            turn.text = (
                f"{len(latest.ranked_beyond_shortlist)} candidates passed the gates but sit below the shortlist. "
                + out.text.replace("\n", " ")
            )
            turn.suggestions = [f"Rerun with a shortlist of {len(latest.shortlist) + 10}"]
            return

        if latest is not None and rid and text:
            parsed = criteria_only(text, config)
            changes = {k: v for k in _CHANGE_FIELDS if (v := getattr(parsed, k)) not in (None, [], {})}
            if changes and (_MODIFIER.search(text) or len(text.split()) <= 14):
                out = box.call("rerun", {"result_id": rid, "changes": changes})
                self.narrate_run(state, out, rerun_of=rid)
                return
        if not text:
            turn.text = "Ask a question, or pick one of the suggestions."
            return
        out = box.call("triage", {"request": text})
        self.narrate_run(state, out, rerun_of=None)

    @staticmethod
    def narrate_run(state: TurnState, out: ToolOutcome, rerun_of: str | None) -> None:
        turn = state.turn
        if out.result is None:
            turn.text = out.text
            return
        turn.text, turn.suggestions = narrate_result(out.result, rerun_of, state.store)


# ---- model driver ----------------------------------------------------------------------------


def turns_to_json(transcript: list[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for t in transcript:
        if isinstance(t, UserTurn):
            out.append({"kind": "user", "text": t.text})
        elif isinstance(t, AssistantTurn):
            raw: Any = t.raw
            if isinstance(raw, list):
                raw = [
                    b.model_dump(mode="json", exclude_none=True) if hasattr(b, "model_dump") else b
                    for b in raw
                ]
            out.append(
                {
                    "kind": "assistant",
                    "text": t.text,
                    "tool_calls": [{"id": c.id, "name": c.name, "input": c.input} for c in t.tool_calls],
                    "stop_reason": t.stop_reason,
                    "usage": t.usage,
                    "raw": raw,
                    "raw_provider": t.raw_provider,
                    "stop_detail": t.stop_detail,
                }
            )
        elif isinstance(t, ToolResultsTurn):
            out.append(
                {
                    "kind": "tool_results",
                    "results": [
                        {"call_id": r.call_id, "name": r.name, "text": r.text, "is_error": r.is_error}
                        for r in t.results
                    ],
                }
            )
    return out


def turns_from_json(items: list[dict[str, Any]]) -> list[Any]:
    out: list[Any] = []
    for d in items:
        kind = d.get("kind")
        if kind == "user":
            out.append(UserTurn(str(d.get("text", ""))))
        elif kind == "assistant":
            out.append(
                AssistantTurn(
                    text=str(d.get("text", "")),
                    tool_calls=[
                        ToolCall(str(c["id"]), str(c["name"]), dict(c.get("input") or {}))
                        for c in d.get("tool_calls", [])
                    ],
                    stop_reason=d.get("stop_reason"),
                    usage=dict(d.get("usage") or {}),
                    raw=d.get("raw"),
                    raw_provider=d.get("raw_provider"),
                    stop_detail=d.get("stop_detail"),
                )
            )
        elif kind == "tool_results":
            out.append(
                ToolResultsTurn(
                    [
                        ToolResult(str(r["call_id"]), str(r["name"]), str(r["text"]), bool(r.get("is_error")))
                        for r in d.get("results", [])
                    ]
                )
            )
    return out


class ModelDriver:
    name = "model"

    @staticmethod
    def _context_line(state: TurnState, req: TurnRequest) -> str:
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
                bits.append("scope strip: " + ", ".join(scope_bits))
        if state.confirmed_tool:
            bits.append(
                f"the user confirmed the pending {state.confirmed_tool} call: call it again with the same "
                "arguments and confirmed=true"
            )
        if state.conv.latest_result_id:
            bits.append(f"latest result_id {state.conv.latest_result_id}")
        return f"[Interface: {'; '.join(bits)}]\n\n" if bits else ""

    def run(self, state: TurnState, box: WebToolBox, req: TurnRequest, config: Config) -> None:
        llm = make_chat_llm(config.llm, config.agent)
        agent = Agent(
            llm,
            box,
            agent_system_prompt(state.profile, UI_RULES),
            max_tool_rounds=config.agent.max_tool_rounds,
            number_guard=config.agent.number_guard,
            specs=WEB_SPECS,
            guard=make_guard(config),
        )
        agent.transcript = turns_from_json(state.conv.model_messages)
        user_text = req.text.strip() or ("Yes, go ahead." if state.confirmed_tool else "")
        reply = agent.send(
            user_text,
            prefix=self._context_line(state, req),
            on_text=lambda delta: state.emit({"type": "text", "delta": delta}),
        )
        if reply.error:
            raise RuntimeError(reply.error)
        state.conv.model_messages = turns_to_json(agent.transcript)
        state.turn.text = reply.text.strip()
        state.turn.unverified = list(reply.unverified_numbers)
        if reply.stop_reason == GUARD_REFUSAL:
            step = Step(tool="guard", label="Request declined by the request guard", status="failed", args={})
            step.detail = "; ".join(f.code for f in reply.guard.findings) if reply.guard else None
            state.turn.steps.append(step)
            state.emit({"type": "step", "step": step.model_dump(), "index": len(state.turn.steps) - 1})
        elif reply.guard_notes:
            step = Step(tool="guard", label="Request guard notice", status="done", args={})
            step.detail = " ".join(reply.guard_notes)
            state.turn.steps.insert(0, step)
            state.emit({"type": "step", "step": step.model_dump(), "index": 0})
        if reply.stop_reason == "max_tokens":
            state.turn.text += "\n\n(The answer was cut off at the length limit.)"


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
    config_dir: Path = DEFAULT_CONFIG_DIR,
    offline: bool | None = None,
    actor: Actor | None = None,
) -> Turn:
    """Execute one user turn and return the assistant turn; both are appended to ``conv``.
    ``actor`` is who the proxy says is asking; it goes into the deviation log, nowhere else."""
    profile = (req.scope.profile if req.scope and req.scope.profile else None) or conv.profile
    config = load_config(profile, config_dir=config_dir)
    conv.profile = profile
    user = Turn(id=_new_id("u"), role="user", text=req.text, focus=req.focus, scope=req.scope)
    conv.turns.append(user)
    if not conv.title and req.text.strip():
        conv.title = req.text.strip()[:72]
    turn = Turn(id=_new_id("a"), role="assistant")
    state = TurnState(
        store=store,
        conv=conv,
        turn=turn,
        emit=emit,
        profile=profile,
        scope=req.scope,
        offline=offline,
        config_dir=config_dir,
        actor=actor,
    )

    pending = _find_pending(conv, req.confirm or req.dismiss)
    if req.dismiss and pending is not None:
        turn.text = "Understood, I have not run it. Change the request or the scope and ask again."
        _finish(store, conv, turn, emit)
        return turn
    if req.confirm and pending is not None:
        state.confirmed_tool = pending.tool
        if not req.text.strip():
            user.text = "Yes, run it."

    box = WebToolBox(state, config)
    driver = "model" if conv.driver in {"model", "claude"} and driver_name(config) == "model" else "rules"
    conv.driver = driver

    def rules() -> None:
        if state.confirmed_tool and pending is not None:
            out = box.call(pending.tool, dict(pending.args))
            RulesDriver.narrate_run(
                state, out, rerun_of=pending.args.get("result_id") if pending.tool == "rerun" else None
            )
        else:
            RulesDriver().run(state, box, req, config)

    try:
        if driver == "model":
            ModelDriver().run(state, box, req, config)
        else:
            rules()
    except Exception as exc:  # model or transport failure: answer with rules and say so
        log.warning("driver %s failed: %s", driver, exc)
        if driver == "model":
            turn.steps.clear()
            turn.error = f"The model was unavailable ({type(exc).__name__}); this answer came from the rule-based assistant."
            emit({"type": "notice", "message": turn.error})
            try:
                rules()
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
