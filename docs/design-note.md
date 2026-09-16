# Design note: Oxbow, a materials-triage system, instantiated for oxide dielectrics

*Deployable agentic triage for materials-science research*

## 1. Project Philosophy

I went into this project with a few goals in mind: I wanted to create something useful, something scientifically robust, and (most importantly) something that I would want to use myself. 

When I was in the field with USGS or Lincoln Labs, nothing would sour a trip quite like software that made the job harder. If I am 5 miles off the Atlantic coast on an 18ft Boston Whaler, during a light rain, in March, I do not want a CLI. If I'm in a remote site a couple hours outside of Vegas with spotty satellite internet, I don't want my workflow to stop when the network goes down.

When it comes to AI-enabled systems, the criterion is even simpler: I don't want something that will lie to me. 

All that, plus the brief, left me with one objective: a usable, deterministic materials-science triage system that is as ready for the hyper-connected university lab as it is for the airgapped research site buried under a mountain.

Oxbow is my answer to this. It takes a natural-language request, ranks known materials from cached public data against explicit criteria, and returns a shortlist in which every number traces to its origin, every data gap is named, and every entry carries the arguments against it. The system is designed from the ground up with LLM integration in mind, but can fall back to a pure rules based approach if needed.

This system does not propose new materials or make publishable claims. The oxide-dielectric instance is built out in full because only a worked instance can show that the citations are real and the caveats are the ones a scientist would raise. A second instance, thermal barrier coatings, shows that the first is not the whole system.

## 2. General Architecture
```mermaid
flowchart TB
    REQ([Scientist request]) --> G
    subgraph EDGE_IN["Front edge"]
        G["Guard<br/>rule-based bins"] --> P["Parser<br/>rules, optional LLM fill-in<br/>validated schema"]
    end
    subgraph SRC["Public sources, cached with a retrieval timestamp"]
        MP[("Materials Project")]
        OQ[("OQMD")]
        OA[("OpenAlex")]
        PC[("PubChem")]
        HZ[/"element hazard table<br/>versioned YAML"/]
    end
    subgraph CORE["Deterministic core: no model, no network, no clock"]
        D["Data layer, SQLite cache<br/>every value carries a status:<br/>KNOWN · ABSENT · NOT_RETRIEVED"]
        subgraph GATES["Hard gates: exclude before scoring, reason stated"]
            G1["E_hull ≤ max"] ~~~ G2["effective band gap ≥ min"] ~~~ G3["element count ≤ max"]
            G4["no blocked hazard-tier element"] ~~~ G5["required / excluded elements"] ~~~ G6["figure of merit known<br/>(only if the profile says<br/>on_missing: exclude)"]
        end
        subgraph CRIT["Seven weighted criteria, each normalised to 0…1"]
            C1["Stability<br/>1 − E_hull / zero-point<br/>OQMD agrees: bonus<br/>OQMD disagrees: penalty"] ~~~ C2["Effective band gap<br/>DFT correction labelled<br/>functional shown"] ~~~ C3["Interface with substrate<br/>most exothermic hull reaction<br/>products named<br/>inside tolerance counts as none"] ~~~ C4["Hazard tier<br/>worst element in the<br/>versioned table"]
            C5["Compositional simplicity<br/>lookup by element count"] ~~~ C6["Literature<br/>log-saturating<br/>thin-film weighted"] ~~~ C7["Figure of merit<br/>declared by the profile:<br/>property · provider · prefer high or low<br/>linear between two saturation points"]
        end
        subgraph AGG["Aggregate under the missing-data policy"]
            A1["raw = Σ w·s (known) / Σ w (known)"] ~~~ A2["coverage = Σ w (known) / Σ w (all)"]
            A3["no_credit (default)<br/>adjusted = raw × coverage<br/>− penalty × (1 − coverage)<br/>an unknown criterion earns nothing"] ~~~ A4["confidence high / medium / low<br/>any missing criterion caps it at medium<br/>any NOT_RETRIEVED forces low"]
        end
        R["Rank<br/>adjusted score descending<br/>ties broken by material id<br/>polymorphs collapse under their best phase<br/>scores within the tie band share a tier<br/>no ranking below the completeness floor"]
        D --> GATES --> CRIT --> AGG --> R
    end
    subgraph EDGE_OUT["Back edge"]
        F["Refutation<br/>rule caveats, optional LLM<br/>over delimited facts<br/>numeric guard"] --> T["Templates<br/>summary · audit · report"]
    end
    P -->|"criteria, weights, substrate"| D
    MP -.warm cache.-> D
    OQ -.-> D
    OA -.-> D
    PC -.-> D
    HZ --> D
    R --> F
    T --> OUT([Shortlist + caveats + gaps])
    style CORE fill:#eef6ee,stroke:#3a7d44
```

### A note on language models and scientific integrity

