# Roadmap: from oxide triage to a general-purpose materials-triage system

Reference material, not a design argument. The design note says what Oxbow is; this file says
what it would take to drop the qualifier "instantiated for oxide dielectrics". Written
2026-09-14 from a code-level survey of the engine and one rung built end to end. The built rung
is parked on branch `claude/agentic-material-generation-51b271` (commit `97b7a26`) and is not
merged; the demo ships without it.

## The claim, and why it is not a count of profiles

Five profiles ship. Three are variants of one class (oxide dielectrics on silicon), one is that
class for a ferroelectric group, and one is a second class (thermal barrier coatings). A sixth
oxide profile that ranks another Materials Project property would add almost no evidence,
because it would exercise only the seam the second class already proved.

What earns the words "general purpose" is a class for each assumption the engine still bakes
in. The survey found six. Five are worth opening; one is deferred with its cost estimated.

| Assumption | Where it lives | Rung |
|---|---|---|
| A property comes from one of two Materials Project routes | `sources/properties.py` (the seam exists; proven once) | 3 |
| Every scoring curve is monotone (a ramp, or a ramp inverted by `prefer: low`) | `scoring/core.py`, the `figure_of_merit` branch of `score_components` | 4 |
| The figure of merit is one scalar | `Config.criteria()` is fixed at seven names | 5 (a workaround behind the seam, not a new seam) |
| There is always a substrate | `InterfaceConfig.substrate: str`, `DataLayer._interface`, `aggregate()` | 6 |
| The anion is oxygen | about fifteen sites, listed under rung 7 | 7 |
| Stability is Materials Project energy above hull | no seam at all | deferred |

Each rung is one pull request: one seam opened (or proven with real data) plus the one class
that cannot work without it, with fixtures, tests, recordings and docs. The order is cheap to
expensive. Every rung must keep `oxbow load-fixtures` then `oxbow serve` working offline, since
a reviewer runs the tool with no key.

The claim is cumulative. After rung 3: a new property is a provider, not an engine change.
After 4: the curve is a profile decision. After 5: a derived figure lives behind the provider
seam. After 6: a class need not have a substrate. After 7: the anion is a profile decision, and
the rename makes the package say so.

## What every class touches

The design note's section 6 lists three files. Building the second and third classes showed
the practice is nine items:

1. `config/profiles/<class>.yaml`: the `figure_of_merit:` block, substrate, weights (a class may
   zero the band gap), gates, universe bounds, the hazard elements it permits and flags, its
   refutation switches, terminology, and the self-check's request, workhorses and leaders. The
   template is `config/profiles/thermal-barrier.yaml`.
2. `oxide_triage/data/cation_allowlist_<class>.yaml`: the cation universe, in families with a
   rationale each.
3. A provider in `oxide_triage/sources/properties.py`, only if the property is not already
   served, registered there and in `PROPERTY_PROVIDERS` in `config.py`; a fetch and batched
   prefetch pair on the Materials Project client if the endpoint is new.
4. Fixture rows in `oxide_triage/data/fixtures/fixture_cache.json` for the workhorses, the
   property block, and the hull systems the substrate needs; a matching `cache.put` in
   `sources/fixtures.py`.
5. A recorded warm (`warm-cache --record tests/recorded --profile <class>`) when the route or
   the universe query is new, and a replay case in `tests/test_recorded.py`.
6. `tests/test_<class>.py`, modelled on `tests/test_thermal_barrier.py`.
7. The golden regression baseline only if the class joins the regression contract (opt-in by
   name in `tests/golden/capture_baseline.py`).
8. Profile tables in both READMEs, a results line in design note section 8, a limitations line
   in section 9, a suggested request in `server/app.py`, aliases in `compound_aliases.yaml`.
9. The live warm and self-check, with the rank line pasted into the profile's comments so the
   saturation points are argued from data a reader can reproduce.

## The ladder

### Rung 3: piezoelectric oxides for MEMS (the provider path). Built, parked.

