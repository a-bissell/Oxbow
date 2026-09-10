# TODO

Ideas / planned work for this project.

## Data layer (blocking)

- [ ] **Fix the band-gap functional lookup against live Materials Project data** — the
      recorded-replay test (`tests/test_recorded.py`) fails: every record resolves its
      functional to `"unknown"`, so the tasks-endpoint lookup does not match what the API
      actually returns. Items below in this section are blocked on this.
- [ ] **Commit the recorded live fixtures** under `tests/recorded/` (108 files across
      Materials Project, OpenAlex, OQMD, PubChem) once the replay test passes.
- [ ] **Finish the re-record within source budgets** — the 2026-09-10 re-record (tighter universe,
      2,651 formulas, 7,953 formula-source requests) stopped early: OpenAlex now meters a daily
      per-IP budget of $0.10 on the free tier, i.e. 100 requests at $0.001 each, resetting at
      midnight UTC; once spent it answers 429 with `Retry-After` ≈ 22 h. `mailto` no longer buys
      anything (verified 2026-09-10 with and without it). OQMD returned 429 at four concurrent
      workers but recovered within a minute. The full warm needs ~2,650 OpenAlex requests
      (≈ $2.65 prepaid). Options: an OpenAlex API key with prepaid credit, spreading the OpenAlex
      warm over ~27 days (the cache skips fetched formulas, so repeated `warm-cache` runs resume),
      or dropping per-formula literature counts from the warm and fetching them on demand.
- [ ] **Per-source concurrency and rate limits** — `fetch_workers` is one number for all three
      formula sources. OQMD 429s at four workers; OpenAlex's budget makes concurrency moot. Make
      the worker count and a requests-per-second cap configurable per source.
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
