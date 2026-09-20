<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/brand/oxbow-lockup-dark.svg">
    <img alt="Oxbow: deterministic materials triage" src="docs/brand/oxbow-lockup.svg" width="520">
  </picture>
</p>

<p align="center">
  <a href="https://github.com/a-bissell/Oxbow/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/a-bissell/Oxbow/actions/workflows/ci.yml/badge.svg"></a>
  <a href="https://github.com/a-bissell/Oxbow/releases"><img alt="Latest release" src="https://img.shields.io/github/v/release/a-bissell/Oxbow?include_prereleases&label=release"></a>
  <img alt="Python 3.11+" src="https://img.shields.io/badge/python-3.11%2B-3776ab">
</p>

# Oxbow
Oxbow ranks candidate materials against explicit criteria for a stated property (dielectric constant, thermal conductivity, or whatever the active profile scores for) drawing on public data from Materials Project, OQMD, OpenAlex and PubChem. Every number in the shortlist traces to a source and every data gap is named. The ranking is deterministic code over a local cache, so the same request against the same cache gives the same answer. The Claude-backed assistant interprets the request and answers follow-ups. Its prose is labelled interpretation, not verified evidence; scientific values, ranks and provenance are rendered separately from structured records.

Oxbow runs as a web app, a command line tool, or an MCP server for Claude Desktop, Cowork, Cursor etc.

The design and the reasoning behind it are in [design-note.md](design-note.md).

## Install

Needs Python 3.11 or newer

```bash
git clone https://github.com/a-bissell/Oxbow.git
cd Oxbow
pip install -e ".[app,llm]"
cp .env.example .env
```

Two keys go in `.env`:

- `ANTHROPIC_API_KEY`, with `LLM_PROVIDER=anthropic`, for the assistant. This is the intended experience: the model reads the request, explains the shortlist and handles follow-ups.
- `MP_API_KEY`, a free Materials Project key, for real data.

Warm the cache once (a few minutes), then start the app:

```bash
oxbow warm-cache
oxbow serve --open
```

`oxbow` and `oxide-triage` are the same command.

### Other ways to run

- **Demo Mode.** `oxbow load-fixtures` installs synthetic demo data instead of warming the cache. With no Anthropic key (or `LLM_PROVIDER=none`) the assistant is driven by rules: it understands the request vocabulary below and simple follow-ups, but does not converse. The ranking is the same either way.
- **Another model.** `LLM_PROVIDER=openai_compatible` with `OPENAI_API_KEY` uses OpenAI instead of Claude. Add `LLM_BASE_URL` (say `http://localhost:11434/v1`) and `LLM_MODEL` to point it at a vLLM, Ollama or llama.cpp server instead; then nothing leaves the machine. The model only interprets requests and phrases answers, so a small local model does the job.
- **Docker.** `cp .env.example .env`, then `docker compose -f docker/compose.yml up --build` serves on port 8000. Warm the cache with `docker compose -f docker/compose.yml run --rm app oxbow warm-cache`.
- **No network.** Each tagged release ships a container image, all wheels, and a pre-warmed, checksummed data cache. Steps are in [docker/OFFLINE.md](docker/OFFLINE.md).

## Run

**Web app.** `oxbow serve --open`. Type a request in plain English; the shortlist, caveats and audit trail appear beside the chat. With a model attached, follow-ups ("why is ZrO2 fifth?", "rerun without lanthanum", "what does this caveat mean?") work in the same conversation. Every number the assistant quotes comes from a tool call; a number that does not is flagged. Summary, Audit and Report downloads are on the canvas.

**Command line.**

```bash
oxbow query                                   # the brief's example request
oxbow query "top 10 lead-free oxides with band gap above 5 eV"
oxbow query -p conservative -t audit          # a profile and an output format
oxbow report --out report.html                # self-contained HTML report with eval checks
oxbow chat                                    # the assistant in the terminal (needs a model)
```

**MCP server.** `oxbow mcp` starts a stdio server; drop `docker/mcp-client-config.example.json` into your client's MCP config. The tools are the same ones the web assistant uses.

Keys and environment go in `.env` (loaded automatically) or the shell:

