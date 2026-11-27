---
layout: default
title: "CFS Gate 4: Multi-Rotation Coherent Folding - A Receipted Null"
---

[&larr; Back to Home](index.html)

# CFS Gate 4: Streaming Multi-Rotation Coherent Dedispersion and Narrow-Plane Chromatic Phase Folding

*Project `guppi_gate4`. Receipts document `run/receipts.json`, collector commit `3793edb`. Every number below is read from that document by the page generator; none is typed by hand. Licensed under the Apache License, Version 2.0.*

## Tier 1: The High-Level Picture (Plain-English Intuition)

A pulsar is a lighthouse: a rotating beam sweeps across the sky, and every pass of the beam deposits a small amount of extra power at one point in the rotation cycle. Radio telescopes record that power as millions of raw samples per second, smeared two ways &mdash; the interstellar plasma drags the low frequencies later than the high ones (**dispersion**), and the beam itself is narrow, so the pulse lands in only one or two time bins. The fix is arithmetic: undo the plasma delay (**coherent dedispersion**), then stack many rotations so the pulse adds up while the noise averages down.

Gate 3 had 1.496 rotations of source and reached `R_plane` z = 4.3178 &mdash; short of the pre-registered bar `z_crit` = 6.0. Gate 4 bought 8.0x more data (128 blocks, 11.968 rotations) and asked one question: does folding coherently across rotations lift the statistic over the bar?

**It does not, and that answer is the deliverable.** The deepest prefix reaches `R_plane` z = 4.7862, still 1.2138 below the bar. The bar was never lowered and the run was never re-selected. What makes this a measurement rather than a shrug is the evidence around it:

- The noise floor falls by 3.05x over the same span, so the averaging machine works; the pulse peak excess *shrinks* by 0.3634x, so the pulse is not adding across rotations.
- 11 of 11 individual rotations were audited one at a time and every one sits inside its own extreme-value noise ceiling (`all_within_2sd_of_ceiling` = yes).
- Blank sky (41 dispersion trials) and 16 phase-randomised surrogates produced 0 + 0 false locks.
- A blind period search over 1,807 trial periods found its best peak at z = 5.4725, while six phase-scrambled nulls of the same bytes reach z = 5.4260 &mdash; the search found nothing the null cannot produce.
- The statistic is immune to the single-channel interference that swamps total intensity: a 200-SNR tone in one frequency plane moves `R_plane` by -0.001302 &mdash; no excess at all &mdash; and Stokes-I by 2,679,610 sigma.

The gates were then shown to be capable of failing: each of the five nodes has a mutant arm that perturbs one input and exits nonzero. A gate that cannot fail is decoration. [Ten governance laws that make this reproducible](omp-governance.html).

## Tier 2: Systems Engineering, Receipts and Honest Boundaries

### 1 Registered constants

Registered before the campaign and supplied by the harness, never by an engine receipt (battery check N11).

| constant | value |
|---|---|
| `beacon_full_depth_z_min` | 8.000000 |
| `chunk_blocks` | 8 |
| `dm` | 10.922000 |
| `dm_trials` | 41 |
| `off_min_blocks` | 32 |
| `on_min_blocks` | 64 |
| `parity_tol` | 0.001000 |
| `rfi_duration_ms` | 2.000000 |
| `rfi_plane` | 4 |
| `rfi_plane_excess_z_max` | 1.000000 |
| `rfi_snr` | 200.000000 |
| `rfi_stokes_excess_z_min` | 1,000.000000 |
| `rss_ceiling_mib` | 4,096.000000 |
| `z_crit` | 6.000000 |

### 2 Delivered bytes and remote-header audit (CFS-G4-01)

Slices are assembled from a byte-range prefix plus downloaded extension segments; the prefix must be bit-exact against the Gate 3 slice, and the block walk must be misalignment-free.

| slice | header src | blocks | expect | tail | GiB | span_s | prefix blk | ext blk | size ok | directio ok | header ok | prefix bit-exact | pass | sha256 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `ON_J0034_x128` | PSR_J0034-0721 | 128 | 128 | 0 | 15.766 | 11.2855 | 16 | 112 | yes | yes | yes | yes | yes | c51de81a767d |
| `OFF_ATLAS_x128` | DIAG_3I_ATLAS | 128 | 128 | 0 | 16.001 | 11.4532 | 8 | 120 | yes | yes | yes | n/a | yes | d8e484d91b59 |

