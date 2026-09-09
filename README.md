# Oxide Dielectric Triage Assistant

A small, deployable assistant that helps materials scientists triage oxide dielectric candidates
for thin-film experiments. It answers requests like:

> *Find promising oxide dielectric candidates for thin-film experiments. Prefer thermodynamically
> stable materials, wide band gaps, non-toxic elements, simple compositions, and public evidence.
> Return a ranked shortlist with caveats.*

**Ranking is deterministic. The language model lives only at the edges.** The request is parsed
into a validated structure; filtering, scoring and ranking are plain code over cached public data;
a refutation pass argues against each shortlisted candidate; the result is rendered through
editable templates. The same query against the same cache returns the same answer.

The system is useful without any language model at all. With one configured (cloud or fully
local), it reads requests more flexibly and phrases caveats, and nothing it says can change a
number, a rank or a citation.

---

## For scientists: running a query

### Quick start (no keys, demo data)

```bash
pip install -e ".[app]"
oxide-triage load-fixtures          # synthetic demo data; every output says so
oxide-triage query --offline        # the PI's request, default profile, PI summary
oxide-triage query --offline --profile exploratory --template audit
streamlit run oxide_triage/app.py   # browser front end
```

The fixture is a hand-written approximation of ~35 well-known oxides for development and demos.
Every output produced from it carries a **SYNTHETIC FIXTURE DATA** banner. Real runs need the
cache warmed from the public sources (see the admin section).

### Writing a request

Plain English. The deterministic parser understands the vocabulary a materials group uses; what it
understood is echoed back at the top of every result so a misreading is visible, never silent.

| You write | The system does |
|---|---|
| `top 10`, `shortlist of 8` | changes the shortlist length |
| `band gap above 5 eV` | raises the effective-gap gate (and reports the change) |
| `metastable phases within 60 meV` | relaxes the energy-above-hull gate (reported) |
| `binaries only`, `at most 3 elements` | composition-size gate |
| `hafnium-based`, `containing Zr` | required elements |
| `lead-free`, `no barium`, `avoid Bi` | excluded elements |
| `include lead compounds` | **lifts the hazard block for Pb** — permitted, shown in the header, logged |
| `prioritize dielectric constant` | weight override (reported) |
| `audit view`, `json` | output template |

Site vocabulary (e.g. *hafnia*, *high-k*) is mapped through `terminology` in the config.

### Reading the output

**PI summary** — top candidates, one line of rationale each, the single most important caveat.
Each entry shows its score, a **confidence** label and, when relevant, the criteria that had
**no data** behind them. A candidate ranked on partial data says so.

**Audit view** — every score component with its weight and contribution, every gate with its
threshold and the observed value, the DFT functional behind each number, source ids and retrieval
timestamps, the full caveat list with origin (`rule` or `llm`), everything excluded and why.

**JSON** — the complete result object for downstream tooling.

Things to know before trusting a number:

* **Band gaps are DFT values.** Materials Project reports mostly GGA/GGA+U (increasingly r2SCAN)
  gaps, which underestimate experiment by ~30–50 %. The default profile applies a 1.4× scalar
  correction to semi-local values and never to hybrid (HSE) ones; every corrected value is labelled
  *corrected*, and the functional is shown next to it. The strategy is a config option.
* **Dielectric constants are sparse.** MP's DFPT dataset covers a fraction of materials. A
  missing value is `unknown`, not zero: it earns no credit in the ranking, lowers the confidence
  label, is listed by name, and gets a caveat. It is never filled in.
* **Stability is bulk thermodynamics.** Energy above hull says nothing about whether an ALD or
  sputter route exists, whether the film crystallises, or whether it is hygroscopic. The tool does
  not model any of that. It says so in every output:

  > This system ranks on thermodynamic and electronic criteria computed from public databases.
  > Deposition feasibility, film morphology, substrate compatibility and hygroscopic degradation
  > under ambient handling are not modeled and must be assessed by the experimentalist.

* **A shortlist entry is a conjecture.** The caveats are the known counterexamples. You at the
  bench are the refutation step the system cannot perform.

