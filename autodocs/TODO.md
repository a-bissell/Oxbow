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
      four sources including OpenAlex, retrieval 100% complete. `docs/live-evaluation.md` and
      `docs/sample_report.html` are generated from that cache.
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

## Data sources

- [ ] **Add JARVIS-DFT bulk dataset as a second dielectric route** (OptB88vdW dielectric
      tensors); flagged as a candidate route in `oxide_triage/acquire.py`.
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

- [ ] **Replace the Qwen3-8B local model with a first-class Gemma 4 12B implementation, running
      natively on a MacBook Pro.** Today the local path is a vLLM + NVIDIA compose overlay
      (`docker/compose.local-llm.yml`) with Qwen-specific settings in the edge client
      (`oxide_triage/edges/llm.py`: the default model name, the thinking-mode switch, the Hermes
      tool-call format the vLLM flags assume). A Mac has no NVIDIA path and Docker on macOS cannot
      reach the GPU, so "first class" means a native serving story, not a container. Scope:
      - **Serving.** Gemma 4 12B through an OpenAI-compatible server on Apple Silicon: Ollama,
        llama.cpp's server, or an MLX server; pick one as the documented default and keep the
        other two as `LLM_BASE_URL` targets. At 4-bit the weights are roughly 7-8 GB and fit a
        32 GB machine beside the app; bf16 needs ~24 GB. Record the measured numbers.
        `LLM_PROVIDER=openai_compatible` stays the contract; the app in Docker reaches the host
        server at `host.docker.internal`, the app run from a venv reaches it on localhost.
      - **Edge client.** Make the client model-agnostic: the Qwen defaults and the thinking-mode
        knob move to a per-model profile (name, extra request body, tool-call and structured-
        output capabilities), and Gemma 4 becomes the shipped default. Verify against the real
        model that (a) the parse edge returns schema-valid JSON, (b) the refutation edge's
        observations pass the numeric guard, and (c) the assistant's tool-use loop (`agent.py`)
        completes the PI request, an explain, a compare and a rerun without a fallback to rules.
        Where Gemma's tool calling differs from the Hermes format, the adapter lives in the
        client, never in the tools.
      - **Tests and evaluation.** Recorded Gemma responses for the replay tests of the local
        driver, the same way the sources are recorded; an eval row that runs the fixture suite
        with the local model and reports fallback counts, so a regression in the model path
        shows as a number.
      - **Docs and packaging.** README "locally hosted model" section and the design note's
        deployment paragraph rewritten around the Mac path; `.env.example` defaults;
        `compose.local-llm.yml` either retargeted to the native server or retired. Ties into the
        offline-release follow-up: a companion weights asset so the USB-stick install can carry
        the model too.
      - **Licence check.** Gemma ships under Google's own terms rather than Apache-2.0; note the
        implications for a commercial deployment before it becomes the default.

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
      - [ ] Optional local-model asset: a companion archive with weights and the vLLM or Ollama
            image for sites that want the model driver offline. Multi-gigabyte, per-site choice.
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
      - [ ] Let a request name the substrate ("on germanium", "on SrTiO3").
      - [ ] Multi-substrate view: the same shortlist against Si, Ge and a perovskite side by side.

## Known limitations (by design, not planned work)

- Literature counts from formula-string search are noisy for short formulae (flagged per candidate).
- The hazard table is a screen, not a toxicological assessment.
- Thin films are not modelled.