### 3 Depth scaling of the coherent plane statistic (CFS-G4-03)

Curve over 8 prefixes of the same ON slice, identical engine settings (`dm` = 10.922, `period` = 0.9429509946, 8 planes, 0.5 ms bins, streaming chunks of 8 blocks).

| blocks | rotations | span_s | `R_plane` z | Stokes-I z | off-pulse sd | peak | peak RSS MiB |
|---|---|---|---|---|---|---|---|
| 16 | 1.4960 | 1.4107 | 4.3178 | 4.3501 | 0.003643 | 0.33194 | 1,761.2 |
| 32 | 2.9921 | 2.8214 | 3.6172 | 4.3219 | 0.002442 | 0.32510 | 1,761.0 |
| 48 | 4.4881 | 4.2321 | 4.3025 | 3.5208 | 0.002014 | 0.32497 | 1,760.4 |
| 64 | 5.9841 | 5.6427 | 3.6386 | 3.4627 | 0.001728 | 0.32263 | 1,760.8 |
| 80 | 7.4802 | 7.0534 | 3.5576 | 3.4368 | 0.001539 | 0.32184 | 1,928.6 |
| 96 | 8.9762 | 8.4641 | 3.5980 | 5.6622 | 0.001393 | 0.32139 | 2,310.1 |
| 112 | 10.4722 | 9.8748 | 4.3657 | 5.9370 | 0.001284 | 0.32198 | 2,693.6 |
| 128 | 11.9683 | 11.2855 | 4.7862 | 5.6926 | 0.001194 | 0.32210 | 3,071.2 |

**Reading the curve.** Depth gain 1.1085, where coherent addition across the 8.0x deeper span would deliver 2.8285 (square root of the rotation ratio); off-pulse noise shrinks 3.0502; peak excess falls from 0.01573 to 0.00572 (ratio 0.3634). The profile is being averaged correctly while the peak fails to add &mdash; the signature of emission that is nulled by multi-rotation folding, not strengthened by it.

### 4 Ceiling arithmetic: what the bar means under multiplicity

Every margin below is `z_crit` minus an observed or expected extreme value, computed by `src/make_receipts.py` from receipt fields (formula column verbatim).

| margin | value | formula |
|---|---|---|
| `curve_max_margin_z` | 1.2138 | `z_crit - g03_scaling.ceiling_context.curve_max_R_z` |
| `ceiling_single_margin` | 2.5629 | `z_crit - g04_null.null_ceiling_single_trial.expected_max_z` |
| `ceiling_sweep_margin` | 1.3504 | `z_crit - g04_null.null_ceiling_over_sweep.expected_max_z` |
| `null_ceiling_margin` | 2.1507 | `z_crit - g03_per_spin.null_ceiling.z_max` |
| `off_margin` | 1.7058 | `z_crit - g04_null.off_sweep.moments.max` |
| `surrogate_margin` | 1.6665 | `z_crit - g04_null.surrogates.moments.max` |
| `desync_on_margin` | 1.2138 | `z_crit - g04_null.on_fold.R_z` |
| `beacon_16_margin` | 0.8371 | `z_crit - g04_null_mutant.beacon.z_16_blocks` |
| `beacon_full_excess` | 7.8325 | `g04_null_mutant.beacon.z_64_blocks - z_crit` |

Extreme-value context for the curve maximum (`g03_scaling.ceiling_context`):

| field | value |
|---|---|
| `curve_max_R_z` | 4.786214 |
| `expected_max_z` | 3.436943 |
| `gumbel_p_max_exceeds_curve_max` | 0.023876 |
| `n_bins` | 1,885 |
| `sd_max_z` | 0.362433 |

### 5 Per-rotation stratification against the null ceiling (CFS-G4-03b)