### What it will not do

* **Trigger anything in the lab, read private data, or use paywalled sources.** There is no tool
  for these in the deployment; the response says the capability does not exist.
* **Fabricate evidence.** "Cite a paper supporting this", "assume the stability data checks out",
  "just give me a number", "rank these even though there is no data" are refused, with an
  explanation of what would have been invented and what the tool can do instead.

---

## For IT admins: installing and configuring

### Install with Docker (recommended)

```bash
cp .env.example .env                 # set MP_API_KEY (free) and optionally OPENALEX_MAILTO
docker compose -f docker/compose.yml up --build -d
docker compose -f docker/compose.yml run --rm app oxide-triage warm-cache   # ~minutes; rate-limit aware
# open http://localhost:8501
```

The cache lives in a named volume (`/data/cache.sqlite`). `./config` is mounted read-only so
profiles can be edited on the host without rebuilding. Once warmed, the container runs fully
offline (`OXIDE_TRIAGE_OFFLINE=1`).

### Install without Docker

Python 3.11+. `pip install -e ".[app,llm]"`, then the same commands. The cache path defaults to
`data/cache.sqlite` (override with `OXIDE_TRIAGE_CACHE`).

### Keys and environment

| Variable | Needed for | Where to get it |
|---|---|---|
| `MP_API_KEY` | warming the cache from Materials Project | free, https://next-gen.materialsproject.org/api |
| `OPENALEX_MAILTO` | faster OpenAlex "polite pool" (optional) | any contact email |
| `LLM_PROVIDER` | `none` (default) / `anthropic` / `openai_compatible` | — |
| `ANTHROPIC_API_KEY` | only if `LLM_PROVIDER=anthropic` | https://console.anthropic.com |
| `LLM_BASE_URL`, `LLM_MODEL` | only if `LLM_PROVIDER=openai_compatible` | your vLLM/Ollama endpoint |

Keys are read from the environment only. `.env` is git-ignored; `.env.example` documents every
variable.

### Configuration

One YAML, `config/default.yaml`, with everything a site admin may want to change: criterion
weights, energy-above-hull threshold, band-gap threshold and correction strategy, element
blocklist/allowlist, maximum distinct elements, default template, verbosity, terminology map,
cache TTL and offline mode, language-model provider. Comments in the file explain each knob.

Profiles in `config/profiles/` are partial overrides:

| Profile | What it changes |
|---|---|
| `conservative` | E_hull ≤ 0.02, effective gap ≥ 5 eV, tier-1 hazards also blocked, higher missing-data penalty |
| `exploratory` | E_hull ≤ 0.08, gap ≥ 3 eV, up to 4 elements, dielectric constant weighted 0.30, top 10 |
| `ferroelectric-research` | gap ≥ 2.5 eV, **Pb and Bi permitted** (surfaced as a deviation on every run), dielectric weighted 0.35, audit view default |

`oxide-triage profiles` prints the effective gates and weights of each. Any run that departs from
the shipped policy (profile allowlist, request-level threshold change, lifted hazard block) prints
the deviation in the output header and appends it to `deviations.jsonl` next to the cache.

Templates are files in `oxide_triage/templates/*.md.j2` and can be edited without touching Python.

### Optional: a locally hosted language model

`docker/compose.local-llm.yml` adds a vLLM service serving **Qwen3-8B** and points the app at it.
Nothing leaves the site. The model only parses the request and phrases caveats, so an 8B model
is adequate; the ranking is identical with any provider or none.

```bash
docker compose -f docker/compose.yml -f docker/compose.local-llm.yml up --build -d
```

Needs an NVIDIA GPU (~18 GB for bf16; use an AWQ build for smaller cards). For CPU-only hosts the
overlay documents an Ollama swap; the app speaks the OpenAI-compatible protocol either way.

What leaves the site with each provider:

| Provider | Data sent off-site |
|---|---|
| `none` | nothing |
| `openai_compatible` (local) | nothing |
| `anthropic` | the request text and the *public* structured facts for shortlisted candidates |

