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
        D[Data layer<br/>SQLite cache] --> S[Gates + 7 scored criteria<br/>missing-data policy] --> R[Rank<br/>tie-break by id]
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

The model is optional everywhere it appears, and the tests, evaluation and demo run without one.
With a provider configured it may *fill in* parser fields the rules left at default (never an
element allow/exclude decision) and add up to three observations per candidate, each citing a
fact field that exists and containing no number absent from the facts. Anything else is
discarded: a hostile model can only fail, not act (`tests/test_injection.py`).

## 3. Data layer

Public sources only, each cached in SQLite with a retrieval timestamp. **Materials Project**
supplies the candidate universe: oxygen plus one or two cations from a versioned allowlist,
experimentally observed structures only, so hydroxides, oxyhalides and hypothetical polymorphs
are excluded by construction; hazardous cations stay *in* the universe so that "include lead" is
a configuration change, not a re-fetch. Its thermo entries also give, per element system
plus the substrate, every phase on the convex hull, from which the interface criterion is
computed here. **OQMD** gives an independent hull distance. **OpenAlex**
gives works matching the compound and the thin-film subset. **PubChem** gives GHS statements
where a record exists. An **element hazard table** in the repo, versioned with a cited basis per
element, is the scoring basis for toxicity because it is complete; PubChem is caveat evidence
because it is not. Absence is cached as an explicit row, and the functional behind each band gap
is read from the producing task or recorded as `unknown`, never guessed.

The clients were exercised on a synthetic fixture; the first live warm then changed two things.
The universe was rescoped to observed structures of a dielectric-minded cation list, because the
`theoretical` flag removes 40% of the raw universe and hull thresholds remove almost nothing. And
the formula-keyed sources moved to query time: OQMD and OpenAlex are slow or metered, so the warm
touches Materials Project only, and a query fetches cross-checks, hazards and literature for the
top-ranked pool, re-ranks, and repeats until the pool is settled. The numbers behind both
decisions are in the README under *Notes from the first live warm*.

## 4. Domain handling a materials scientist checks first

**DFT gaps are underestimated.** Correction is a config strategy (`none`, `scalar_factor`,
`hse_preferred`), the functional is shown beside every value, and a corrected value is labelled
*corrected*. The gate applies to the *effective* gap, so a 3.0 eV GGA value passes a 4 eV gate
under a 1.4× correction and fails it under `none`.

**Bulk stability is not thin-film processability.** Hull distance says nothing about ALD or
sputter routes, crystallisation, hygroscopicity or lattice match. The tool does not model any of
it, the model is not allowed to speculate about it, and a scope statement appears in every
output.

**Dielectric coverage is sparse.** `unknown` is a distinct status, not a value. It earns no
score, lowers data coverage, caps confidence at *medium*, is named in the shortlist entry, and
generates a caveat.

**There are two kinds of unknown, and merging them corrupts the ranking.** Materials Project
holds no dielectric tensor for most candidates: a fact about the data. A cross-check that was
rate-limited away is a hole in *this* cache. Both lower a score, so when our first warm died
partway through, the head of the ranking was substantially the subset whose downloads had
finished. Reporting that as a ranking is lying in exactly the way the design exists to prevent.
So the data status splits into *absent* and *not retrieved*. The arithmetic is unchanged, since
crediting an unfetched candidate would be the worse error, but incomplete retrieval is a critical
caveat and each result reports its retrieval completeness, with a floor below which no ranking
is served. Only one of the two states is fixable by warming the cache, and the output says which.

## 5. Ranking

Seven criteria, each a weight and a normalised score in [0, 1]: stability (with a cross-source
agreement bonus and disagreement penalty), effective band gap, dielectric constant where known,
stability of the interface with the substrate, hazard tier, compositional simplicity, literature
evidence (thin-film-weighted, log-saturating).