Each rotation is folded on its own and classified by the rule recorded in the receipt: a spin is `active` if its own `R_plane` z reaches the Gate 3 single-slice level, otherwise `nulled`. Each class is then scored against the Gumbel ceiling for its own trial count.

| classification rule | spins audited | spins unaudited |
|---|---|---|
| `per-spin R_z >= 4.161809` | 11 | 0 |

| class | rotations | coverage | `R_plane` z | Stokes-I z | peak | off mean | off sd | peak phase ms | phase resultant | note |
|---|---|---|---|---|---|---|---|---|---|---|
| active | 0 | 0.0000 | n/a | n/a | n/a | n/a | n/a | n/a | n/a | class empty |
| nulled | 11 | 0.9191 | 3.1797 | 4.9487 | 0.32033 | 0.316377 | 0.001243 | 535.0 | 0.4076 |  |

Per-spin `R_plane` z, one row per audited rotation (the exact receipts behind the class assignment):

| spin index | per-spin `R_plane` z |
|---|---|
| 0 | 3.6737 |
| 1 | 3.4825 |
| 2 | 3.3644 |
| 3 | 3.7125 |
| 4 | 3.2980 |
| 5 | 3.7096 |
| 6 | 3.2700 |
| 7 | 3.6938 |
| 8 | 3.8493 |
| 9 | 3.1738 |
| 10 | 3.7911 |

| field | value |
|---|---|
| `active_z` | 4.161809 |
| `all_within_2sd_of_ceiling` | yes |
| `ceiling.a_n` | 3.273829 |
| `ceiling.b_n` | 0.282588 |
| `ceiling.expected_max_z` | 3.436943 |
| `ceiling.n_bins` | 1,885 |
| `ceiling.sd_max_z` | 0.362433 |
| `gumbel_p_max_exceeds_z_max` | 0.274266 |
| `n_spins` | 11 |
| `peak_phase_resultant_all` | 0.407644 |
| `rayleigh_expected_resultant` | 0.377888 |
| `rayleigh_p_uniform_phases` | 0.160750 |
| `z_max` | 3.849264 |
| `z_mean` | 3.547142 |
| `z_min` | 3.173767 |
| `z_sd` | 0.236198 |

**The `active` class is empty**: none of the 11 audited rotations reaches the Gate 3 single-slice level on its own, and the class holding all of them (`nulled`, 0.9191 coverage) aggregates to 3.1797 - below the ceiling expectation 3.4369 for its own bin count.

Interpretation: the audited rotations are individually indistinguishable from noise maxima (Gumbel p = 0.2743 that the largest is that extreme; Rayleigh p = 0.1607 for uniform peak phases). No rotation carries a lock that survives its own multiplicity correction.

### 6 Blind period search on the full span (CFS-G4-03c)

No ephemeris: every trial period in a 1% band, coverage-complete bins only.

| field | value |
|---|---|
| `trials` | 1,807 |
| `bin_stats.trials` | 12,674 |
| `bin_stats.sliver_bins_dropped` | 12,653 |
| `rotations` | 11.9683 |
| `span_s` | 11.2855 |
| `search_z_mean` | 3.4490 |
| `search_z_std` | 0.3430 |
| `search_z_max` | 5.4725 |
| `expected_max_z_gaussian` | 3.8728 |
| `catalogue_p0_z_full` | 4.7857 |
| `catalogue_p0_H_full` | 12.3990 |
| `null_best_z_max` | 5.4260 |
| `null_best_z_sd` | 0.2505 |
| `peak_rss_mib` | 795.0039 |
| `wall_s` | 815.9748 |

Top five trial periods by z, and the six phase-scrambled null controls run through the same search:

| trial period s | z | H | peak bin |
|---|---|---|---|
| 0.9495923770 | 5.4725 | 8.263 | 353 |
| 0.9399331337 | 4.8790 | 11.795 | 1,130 |
| 0.9411966671 | 4.8321 | 10.515 | 1,362 |
| 0.9489136193 | 4.7970 | 8.209 | 68 |
| 0.9489031769 | 4.7357 | 8.320 | 68 |

