# Ranking decisions: what was set after seeing data, and what it moved

A ranking is a stated preference over public numbers. Some of the numbers in `config/default.yaml`
were chosen before any data was seen and some after, and a reader is entitled to know which,
in what order, and what each one did to the list. This is that record. Every entry names the
trigger, the change, the effect on the live ranking, the reason, and where the reason is
written in the repository. The last section lists what was deliberately *not* changed.

The evaluation report (`docs/live-evaluation.md`, check 6) shows how the ranking behaves when
every one of these settings is moved by a lot. That table, not this narrative, is the answer to
"is it overfit".

All dates are 2026-09-10; the project was built in one stretch and the sequence is what matters.

## Set before any data, on the synthetic fixture

**Missing-data policy: renormalize → no_credit.** Trigger: the known-answer check on the
fixture put five candidates with *no* dielectric value above HfO2, because renormalising over
the criteria that have data lets an unknown criterion help a candidate. Effect: a candidate can
no longer outrank another by having less data. Reason in the design note §5 and the yaml comment.
`renormalize` is kept as an option and the sensitivity table runs it.

**Dielectric curve 8→30 and band-gap saturation at 5.5 eV effective.** Trigger: the same
fixture check. Reason: for a gate stack k of 20 to 30 is the useful range. Recorded in the yaml
comment. The calibration set was four compounds, which is why the profiles exist.

## Set after the first live warm

**Self-check windows.** Trigger: on live data HfO2 was 6th and Al2O3 16th of 497, and the check
had required both in the top five. Change: HfO2 must be in the top ten; Al2O3 is a workhorse
(must be above the median) but not a leader; two of four workhorses must be in the exploratory
top 25. Reason: Al2O3's k of 10 sits at the bottom of the dielectric preference, so no profile
that weights k can put it in the top five, and the check was demanding a contradiction. Recorded
in the yaml comment under `selfcheck`. This is a relaxation of the check, made after seeing the
result, and it is the first of four such relaxations listed here.

