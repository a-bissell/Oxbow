# TODO

Ideas / planned work for this project.

## Data layer (blocking)

- [x] **Fix the band-gap functional lookup against live Materials Project data** — done
      2026-09-10: the task id comes from the `electronic_structure` origin (the live API has no
      `band_gap` origin); verified live, 40/40 resolve to GGA.
- [x] **Commit the recorded live fixtures** — done 2026-09-10: 94 responses (Materials Project,
      OQMD, PubChem; 4.1 MB) from the MP-only warm of the rescoped universe plus the self-check's
      on-demand pool. OpenAlex is absent because the day's budget was spent; the replay test
      passes without it, and a later warm with a live OpenAlex adds those files.
- [x] **Rescope the universe** — done 2026-09-10: observed structures only
      (`candidates.observed_only`) and the dielectric-minded cation allowlist v2 (wide list kept
      as `cation_allowlist_wide.yaml`). Confirm the cation list with the PI's group.
- [x] **Fetch OQMD, PubChem and OpenAlex per query** — done 2026-09-10
      (`candidates.formula_sources: on_demand`): the warm is Materials Project only; a query
      fills the top-ranked pool and re-ranks until it settles; completeness is measured over the
      pool; the self-check fills its own pool online on a live cache.
- [x] **Finish the re-record** — done 2026-09-10 in about five minutes end to end (708 candidates).
- [x] **Self-check windows on real data** — done 2026-09-10: the default top five on live data
      are SrHfO3, LaAlO3, LaScO3, CaZrO3 and ScTaO4 (k 25-49), with HfO2 6th, ZrO2 7th, Al2O3
      16th of 497. The check now requires HfO2 in the top 10 (Al2O3 is a workhorse, not a
      leader) and two workhorses in the exploratory top 25, all configurable under `selfcheck`.
      The dielectric curve's saturation at k=30 is the reason; retuning it is a PI decision.
- [x] **Per-source concurrency and rate limits** — done 2026-09-10: `candidates.fetch.<source>`
      sets `workers` and `max_rps` per source (OQMD 8 workers at 1 req/s, PubChem 4 at 4 req/s,
      OpenAlex 4 at 5 req/s); the cap lives in the HTTP client, so retries and the on-demand
      literature fill are covered too.
