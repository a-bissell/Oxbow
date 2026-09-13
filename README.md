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

Oxbow is designed to help researchers organize, triage, assess, and report on a massive variety of oxide dielectric candidates.

The ranking process is fully deterministic, and every number has a traceable provenance back to open-access, publicly available data. Each natural language request is parsed into a validated structure; filtering, scoring, and ranking are performed by code-based tools; a refutation pass argues against each shortlisted candidate; the result is rendered through editable templates. The same query against the same cache returns the same answer.

Oxbow was designed from the ground up with deep LLM integration in mind. The model acts as a research assistant; it interprets user requests with some flexibility and states caveats, it engages in followups conversationally and can help clarify or explain anything in the report. Nothing it says can change a number, a rank, or a citation (see docs/design-note.md for a deeper dive on this)

For airgapped installations, full support has been baked in for locally hosted language models such as Gemma4 and Qwen 3.8. Because Oxbow is built upon a robust suite of deterministic tools and operates in a well defined problem space, even small models that run on consumer laptops offer impressive performance.

If needed, the whole system can be run offline with no language model at all. With no model attached, the rules-based heuristics engine kicks in and supports natural language based querying and simple followup workflows. 

The Oxbow web app is the intended as the premiere interface. It features a dynamic and interactive reporting window, auditing tools, an admin panel, and allows users to complete entire workflows in one place. Optionally, we include an MCP server so scientists can fit it inside their existing workflows in Claude Cowork, Cursor, Hermes Agent etc. 

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