A model's characterisation of an otherwise deterministic result ("this looks promising", "this is a novel approach") propagates downstream with the same authority as the value it describes. I call this semantic smuggling. To prevent it, computation and narration never share a code path: the core in `scoring/` imports nothing from `edges/`, where the model lives, and the renderer receives a finished result and a template. The refutation stage may annotate structured facts but cannot touch a rank or a score.

The model is optional, though the full experience assumes one. With a model configured it may fill parser fields the rules left at default and add up to three observations per candidate, each citing an existing fact field and containing no number absent from the facts. Follow-up conversation and drilling into a result are where the model earns its place.

## 3. Data layer

Public sources only, each cached in SQLite with a retrieval timestamp.

- **Materials Project** supplies the candidate universe (oxygen plus one or two cations from a versioned allowlist, observed structures only), hull and band-gap data with the functional read from the producing task, the hull phases behind the interface criterion, and the property behind the figure of merit.
- **OQMD** gives an independent hull distance.
- **OpenAlex** gives a compound's works and their thin-film subset.
- **PubChem** gives GHS statements where a record exists.
- A versioned **element hazard table** in the repo scores toxicity because it is complete; PubChem is caveat evidence because it is not.

## 4. Domain handling

Three things about the data will mislead you if the system doesn't account for them up front.

**DFT band gaps are too low.** Every computational chemist knows this, but a triage tool that silently passes through raw DFT values will gate out materials that belong on the shortlist. The correction strategy lives in config: the functional is shown beside every value, any corrected value is labelled as such, and the gate applies to the effective (corrected) gap.

**Bulk stability is not thin-film processability.** Hull distance tells you whether a compound wants to exist. It says nothing about whether you can actually deposit it as a film, or whether it will match your substrate's lattice. The tool does not model deposition or lattice match, the language model is not allowed to speculate about them, and every output says so.

**There are two kinds of missing data, and confusing them corrupts the ranking.** Materials Project holds no dielectric tensor for most candidates. That is a fact about the source: the data does not exist. But a cross-check that failed because the API rate-limited us is a hole in *this particular cache*: the data exists, we just don't have it yet. Both lower a score, which is correct. But we learned the hard way what happens when you don't distinguish between them. Our first cache warm died partway through, and the top of the ranking was just the subset whose downloads had finished.

So every value carries one of three statuses: *known*, *absent*, or *not retrieved*. The scoring arithmetic treats absent and not-retrieved the same way (crediting an unfetched candidate would be worse), but the output treats them very differently. Each result reports its completeness. Below a completeness floor, no ranking is served at all. And critically, only one of the two is fixable by running the cache warm again, and the output says which. If a source holds a record but flags it as unusable, that is *absent* with the warning quoted, never a value.

## 5. Ranking

Seven criteria, each a weight and a normalised score in [0, 1]. Six are the same for any material class:

- **Thermodynamic stability** — hull distance, with a bonus when OQMD agrees with Materials Project and a penalty when it disagrees.
- **Effective band gap** — the corrected gap from section 4.
- **Interface stability** — against a substrate the profile sets and a request may name ("on germanium"). More on this below.
- **Hazard tier** — the worst element in the versioned table.
- **Compositional simplicity** — a lookup by element count.
- **Literature evidence** — thin-film-weighted and log-saturating, so a mountain of papers stops helping past a point.

The seventh is the application's **figure of merit**, declared by the profile rather than the engine: which property, from which provider, under what label, units and method, whether high or low is better between two saturation points, and what happens when it is missing. For the oxide-dielectric profiles it is the DFPT dielectric constant.

