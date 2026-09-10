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

It runs three ways from the same code: a CLI, a Streamlit app, and an **MCP server** so
scientists can drive it from Claude Desktop, Claude Cowork, Cursor or any MCP-capable app. In
that mode the client's model is the front edge and every guarantee still lives inside the tools.
The app's **Agent page** hosts that same client in-process: a conversation with a model that
drives the tools, beside a report whose every part can be asked about.

---

## For scientists: running a query

### Quick start (no keys, demo data)

```bash
pip install -e ".[app]"
oxide-triage load-fixtures          # synthetic demo data; every output says so
oxide-triage query --offline        # the PI's request, default profile, PI summary
oxide-triage query --offline --profile exploratory --template audit
oxide-triage report --offline --out triage_report.html   # self-contained HTML report
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
| `audit view`, `json`, `html report` | output template |

Site vocabulary (e.g. *hafnia*, *high-k*) is mapped through `terminology` in the config.

### Reading the output

**PI summary** — top candidates, one line of rationale each, the single most important caveat.
Each entry shows its score, a **confidence** label and, when relevant, the criteria that had
**no data** behind them. A candidate ranked on partial data says so.

**Audit view** — every score component with its weight and contribution, every gate with its
threshold and the observed value, the DFT functional behind each number, source ids and retrieval
timestamps, the full caveat list with origin (`rule` or `llm`), everything excluded and why.

**JSON** — the complete result object for downstream tooling.

**HTML report** — a single self-contained file (no scripts, no external assets, prints cleanly):
shortlist cards with score bars and the main caveat, an expandable full breakdown per candidate,
a data-gap map showing which criteria had data behind them for every passing candidate, the
excluded list with reasons, the scoring rules, the scope statement, and optionally the
evaluation checks. `oxide-triage report --out triage_report.html` (see `docs/sample_report.html`,
generated from the synthetic fixture). Retrieved text is escaped, never interpreted.

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

### Following up on a result

Every result is a complete object, so follow-ups never re-derive anything:

* **Explain** a candidate (ranked or excluded): every component with weight and contribution,
  every gate, the band-gap correction, provenance, all caveats. In the app: *Follow up → Explain*.
  Over MCP: `explain(result_id, "Ta2O5")`.
* **Rerun with a change** (gap threshold, hull threshold, element allow/exclude, weights,
  shortlist length). The change is applied through the same deterministic core and printed as a
  configuration deviation on the new result. In the app: *Follow up → Rerun*. Over MCP:
  `rerun(result_id, {"min_band_gap_ev": 3.5})`.
* **Clarify before running.** When a request changes something material (lifts a hazard block,
  moves a gate, zeroes a criterion, or contains a part the deployment cannot do) the system asks
  first. The app shows the questions and a confirm box; the CLI prompts (or `--yes`); over MCP,
  `triage`/`rerun` return the questions and the client calls again with `confirmed=true`.

### Talking to the agent (Agent page, `oxide-triage chat`)

The app's **Agent** page is a conversation. You ask in plain English; a language model turns
your words into calls to the same eight tools the MCP server exposes (`triage`, `explain`,
`rerun`, `parse_request`, `profiles`, …) and relays what they computed. The result appears
beside the chat as cards, and every part of the report carries a button that asks about it:
*Why this rank?* on a candidate, *Ask* on a caveat, *Why excluded?* on an excluded row, *Missing
dielectric?* on the data-gap map, *Rerun with gap ≥ 5 eV* on the scoring rules. A button seeds a
chat turn naming the result id and the candidate; the model calls `explain` or `rerun` and
answers from the output. `oxide-triage chat` is the same agent in the terminal.

What keeps it honest is the same as over MCP: the model reaches data only through tools, each
call is validated against the tool's schema before it runs, and the guard, the deterministic
core, the self-check gate and the fixture banner run inside the tools. Two things are added for
chat. The transcript is append-only, so nothing is edited or re-derived behind you. And a
**number guard** checks every number in a reply against the numbers the tools printed (and your
own words); anything else is marked ⚠ and listed under the reply as unverified. It flags, it does
not rewrite, so a count such as “top 3” may be flagged too.

The page needs a model: `LLM_PROVIDER=anthropic` with `ANTHROPIC_API_KEY` (the conversation and
the tool outputs go to Anthropic; no private data exists in this system), or
`LLM_PROVIDER=openai_compatible` against the local overlay (nothing leaves the site). With
`none` the page says so and the Triage page works as before. `oxide-triage doctor` reports
whether chat is ready. The agent is limited to `agent.max_tool_rounds` tool calls per message
(`config/default.yaml`).

### Using it from Claude Desktop, Claude Cowork or Cursor (MCP)

```bash
pip install -e ".[mcp]"
oxide-triage mcp                 # stdio server; or: oxide-triage mcp --transport http --port 8765
```

Add the server to your app's MCP config (see `docker/mcp-client-config.example.json`):

```json
{"mcpServers": {"oxide-triage": {"command": "oxide-triage", "args": ["mcp"],
  "env": {"OXIDE_TRIAGE_CACHE": "/absolute/path/cache.sqlite", "OXIDE_TRIAGE_OFFLINE": "1"}}}}
