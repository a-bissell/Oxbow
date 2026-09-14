<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/brand/oxbow-lockup-dark.svg">
    <img alt="Oxbow: deterministic triage for oxide dielectrics" src="docs/brand/oxbow-lockup.svg" width="520">
  </picture>
</p>

<p align="center">
  <a href="https://github.com/a-bissell/oxide-triage/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/a-bissell/oxide-triage/actions/workflows/ci.yml/badge.svg"></a>
  <a href="https://github.com/a-bissell/oxide-triage/releases"><img alt="Latest release" src="https://img.shields.io/github/v/release/a-bissell/oxide-triage?include_prereleases&label=release"></a>
  <img alt="Python 3.11+" src="https://img.shields.io/badge/python-3.11%2B-3776ab">
  <img alt="MIT" src="https://img.shields.io/badge/license-MIT-lightgrey">
</p>

# Oxbow

**An agentic research assistant with a fully deterministic core**

Oxbow is a materials-triage system for a research center: it ranks known candidate materials from public data against explicit criteria and returns a shortlist where every number traces to a source and every gap is named. It ships instantiated for two material classes, oxide dielectrics for thin-film gate stacks (the worked example from the brief) and thermal barrier coatings, and a third class is a configuration profile away.

The ranking process is fully deterministic, and every number has a traceable provenance back to open-access, publicly available data. Each natural language request is parsed into a validated structure; filtering, scoring, and ranking are performed by code-based tools; a refutation pass argues against each shortlisted candidate; the result is rendered through editable templates. The same query against the same cache returns the same answer.

Oxbow was designed from the ground up with deep LLM integration in mind. The model acts as a research assistant; it interprets user requests with some flexibility and states caveats, it engages in followups conversationally and can help clarify or explain anything in the report. Nothing it says can change a number, a rank, or a citation (see docs/design-note.md for a deeper dive on this)

If needed, the whole system can be run offline with no language model at all. With no model attached, the rules-based heuristics engine kicks in and supports natural language based querying and simple followup workflows. 

The Oxbow web app is the premiere interface. It features a dynamic and interactive reporting window, auditing tools, an admin panel, and allows users to complete entire workflows in one place. Optionally, we include an MCP server so scientists can fit it inside their existing workflows in Claude Cowork, Cursor, Hermes Agent etc. 

For the old school Linux types: yes there is also a CLI


## Quick start

Offline with demo data:

```bash
pip install -e ".[app]"
oxbow load-fixtures          # synthetic demo data
oxbow serve --open           # the web app; or: oxbow query --offline
```

