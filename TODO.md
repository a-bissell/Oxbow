# TODO

Ideas / planned work for this project.

## Data layer (blocking)

- [x] **Fix the band-gap functional lookup against live Materials Project data** — done
      2026-09-10: the task id comes from the `electronic_structure` origin (the live API has no
      `band_gap` origin); verified live, 40/40 resolve to GGA.
- [ ] **Commit the recorded live fixtures** under `tests/recorded/` once the replay test
      passes on a complete recording (Materials Project, OQMD, PubChem).
- [x] **Rescope the universe** — done 2026-09-10: observed structures only
      (`candidates.observed_only`) and the dielectric-minded cation allowlist v2 (wide list kept
      as `cation_allowlist_wide.yaml`). Confirm the cation list with the PI's group.
- [x] **Fetch OQMD, PubChem and OpenAlex per query** — done 2026-09-10
      (`candidates.formula_sources: on_demand`): the warm is Materials Project only; a query
      fills the top-ranked pool and re-ranks until it settles; completeness is measured over the
      pool; the self-check fills its own pool online on a live cache.
- [ ] **Finish the re-record** — the warm now covers Materials Project only (minutes). Run
      `oxide-triage warm-cache --record tests/recorded` against a fresh cache, then commit the
      recordings once `tests/test_recorded.py` passes. The partial recordings under
      `tests/recorded/` predate the rescoped universe query and can be discarded.
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

- [ ] **Admin UI panel** — a settings screen for changing app/project configuration.
- [ ] **Interactive Agent UI** — a chat-style interface where the user asks questions,
      gets a generated report back, and can click into parts of the report to
      conversationally "drill down" with the AI agent for more detail. Backed by the
      Claude API (requires an API key).

## With the PI's group

- [ ] **Run against real data with the group and retune the scoring profiles** with them.

## Known limitations (by design, not planned work)

- Literature counts from formula-string search are noisy for short formulae (flagged per candidate).
- The hazard table is a screen, not a toxicological assessment.
- Thin films are not modelled.
