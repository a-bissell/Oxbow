# Design note: Oxide Dielectric Triage Assistant

*Deployable agentic triage for a materials-science research centre. Prototype, one week.*

## 1. What the tool is for, and what it is not

A PI wants scientists to triage candidate oxide dielectrics before committing bench time. The
brief asks for something **useful, honest, traceable, configurable and robust**, deployable at a
research centre, with no wet-lab actions, no private data and no paywalled sources.

The tool takes a natural-language request, ranks known oxides from cached public data against
explicit criteria, and returns a shortlist where every number traces to a source, every gap in the
data is named, and every entry carries the arguments against it. It does not propose new
materials, predict deposition routes, or make publishable claims.

## 2. Core principle: the model lives only at the edges

```mermaid
flowchart LR
    subgraph EDGE_IN["Front edge"]
        G[Guard<br/>rule-based bins] --> P[Parser<br/>rules, optional LLM fill-in<br/>validated schema]
    end
    subgraph CORE["Deterministic core — no model, no network, no clock"]
        D[Data layer<br/>SQLite cache] --> S[Gates + 6 scored criteria<br/>missing-data policy] --> R[Rank<br/>tie-break by id]
    end
    subgraph EDGE_OUT["Back edge"]
        F[Refutation<br/>rule caveats, optional LLM<br/>over delimited facts<br/>numeric guard] --> T[Templates<br/>PI summary · audit · JSON]
    end
    REQ([Scientist request]) --> G
    P -->|Criteria| S
    R --> F
    T --> OUT([Shortlist + caveats + gaps])
    MP[(Materials Project)] -.warm-cache.-> D
    OQ[(OQMD)] -.-> D
    OA[(OpenAlex)] -.-> D
    PC[(PubChem)] -.-> D
    HZ[/element hazard table<br/>versioned YAML/] --> D
    style CORE fill:#eef6ee,stroke:#3a7d44
```

In an agentic pipeline, a model's editorial characterisation of a deterministic result ("this
looks promising") propagates downstream with the same apparent authority as the measured value it
describes. The only structural defence is to make sure computation and narration never share a
code path. Here the core (`scoring/`) imports nothing from `edges/`; the renderer receives a
finished result object and a template file and has no handle on any data source; the refutation
stage receives structured facts and may annotate but cannot touch a rank or a score. A run's
`config_hash` and `cache_fingerprint` identify the answer: same query, same cache, same output,
next month too.

The model is optional everywhere it appears; with `llm.provider: none` rules parse and templates
render, and the tests, evaluation and demo all run that way. With a provider configured, the model may *fill in* fields the rules left at default (never
override an element allow/exclude decision), and may add up to three "observations" per candidate,
each of which must cite a fact field that exists and may contain no number that is not already in
the facts. Anything else is discarded. A hostile model can therefore only fail, not act
(`tests/test_injection.py`).

## 3. Data layer

Public sources only, each cached in SQLite with a retrieval timestamp before anything downstream
sees it. **Materials Project** (REST, no pymatgen) supplies the candidate universe: oxygen plus one
or two cations from a versioned allowlist, so hydroxides, carbonates and oxyhalides are excluded by
construction; hazardous cations stay *in* the universe so that "include lead" is a configuration
change rather than a re-fetch. **OQMD** gives an independent hull distance. **OpenAlex** gives two
counts per compound: works matching the formula or its common names, and the subset that also
match a thin-film deposition term. **PubChem** gives compound-level GHS statements where a record
exists. An **element hazard table** in the repo, versioned and with a cited basis per element,
is the scoring basis for toxicity because it is complete; PubChem is layered on as caveat evidence
because it is not. Absence is cached (an explicit `{found: false}` row with a timestamp), and
the functional behind each gap is read from the producing MP task's `run_type`, or recorded as
`unknown`, never guessed. **ICSD** and other closed sources are excluded and the README says why; MP's `theoretical` flag stands in for "has an experimentally observed structure".

The prototype was built without egress to any source: the clients follow the documented
endpoints, sit behind one adapter, and were exercised against a **synthetic fixture** of ~35
well-known oxides. The first live `warm-cache` (September 2026) then checked them against reality.
It found one shape mismatch, in the adapter as intended: MP's summary carries no `band_gap` origin,
so the functional behind a gap is now read from the `electronic_structure` origin's task. It also
measured the universe and the cost of filling it, and both numbers changed the design.

**Scope.** At the original fetch bounds (hull ≤ 0.20 eV/atom, reported gap ≥ 1.0 eV) the universe
was 7,124 materials. No shipped profile gates looser than 0.10 eV/atom and 1.8 eV before
correction, and at those bounds it is 4,769 materials over 2,651 formulas, 690 of them with a DFPT
dielectric tensor. Within that set:

| Filter | Materials | Formulas | With DFPT dielectric |
|---|---|---|---|
| bounds only | 4,769 | 2,651 | 690 |
| experimentally observed (MP `theoretical: false`) | 2,221 | 1,584 | 449 |
| observed, hull ≤ 0.05 | 2,129 | 1,553 | 440 |
| observed, hull ≤ 0.02 | 1,828 | 1,419 | 406 |

Two conclusions. Hull is not a scoping lever: below 0.10 it removes almost nothing, so the fetch
bound sits at the loosest profile and the profiles' own gates do the rest. The `theoretical`
flag is: it removes 40% of the universe and a third of the dielectric-bearing entries, nearly all
hypothetical polymorphs, and it matches the tool's purpose. The group will deposit films of what
comes out, so a structure nobody has synthesised is a discovery target, not a triage candidate.
The commonest cations among the observed compounds (P, Si, B, Na, Al, V, Te, Ca, Li, K) say the
allowlist is the other honest lever: phosphates, borates and alkali oxides are mobile-ion or
glass-forming compounds no gate stack would use. The intended universe is therefore
experimentally observed oxides of a dielectric-minded cation list (early transition metals, Al,
Si, rare earths, alkaline earths, plus the hazardous cations a profile may opt into), which should
roughly halve the universe again. That is a domain judgment, so it lives in the versioned
allowlist with a rationale per family and is confirmed with the PI rather than set here.
Polymorphs stay: which phase a film adopts is the whole question for the ferroelectric profile.

**Cost.** Materials Project is cheap: universe, dielectric and task lookups batch a hundred ids
per call and the whole universe warms in minutes. The formula-keyed sources are not. OQMD answers
a composition it has not cached in 10–50 s and returns 429 above about 2.4 requests/s; OpenAlex
now meters a daily budget ($0.10/day per IP, $1/day with a free key, $0.001 per full-text
search, two per formula). Warming literature for thousands of formulas would take days, so it is
fetched per query for the top-ranked pool instead (§8) and never at warm time, and OQMD and
PubChem get the same treatment: a warm touches MP only, so a reviewer's first run takes minutes,
and a query fills the ranked pool, re-ranks, and repeats until the pool is settled, because
unlike literature an OQMD disagreement lowers a score. The shortlist is drawn from that fully
retrieved pool, rows below it say so, and retrieval completeness is measured over the pool.
Per-source worker pools and request-rate caps bound the pressure on each source.

## 4. Domain handling a materials scientist checks first

**DFT gaps are underestimated.** Correction is a config strategy (`none`, `scalar_factor`,
`hse_preferred`), the functional is displayed beside every value, and a corrected value is always
labelled *corrected*. `hse_preferred` mostly falls back to the scalar path because HSE is sparse in
MP; the audit view shows exactly that rather than implying a hybrid result. The gate applies to
the *effective* gap, so a 3.0 eV GGA value passes a 4 eV gate under a 1.4× correction and fails it
under `none`.

**Bulk stability is not thin-film processability.** Hull distance says nothing about ALD or
sputter routes, crystallisation, hygroscopicity or lattice match. None of that is computed from
public thermodynamic data, so the tool does not model it and the model is not allowed to speculate
about it. A structural scope-limitation statement appears in every output.

**Dielectric coverage is sparse.** `unknown` is a distinct `DataStatus`, not a value. It
propagates through scoring as `None`, lowers `data_coverage`, caps the confidence label at
*medium*, appears by name in the shortlist entry, and generates a caveat.

**There are two kinds of unknown, and merging them corrupts the ranking.** This is the thing the
first live warm taught us that the fixture never could. MP holds no DFPT tensor for 1,979 of the
ranked candidates — a fact about the data, stable across runs, and exactly the sparse-coverage
case the brief describes. But our warm also died partway through: OQMD rate-limited and OpenAlex
hit its daily budget, leaving 1,791 candidates with no cross-check and 1,731 with no literature
counts. Both arrived at the scorer as the same `UNKNOWN`.

That is not a labelling nicety. Because missing criteria reduce `data_coverage`, and coverage
multiplies the score under `no_credit`, a candidate whose fetches failed is pushed down the
ranking for a reason that has nothing to do with the material. On our own partial cache the top
twenty were 17/20 cross-checked against 20% across the ranked set: the head of the list was
substantially *the subset whose downloads finished*. A tool that reports this as a ranking is
lying, and lying in the specific way the whole design is meant to prevent — the presentation
carrying more authority than the evidence under it.

