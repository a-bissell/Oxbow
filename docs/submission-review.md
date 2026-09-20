# Submission review follow-up

The checkout was `3b82b5648c2d0b2361b37ed9b032161383f91b13`, with no local changes and no applicable AGENTS.md. Before edits, `python -m pytest` passed all 374 tests.

Reproduced before editing:

- `numeric_guard('The band gap is 30 eV', {'band_gap': 5.6, 'dielectric': 30})` returned true.
- An injected model observation claiming complete safety based on a fictional Smith study was accepted when `evidence_fields` was `['hazard']`.
- Both review citation requests returned `proceed=False`.
- Candidate detail used `https://doi.org/${w.doi}`, although stored identifiers can already be DOI URLs. The PI summary contained no candidate source links.
- The high/medium/low label came from scoring-data coverage, capped for missing/unretrieved criteria. Disagreement and corrected-gap caveats already existed and are preserved.

## Changes and limits

Production refutation now uses deterministic rules only; legacy model configuration still loads. The retained model-selection helper accepts existing caveat codes only, and is not invoked by the production pipeline. Conversation is labelled interpretation before streaming and in completed responses. It is not semantically verified. The numeric inventory diagnostic cannot certify a statement, and excludes user turns and failed tool outputs; tool output can itself echo request values, so inventory membership still means only token presence.

Canonical numerical statements bind candidate ID and formula, property, exact value, unit and source provenance. The strict statement guard accepts only those rendered statements, not arbitrary prose or numeric bags. This is traceability to a record, not independent verification of the underlying science. Source errors and approximate calculations remain possible.

The guard permits evidence searches and recognizes local negations of fabrication instructions. Active fabrication and uncertainty-suppression instructions remain blocked, including alongside legitimate searches. This is a rule-based intent guard, not a complete natural-language understanding system. No lab-action or private-data tools were added.

DOI normalization accepts bare identifiers and DOI resolver URLs, returning no link for malformed IDs. Candidate property source links and literature matches are separate in the standalone summary and candidate view. Search matches are not claimed to support a property. DOI syntax validation does not prove existence or relevance.

Public coverage labels changed without changing scoring arithmetic, serialized legacy field names, deployment files or ranking thresholds. Existing scientific caveats remain independent of coverage.

## Verification

- Python regression tests cover misattributed properties/candidates/units/provenance, invented citations, qualitative assertions, valid attributed statements, conversation streaming labels, local negation and mixed requests, malformed DOIs, rendered PI summaries and audit exports.
- `cd ui && npm run test:render` renders the actual candidate-detail React tree and checks bare/full/malformed/missing DOI cases, property provenance, literature qualification, corrected gaps and data-coverage labels. It uses local synthetic fixtures and no model/provider calls. Set `PYTHON` if the Python environment is not named `python`.
- `cd ui && npm run build` rebuilds the committed bundle used by Python installs.

The completed brief was incorporated: substrate statements report bulk-hull energy and the configured tolerance, and phase/kinetics limitations remain explicit. Scientific thresholds and weights were unchanged. The default disabled retrieval-refusal threshold and the retrospective status of the Hubbard–Schlom benchmark are now documented consistently.

Browser screenshot inspection was unavailable: local serving was denied by the sandbox, and the browser policy blocked file URLs. Verification used the actual React-rendered HTML and export assertions; no visual screenshot review is claimed.

Work remained on the existing dedicated `bugfix/integrity_update` branch. No hosted deployment changes or new public-source retrieval were performed.

Final checks: 416 Python tests passed (one existing Starlette deprecation warning); Ruff lint/format checks, `git diff --check`, frontend build and React-render assertions passed. Fixture and recorded-response PI-summary/audit outputs were inspected; recorded replay supplied 15 DOI links in the PI summary and 114 in the audit without live retrieval. The retrospective benchmark remained 21/21 hard and 10/11 soft agreements. Golden ranking tests retain all numerical assertions; only rationale and interface-label wording are excluded from that historical text snapshot and covered by dedicated rendering tests.