| Variable | Purpose |
| --- | --- |
| `ANTHROPIC_API_KEY` | the assistant; set `LLM_PROVIDER=anthropic` alongside it |
| `LLM_PROVIDER` | `anthropic` (recommended), `openai_compatible`, or `none` for the rules-only fallback |
| `OPENAI_API_KEY` | the assistant with `LLM_PROVIDER=openai_compatible` and no `LLM_BASE_URL` |
| `LLM_BASE_URL`, `LLM_API_KEY` | a self-hosted OpenAI-compatible server (vLLM, Ollama, llama.cpp) for `openai_compatible`; the key only if the server wants one |
| `LLM_MODEL` / `AGENT_MODEL` | optional model overrides for the parse edge and the assistant |
| `MP_API_KEY` | warming the cache from Materials Project (free) |
| `OPENALEX_API_KEY` | optional; more literature lookups per day |
| `OXIDE_TRIAGE_CACHE` | path of the SQLite cache (default `data/cache.sqlite`) |
| `OXIDE_TRIAGE_OFFLINE` | `1` forbids all network fetches |
| `OXIDE_TRIAGE_ADMIN` | `1` lets the admin panel edit settings and run cache jobs |
| `OXIDE_TRIAGE_CONFIG_DIR` | directory holding `default.yaml` and `profiles/` when not running from a checkout |
| `OXBOW_PASSWORD` | optional shared password in front of the web app |

## Adjusting it

In short: a **scientist** says it in the request, a **site admin** edits `site.yaml` or the Admin panel, a **deployment engineer** ships `oxbow bundle` and points `OXIDE_TRIAGE_CONFIG_DIR` at the site's config.

**A scientist changes things in the request itself.**

| Say | Effect |
| --- | --- |
| "top 10" | shortlist length |
| "band gap above 5 eV", "only on-hull" | gate thresholds for this run |
| "prioritize the dielectric constant" | weight that criterion up |
| "lead-free", "no lanthanum" | exclude elements |
| "include lead" | permit a blocked element; asked to confirm, then logged |
| "on germanium" | substrate for the interface check |
| "audit view", "as json" | output format |

Anything the request asks for that is outside the shipped policy is listed in the header of the result as a deviation.

**A site admin changes the defaults.** Turn on `OXIDE_TRIAGE_ADMIN=1` and use the Admin panel in the web app, or edit `site.yaml` by hand. Common edits:

- **Ranking criteria.** `weights:` sets the relative weight of stability, band gap, interface, toxicity, simplicity and literature; `figure_of_merit.weight` sets the weight of the profile's figure of merit (the dielectric constant in the default profile). `gates:` sets the hard cut-offs (energy above hull, minimum gap, element count).
- **Hazard policy.** `toxicity.blocklist_tiers`, `element_blocklist`, `element_allowlist`, and `never_lift` for elements no request may unblock.
- **Terminology.** `terminology:` maps local vocabulary to canonical terms, for example `hafnia: HfO2` or `hi-k: dielectric`. Add whatever your group says.
- **Output and verbosity.** `output.default_template` picks how much detail every result carries: `pi_summary` is the one-page summary, `advanced` adds the per-criterion breakdown, `audit` shows every threshold and exclusion, `json` is machine-readable. Also `output.top_k`, `output.tie_band` (how close two scores must be to count as a tie) and `output.group_polymorphs`. For finer control, the templates are Jinja files in `oxide_triage/templates/`; the report is `report.html.j2`.

Or pick a shipped profile with `-p` on the command line or in the web app:

| Profile | What it changes |
| --- | --- |
| `default` | balanced oxide-dielectric triage on silicon |
| `conservative` | tighter gates, tier-1 hazards blocked, literature weighted up |
| `exploratory` | wider hull and gap windows, missing data penalized lightly |
| `ferroelectric-research` | lead and bismuth permitted and flagged, lower gap bar |
| `thermal-barrier` | a different material class: ranks for minimum thermal conductivity on alumina |

A new profile is a YAML file in `config/profiles/` that overrides only what differs. Ranking for a different property means replacing the `figure_of_merit:` block, as `thermal-barrier.yaml` does.

**A forward deployed engineer sets things up once per site:**

- `oxbow bundle build` packages a warmed cache with a manifest; `oxbow bundle verify` and `oxbow bundle install` load it on a machine with no network. See [docker/OFFLINE.md](docker/OFFLINE.md).
- `OXIDE_TRIAGE_CONFIG_DIR` points an installed wheel at a directory of config files outside a checkout; `OXIDE_TRIAGE_CACHE` places the cache and, with it, `site.yaml` and the logs.
- Two append-only logs next to the cache, `deviations.jsonl` and `retrieval.jsonl`, record every run that departed from policy and every data gap. The Admin panel summarizes them.