| null seed | best period s | best z | best H |
|---|---|---|---|
| 1,000 | 0.9408625095 | 4.9258 | 14.139 |
| 1,001 | 0.9369988122 | 4.9794 | 15.926 |
| 1,002 | 0.9360485515 | 4.9924 | 17.231 |
| 1,003 | 0.9410818005 | 4.7191 | 16.354 |
| 1,004 | 0.9351713878 | 4.7692 | 17.150 |
| 1,005 | 0.9468773464 | 5.4260 | 12.148 |

The catalogue period scores 4.7857; the search maximum 5.4725 is 0.0465 above the best of six nulls whose spread is 0.2505 &mdash; inside the null scatter. There is no period at which these bytes beat their own scrambled version. Both the search maximum and the null maxima sit far above the analytic Gaussian expectation for the largest of 1,807 trials (3.8728): trial periods are correlated, so the analytic ceiling is optimistic and the empirical scrambled null is the only honest counterparty.

### 7 Single-plane RFI immunity (CFS-G4-03d)

A 200-SNR tone is injected into plane 4 for 2.0 ms, once and once per rotation, at the pulse phase.

| arm | `R_plane` z | Stokes-I z | peak phase ms |
|---|---|---|---|
| `baseline` | 4.7862 | 5.6926 | 776.0 |
| `tone_single_burst` | 4.7882 | 223,363.3848 | 776.0 |
| `tone_every_rotation` | 4.7849 | 2,679,616.1380 | 776.0 |

Plane excess over baseline: 0.001969 (single burst) and -0.001302 (every rotation). Stokes-I excess: 223,358 and 2,679,610. A single bad channel is worth ~2.7 million sigma in total intensity and nothing in the chromatic statistic.

### 8 Null counterparty battery (CFS-G4-04)

Blank-sky slice `OFF_ATLAS_x128` swept over 41 dispersion trials; the ON slice is its own phase-randomised surrogate (16 trials, all spin-desynchronised). Battery clean: yes.

| id | status | assertion | observed |
|---|---|---|---|
| N1 | ok | every OFF DM trial R_z < z_crit=6.0 | trials=41 max_R_z=4.2942 locks=0 |
| N2 | ok | no OFF trial clears the multiple-trial extreme-value ceiling | max_R_z=4.2942 ceiling=4.6496+2sd=5.3744 |
| N3 | ok | OFF sweep mean z sits on the single-trial noise ceiling | mean=3.4988 expected=3.4371 sd=0.3624 |
| N4 | ok | OFF trial peak phases uniform (Rayleigh) | R=0.1036 p=0.6443 n=41 |
| N5 | ok | every spin-desynchronised surrogate R_z < z_crit=6.0 | trials=16 max_R_z=4.3335 all_desynced=True |
| N6 | ok | surrogate mean z sits on the analytic noise ceiling | mean=3.4313 sd=0.3552 expected=3.4371 |
| N7 | ok | surrogate peak phases uniform (Rayleigh) | R=0.2651 p=0.3248 n=16 |
| N8 | ok | honest ON fold claims no lock: below z_crit, or inside the surrogate null | on_z=4.7862 z_crit=6.0 surr_mean=3.4313 surr_sd=0.3552 excess_over_surr_sd=3.8142 sweep_ceiling=4.6496+-0.3624 excess_over_sweep_sd=0.3771 |
| N9 | ok | OFF / surrogate / ON depths, period and fold bin matched | off_blocks=128 surr_blocks=128 period=0.9429510 fold_ms=0.5 |
| N10 | ok | peak RSS of every engine leg (all VmHWM fields) < 4096 MiB | peak_rss_mib=3612.9 off=3612.9 surr=3119.6 |
| N11 | ok | no input receipt carries a threshold field | none |

### 9 The RSS ceiling, and the one time it broke

The registered memory ceiling is 4,096 MiB, checked against `VmHWM` for **every** engine leg, not just the ingest chunk. Post-fix maxima: scaling leg 3,071.2 MiB (headroom 1,024.81), full-128 parity leg 3,118.7 MiB (headroom 977.25), counterparty battery 3,612.9 MiB (headroom 483.11).

