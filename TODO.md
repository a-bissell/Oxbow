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
- [ ] **Fill or accept recording gaps** — formulas whose fetch failed have no OQMD / OpenAlex /
      PubChem recordings. Re-record, or document that the gaps are expected.
- [ ] **Validate the rest of the first live warm** — check field names and dielectric
      coverage in the recorded responses, and retune defaults if they moved.

## Data sources

- [ ] **Add JARVIS-DFT bulk dataset as a second dielectric route** (OptB88vdW dielectric
      tensors); flagged as a candidate route in `oxide_triage/acquire.py`.

## UI

- [x] **Admin UI panel** — done 2026-09-10: profiles, universe families, language model,
      data and cache jobs, sources and limits, deviations log; edits go to `config/site.yaml`.
- [x] **Interactive Agent UI** — done 2026-09-10: chat beside a results canvas, click a
      candidate to focus it, compare, rerun with a diff, clarify-before-run in the
      conversation. Two drivers: rules (no key) and Claude (`ANTHROPIC_API_KEY`).
- [ ] **Preview a profile edit** — in Admin → Profiles, re-rank the last result under the
      draft before saving (the rerun primitive already exists; needs an endpoint that takes an
      overlay instead of a saved one).
- [ ] **Conversation history page** — search and delete; the landing page lists the last
      eight only.
- [ ] **Stop a running turn** — the event stream can be closed by the client, but the
      pipeline keeps running; thread a cancellation token through the on-demand fill.
- [ ] **Local model driver** — the `openai_compatible` provider parses and refutes but does
      not yet drive the assistant; the rules driver is used instead.

## With the PI's group

- [ ] **Run against real data with the group and retune the scoring profiles** with them.

## Known limitations (by design, not planned work)

- Literature counts from formula-string search are noisy for short formulae (flagged per candidate).
- The hazard table is a screen, not a toxicological assessment.
- Thin films are not modelled.
