# TODO

Ideas / planned work for this project.

## Data layer (blocking)

- [ ] **Fix the band-gap functional lookup against live Materials Project data** — the
      recorded-replay test (`tests/test_recorded.py`) fails: every record resolves its
      functional to `"unknown"`, so the tasks-endpoint lookup does not match what the API
      actually returns. Items below in this section are blocked on this.
- [ ] **Commit the recorded live fixtures** under `tests/recorded/` (108 files across
      Materials Project, OpenAlex, OQMD, PubChem) once the replay test passes.
- [ ] **Finish the re-record** — the 2026-09-10 re-record (tighter universe: 4,769 materials,
      2,651 formulas) stopped early on OpenAlex's daily budget. Literature is now fetched per
      query instead of at warm time, so the warm and the recording cover Materials Project,
      OQMD and PubChem only (~5,300 formula requests). OQMD returned 429 at four concurrent
      workers but recovered within a minute; a retry pass resumes from `data/record-cache.sqlite`
      (`OXIDE_TRIAGE_CACHE=data/record-cache.sqlite oxide-triage warm-cache --record tests/recorded`).
- [ ] **Per-source concurrency and rate limits** — `fetch_workers` is one number for all
      formula sources. OQMD 429s at four workers. Make the worker count and a requests-per-second
      cap configurable per source.
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