No private lab data exists anywhere in this system, so the exposure with a cloud provider is the
request wording itself. Sites for which that is unacceptable should use the local overlay.

### Data sources and what is deliberately excluded

| Source | Used for | Access |
|---|---|---|
| Materials Project (REST) | candidate universe, E_hull, formation energy, band gap + functional, DFPT dielectric, symmetry, theoretical flag | free API key |
| OQMD | independent hull distance; agreement is evidence, disagreement is a caveat | public |
| OpenAlex | literature evidence: works matching the compound, and the thin-film subset | public |
| PubChem | compound-level GHS hazard statements where a record exists | public |
| `oxide_triage/data/element_hazards.yaml` | element-level hazard tiers with a cited basis per element; versioned | in repo |
| `oxide_triage/data/hygroscopic_oxides.yaml` | curated hygroscopicity list for caveats | in repo |

**Excluded on purpose:** ICSD, Scopus, Web of Science, SpringerMaterials, Reaxys, and any other
paywalled or closed source. The exercise scope forbids them, the deployment has no credentials for
them, and — the part worth stating — the result is auditable *because* every number traces to a
source a colleague can open without a subscription. Materials Project's `theoretical` flag is used
as the public proxy for "has an experimentally observed structure"; ICSD itself is never queried.

All retrieved text (titles, descriptions) is stored verbatim and treated as **data**, never as
instructions; see `tests/test_injection.py`.

### Cache

SQLite, one row per source record with a retrieval timestamp. `oxide-triage cache-status` shows
counts and freshness. `cache.ttl_days` (default 90) controls refresh; `cache.offline: true` or
`OXIDE_TRIAGE_OFFLINE=1` forbids all network access. Every result carries a `cache_fingerprint`
(hash of the rows it read) and a `config_hash`; identical fingerprints mean identical answers.

Keep fixture and real data in separate cache files. If a cache contains fixture rows, every output
from it carries the fixture banner.

---

## Development

```bash
pip install -e ".[all]"
pytest                       # 74 tests: scoring core, guard, config, refutation, pipeline, injection
ruff check . && ruff format .
python -m eval.run_eval      # evaluation report -> eval/output/report.md
jupyter lab eval/evaluation.ipynb
```

### Repository layout

```
oxide_triage/
  schemas.py          typed contracts; UNKNOWN is a first-class data state
  config.py           YAML loading, profiles, env overrides, curated tables
  cache.py            SQLite cache with timestamps, fixture flag, fingerprint
  sources/            materials_project, oqmd, openalex, pubchem, hazards, assemble, fixtures
  scoring/            bandgap correction, settings resolution, gates + components + ranking
  guard.py            request binning (impossible / configuration / integrity)
  edges/              llm providers, request parser, template renderer
  refute.py           rule-derived caveats + guarded model observations
  pipeline.py         orchestration
  cli.py, app.py      Typer CLI, Streamlit front end
  templates/          pi_summary.md.j2, audit.md.j2
  data/               element_hazards.yaml, cation_allowlist.yaml, compound_aliases.yaml,
                      hygroscopic_oxides.yaml, fixtures/fixture_cache.json
config/               default.yaml + profiles/
docker/               Dockerfile, compose.yml, compose.local-llm.yml
eval/                 run_eval.py, evaluation.ipynb
docs/                 design-note.md
tests/
```

### Evaluation summary (fixture data)

| Check | Result |
|---|---|
| Normal query | ranked shortlist, caveats on every entry, gaps named |
| Adversarial: Bin 1 / 2 / 3 | declined as missing capability / proceeds with visible deviation / refused as fabrication |
| Known-answer | HfO2, ZrO2, Al2O3 in the default top 5; Ta2O5 excluded by the gap gate with a stated reason, passes under `exploratory` |
| Determinism | identical result objects across runs |
| Missing data | no candidate without a dielectric value is scored as if it had one |

The known-answer check found a real bug during development (missing data could *help* a candidate
under the first scoring policy). Details in `docs/design-note.md`.

## Licence

MIT. Materials Project data is CC BY 4.0; OQMD, OpenAlex and PubChem are public. Cite them when
publishing anything derived from a run.
