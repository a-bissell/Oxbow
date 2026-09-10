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

## With the PI's group

- [ ] **Run against real data with the group and retune the scoring profiles** with them.
- [x] **Tiers and the literature curve** — done 2026-09-10: candidates within `output.tie_band`
      (0.04) of a tier's leader print as one tier with the order stated arbitrary; the literature
      saturation went from 50 to 500 thin-film works because the brief asks for public evidence
      and the first curve gave HfO2 (6,975) and LaScO3 (29) the same score. README walks the
      live ranking under both.
- [ ] **Interface stability with silicon as a criterion** — the discriminator the top of the
      list lacks. Materials Project's interface-reactions endpoint is gone from the current API,
      but the chemsys thermo entries (e.g. Hf-O-Si) are public, so the reaction energy of an
      oxide with Si against the hull is computable here. Needs a small convex-hull routine
      without pymatgen.

## Known limitations (by design, not planned work)

- Literature counts from formula-string search are noisy for short formulae (flagged per candidate).
- The hazard table is a screen, not a toxicological assessment.
- Thin films are not modelled.