No engine change. Materials Project's DFPT piezoelectric tensors, reduced by the source to the
largest longitudinal stress coefficient `e_ij_max` (C/m²), higher preferred, on silicon.
The route was one provider class, a fetch and prefetch pair on the client, and two registry
lines. The profile permits lead and flags it, because PbTiO3 is the end member of PZT; the gap
gate is 0.5 eV, which screens metals rather than narrow-gap insulators (ZnO reads 0.7 eV in
GGA). Workhorses PbTiO3, BaTiO3, LiNbO3, KNbO3, ZnO.

Live, 2026-09-14: 290 candidates, 185 passing, 26 with a tensor at the source. KNbO3 1st,
LiNbO3 4th, BaTiO3 5th, PbTiO3 22nd, ZnO 84th (it reacts with Si at -0.53 eV/atom on the hull,
which the self-check names as the reason). Ca3Ti2O7 and NaNbO3 take 2nd and 3rd on the
coefficient alone.

Two things the live warm found, both fixed on the branch:

- Materials Project now issues hashed material ids and prints formulas in its own element
  order (PbTiO3 as TiPbO3). The self-check matched workhorses by string and reported PbTiO3
  missing from the universe. The self-check and the compound alias table now match by
  stoichiometry through `formula.canonical`, with a test. The 2026-09-10 live cache has the
  old ids and cannot be layered under a new warm.
- OpenAlex's daily budget on the free key is about ten cents; one profile's on-demand pool
  spends it. A second profile's self-check the same day comes back inconclusive on literature
  gaps, which is what the thermal-barrier replay recording shows.

Before merging the branch: the README and design-note edits on it were machine-written and
should be redone by hand, per the project's convention for documents a reviewer reads; the
autodocs edits can stay. Size M.

### Rung 4: anti-reflection coating on silicon (the curve-shape seam)

Engine: `FigureOfMeritConfig` gains `curve: ramp | window` with `ideal_low` and `ideal_high`;
a validator requires `low < ideal_low <= ideal_high < high` and rejects an explicit `prefer`
with a window; `score_components` gets a third branch scoring 1 inside the ideal band and
falling linearly to 0 at `low` and `high`. The regression test proves ramp output is unchanged.

Class: refractive index `n` from the existing dielectric provider (the payload already carries
it), window 1.8 to 2.1 with 0 outside 1.5 to 2.5 (a quarter-wave layer on Si wants n near
1.97), band gap gate 3.0 eV for visible transparency. Workhorses SiO2, TiO2, Ta2O5, HfO2,
Al2O3, ZnO, with SiO2 and TiO2 expected to land low and the profile comment saying so. No new
provider, no recording. Size S engine, S class.

### Rung 5: gate-stack k·Eg (a composite figure behind the provider seam)

No engine change. A provider that reads `e_total` from the dielectric payload and the band gap
from the cached summary and returns their product as its single value, both parts in extras.
Robertson's trade-off: high-k oxides have small gaps. Workhorses HfO2, ZrO2, Al2O3, La2O3,
Y2O3. Documents the limit it works around: two independent weighted figures of merit stay out
of scope because `Config.criteria()` is seven names. Nothing new to fetch or record. Size S.

### Rung 6: bulk hard-magnet ferrites (the no-substrate seam)

Engine: `InterfaceConfig.substrate` becomes optional; `DataLayer._interface` returns a
not-applicable record without touching a hull; `aggregate()` leaves not-applicable components
out of coverage and the missing list (`DataStatus.NOT_APPLICABLE` already exists and is
handled at the component level). The existing rule that any missing criterion caps confidence
whatever its weight is deliberate and stays. Templates render "n/a" for the interface row.

Class: magnetization per volume from `/materials/magnetism/`, higher preferred, band gap and
interface weights zero. Two corrections from a live probe on 2026-09-14: CoFe2O4 comes back
with ordering "Unknown" and a full moment, so the provider must judge on the magnetization,
not the ordering label; and BaFe12O19 sits 0.055 eV/atom above the mixed-functional hull, so
the class needs a hull gate wider than 0.05 with the reason stated. Workhorses BaFe12O19,
SrFe12O19, CoFe2O4, Fe3O4, NiFe2O4. New provider, new recording. Size M engine, M class.

### Rung 7: fluoride UV optics (the anion seam)