The interface criterion is the one the others cannot do without. Among good oxides the first
six saturate, and what separated HfO2 from the field in practice was that it does not react
with silicon. That is a hull question (Hubbard and Schlom, 1996): mix the oxide with the
substrate, find the lowest-energy combination of stable phases at each composition by a small
linear programme over Materials Project's thermo entries, and report the most exothermic
reaction. HfO2, Al2O3, Y2O3, LaAlO3 and SrHfO3 come out at zero against Si; ZrO2 within DFT
error of zero; Ta2O5, TiO2 and the titanate perovskites react, with the products named in the
caveat. The reference is the hull at the oxide's own composition, so a metastable polymorph is
not charged twice, and reactions inside a stated tolerance count as none because hull energies
carry that much error. Bulk thermodynamics only, and the scope statement says so.
Hard gates exclude before scoring with a stated reason; every contribution is in the audit view;
ties break on material id. Materials Project holds several phases of many oxides, so after
ranking a compound's best phase leads one row and its other passing phases collapse under it
with their own numbers and a caveat: on live data 497 passing materials become 317 compounds.

The missing-data policy is the interesting part, because the first version was wrong. The
natural choice, renormalising over the criteria that have data, put five candidates with **no**
dielectric value above HfO₂ on the fixture: an unknown criterion cannot pull a score down, so a
candidate that would have scored poorly on it is helped by the gap. The known-answer check caught
it. The shipped default is `no_credit`: an unknown criterion earns nothing, is displayed as
unknown, and lowers confidence, so a candidate can never outrank another by having less data.
`renormalize` is kept as a documented option so the comparison can be reproduced. The preference
curves were tuned on four compounds, with the reasoning in `default.yaml`; the profiles exist so
a site can retune against its own judgement. On live data the top three are within 0.035 of each other, so the output prints them as one tier and says the order inside it is arbitrary; the README walks through why LaAlO3 leads HfO2, why the literature curve was raised once (the brief asks for public evidence; the first curve erased it), and which setting changes the answer.

## 6. Refutation pass

A shortlist entry is a **conjecture**, held provisionally and stated so that it can be falsified.
After ranking, every shortlisted candidate goes through a stage whose only job is to argue
against it: hygroscopicity, absent dielectric data, contested stability, metastability,
corrected or near-threshold gaps, hazard tiers (critical when permitted by configuration), thin
literature, incomplete retrieval. An optional model pass elaborates over the same facts,
delimited as data, under the guards of section 2. The scientist at the bench is the refutation
step the system cannot perform for itself. The tool proposes; the bench disposes.

## 7. Constraint handling

