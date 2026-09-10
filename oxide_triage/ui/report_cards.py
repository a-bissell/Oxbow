"""A triage result as native Streamlit widgets, with a small "ask" button on every part a
scientist might want to drill into. Each button seeds one chat turn through ``ask(seed)``; the
seed names the result_id, the candidate and the intended tool arguments in words, so the agent
has everything `explain` / `rerun` need. That single hook is also where a clickable HTML report
would plug in later.

Everything shown here is read from the stored ``TriageResult``; nothing is computed.
"""

from __future__ import annotations

from collections.abc import Callable

import streamlit as st

from oxide_triage.edges.render import fmt, pct
from oxide_triage.refute import primary_caveat
from oxide_triage.schemas import ScoredCandidate, TriageResult

Ask = Callable[[str], None] | None

CONFIDENCE_COLOR = {"high": "green", "medium": "orange", "low": "red"}
SEVERITY_ICON = {"critical": "🛑", "warning": "⚠️", "info": "ℹ️"}


def _chip(ask: Ask, label: str, seed: str, key: str) -> None:
    if ask is None:
        return
    st.button(label, key=f"ask:{key}", type="tertiary", on_click=ask, args=(seed,))


def _chips(ask: Ask, items: list[tuple[str, str, str]]) -> None:
    if ask is None or not items:
        return
    with st.container(horizontal=True, wrap=True, gap="small"):
        for label, seed, key in items:
            _chip(ask, label, seed, key)


def _who(sc: ScoredCandidate) -> str:
    return f"{sc.record.formula} ({sc.record.material_id})"


def _banners(result: TriageResult) -> None:
    if result.fixture_data:
        st.warning("**Synthetic fixture data.** Every number below is illustrative.")
    if not result.guard.proceed:
        st.error(result.warnings[0] if result.warnings else "Request declined.")
        return
    for d in result.deviations:
        st.warning(f"**Configuration deviation ({d.origin}).** {d.description}")
    for f in result.guard.findings:
        if f.bin.value == "architecturally_impossible":
            st.info(f'**Not possible in this deployment.** "{f.matched_text}" — {f.explanation}')
    for w in result.warnings:
        st.info(w)
    if result.selfcheck_status == "failed":
        st.error("The self-check on this cache failed; see the warnings above.")


def _candidate_card(
    result: TriageResult, rid: str, sc: ScoredCandidate, passing: list[ScoredCandidate], ask: Ask
) -> None:
    r = sc.record
    cav = primary_caveat(sc)
    who = _who(sc)
    with st.container(border=True):
        head = st.columns([1, 6, 3])
        head[0].markdown(f"### {sc.rank}")
        head[1].markdown(
            f"### {r.formula}  \n"
            f"<span style='color:gray'>{r.material_id} · {r.crystal_system or '?'} {r.spacegroup_symbol or ''}</span>",
            unsafe_allow_html=True,
        )
        with head[2]:
            st.badge(f"confidence {sc.confidence}", color=CONFIDENCE_COLOR.get(sc.confidence, "gray"))
            if sc.missing_criteria:
                st.badge("no data: " + ", ".join(sc.missing_criteria), color="gray")
        st.progress(
            min(max(sc.adjusted_score or 0.0, 0.0), 1.0),
            text=f"score {fmt(sc.adjusted_score)} · coverage {pct(sc.data_coverage)}",
        )
        st.markdown(f"**Why:** {sc.rationale}")
        if cav is not None:
            st.markdown(f"{SEVERITY_ICON.get(cav.severity, '')} **Main caveat:** {cav.text}")
        chips = [
            (
                "Why this rank?",
                f"In result {rid}, explain {who}: why is it ranked {sc.rank}? Walk me through the components and gates that decided it.",
                f"{rid}:{r.material_id}:rank",
            )
        ]
        if cav is not None:
            chips.append(
                (
                    "Main caveat?",
                    f"In result {rid}, explain the caveat '{cav.code}' on {who}: what is the evidence behind it and how serious is it for a thin-film experiment?",
                    f"{rid}:{r.material_id}:caveat",
                )
            )
        if sc.rank and sc.rank > 1:
            above = next((p for p in passing if p.rank == sc.rank - 1), None)
            if above is not None:
                chips.append(
                    (
                        f"Compare with #{above.rank}",
                        f"In result {rid}, compare {who} (rank {sc.rank}) with {_who(above)} (rank {above.rank}): explain both and tell me which components separate them.",
                        f"{rid}:{r.material_id}:compare",
                    )
                )
        _chips(ask, chips)
        with st.expander("Components, gates, caveats"):
            st.dataframe(
                [
                    {
                        "criterion": c.criterion,
                        "weight": round(c.weight, 3),
                        "observed": c.raw_label,
                        "normalised": None if c.normalized is None else round(c.normalized, 3),
                        "contribution": None if c.contribution is None else round(c.contribution, 4),
                        "status": c.status.value,
                    }
                    for c in sc.components
                ],
                hide_index=True,
                width="stretch",
            )
            st.dataframe(
                [
                    {
                        "gate": g.gate,
                        "threshold": g.threshold_label,
                        "observed": g.observed_label,
                        "result": "pass" if g.passed else ("indeterminate" if g.passed is None else "FAIL"),
                    }
                    for g in sc.gates
                ],
                hide_index=True,
                width="stretch",
            )
            st.caption(
                f"Band gap: {sc.band_gap_assessment.correction_note} · OQMD cross-check: {sc.cross_source_agreement}."
            )
            for i, c in enumerate(sc.caveats):
                cols = st.columns([8, 2])
                cols[0].markdown(f"{SEVERITY_ICON.get(c.severity, '')} `{c.code}` ({c.origin}): {c.text}")
                with cols[1]:
                    _chip(
                        ask,
                        "Ask",
                        f"In result {rid}, explain the caveat '{c.code}' on {who}: what is the evidence and what would it mean at the bench?",
                        f"{rid}:{r.material_id}:cav{i}",
                    )