The interface criterion is the one the others cannot do without. Among good oxides the rest saturate — everything near the top is stable, wide-gap, and well studied. What actually separated HfO2 from the field is that it does not react with silicon. That is a thermodynamics question, and Hubbard and Schlom showed how to ask it in 1996: mix the candidate with the substrate, find the lowest-energy combination of stable phases at each composition (a small linear programme over Materials Project's thermo entries), and report the most exothermic reaction with its products named. Reactions inside a stated tolerance count as none, because hull energies carry about that much error. Hard gates exclude before scoring with a stated reason; every contribution appears in the audit view; ties break on material id; polymorphs collapse under their best-scoring phase.

The missing-data policy is battle tested, because my first version was flat wrong. It renormalised over the criteria that had data — and on the fixture, that put five candidates with **no** dielectric value at all above HfO2. An unknown criterion cannot pull a score down, so a candidate that would have scored badly on it is helped by the gap. The ground-truth check caught it (this is why we love ground truth; all hail ground truth). The shipped default is `no_credit`: an unknown criterion earns nothing, shows as unknown, and lowers confidence — so less data can never mean a higher rank. Scores closer than a tie band print as one tier, with the order inside declared arbitrary.

## 6. Retargeting to a new material class

The cleanest way to describe retargeting is by what changes and what is not allowed to.

**What changes.** Three files:

- **`config/profiles/<class>.yaml`** — the `figure_of_merit:` block, the substrate, the weights, the universe bounds, the words the parser and guard accept, the self-check's workhorses and request, and the refutation switches. A class may zero a class-specific weight — the band gap is an insulator-class criterion — but not a universal one.
- **`oxide_triage/data/cation_allowlist_<class>.yaml`** — the cation universe, in families, each with a written rationale.
- **`oxide_triage/sources/properties.py`** — a new provider, only if the property is not already served. A provider answers "property *P* for material *M*, with provenance and a retrieval timestamp"; the two shipped ones are Materials Project's dielectric and elasticity routes.

**What doesn't.** Nothing in `scoring/`, the refutation pass, the guard, the templates, or the front end names a material class. Two limits are fixed no matter the profile: the universe is oxides (the anion is a second seam a profile could own but does not yet — the reason fluoride UV windows were not the second class), and the arithmetic and the model boundary are never per-class.

**How it happens.** A forward-deployed engineer fills those files with the site's scientists: agree the figure of merit and its saturation points, name the workhorse materials the self-check must reproduce (it fails loudly if a known-good material vanishes or sinks, and states the honest reasons when it does), choose the substrate, prune the cation universe, and switch on the refutation rules the class needs.

## 7. Refutation pass

A shortlist entry is treated as a **conjecture**: held provisionally, and stated so that it can be falsified. So after ranking, a dedicated stage does nothing but argue against each shortlisted candidate.

Its standing caveats cover hygroscopicity, missing figure-of-merit data, contested stability, metastability, corrected or near-threshold band gaps, hazard tiers, thin literature, and incomplete retrieval. Two more switch on per profile where a class needs them: a competing polymorph within a few meV/atom, and a 4f-element band gap that DFT cannot be trusted with.

An optional model pass can elaborate on these caveats, but only over the same structured facts, delimited as data, under the guards of section 2. It can add words; it cannot add claims.

## 8. Evaluation

Start with the answer. On live data the oxide-dielectric default ranks LaAlO3, HfO2, SrHfO3, LaScO3, ZrO2 as its top five. HfO2 is 2nd of 317 candidates and ZrO2 5th; Al2O3 is 9th, and Ta2O5 falls to 99th, with its caveats explaining why: it reacts with silicon.

The thermal-barrier profile runs the same engine on the same cache with no code change and gives a visibly different, defensible answer. Its figure of merit is Clarke's minimum thermal conductivity from Materials Project's elastic tensors, lower preferred; its substrate is the alumina scale on the bond coat. Of 750 candidates 394 pass the gates. The rare-earth sesquioxides lead — Gd2O3, Er2O3, La2O3 and Lu2O3 at 0.8–0.9 W/m·K against zirconia's 1.13 — with HfO2 12th and ZrO2 15th, and the caveats argue with the leaders as designed: hygroscopicity, reaction with the alumina scale (Y2O3 forms the garnet), a competing polymorph. One workhorse cannot be verified from public data, and the self-check says so by name: Materials Project holds no elastic tensor for La2Zr2O7, which ranks 131st on its other criteria; Gd2Zr2O7 has no experimentally observed entry and is outside the universe by construction.

Behind those results sits a harness of eight checks, run as pytest tests and as a report, passing on both the fixture and the live cache:

1. The PI's original request.
2. Adversarial requests in every guard bin, with legitimate phrasings confirmed to run plain.
3. Every shipped profile's known answers: workhorses surface, or are excluded by a gate that states its reason.
4. Determinism — the same request twice gives the same answer.
5. Missing data is never scored as if it were a value.
6. A sensitivity sweep of every weight and tuned parameter.
7. The interface criterion against Hubbard and Schlom's held-out classification: 21 of 21 hard assertions.
8. Held-out phrasings, written after the rules were frozen.

The known-answer check has caught two real bugs, and reports `INCONCLUSIVE` rather than `FAIL` on a sparse cache — a missing answer is not the same as a wrong one.

Finally, the profile boundary itself is proven twice: a golden snapshot of every number, status and caveat the four oxide-dielectric profiles produce on the fixture, and a before-and-after diff of the live-cache evaluation across the refactor — identical ranks, sensitivity table and cache fingerprint, with only labels and the config hash moved.

## 9. Limitations

Stated plainly:

- **Polymorphs.** Which polymorph a film actually adopts is not modelled — only the collapsed listing in the ranking and, for thermal barriers, a caveat.
- **Literature counts.** Formula-string search is noisy for short formulae, and noisy counts are flagged.
- **Hazards.** The hazard table is a screen, not a toxicological assessment.
- **Films.** Nothing about thin films is modelled beyond the bulk thermodynamics of the interface.
- **Thermal barriers.** That profile ranks on conductivity, stability and compatibility only. Melting point, thermal expansion, sintering, CMAS attack and toughness have no public per-material source, and every output names them as unmodelled.

And the universe is oxides. Another anion would be the natural next step, but it is out of scope for this project.