**INCONCLUSIVE below 90% retrieval.** Trigger: the check failed on a half-warmed cache for a
reason that had nothing to do with ranking (the workhorses' cross-checks were never fetched).
Change: the check measures retrieval completeness first and refuses to judge ranks below the
floor. Reason in the design note §4. A relaxation in form; in substance it stops the check from
lying.

**Universe rescope and on-demand formula sources.** Trigger: 7,124 materials at the original
bounds, most of them hypothetical polymorphs, and a warm that took days. Change: observed
structures only, a dielectric-minded cation list, and OQMD/OpenAlex/PubChem fetched per query
for the ranked pool. Effect on ranking: the universe changed, not the scores. README, *Notes from
the first live warm*.

## Set after the live ranking was read

**Polymorph grouping.** Trigger: SrZrO3 twice in the exploratory top five. Change: one row per
compound. Effect: no score changes; 497 passing materials become 317 compounds. Design note §5.

**Literature saturation 50 → 500 thin-film works.** Trigger: measuring the top 25 showed four
of six criteria at their maximum for most candidates, so the order was being decided by 0.02
gaps on one curve; and the literature curve gave HfO2 (6,975 works) and LaScO3 (29) the same
score. Effect: HfO2 5th → 2nd; SrHfO3 2nd → 3rd; LaScO3 3rd → 5th. Reason: the brief says
"prefer public evidence" and the curve was erasing it. Yaml comment, README ranking section.
**This is the change most open to the charge of tuning**: it was made after seeing HfO2 fifth,
and it moved HfO2 up. The defence is that the reason is the brief's own words and that the
sensitivity table shows the shortlist with the saturation back at 50.

**Tiers (tie band 0.04).** Trigger: the same measurement. Change: candidates within 0.04 of a
tier's leader share the tier and the order inside it is stated to be arbitrary. Effect: no score
changes. Reason: the inputs do not support an order at that resolution. README ranking section.

**Evaluation known-answer check defers to the configured self-check.** Trigger: the evaluation
report carried its own, stricter windows (Al2O3 in the top five) that disagreed with the
self-check and failed on live data. Change: one set of windows. A relaxation; the third.

## The interface criterion

**Interface criterion, weight 0.15.** Trigger: the same measurement (the criteria stop
discriminating at the top) and the observation that what separated HfO2 in practice was that it
does not react with silicon. Change: the Hubbard & Schlom (1996) hull screening as a seventh
criterion. Effect: HfO2 2nd → 2nd, LaAlO3 1st → 1st, SrHfO3 3rd → 3rd; CaZrO3 6th → 10th,
SrZrO3 9th → 19th, Ta2O5 24th → 99th. The criterion moved the reactive oxides and nothing else.
Design note §5, yaml comment, README ranking section. The weight, 0.15, was chosen to equal the
dielectric weight and not tuned; the sensitivity table halves and multiplies it.

**Interface tolerance 0.05 eV/atom.** Trigger: the fixture self-check failed because ZrO2 at
−0.07 eV/atom against Si scored 0.65 and fell to 12th of 21. Change: reactions less exothermic
than 0.05 eV/atom count as none. Reason: DFT formation energies carry errors of that size (the
same calculation under GGA gives ZrO2 −0.03), and Hubbard & Schlom classed ZrO2 stable. Yaml
comment. **The second change most open to the charge of tuning**: it was added when a test
failed and its effect is to protect ZrO2. The sensitivity table runs the tolerance at 0 and at
0.10.

**Self-check accepts a drop the interface criterion explains.** Trigger: on live data Ta2O5 fell
to 160th of 317, below the median, and the check failed. Change: a workhorse below the median
passes if its interface record shows a reaction past the tolerance, and the check prints the
reason. Reason: Ta2O5 reacts with Si (−0.30 eV/atom, forming SiO2 and tantalum silicides), which
is why it is a capacitor dielectric on TiN and not a gate oxide; a Si-substrate triage that
ranked it high would be wrong. The fourth relaxation.

## Not changed

**Dielectric saturation at 30**, despite HfO2 at fifth under the earlier literature curve.
Setting it to 20 puts HfO2 first; setting it to 25 puts HfO2 third. It stayed at 30 because the
reason beside it (k of 20 to 30 is the useful gate-stack range; larger k brings smaller gaps and
paraelectric instability) is a physical argument and "the list would look more familiar" is not.

**The workhorse list** (HfO2, ZrO2, Al2O3, Ta2O5), despite Ta2O5 now sitting at 99th. Removing
it would have hidden the criterion's most informative result.

## What the sensitivity table says

Check 6 of the evaluation (`docs/live-evaluation.md`) moves every weight to half and to one and a
half times its value, and moves each parameter set after seeing live data past its original
value: the literature saturation back to 50 and up to 5,000, the interface tolerance to 0 and to
0.10, the dielectric saturation to 20 and to 40, the tie band to 0.02 and 0.08. Twenty-two
perturbations. The result on the live cache:

- The base tier 1 (LaAlO3, HfO2, SrHfO3) is reproduced exactly in 7 of the 22. The tier
  *boundary* is sensitive: LaScO3, ZrO2 and SiO2 join or leave it as the settings move.
- The three members never fall below seventh. Worst ranks: LaAlO3 2, HfO2 5, SrHfO3 7. ZrO2
  never falls below 9, Al2O3 below 12.
- With the literature saturation back at its first value, HfO2 is 4th and the shortlist is the
  one the write-up describes as "before". With the interface tolerance at 0, ZrO2 is 7th. Neither
  parameter decides who the leaders are; each decides their order inside the top seven.
- The rejected missing-data policy (`renormalize`), shown for contrast and not counted, puts
  SrHfO3 at 61 and ZrO2 at 63. That is the failure mode the design note §5 describes, reproduced.

The honest reading: the settings decide the order among the top seven and not who is in it, and
the tiers exist because the order among them is not something the inputs can support.

## What would settle it

A validation set the tool was never tuned against: an independently published list of oxides
judged stable or unstable in contact with silicon. Agreement with such a list is evidence;
agreement with the four workhorses used to set the windows is not. That comparison is the next
entry here.