def _gap_map(result: TriageResult, rid: str, passing: list[ScoredCandidate], ask: Ask) -> None:
    if not passing:
        return
    criteria = [c.criterion for c in passing[0].components]
    rows = []
    for sc in passing:
        row = {"rank": sc.rank, "formula": sc.record.formula}
        for c in sc.components:
            row[c.criterion] = "●" if c.status.value == "known" else "?"
        row["coverage"] = pct(sc.data_coverage)
        row["confidence"] = sc.confidence
        rows.append(row)
    st.subheader("Data-gap map")
    st.caption("● data present · ? no data (earns no credit, lowers confidence, named on the card).")
    st.dataframe(rows, hide_index=True, width="stretch")
    missing = [c for c in criteria if any(r.get(c) == "?" for r in rows)]
    _chips(
        ask,
        [
            (
                f"Missing {c}?",
                f"In result {rid}, what does missing {c} data do to the ranking and the confidence labels, and which candidates does it affect?",
                f"{rid}:gap:{c}",
            )
            for c in missing
        ],
    )


def _excluded(result: TriageResult, rid: str, ask: Ask) -> None:
    if not result.excluded:
        return
    st.subheader(f"Excluded ({len(result.excluded)})")
    st.dataframe(
        [
            {
                "formula": sc.record.formula,
                "id": sc.record.material_id,
                "reason": "; ".join(sc.exclusion_reasons),
            }
            for sc in result.excluded
        ],
        hide_index=True,
        width="stretch",
        height=min(35 * (len(result.excluded) + 1), 300),
    )
    _chips(
        ask,
        [
            (
                f"Why {sc.record.formula}?",
                f"In result {rid}, why was {_who(sc)} excluded, and what single change to the criteria would let it pass?",
                f"{rid}:{sc.record.material_id}:excl",
            )
            for sc in result.excluded[:12]
        ],
    )


def _scoring(result: TriageResult, rid: str, ask: Ask) -> None:
    st.subheader("Scoring rules")
    st.code(result.scoring.formula, language=None)
    c1, c2 = st.columns(2)
    c1.dataframe(
        [{"criterion": k, "weight": round(v, 3)} for k, v in sorted(result.scoring.weights.items())],
        hide_index=True,
        width="stretch",
    )
    c2.dataframe(
        [{"gate": k, "value": str(v)} for k, v in sorted(result.scoring.gates.items())],
        hide_index=True,
        width="stretch",
    )
    gap = float(result.scoring.gates.get("min_effective_band_gap_ev", 4.0))
    _chips(
        ask,
        [
            (
                f"Rerun with gap ≥ {gap + 1:g} eV",
                f"Rerun result {rid} with min_band_gap_ev = {gap + 1:g}.",
                f"{rid}:rerun:gap",
            ),
            ("Rerun with top 10", f"Rerun result {rid} with top_k = 10.", f"{rid}:rerun:top10"),
            (
                "Weight dielectric 0.35",
                f'Rerun result {rid} with weight_overrides = {{"dielectric": 0.35}}.',
                f"{rid}:rerun:diel",
            ),
            (
                "Metastable within 60 meV",
                f"Rerun result {rid} with max_energy_above_hull_ev_atom = 0.06.",
                f"{rid}:rerun:hull",
            ),
        ],
    )


def render_report(result: TriageResult, rid: str, ask: Ask) -> None:
    """Render one result. ``ask`` is None when there is no agent to talk to (chips are hidden)."""
    st.caption(
        f"result `{rid}` · profile `{result.profile_name}` · {result.n_candidates_considered} considered · "
        f"{len(result.shortlist) + len(result.ranked_beyond_shortlist)} passed · {len(result.excluded)} excluded · "
        f"self-check {result.selfcheck_status}"
    )
    if result.needs_confirmation:
        st.warning(
            "**Not run yet: confirmation needed.**\n\n" + "\n".join(f"- {q}" for q in result.clarifications)
        )
        _chip(
            ask,
            "Confirm and run",
            "I confirm. Call the same tool again with the same arguments and confirmed=true.",
            f"{rid}:confirm",
        )
        return
    _banners(result)
    if not result.guard.proceed:
        return
    if result.criteria.interpretation_notes:
        st.caption("How the request was read: " + "; ".join(result.criteria.interpretation_notes))
    passing = result.shortlist + result.ranked_beyond_shortlist
    st.subheader("Shortlist")
    if not result.shortlist:
        st.info("No candidate passed every gate under this configuration.")
    for sc in result.shortlist:
        _candidate_card(result, rid, sc, passing, ask)
    if result.ranked_beyond_shortlist:
        with st.expander(f"Ranked beyond the shortlist ({len(result.ranked_beyond_shortlist)})"):
            st.dataframe(
                [
                    {
                        "rank": sc.rank,
                        "formula": sc.record.formula,
                        "id": sc.record.material_id,
                        "score": fmt(sc.adjusted_score),
                        "coverage": pct(sc.data_coverage),
                        "confidence": sc.confidence,
                        "no data": ", ".join(sc.missing_criteria) or "—",
                    }
                    for sc in result.ranked_beyond_shortlist
                ],
                hide_index=True,
                width="stretch",
            )
    _gap_map(result, rid, passing, ask)
    _excluded(result, rid, ask)
    _scoring(result, rid, ask)
    st.caption(result.scope_limitation)