- [x] **Fill or accept recording gaps** — done 2026-09-10: clean re-record from a fresh cache
      (MP warm plus the self-check's pool and one query per profile), 134 responses over all
      four sources including OpenAlex, retrieval 100% complete. `autodocs/live-evaluation.md` and
      `autodocs/sample_report.html` are generated from that cache.
- [x] **Validate the rest of the first live warm** — done 2026-09-10: all five evaluation checks
      pass on the live cache; the eval's known-answer check now defers to the configured
      self-check instead of carrying its own windows. Live default top five: LaAlO3, SrHfO3,
      LaScO3, CaZrO3, HfO2.
- [x] **Group polymorphs by formula in the shortlist** — done 2026-09-10
      (`output.group_polymorphs`, `oxide_triage/grouping.py`): the best-ranked phase leads the
      row, other passing phases collapse under it with their own numbers and a caveat, ranks
      are over compounds, the on-demand pool counts compounds, and `explain` still resolves a
      collapsed phase by id. Live: 497 passing materials become 317 compounds.
- [x] **Anthropic structured output** — done 2026-09-10: the API rejects `minimum`/`maximum`/
      `maxItems`/`maxLength` and a `null` inside an `enum`; the schemas are cleaned on the way
      out (Python validates the result anyway). Before this the model edges always fell back to
      rules against the real API.

## Material classes (the figure-of-merit seam)

- [x] **Make the seventh criterion a configurable figure of merit** — done 2026-09-13
      (`figure_of_merit:` in `config/default.yaml`; providers in
      `oxide_triage/sources/properties.py`): property, provider, label, units, method,
      preference direction and curve, weight, missing-data handling, and the words the parser
      and guard accept. The self-check judges the active profile with one stored verdict per
      profile. `tests/test_fom_regression.py` holds the four oxide-dielectric profiles byte for
      byte against a baseline captured before the seam. The record's JSON key `dielectric`
      became `figure_of_merit`; old site files are migrated on load with a warning.
- [x] **Ship a second material class: thermal barrier coatings** — done 2026-09-13
      (`config/profiles/thermal-barrier.yaml` and its cation allowlist): Clarke's minimum
      thermal conductivity from Materials Project elastic tensors, lower preferred, Al2O3
      substrate, band gap zeroed and ungated, two class-specific refutation rules. No code path
      names the class. Live: 750 candidates, 394 passing; ZrO2 15th, HfO2 12th, La2Zr2O7 named
      as having no elastic tensor at the source rather than failed on. The design note
      (`docs/design-note.md`, section 6) lists the files a new class touches.
- [ ] **The anion as the second seam.** The universe is oxides by construction (the acquisition
      filter, the formula parser's expectations, the cation allowlists and the hazard screen all
      assume it). Fluoride UV windows were not chosen as the second class for this reason. Out
      of scope for the current project; named here so the next class is not picked to work
      around it.
- Thermal barriers rank on conductivity, stability and compatibility only. Melting point,
  thermal expansion, sintering, CMAS attack and toughness have no public per-material source
  and are named as unmodelled on every output. Not planned work until a source exists.

## Data sources

- [ ] **Add JARVIS-DFT bulk dataset as a second dielectric route** (OptB88vdW dielectric
      tensors). Since the figure-of-merit seam this is a `PropertyProvider` in
      `oxide_triage/sources/properties.py` beside the two Materials Project routes, selected by
      `figure_of_merit.provider` in a profile; the dielectric provider's no-route reason names it
      as the candidate. Needs a cache key and recorded responses like every other source.
- [ ] **Literature evidence graph** — under consideration, not committed. The weakest input
      today is literature: two OpenAlex counts from a formula-string search, flagged as noisy
      for short formulae. The question a PI actually asks is not "how many papers mention
      HfO2" but "has anyone deposited HfO2 by ALD on silicon and measured its dielectric
      constant", which is a path, not a count: material → deposition method → substrate →
      measured property → work → DOI. Proposed scope:
      - Extract (method, substrate, property measured) from the sample-work abstracts already
        fetched, through the existing validated model edge: delimited data in, a fixed schema
        out, every term from a closed vocabulary, anything else discarded. Rules-only fallback
        keyword-matches the same vocabulary (the thin-film term list is a start).
      - Store as typed edges in the existing SQLite cache (an `edges` table: subject,
        predicate, object, provenance), one mechanism that also subsumes the alias table, the
        OQMD formula match and polymorph grouping (`same_composition`, `reported_as`). No
        graph server; deployment stays three commands and one file.
      - Consume it in the refutation pass only: a caveat can cite the specific work that
        contradicts or supports a candidate ("no deposition report found; the two thin-film
        works are on sputtered films, not ALD"). The graph never touches a score, a rank or a
        gate; it is retrieved evidence like everything else under the numeric guard.
      - Later, family and hazard facts (`cation_allowlist.yaml`, `element_hazards.yaml`,
        `hygroscopic_oxides.yaml`) could live in the same edge table, so "avoid anything in
        lead's hazard tier" resolves by traversal instead of a new parser rule.
      Why not now: the current counts are enough to flag thin evidence, which is what the
      shortlist needs; extraction quality on abstracts is unmeasured; and it is a week of work
      that should follow, not precede, the profile retune with the PI's group.

## Local language model

- [ ] **Remove the locally hosted model path; it is out of scope.** The project ships two
      drivers a reviewer can use, rules only (no key) and Anthropic, and the local path was a
      third that nobody runs: a vLLM + NVIDIA compose overlay (`docker/compose.local-llm.yml`)
      with Qwen-specific settings in the edge client. Keeping it means a serving story, a
      per-model profile, recorded model responses and a weights asset for the offline release,
      none of which the brief asks for. Remove rather than replace:
      - `oxide_triage/edges/llm.py`: the `openai_compatible` provider and the Qwen defaults
        (model name, thinking-mode switch, Hermes tool-call assumptions); the provider enum in
        `config/default.yaml`, `oxide_triage/config.py`, `doctor.py` and the CLI `--llm` help.
      - `docker/compose.local-llm.yml`, its reference in `docker/compose.yml` and
        `docker/OFFLINE.md`; the `LLM_BASE_URL` block in `.env.example`.
      - README "A locally hosted model" and the design note's deployment paragraph, rewritten
        to say the model is Anthropic or nothing; the agent UI's driver label.
      - The tests that exercise the provider (`tests/test_agent.py`, `test_config.py`,
        `test_review_fixes.py`) and the deployment follow-up for a local-model asset.
      The privacy argument the local path made ("nothing leaves the site") still holds for the
      rules-only driver, which is the offline release's driver anyway.

## UI

- [x] **Admin UI panel** — done 2026-09-10: profiles, universe families, language model, data
      and cache jobs, sources and limits, deviations log, in the web app. Edits go to the site
      overrides file next to the cache (`site.yaml`); the shipped YAML is never written, every
      field shows the shipped value where it differs, and a policy change prints a `site`
      deviation on every result. `OXIDE_TRIAGE_ADMIN=1` gates editing and cache fetches.
      Follow-ups:
      - [ ] Preview a profile edit: re-rank the last result under the draft before saving
            (`preview_config` exists; needs an endpoint that runs a rerun against it).
      - [ ] Site-defined profiles (new named profiles created from the panel).
      - [ ] Real authentication in front of the admin panel (a proxy today).
- [x] **Interactive Agent UI** — done 2026-09-10: chat beside a results canvas, click a
      candidate to focus it, compare, rerun with a diff, clarify-before-run in the
      conversation. The assistant drives the same tools the MCP server exposes through the
      shared `ToolBox`; two drivers: rules (no key) and the shared chat agent (Anthropic tool
      use or an OpenAI-compatible local model) with its number guard. Conversations persist
      next to the cache. `oxide-triage chat` is the same agent in the terminal. Follow-ups:
      - [ ] Conversation history page with search and delete (the landing page lists eight).
      - [ ] Stop a running turn: the client can close the stream, but the pipeline keeps
            running; thread a cancellation token through the on-demand fill.
      - [ ] Clickable HTML report export that seeds the same follow-ups as the canvas.

## Deployment

- [x] **Offline release pipeline** — done 2026-09-10 (`.github/workflows/release.yml`,
      `oxide_triage/bundle.py`, `docker/compose.offline.yml`, `docker/OFFLINE.md`): a tag warms
      the cache with the repository's key, fills every profile's on-demand pool, self-checks,
      packages `cache.sqlite` with a manifest (sources, timestamps, self-check, commit, SHA-256),
      saves the image and an index-free wheel set, then proves the lot with `--network none`
      (install, self-check, the PI's request, the documented compose steps) before publishing
      with a provenance attestation. `oxide-triage bundle build|verify|install`; `doctor` and
      the web status name the installed release. CI (`ci.yml`) lints, tests and smokes the
      image on every push. Follow-ups:
      - [ ] Multi-arch image (arm64) and a wheel set per platform; the wheels are built for the
            runner (linux x86_64, CPython 3.11) and say so in their name.
      - ~~Optional local-model asset~~ — dropped 2026-09-13 with the local model path (see
        "Local language model").
      - [ ] Refresh bundle: a scheduled run that re-warms and publishes a cache-only release so
            a site can update the data without a new image.
      - [ ] Bundle install from the admin panel (upload a bundle, verify, install) for sites
            where nobody wants to type a compose command.

## With the PI's group

- [ ] **Run against real data with the group and retune the scoring profiles** with them.
- [x] **Tiers and the literature curve** — done 2026-09-10: candidates within `output.tie_band`
      (0.04) of a tier's leader print as one tier with the order stated arbitrary; the literature
      saturation went from 50 to 500 thin-film works because the brief asks for public evidence
      and the first curve gave HfO2 (6,975) and LaScO3 (29) the same score. README walks the
      live ranking under both.
- [x] **Interface stability with silicon as a criterion** — done 2026-09-10
      (`interface`, weight 0.15; `scoring/hull.py`, numpy + scipy `linprog`): the most
      exothermic reaction of the oxide with the configured substrate against the Materials
      Project hull of oxide + substrate, one cached hull per element system (217 for the live
      universe, fetched in a minute). Reproduces Hubbard & Schlom 1996: HfO2, Al2O3, Y2O3, LaAlO3,
      SrHfO3 at 0 against Si; ZrO2 inside the 0.05 eV/atom DFT tolerance; Ta2O5, TiO2, the
      titanates react and the caveat names the products. Follow-ups:
      - [x] Held-out validation against Hubbard & Schlom 1996 — done 2026-09-10
            (`oxide-triage validate`, evaluation check 7): 21/21 hard, 10/11 soft; SrO, CaZrO3,
            SrZrO3 named as disagreements.
      - [ ] Chase the three disagreements: are the MP alkaline-earth silicate energies the cause?
      - [x] Let a request name the substrate — done 2026-09-13: "on germanium", "on a sapphire
            substrate", "on SrTiO3" set `Criteria.substrate` (validated as a formula of real
            elements); the interface criterion is computed against it, a `request_substrate`
            deviation says so, and `rerun` accepts `{"substrate": "Ge"}`. A name with no hull
            phase (glass, graphene) is reported as not acted on. Offline, a system whose hull
            is not cached is named on the candidate; a live run fetches it.
      - [ ] Multi-substrate view: the same shortlist against Si, Ge and a perovskite side by side.

## Known limitations (by design, not planned work)

- The design note a reader should trust is `docs/design-note.md`. The generated documents in
  this directory (`live-evaluation.md`, `ranking-decisions.md`, `sample_report.html`) are
  regenerated from the live cache; this file is hand-maintained.

- Literature counts from formula-string search are noisy for short formulae (flagged per candidate).
- The hazard table is a screen, not a toxicological assessment.
- Thin films are not modelled.