Engine: `candidates.anion` (default "O", read-only for site overrides since it changes cache
keys) threaded through the universe query and key (the key gains the anion only when it is not
oxygen, so old caches keep theirs), the server-side exclusion list, the four local re-filters
(`assemble.py`, `fixtures.py`, `pipeline.py` twice, `server/app.py`), a `cations_of` helper
replacing the three cation-by-subtraction sites (`hazards.py`, `refute.py`, `elements.py`), a
config-driven hygroscopic table, and a class noun for the guard's scope check and the parser's
four `oxides?` alternations. About fifteen sites, all the same substitution.

Class: LiF, MgF2, CaF2, BaF2, LaF3 as workhorses; refractive index low, band gap high with a
6 eV gate; CaF2 substrate or none via rung 6; its own allowlist, hygroscopic table and aliases.
The design note already names fluoride UV windows as the class set aside for this reason.
Size L.

### Then: rename the package to `oxbow`

A dedicated pull request with no behaviour change, immediately after rung 7: `git mv
oxide_triage oxbow`, imports, pyproject scripts (`oxide-triage` kept as an alias for one
release), env prefix `OXBOW_*` with a one-time warning when the old names are read, MCP server
name, compose files and docs. Cache source names and bundle manifest keys do not move. The
repository name stays. Size M.

### Deferred: the stability seam

Stability is Materials Project energy above hull in the record, the gate, the curve, the
cross-check and the universe filter, with no provider seam. A battery cathode or electrolyte
class needs a grand-potential hull at fixed lithium chemical potential instead. That is a
second provider protocol mirroring the property one, a stability mode in config, and changes
in the data layer, both gate and score, the parser and every energy-above-hull string: the most
invasive of the six, size M to L. Named in the design note's limitations when rung 7 lands.

### Rung 8: an agent that writes the next class

Already written up in `autodocs/TODO.md` under "Material classes". A Claude Opus 5 agent
through the SDK tool runner with three tools (read a repository file, write one of the class
files, run the self-check), given a brief and the shipped profiles as worked examples, handing
back a pull request. It never writes under `scoring/`, `refute.py`, `guard.py` or `edges/`.
The held-out experiment regenerates thermal-barrier from the commit before it shipped. Each
rung above adds a file type it may write and removes an excuse when it fails, which is why it
comes last. Size L.

## Sequencing

| # | Pull request | Size | What it unlocks |
|---|---|---|---|
| 1 | Rung 3, piezoelectric (parked on the branch) | M | a provider exemplar with a diff |
| 2 | Rung 4, anti-reflection coating | S+S | a curve vocabulary |
| 3 | Rung 5, gate-stack | S | the composite pattern |
| 4 | Rung 6, ferrites | M+M | a null substrate (rung 7 uses it) |
| 5 | Rung 7, fluorides | L | the anion |
| 6 | Rename to `oxbow` | M | the name matches the claim |
| 7 | Rung 8, the agent | L | class generation |

## Verification, per rung

```
ruff check . && ruff format --check .
pytest
pytest tests/test_fom_regression.py tests/test_recorded.py
oxbow load-fixtures --profile <class> && oxbow selfcheck --profile <class>
oxbow warm-cache --profile <class> && oxbow selfcheck --profile <class>    # live; paste the rank line into the profile
oxbow eval                                                                  # the profile table shows the new row
oxbow serve                                                                 # /api/status lists the profile; its suggested request runs on fixtures
```

Plan one live warm per day around the OpenAlex budget. Recapture the golden baseline only if
the class is added to the regression contract.

## Out of scope, and why

- Electrochemical stability: deferred by decision, estimated above.
- Two independent weighted figures of merit: the criteria tuple is fixed; rung 5 is the workaround.
- Disabling the element cap, high-entropy alloys, solid solutions: Materials Project has no
  ordered entries for them; the universe key and the gate both assume a cap. YSZ already stands
  in as ZrO2 and PZT as PbTiO3.
- Composites, polymers, frameworks: no formula-keyed public thermodynamics; every source
  adapter assumes an inorganic formula.
- Renaming the repository: breaks the portfolio links, the hosted demo and the badges for no
  engine gain.