**Auth is prototype-grade** `OXBOW_PASSWORD` puts a single shared password in front of the web app and `OXIDE_TRIAGE_ADMIN=1` gates the admin panel; neither carries a user identity, so the logs above record *what* departed from policy but not *who*. At a real site, we would be integrating with the customer SSO, or setting up an authenticating reverse proxy.

## Troubleshooting

Start with `oxbow doctor`, which reports on keys, cache health and the known-answer test.

| Symptom | Cause and fix |
| --- | --- |
| Empty shortlist, or every candidate excluded | Cache not warmed, or gates too tight for the request. Run `oxbow warm-cache`, then `oxbow config --changed-only` to see the gates in force. |
| `serve` refuses to start after a cache warm | The self-check found that the workhorse dielectrics (HfO2, ZrO2, Al2O3, Ta2O5) did not surface where they should. Re-run `oxbow selfcheck` for the failing case; a partial warm is the usual cause. |
| Data looks stale, or literature lookups are missing | Check `retrieval.jsonl` for the gaps, then re-warm. `OPENALEX_API_KEY` raises the daily literature limit. |
| Assistant does not respond, or replies in fixed phrasing | No model attached. Confirm `ANTHROPIC_API_KEY` and `LLM_PROVIDER=anthropic` (or `OPENAI_API_KEY`, or `LLM_BASE_URL` and `LLM_MODEL`, with `openai_compatible`); `oxbow doctor` says which is missing. Without them the rules-only fallback is running and the ranking is unaffected. |

## Evaluation

`oxbow eval` runs the suite on the demo fixture; add `--live-cache` for real data. It covers a normal request, adversarial requests, a known-answer check, determinism, and missing-data handling.

On live data the top five for the brief's request are LaAlO3, HfO2, SrHfO3, LaScO3 and ZrO2, with HfO2 second of 317 passing compounds and Ta2O5 far down, explained by its reaction with silicon. The cases are in [oxide_triage/evaluation.py](oxide_triage/evaluation.py) (`oxbow eval` and [eval/run_eval.py](eval/run_eval.py) both call it); the last live run is in [docs/live-evaluation.md](docs/live-evaluation.md). [eval/evaluation.ipynb](eval/evaluation.ipynb) is the same suite as a notebook, with outputs saved, so it reads on GitHub without running anything.

Every ranking parameter that was set after seeing data is logged in [docs/ranking-decisions.md](docs/ranking-decisions.md), with what triggered it, what it moved in the live ranking, and why.

## Development

```bash
pip install -e ".[all]"
ruff check . && ruff format --check .
pytest -q
cd ui && npm ci && npm run build      # front end only; the built bundle is committed
```

CI runs lint, tests, a clean wheel install, and an offline smoke test of the container image.

### Evidence boundaries

Caveats in reports are rule-derived. The legacy `llm.use_for.refute` setting remains loadable, but production refutation no longer calls a model. Model-written assertions cannot become evidence-backed caveats by naming a field. The assistant remains available for conversation; its numeric-token diagnostic does **not** validate attribution, units, citations, qualitative assertions or meaning, even when no tokens are flagged. User numbers are not scientific evidence.

Candidate explanations render known numerical facts with candidate identity, property, units and source provenance. Missing provenance is not invented. This establishes traceability to the retrieved record, not experimental truth: sources may be wrong, approximate or mismatched. DOI validation checks syntax only, not resolution or paper contents. Property sources are distinct from literature search matches, whose support for a property has not been established.

The displayed high/medium/low label means **data coverage**, not scientific confidence. The JSON field `confidence` and configuration key `confidence_thresholds` retain their legacy names for compatibility. Source disagreement, corrected DFT gaps, missing data and retrieval failures retain their separate caveats.

### Retrieval and evaluation guarantees

The default warns below 95% retrieval completeness (`retrieval.min_completeness_warn: 0.95`). Its ranking-refusal floor is **disabled** (`min_completeness_serve: 0.0`); incomplete rankings can be returned with warnings and per-candidate retrieval caveats. A site may opt into a positive refusal floor. The separate known-answer self-check becomes inconclusive below 90% retrieval; that is not the ranking-refusal policy. None of these defaults changed in this review.

Ranking is deterministic for fixed structured criteria, configuration and cache. Model-assisted request parsing and conversational explanations can vary, and model explanations are not validated scientific evidence. The `validate` command retains its name for compatibility, but the Hubbard–Schlom comparison is a **retrospective benchmark**: its ZrO₂ case informed the tolerance choice, and the assessment band was adjusted after examining outcomes. It is not independent validation. The [future evaluation protocol](docs/future-evaluation.md) specifies frozen parameters and untouched cases; that evaluation has not been performed.