The first positive run of the counterparty arm did **not** pass its own battery: peak 4,579.1 MiB i.e. 483.1 MiB **over** the 4,096 MiB ceiling (`rss_over_ceiling_mib` = -483.11), because `fold_series` kept every dedispersed plane alive while the accumulators ran. The fix released each plane as its contribution landed &mdash; buffer lifetimes only, no arithmetic change &mdash; and the ceiling was **not** raised. The superseded receipt is quarantined at `run/stale_pre_patch/g04_null.json` (`sha256` `02f07bc934669bc4`) and the 51 arm receipts the fix invalidated are indexed with their hashes in `run/stale_pre_patch/manifest.json`, so no `--reuse` path can hand back a pre-fix number.

### 10 Mutant arms: the gates can fail

Each mutant perturbs exactly one input and must break the corresponding gate.

| arm | node | arm | gate command | passed | failed | checks | rc | commit |
|---|---|---|---|---|---|---|---|---|
| `g01_mut` | CFS-G4-01 | mutant | `bash harness/g01_gate.sh --mutant-seam-offset 512` | 14 | 0 | 14 | 1 | 0bc5edb |
| `g01_pos` | CFS-G4-01 | positive | `bash harness/g01_gate.sh` | 14 | 0 | 14 | 0 | 0bc5edb |
| `g02_mut` | CFS-G4-02 | mutant | `bash harness/g02_gate.sh --mutant-wrong-p0` | 8 | 2 | 10 | 1 | 0bc5edb |
| `g02_pos` | CFS-G4-02 | positive | `bash harness/g02_gate.sh` | 10 | 0 | 10 | 0 | 0bc5edb |
| `g03_mut` | CFS-G4-03 | mutant | `bash harness/g03_gate.sh --mutant-phase-scramble` | 15 | 2 | 17 | 1 | 0bc5edb |
| `g03_pos` | CFS-G4-03 | positive | `bash harness/g03_gate.sh` | 24 | 0 | 24 | 0 | 0bc5edb |
| `g04_mut` | CFS-G4-04 | mutant | `bash harness/g04_gate.sh --mutant-inject-weak-beacon` | 24 | 0 | 24 | 1 | 0bc5edb |
| `g04_pos` | CFS-G4-04 | positive | `bash harness/g04_gate.sh` | 19 | 0 | 19 | 0 | 0bc5edb |
| `g05_mut` | CFS-G4-05 | mutant | `bash harness/g05_gate.sh --mutant-row-swap` | 1 | 0 | 1 | 1 | 0bc5edb |
| `g05_pos` | CFS-G4-05 | positive | `bash harness/g05_gate.sh` | 20 | 0 | 20 | 0 | 0bc5edb |

One arm needs reading, because a clean tally and a nonzero exit look contradictory: `g04_mut` injects one weak coherent dispersed beacon; the battery it guards then breaks (N1-N3) while the beacon calibration checks M0-M4 pass - invisible at the 16-block Gate 3 prefix depth, a hard lock at 64, locked at the injected DM. The gate exits 1 with 0 failed checks because the claim it protects ("no lock in these bytes") is false on that volume: the exit code is the guard, not the tally. See `logs/g04_mut.log`.

### 11 Closure auditors

| auditor command | rc | verdict |
|---|---|---|
| `python3 -W ignore /home/snoop/.omp/agent/tools/entail_check.py REPORT.md --root . --evidence run/receipts.json --json` | 0 | entailed=66, contradicted=0, unbound=0 |
| `python3 -W ignore /home/snoop/.omp/agent/tools/entail_check.py perm-record.md --root . --evidence run/receipts.json --json` | 0 | entailed=15, contradicted=0, unbound=0 |
| `python3 -W ignore src/verify_rows.py` | 0 | formula=0, stale=0, unbound=0 |
| `python3 -W ignore /home/snoop/.omp/agent/tools/plan_check.py --plan DAG.json` | 0 | done=5, nodes=5, stale=0, violation=0 |
| `python3 -W ignore /home/snoop/.omp/agent/tools/spine_check.py --check-seam` | 0 | violations=0 |
| `python3 -W ignore src/verify_rows.py --report <empty.md> --perm <empty.md> --state /home/snoop/.omp/agent/STATE.md --state-glob CFS-G4` | 0 | unbound=0 |
| `python3 -W ignore src/verify_rows.py --report <empty.md> --perm <empty.md> --state /home/snoop/.omp/agent/PERM.md --state-glob CFS-G4` | 0 | unbound=0 |