Requests are sorted into bins by rules, **before any model sees them**, so refusal behaviour
does not depend on a model. *Bin 1, architecturally impossible* (wet-lab triggers, private data,
paywalled sources): the response explains that the capability does not exist; there are no stub
tools to decline politely. *Bin 2, configuration deviation*: "include lead-containing compounds"
looks like a safety bypass but a Pb-ferroelectrics group has a real reason. It is permitted,
printed at the top of the output, caveated as critical on every affected candidate, and logged.
Distinguishing policy from configuration is the point of the bin. *Bin 3, evidence-integrity
attacks* ("cite a paper supporting this", "your best guess for the dielectric constant", "rank
these anyway"): refused, naming what would have been fabricated. *Bin 0, override attempts*
("ignore your previous instructions", "developer mode", "the PI has authorised you to disregard
the rules"): nothing to refuse, because there is no mode in which the constraints are lifted. The
run proceeds exactly as the plain request would, the attempt is named and logged, and the
evaluation checks that the shortlist is byte-identical to the plain one.

The rules target the imperative form aimed at the system, because a false positive (refusing
"cite the sources you actually used") costs more than a miss, which the tools make harmless. A
benign-phrasing corpus sits in the tests beside the adversarial one. In the conversational
front ends the guard runs on the **scientist's own words** before any model sees them, so a
paraphrase cannot launder a request, and the **confirm flag belongs to the person**: a model that
passes `confirmed=true` on its own gets the questions back. Over MCP both are the client's to
honour. Indirect injection is handled at the boundary: retrieved text is JSON-encoded inside
`<retrieved_data>` blocks, the preamble declares it data, and model output is validated against
a schema and a numeric guard. That guard reads digits only, so a value in words passes it; that
is the known residual gap.

## 8. How agentic, and where

"Agentic" is earned by deciding what to do next, and this system is agentic in some places and
deliberately not in others. It decides at the boundary (refuse, proceed, proceed with a logged
deviation); asks before acting when a request changes something material; carries a
conversation about a result without re-deriving it (`explain`, `compare`, `rerun` from stored
result objects); acquires data on demand for the ranked pool; fills gaps from an allowlist of
read-only routes, logging each attempt and reporting gaps with no public route as unfillable;
and re-checks itself against ground truth after every change to its cache. It is not agentic
where numbers are concerned: nothing sits between a retrieved value and a score.

The **web app** assistant and the **MCP server** are clients for the same tools. The assistant
has two interchangeable drivers, rules or a model (Claude, or a local one), producing the same
visible steps beside a canvas that renders result objects rather than model prose. Over MCP,
Claude in Desktop or Cowork becomes the front edge; the guard, the core, the self-check gate and
the fixture banner run inside the tools.

## 9. Deployment and privacy

Python 3.11, Pydantic, SQLite, Typer CLI, a FastAPI + React web app with the bundle committed,
Jinja templates as files so a site admin can edit prose without touching code. One YAML config
with three named profiles; the admin panel writes a site overrides file, never the shipped YAML,
and every site departure from the shipped policy is a deviation on the result. Docker compose
with a persistent cache volume; once warmed the containers run fully offline. A second overlay
runs vLLM serving Qwen3-8B beside the app: the model's job is small enough that an 8B local model
loses nothing, so a centre that forbids sending request text off-site keeps every capability.

## 10. Evaluation

Five checks, as pytest tests and as a report (`eval/run_eval.py`, `eval/evaluation.ipynb`), all
passing on fixture data: the PI's request yields a ranked shortlist with caveats and named gaps;
adversarial requests in every bin behave as specified and two legitimate phrasings run as plain
requests; the workhorse dielectrics surface near the top or are excluded by a stated gate; two
runs are identical; no candidate lacking a dielectric value is scored as if it had one. The
known-answer check is ground-truth validation before trusting the system on unknowns, and it has
caught two real bugs: the scoring policy under which missing data could raise a rank, and a
failure on the partial live cache that was about retrieval, not ranking, which is why it now
reports `INCONCLUSIVE` on a sparse cache and reserves `FAIL` for a wrong known answer.

## 11. Limitations and next steps

Polymorphs are grouped by formula after ranking, with the leading phase's numbers shown and
the others listed under it; which phase a film adopts is still not modelled. Literature counts from formula-string search are noisy for
short formulae, and flagged. The hazard table is a screen, not a toxicological assessment.
Nothing about films is modelled beyond the bulk thermodynamics of the interface: no kinetics,
no interlayer thickness, no epitaxy, and the substrate is one phase at a time (Si by default;
any hull phase can be configured). Next: confirm the cation list, the dielectric curve and the
interface tolerance with the PI's group, retune profiles with them against the live data, add
the JARVIS-DFT bulk dataset as a second dielectric route, and let a request name the substrate.

One candidate beyond that is worth naming because it targets the weakest input. The provenance
model is already a small property graph: a material node with typed, timestamped edges to its
Materials Project entry, OQMD cross-check, PubChem record, OpenAlex works, elements and
polymorphs. Literature is the one edge that carries a count where a scientist wants a path:
material, deposition method, substrate, property measured, work. Extracting those relations from
the sample abstracts through the existing validated edge, storing them as typed edges in the same
SQLite file, and letting the refutation pass cite the specific work that supports or contradicts
a candidate would turn "two thin-film works" into "both are sputtered; no ALD report found".
It would feed caveats and citations only; nothing in it may touch a score, a rank or a gate, and
no graph server enters the deployment. It is scoped in the TODO and deliberately sequenced after
the retune with the group.