`oxbow` and `oxide-triage` are the same command. Real data needs a free Materials Project key
and `oxbow warm-cache`; see [Installing](#installing).

<!-- Optional: one screenshot of the web app here (docs/screenshots/...). -->

## What it does

<!-- The PI's request as a blockquote, then how the answer is produced: parsed into a validated
     structure; filtering, scoring and ranking as plain code over cached public data; a
     refutation pass arguing against each shortlisted candidate; rendered through editable
     templates. Same query + same cache = same answer. -->

<!-- The three ways to run it: CLI, web app (assistant beside a results canvas, admin panel),
     MCP server (Claude Desktop, Claude Cowork, Cursor). -->

## For scientists

### Writing a request

<!-- Plain English. The vocabulary table (top 10, band gap above 5 eV, lead-free, include lead
     compounds, prioritize dielectric constant, audit view ...). -->

### Reading the output

<!-- PI summary, audit view, JSON, HTML report. Then the things to know before trusting a
     number: DFT band gaps and the correction, sparse dielectric constants (unknown is not
     zero), stability is bulk thermodynamics, a shortlist entry is a conjecture. -->

### Following up

<!-- Focus a candidate, explain, compare, rerun with a change, what was excluded and why.
     Confirmation before a change that lifts a hazard block or moves a gate. -->

### From Claude Desktop, Cowork or Cursor (MCP)

<!-- The client config snippet and the tool list. -->

### What it will not do

<!-- Trigger anything in the lab; fabricate evidence; enter a mode where the constraints are
     lifted. One line each on what happens instead. -->

## Installing

### With Docker

```bash
cp .env.example .env                      # set MP_API_KEY (free)
docker compose -f docker/compose.yml up --build
docker compose -f docker/compose.yml run --rm app oxbow warm-cache
```

### Without Docker

```bash
pip install -e ".[web,llm]"
oxbow warm-cache
oxbow serve
```

### Airgapped (offline) install

Every tagged release ships the whole deployment for a off-network machines: self-checked data cache w/ manifest, the container image, and the package
with every dependency as wheels. The release workflow loads the image with the network disabled
and runs the install, the self-check and the PI's example request before anything is published.
Install steps are in [`docker/OFFLINE.md`](docker/OFFLINE.md).

<!-- A sentence on why this exists: the two ends of the deployment spectrum. -->

### Keys and environment

| Variable | Needed for |
|---|---|
| `MP_API_KEY` | warming the cache from Materials Project (free) |
| `OPENALEX_API_KEY` | optional; literature counts beyond the free daily budget |
| `LLM_PROVIDER` | `none` (default, rules-driven assistant) or `anthropic` |
| `ANTHROPIC_API_KEY` | only with `LLM_PROVIDER=anthropic` |
| `LLM_MODEL` | optional with `LLM_PROVIDER=anthropic`; the model for the parse and refute edges |
| `OXIDE_TRIAGE_CACHE` | path of the SQLite cache (default `data/cache.sqlite`) |
| `OXIDE_TRIAGE_OFFLINE` | `1` forces cache-only operation |
| `OXIDE_TRIAGE_ADMIN` | `1` enables editing and cache jobs in the admin panel |
| `OXIDE_TRIAGE_CONFIG_DIR` | directory holding `default.yaml` and `profiles/` outside a checkout |

A `.env` in the working directory or the repository root is loaded automatically.

## For admins

### The candidate universe and the cache

<!-- What is fetched at warm time (Materials Project) and per query (OQMD, PubChem, OpenAlex);
     the self-check before serving; retrieval completeness and why a ranking can refuse to be a
     ranking; adaptive acquisition. -->

### Configuration and profiles

`config/default.yaml` holds every knob; the profiles under `config/profiles/` are partial overrides on it, and the admin panel writes a site overrides file (`site.yaml` next to the cache) rather than the shipped YAML. Every site departure from the shipped policy is printed as a deviation on every result.

| Profile | Figure of merit | What it changes |
|---|---|---|
| `default` | dielectric constant (DFPT, Materials Project) | balanced oxide-dielectric triage on Si |
| `conservative` | dielectric constant | tighter gates, tier-1 hazards blocked, literature weighted up |
| `exploratory` | dielectric constant | wider hull and gap windows, missing data penalised lightly |
| `ferroelectric-research` | dielectric constant | Pb and Bi permitted and flagged, lower gap bar |
| `thermal-barrier` | minimum thermal conductivity (Clarke, from Materials Project elastic tensors; lower is better) | alumina substrate, no gap gate, its own cation universe and workhorses |

A profile for another class replaces the `figure_of_merit:` block (property, provider, label, units, curve, weight, missing-data handling, request vocabulary), the substrate, the cation allowlist and the self-check's workhorse list. See section 6 of the design note.

### What sites feed back to the platform team

Two Admin panels report on the two append-only logs written next to the cache. They are read-only: nothing in them changes a default, and a person who reads them edits a profile or the overlay.

- **Deviations log** (`deviations.jsonl`): every run that departed from shipped policy, and a summary by gate and by who decided. `request` means a scientist worked around the defaults in the request itself; if it repeats, retune the profile for that group. `site` means an admin already decided the shipped default is wrong here. `profile` and `cli` are expected.
- **Data gaps** (`retrieval.jsonl`): per criterion, how often the ranked candidates lacked the value, split by why. *Absent* means no permitted public source holds it, so the fix is a new source or a measurement. *Not retrieved* means this cache never fetched it, so the fix is ops: check Sources & limits, then warm the cache.

Both files grow without bound; rotate them with the site's usual tooling, since nothing reads them back into a run.

### Data sources

| Source | Used for | Access |
|---|---|---|
| Materials Project | candidate universe, hull energy, band gap and functional, DFPT dielectric, symmetry | free API key |
| OQMD | independent hull distance; agreement is evidence, disagreement is a caveat | public |
| OpenAlex | literature evidence: works matching the compound, and the thin-film subset | public |
| PubChem | compound-level GHS hazard statements where a record exists | public |
| shipped tables | element hazard tiers, hygroscopic oxides, cation families, compound aliases | in repo |

<!-- The "excluded on purpose" paragraph: ICSD, Scopus, Web of Science and why the result is
     auditable because of it. -->

## Design

Six of the seven scoring criteria are what any material class wants: thermodynamic stability, an insulating gap (which a profile may zero), stability against the substrate, a hazard screen, compositional simplicity and public literature evidence. The seventh is the application's figure of merit, declared by the profile: the dielectric constant for the oxide-dielectric profiles, Clarke's minimum thermal conductivity for the thermal-barrier one. The model lives only at the edges; the ranking is plain code over cached public data. The design note is [docs/design-note.md](docs/design-note.md); the generated documents (evaluation report, ranking decisions, sample report) live under [autodocs/](autodocs/).

## Development

```bash
pip install -e ".[all]"
ruff check . && ruff format --check .
pytest -q
cd ui && npm ci && npm run build      # only to change the front end; the bundle is committed and CI checks it is current
```

CI runs lint, the test suite, a wheel install into a clean environment, and a `--network none`
smoke of the container image on every push and pull request. Tags starting with `v` run the
release workflow.

### Repository layout

<!-- One line per top-level directory. -->

## Licence

MIT.