```

Tools: `parse_request` (how a request will be read, plus clarification questions), `triage`,
`explain`, `rerun`, `add_material` (pull one compound into the universe; online only),
`fill_gaps` (adaptive acquisition pass), `cache_status`, `selfcheck`, `profiles`. Resources: `oxide-triage://result/{id}` (full JSON) and
`oxide-triage://scope` (the scope limitation). The server's instructions tell the client model
to relay numbers, ranks and citations as given and to surface the fixture banner. The request
guard, the deterministic core and the fixture banner run inside the tools, so a client prompt
cannot bypass them.

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
cp .env.example .env                 # set MP_API_KEY (free); OPENALEX_API_KEY is optional
docker compose -f docker/compose.yml up --build -d
docker compose -f docker/compose.yml run --rm app oxide-triage warm-cache   # ~minutes; rate-limit aware
# open http://localhost:8501
```

The cache lives in a named volume (`/data/cache.sqlite`). `./config` is mounted read-only so
profiles can be edited on the host without rebuilding; the Admin page's site overrides go to
`/data/site.yaml` in the same volume (`OXIDE_TRIAGE_ADMIN=1 docker compose ...` to enable
editing). Once warmed, the container runs fully offline (`OXIDE_TRIAGE_OFFLINE=1`).

### Install without Docker

Python 3.11+. `pip install -e ".[app,llm]"`, then the same commands. The cache path defaults to
`data/cache.sqlite` (override with `OXIDE_TRIAGE_CACHE`).

### Keys and environment

| Variable | Needed for | Where to get it |
|---|---|---|
| `MP_API_KEY` | warming the cache from Materials Project | free, https://next-gen.materialsproject.org/api |
| `OPENALEX_API_KEY` | optional. Literature counts are fetched per query for the top-ranked candidates (about 50 searches, $0.05). Without a key OpenAlex allows $0.10/day per IP (two queries); a free account's key allows $1/day (twenty) | free account at https://openalex.org |
| `OPENALEX_MAILTO` | contact email on OpenAlex requests (optional; no rate-limit effect any more) | any contact email |
| `LLM_PROVIDER` | `none` (default) / `anthropic` / `openai_compatible`. The Agent page and `oxide-triage chat` need one of the latter two | — |
| `ANTHROPIC_API_KEY` | only if `LLM_PROVIDER=anthropic` | https://console.anthropic.com |
| `LLM_BASE_URL`, `LLM_MODEL` | only if `LLM_PROVIDER=openai_compatible` | your vLLM/Ollama endpoint |
| `MCP_TRANSPORT`, `MCP_HOST`, `MCP_PORT` | MCP server defaults (`stdio`, `127.0.0.1`, `8765`) | — |
| `OXIDE_TRIAGE_ADMIN` | `1` enables editing and cache operations on the Admin page (read-only otherwise) | — |
| `OXIDE_TRIAGE_SITE_CONFIG` | path of the site overrides file (default `site.yaml` next to the cache; `off` disables) | — |

Keys are read from the environment. A `.env` file in the current directory or the repository root
is loaded automatically by the CLI, the app and the MCP server (existing environment variables win).
`.env` is git-ignored; `.env.example` documents every variable.

### Literature counts are fetched per query, not at warm time

OpenAlex meters API use against a small daily budget, and each formula needs two full-text
searches, so warming literature for every candidate would take days. By default
(`literature.fetch: on_demand`) the warm does not touch OpenAlex at all. At query time, after
ranking, the top `literature.on_demand_pool` candidates (25, never fewer than the shortlist) get
their counts fetched and cached, and the ranking is recomputed. Literature credit is never
negative, so this can only move pool members up relative to the rest; candidates below the pool
carry no literature credit and the output says so. A second query on the same profile costs
nothing, since the counts are cached. `literature.fetch: warm` restores fetching for every formula
during `warm-cache` (budget it: about 2 × formulas × $0.001); `never` leaves literature unknown.

### Recording the first live run

The clients were written against the documented APIs and exercised only on synthetic data until
the first live warm. Capture that run so it becomes a permanent regression test:

```bash
oxide-triage warm-cache --record tests/recorded     # or: OXIDE_TRIAGE_RECORD_DIR=tests/recorded
pytest tests/test_recorded.py                        # replays the recordings through the real clients
```

Every raw response is saved as `tests/recorded/<host>/<key>.json` (URL, parameters, response;
headers and keys are never written). While that directory holds only its README the replay test
is skipped; once recordings exist it fails loudly if a field the code depends on is missing from
the real API shape, checks that the workhorses are in the live universe, that dielectric coverage
is partial as expected, and that the self-check passes. With the default on-demand literature
setting the warm, and therefore the recording, covers Materials Project, OQMD and PubChem only. Commit the recordings if their size is
acceptable, or keep them out of git and run the test locally.

### Self-check before serving

After every `warm-cache`, `load-fixtures` or `add-material`, the system runs the known-answer
check on itself: the workhorse dielectrics (HfO2, ZrO2, Al2O3, Ta2O5) must surface near the top
of an unconstrained run or be excluded by a stated gate, and a workhorse missing from the
candidate universe is a failure (it means the fetch is broken). The outcome is stored in the
cache and read on every run. By default a failed check **blocks**: no shortlist is served and the
output says why. `selfcheck.on_failure: warn` downgrades that to a banner. `oxide-triage
selfcheck` runs it on demand; `oxide-triage cache-status` shows the last outcome.

### Adaptive acquisition (gap filling)

The first pass over the sources is a fixed set of queries, and it leaves gaps: OQMD may have no
entry under the exact formula string, a formula token like `LaLuO3` may return zero works in
OpenAlex, the functional behind a band gap may not resolve. After every `warm-cache` and
`add-material` (and on demand with `oxide-triage fill-gaps` or the `fill_gaps` MCP tool) an
acquisition pass turns each gap into a small plan from an **allowlist of read-only routes**:

| Gap | Routes tried, in order |
|---|---|
| no OQMD match by composition | chemical-system query, keep entries with the same stoichiometry |
| no literature counts | retry the formula query; then search on common names only (query path and `add-material` only, since literature is fetched on demand) |
| band-gap functional unresolved | re-fetch the summary and the producing task's `run_type` |
| no DFPT dielectric record | **none** — reported as unfillable with the reason; never estimated |

Every attempt is recorded (gap, route, outcome) and the report is stored in the cache; the audit
view summarises the last pass, and provenance says when a value came from an alternative route.
The default planner is a deterministic ladder. With `acquisition.planner: llm` the configured
model may *reorder or skip* the allowed routes for a gap; its answer is validated to be a subset
of the allowlist, otherwise the ladder is used. The model chooses what to try, never a value.
`acquisition.budget` caps the attempts per pass.

The alternative routes are exercised end to end against fake responses in the tests; their
behaviour against the live endpoints is verified on the first real cache warm.

### Adding a compound on demand

`oxide-triage add-material SrHfO3` (or the `add_material` MCP tool) pulls every non-deprecated
Materials Project entry for that formula, its OQMD cross-check, literature counts and PubChem
record into the cache and the candidate universe, then re-runs the self-check. Online only; in
offline mode the request is declined. The fetched values are data like any other row.

### Configuration

One YAML, `config/default.yaml`, with everything a site admin may want to change: criterion
weights, energy-above-hull threshold, band-gap threshold and correction strategy, element
blocklist/allowlist, maximum distinct elements, default template, verbosity, terminology map,
cache TTL and offline mode, language-model provider. Comments in the file explain each knob.
Fetch pressure on the public sources is set per source under `candidates.fetch` (`workers` for
the warm's thread pool, `max_rps` for a request-rate cap that also covers retries and the
per-query literature fill); the defaults are what OQMD, PubChem and OpenAlex tolerated in practice.

A site changes any of it without touching those files. The app's **Admin** page (and a hand
edit) writes a generated **site overrides file**, `site.yaml` next to the cache (`data/site.yaml`;
`/data/site.yaml` in Docker; `OXIDE_TRIAGE_SITE_CONFIG` to relocate it, `off` to disable it).
Its `base` section is applied as if `default.yaml` had been edited, its `profiles.<name>` sections
as if that profile file had been; environment variables still win over both. The page shows every
value beside what it falls back to and where that comes from, validates the result and shows the
ranking hash before and after, and re-runs the self-check when the policy moved. A site change to
the ranking policy prints a `site` deviation on every result, like a request or profile deviation
does. `oxide-triage config --changed-only` lists what the site file changes and `oxide-triage
doctor` reports the file. Editing and the page's cache operations need `OXIDE_TRIAGE_ADMIN=1`;
without it the page is read-only. There is no per-user login: with the flag set, anyone who can
reach the port can change the site policy, so keep it off on a shared network or put an
authenticating proxy in front. `data/` is git-ignored, so version the file by downloading it from
the page or by pointing `OXIDE_TRIAGE_SITE_CONFIG` at a tracked path.

Profiles in `config/profiles/` are partial overrides:

| Profile | What it changes |
|---|---|
| `conservative` | E_hull ≤ 0.02, effective gap ≥ 5 eV, tier-1 hazards also blocked, higher missing-data penalty |
| `exploratory` | E_hull ≤ 0.08, gap ≥ 3 eV, up to 4 elements, dielectric constant weighted 0.30, top 10 |
| `ferroelectric-research` | gap ≥ 2.5 eV, **Pb and Bi permitted** (surfaced as a deviation on every run), dielectric weighted 0.35, audit view default |

`oxide-triage profiles` prints the effective gates and weights of each. Any run that departs from
the shipped policy (profile allowlist, site override, request-level threshold change, lifted
hazard block) prints the deviation in the output header and appends it to `deviations.jsonl` next
to the cache; the Admin page shows that log.

Templates are files in `oxide_triage/templates/*.md.j2` and can be edited without touching Python.

### MCP over the network

`docker/compose.yml` also starts an `mcp` service (streamable HTTP on `127.0.0.1:8765/mcp`,
loopback only). For LAN access put a reverse proxy with authentication in front of it; the
server itself has no auth. Desktop apps on the same machine can instead launch `oxide-triage mcp`
over stdio.

### Optional: a locally hosted language model

`docker/compose.local-llm.yml` adds a vLLM service serving **Qwen3-8B** and points the app at it.
Nothing leaves the site. The model only parses the request, phrases caveats and drives the tools
on the Agent page, so an 8B model is adequate; the ranking is identical with any provider or
none. The overlay starts vLLM with tool calling enabled (`--enable-auto-tool-choice
--tool-call-parser hermes`); the chat path is exercised against a fake server in the tests and
has not yet been verified against a live vLLM.

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
| `anthropic` | the request text and the *public* structured facts for shortlisted candidates; on the Agent page, the conversation and the tool outputs |

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
pytest                       # 195 tests: scoring core, guard, config + site overrides, refutation, pipeline, injection, session, self-check, MCP, chat agent, admin page, acquisition, HTML report, record/replay
ruff check . && ruff format .
python -m eval.run_eval      # evaluation report -> eval/output/report.md
jupyter lab eval/evaluation.ipynb
```

### Repository layout

```
oxide_triage/
  schemas.py          typed contracts; UNKNOWN is a first-class data state
  config.py           YAML loading, profiles, site overrides file, env overrides, provenance, curated tables
  doctor.py           environment (keys masked), site file, source probes, deviations log (CLI + Admin page)
  cache.py            SQLite cache with timestamps, fixture flag, fingerprint
  sources/            materials_project, oqmd, openalex, pubchem, hazards, assemble, fixtures
  scoring/            bandgap correction, settings resolution, gates + components + ranking
  guard.py            request binning (impossible / configuration / integrity)
  edges/              llm providers (+ chat protocol with tool use), request parser, template renderer
  refute.py           rule-derived caveats + guarded model observations
  pipeline.py         orchestration: guard -> parse -> clarify -> self-check gate -> core -> refute
  session.py          result store, explain, rerun, clarification questions (app + MCP)
  tools.py            the eight tools (one implementation for the MCP server and the chat agent)
  agent.py            chat agent: provider-neutral tool-use loop, append-only transcript, number guard
  selfcheck.py        known-answer check run on the cache itself
  acquire.py          adaptive acquisition: gaps -> allowlisted routes -> report
  formula.py          formula parsing for cross-database stoichiometry matching
  mcp_server.py       MCP tools/resources for Claude Desktop, Cowork, Cursor
  cli.py, app.py      Typer CLI, Streamlit entry point (pages in ui/: sidebar, triage_page, agent_page,
                      report_cards, admin_page, admin_form)
  templates/          pi_summary.md.j2, audit.md.j2, report.html.j2
  evaluation.py       the five evaluation checks (also `python -m eval.run_eval`)
  data/               element_hazards.yaml, cation_allowlist.yaml, compound_aliases.yaml,
                      hygroscopic_oxides.yaml, fixtures/fixture_cache.json
config/               default.yaml + profiles/
docker/               Dockerfile, compose.yml (app + mcp), compose.local-llm.yml, mcp-client-config.example.json
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