So `DataStatus` splits into `ABSENT` (the source answered; it holds no record) and
`NOT_RETRIEVED` (we never got an answer here). The fetch layer always knew the difference; it
was being discarded one layer up. The scoring arithmetic is deliberately unchanged — we do not
know the value either way, and crediting an unfetched candidate would be the worse error — but
everything a reader sees now keeps them apart. A candidate carries `retrieval_gap` and a
`comparable` flag; the refutation pass raises `incomplete_retrieval` as a *critical* caveat
saying the rank is not comparable; and each result reports a `retrieval_completeness` measure
that warns when the cache is too patchy to compare candidates at all, with a configurable floor
below which no ranking is served. Only one of the two states is fixable by warming the cache,
and the output now says which.

## 5. Ranking

Six criteria, each a weight and a normalised score in [0, 1]: stability (with a cross-source
agreement bonus and disagreement penalty), effective band gap (threshold plus preference curve),
dielectric constant where known, hazard tier, compositional simplicity, literature evidence
(thin-film-weighted, log-saturating). Hard gates exclude before scoring with a stated reason; every
component's contribution is in the audit view; ties break on material id.

The missing-data policy is the interesting part, because the first version was wrong. The natural
choice is to renormalise over the criteria that have data, then subtract a small penalty for the
missing weight. Under that policy the fixture run put five candidates with **no** dielectric
value above HfO₂: an unknown criterion cannot pull a score down, so a candidate that would have
scored poorly on it is helped by the gap. The known-answer check in the evaluation suite caught it
before anything else did. The shipped default is `no_credit`: an unknown criterion earns nothing
for ranking, is displayed as unknown, and lowers confidence. A candidate can never outrank another
by having less data. `renormalize` is kept as a documented option so the comparison can be
reproduced, and the notebook does so.

Two default curves were retuned after the same check (dielectric preference 8→30, gap preference
saturating at 5.5 eV effective); the reasoning is written into `default.yaml`, and the calibration
set is four compounds, so the profiles exist for sites to retune against their own judgement.

## 6. Refutation pass

A shortlist entry is a **conjecture**, held provisionally and stated so that it can be falsified.
After ranking, every shortlisted candidate goes through a stage whose only job is to argue against
it. Its output is the caveats column: hygroscopicity, absent dielectric data, single-source or
contested stability, metastability, theoretical structures, corrected or near-threshold gaps,
hazard tiers (critical when permitted by configuration), thin literature, partial coverage. An optional model pass
elaborates over the same structured facts, delimited as data, under the guards described in
section 2. The
scientist at the bench is the refutation step the system cannot perform for itself. The tool
proposes; the bench disposes.

## 7. Constraint handling

Requests are sorted into three bins **before any model sees them**, by rules, so refusal
behaviour does not depend on a model. *Bin 1, architecturally impossible* (wet-lab triggers,
private data, paywalled sources): the response explains that the capability does not exist in the
deployment; no stub tools exist to decline politely. *Bin 2, configuration
deviation*: "include lead-containing compounds" looks like a safety bypass but a Pb-ferroelectrics
group has a real reason. It is permitted, the deviation is printed at the top of the output, a
critical hazard caveat stays attached to every affected candidate, and the event is appended to a
log. Distinguishing policy from configuration is the point of the bin. *Bin 3, evidence-integrity
attacks* ("cite a paper supporting this", "just give me a number", "rank these anyway"): refused,
naming what would have been fabricated.

Indirect injection is handled at the boundary: retrieved text is JSON-encoded inside
`<retrieved_data>` blocks, the preamble declares it data, and model output is validated against a
schema and a numeric guard. The test plants an instruction in a paper title, runs a fake model that
obeys anything, and checks that ranks are byte-identical and no smuggled number appears.

## 8. How agentic, and where

