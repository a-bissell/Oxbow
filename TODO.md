# TODO

Ideas / planned work for this project.

## Data layer (blocking)

- [x] **Fix the band-gap functional lookup against live Materials Project data** — done
      2026-09-10: the task id comes from the `electronic_structure` origin (the live API has no
      `band_gap` origin); verified live, 40/40 resolve to GGA.
- [ ] **Commit the recorded live fixtures** under `tests/recorded/` once the replay test
      passes on a complete recording (Materials Project, OQMD, PubChem).
- [ ] **Finish the re-record** — the 2026-09-10 re-record (tighter universe: 4,769 materials,
      2,651 formulas) stopped early on OpenAlex's daily budget. Literature is now fetched per
      query instead of at warm time, so the warm and the recording cover Materials Project,
      OQMD and PubChem only (~5,300 formula requests). OQMD returned 429 at four concurrent
      workers but recovered within a minute; a retry pass resumes from `data/record-cache.sqlite`
      (`OXIDE_TRIAGE_CACHE=data/record-cache.sqlite oxide-triage warm-cache --record tests/recorded`).
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
