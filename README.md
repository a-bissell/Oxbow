<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/brand/oxbow-lockup-dark.svg">
    <img alt="Oxbow: deterministic triage for oxide dielectrics" src="docs/brand/oxbow-lockup.svg" width="520">
  </picture>
</p>

<p align="center">
  <a href="https://github.com/a-bissell/oxide-triage/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/a-bissell/oxide-triage/actions/workflows/ci.yml/badge.svg"></a>
  <a href="https://github.com/a-bissell/oxide-triage/actions/workflows/release.yml"><img alt="Release" src="https://github.com/a-bissell/oxide-triage/actions/workflows/release.yml/badge.svg"></a>
  <a href="https://github.com/a-bissell/oxide-triage/releases"><img alt="Latest release" src="https://img.shields.io/github/v/release/a-bissell/oxide-triage?include_prereleases&label=release"></a>
  <img alt="Python 3.11+" src="https://img.shields.io/badge/python-3.11%2B-3776ab">
  <img alt="MIT" src="https://img.shields.io/badge/license-MIT-lightgrey">
</p>

# Oxbow

**An agentic research assistant with a fully deterministic core**

<!-- One paragraph: who this is for, what it answers, and the one-sentence
     guarantee (nothing the model says can change a number, a rank or a citation). -->


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
| `LLM_PROVIDER` | `none` (default, rules-driven assistant), `anthropic`, or `openai_compatible` |
| `ANTHROPIC_API_KEY` | only with `LLM_PROVIDER=anthropic` |
| `LLM_BASE_URL`, `LLM_MODEL` | only with `LLM_PROVIDER=openai_compatible` |
| `OXIDE_TRIAGE_CACHE` | path of the SQLite cache (default `data/cache.sqlite`) |
| `OXIDE_TRIAGE_OFFLINE` | `1` forces cache-only operation |
| `OXIDE_TRIAGE_ADMIN` | `1` enables editing and cache jobs in the admin panel |
| `OXIDE_TRIAGE_CONFIG_DIR` | directory holding `default.yaml` and `profiles/` outside a checkout |

A `.env` in the working directory or the repository root is loaded automatically.

### A locally hosted model

<!-- The local-model story: what the model is and is not used for, and why an 8B-12B local
     model loses nothing. Point at docker/compose.local-llm.yml (and the Gemma 4 plan in TODO). -->

## For admins

### The candidate universe and the cache

<!-- What is fetched at warm time (Materials Project) and per query (OQMD, PubChem, OpenAlex);
     the self-check before serving; retrieval completeness and why a ranking can refuse to be a
     ranking; adaptive acquisition. -->

### Configuration and profiles

<!-- default.yaml, the named profiles, the site overrides file the admin panel writes, and the
     rule that every site departure from shipped policy is printed as a deviation. -->

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

<!-- Two or three sentences, then a link to docs/design-note.md. The generated documents from
     the build (evaluation report, ranking decisions, sample report) live under autodocs/. -->

## Development

```bash
pip install -e ".[all]"
ruff check . && ruff format --check .
pytest -q
cd ui && npm ci && npm run build      # only to change the front end; the bundle is committed
```

CI runs lint, the test suite, a wheel install into a clean environment, and a `--network none`
smoke of the container image on every push and pull request. Tags starting with `v` run the
release workflow.

### Repository layout

<!-- One line per top-level directory. -->

## Licence

MIT.