### 12 Receipt index

| receipt | sha256 | mtime_utc | bytes | path |
|---|---|---|---|---|
| `g01_remote_probe` | 704635e91eff1faa | 2026-11-26T18:16:05Z | 1,724 | `run/receipts/g01_remote_probe.json` |
| `g01_slices` | d3a142ab2c356454 | 2026-11-26T22:00:33Z | 151,926 | `run/receipts/g01_slices.json` |
| `g01_slices_mutant` | df5831ed9ad371e0 | 2026-11-27T17:41:57Z | 1,783 | `run/receipts/mutant/g01_slices.json` |
| `g02_parity` | 4d97d1cb33fd2940 | 2026-11-27T17:47:31Z | 2,105 | `run/receipts/g02_parity.json` |
| `g02_parity_mutant` | 42531328c7eef5ad | 2026-11-27T17:52:21Z | 2,116 | `run/receipts/mutant/g02_parity.json` |
| `g03_per_spin` | 2fc7bd8d68bca032 | 2026-11-27T17:52:23Z | 2,630 | `run/receipts/g03_per_spin.json` |
| `g03_period_search` | ec6a42d76ebbc566 | 2026-11-27T18:05:59Z | 4,233 | `run/receipts/g03_period_search.json` |
| `g03_rfi` | 718771ef66b824d8 | 2026-11-27T17:52:23Z | 618 | `run/receipts/g03_rfi.json` |
| `g03_scaling` | 81dbf8d562c4d030 | 2026-11-27T17:52:23Z | 5,607 | `run/receipts/g03_scaling.json` |
| `g03_scaling_mutant` | 52995288fd9be595 | 2026-11-27T18:06:00Z | 3,221 | `run/receipts/mutant/g03_scaling.json` |
| `g04_null` | 8e2314f6493b9b14 | 2026-11-27T18:34:47Z | 4,390 | `run/receipts/g04_null.json` |
| `g04_null_mutant` | a385412c02469d3e | 2026-11-27T19:07:05Z | 5,646 | `run/receipts/mutant/g04_null.json` |
| `g04_null_pre_fix` | 02f07bc934669bc4 | 2026-11-27T03:14:40Z | 4,402 | `run/stale_pre_patch/g04_null.json` |
| `g04_off` | 79f35931902c44ea | 2026-11-27T18:30:47Z | 8,320 | `run/receipts/_g04_off_sweep.json` |
| `g04_surrogates` | d32277fbcdd58bd6 | 2026-11-27T18:34:47Z | 4,636 | `run/receipts/_g04_surrogates.json` |
| `g05_closure` | n/a | n/a | 5,815 | `run/receipts/g05_closure.json` |
| `gate3_reference` | 2c827bca681f255c | 2026-11-26T09:31:50Z | 51,421 | `../guppi_gate3/run/receipts/g04_realpulse.json` |
| `stale_manifest` | 806f9ed71e5a6f7d | 2026-11-27T04:05:33Z | 14,120 | `run/stale_pre_patch/manifest.json` |

### 13 What would have to change

Nothing in this run can be re-cut into a detection: the deficit is 1.2138 of `z_crit` at 128 blocks, the measured depth gain is 1.1085 against the 2.8285 coherent addition would need, and every audited rotation sits inside its own null ceiling. A crossing needs more collected time on source, a brighter source, or a beam/rotation regime where the emission is not nulled &mdash; none obtainable from these bytes. The honest output of Gate 4 is the stratification plus ceiling evidence, not a lock.

*Reproduce: `bash harness/g05_gate.sh` in `guppi_gate4` re-runs the auditors above; `python3 src/make_receipts.py --check` re-stamps every receipt hash in this table. This page is a build artifact: `python3 src/tools/make_docs_pages.py --receipts <receipts.json>` regenerates it, so edit the generator, not the page.*