"Agentic" is earned by deciding what to do next, and this system is deliberately agentic in
some places and deliberately not in others. It decides at the boundary (refuse, proceed, proceed with a logged
deviation), asks before acting when a request changes something material (a
clarify-before-run step in every front end), carries a conversation about a result without
re-deriving it (`explain`, `rerun` from stored result objects), acquires data on demand and
adaptively: literature counts are fetched at query time for the top-ranked pool only (OpenAlex
meters a small daily budget), and after every warm, gaps in what was retrieved (no OQMD match, an
unresolved functional) are attacked with an allowlist of read-only routes such as a
chemical-system query with stoichiometry matching or a common-name search, each attempt logged,
each filled value labelled with its route, and gaps with no public route (dielectric constants
beyond MP's DFPT set) reported as unfillable rather than estimated. A model may order the
allowed routes for a gap and nothing more, and checks itself against ground truth
after every change to its cache, refusing to serve from a cache that fails. It is not agentic
where numbers and ranks are concerned: no model, no loop and no runtime choice sits between a
retrieved value and a score. The **web app** is where this is most visible. Its assistant is a client for the same tools
the MCP server exposes (triage, explain, compare, rerun, add a material, cache status), and it
has two interchangeable drivers: a rule-based one that routes each message by intent and
narrates from the result object, and a Claude model that orchestrates the tools and phrases the
answer. Both produce the same visible steps in the conversation, one line per tool call, and the
same canvas beside it, which renders result objects rather than model prose: shortlist cards,
the excluded list with its gates, a data-gap map, a focus view per candidate with every
component and provenance, and a diff after every rerun. A value set on the scope strip (profile,
material families, shortlist length, gates) is applied deliberately, so it is not asked about,
but it is still printed as a deviation; a change made in words that lifts a hazard block or
moves a gate is held until the person confirms it. The **MCP server** makes the split literal. Claude in Desktop or
Cowork becomes the front edge, turning a scientist's words into tool calls; the guard, the core,
the self-check gate and the fixture banner run inside the tools and the server's instructions tell
the client to relay results as given. A client prompt cannot bypass any of it. The **Agent
page** of the app hosts that same client in-process: a model (Claude, or a local model over the
OpenAI-compatible protocol) drives the same tools through a tool-use loop, the report it
produces is shown as cards whose parts seed follow-up questions, and a number guard flags any
number in a reply that no tool printed. The transcript is append-only and every tool call is
validated against its schema before it runs, so the conversational front end adds no path by
which a number could be invented.

## 9. Deployment and privacy

Python 3.11, Pydantic schemas, SQLite, Typer CLI, a FastAPI + React web app (built bundle committed, so a pip install is the whole deployment), Jinja templates as files
so a site admin can edit prose without touching code. One YAML config with three named profiles;
the admin panel edits any of it into a site overrides file next to the cache, never the shipped
YAML, and every site departure from the shipped ranking policy is a deviation on the result.
`Dockerfile` plus compose with a persistent cache volume and an MCP service on loopback; once
warmed the containers run fully offline. Keys come from the environment; `.env.example` documents each one.

A second compose overlay runs **vLLM serving Qwen3-8B** beside the app. The model's job is small
enough (parse a sentence, phrase caveats over given facts) that an 8B local model loses nothing
against a frontier API, and a centre whose data-governance rules forbid sending request text to an
external provider keeps every capability. The README tabulates what leaves the site under each
provider; with `none` or the local overlay the answer is nothing.

## 10. Evaluation

Five checks, as pytest tests and as a report (`eval/run_eval.py`, `eval/evaluation.ipynb`). On
fixture data all pass: the PI's request yields a ranked shortlist with caveats and named gaps; one
request per bin behaves as specified; HfO₂ and Al₂O₃ lead the default run, ZrO₂ is third, and
Ta₂O₅ is excluded by the 4 eV gate *with the reason stated* and ranks seventh under the wide-net
profile; two runs are identical; no candidate lacking a dielectric value is scored as if it had one.
The known-answer check is presented as what it is: ground-truth validation before trusting the
system on unknowns. It has now caught two real bugs, which is the argument for having it. The
first was a scoring policy under which missing data could *raise* a candidate's rank. The second
was subtler. Replayed against the incomplete live recording the check failed — HfO₂ at 11, Al₂O₃
at 39 — and the failure was not about ranking at all: the workhorses had sunk because their
cross-check and literature lookups were never retrieved. A ground-truth test run on data that was
never fetched tests the cache, not the ranker, and calling that FAIL points the operator at the
wrong thing. The check now measures retrieval completeness first and reports `INCONCLUSIVE` with
the reason when the cache is too sparse to validate ranks (76.8% on that recording, against a 90%
floor), reserving FAIL for a ranker that genuinely got a known answer wrong.

## 11. Limitations and next steps

The first live warm replaced the fixture as evidence (§3), but its recording is partial: OQMD's
latency and OpenAlex's budget stopped it, and it completes on the rescoped, MP-only warm. Until then the system declines to present that
cache as a ranking — 78.9% retrieved against a 95% floor — rather than quietly serving an order
that partly reflects which downloads finished. Polymorphs are also not deduplicated, so one
compound can occupy several shortlist rows. Literature counts from formula-string search are
noisy for short formulae (flagged per candidate). The hazard table is a screen, not a
toxicological assessment. Nothing about films is modelled, by design. The alternative
acquisition routes are tested against fake responses; only the OpenAlex ones have run live. Next:
confirm the cation list with the PI, complete and commit the recorded live fixture, then run
against real data with the group, retune profiles with them, and add the JARVIS-DFT bulk
dataset as a second dielectric route.
