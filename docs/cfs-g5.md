---
layout: default
title: "CFS Gate 5: The Promotion Seam - 63 RFI Locks, 0 Standing"
---

# CFS-G5: The Promotion Seam &mdash; 63 RFI Locks, 0 Standing

> *Every numeral here is read from `guppi_gate5`'s promotion seam, `run/receipts.json`: 2,427 rows, one per numeral, each carrying the receipt path it was sliced from. The vendored snapshot is `harness/vendor/guppi_gate5/run/receipts.json` (`content_sha256` `17bc9aeacf4066fd`&hellip;); the arbiter is `docs/PRE_REGISTRATION.md + amendments A2-A7; harness/g05_gate.sh is the arbiter`.*

## Tier 1 &mdash; what happened, in plain language

- **The claim is a promotion, not a discovery.** Gate 5 asks whether a 2-bit GUPPI backend can be promoted to a real coherent-fold search: 7 slices, 12.07 GiB, 6 of them 2-bit and 1 an 8-bit pulsar calibration slice, all read through the same engine.
- **The threshold was fitted, not typed.** `z_crit = 7.392` comes from an exponential tail fit on the null's own 1,200 draws (largest 5.7711), above the pre-registered floor of 6.
- **The search found 63 candidate locks &mdash; and kept none.** All 63 are RFI: each sits at a trial whose blank-sky window shows z&nbsp;=&nbsp;786.0187 at the same trial, and the 63 locks span only 383.88 Hz. **0 standing.**
- **A planted signal still survives that veto.** The drift beacon (0.06 Hz/s at 2.821 s, SNR 0.02) is recovered with an ON-minus-OFF z delta of at least 116.7043, while the same cell in blank sky reaches 0.9752.
- **One published control failed, and the failure is kept.** Amendment A7's surrogate-pooled fit gives z_crit&nbsp;=&nbsp;9.6962, above the 1.1&times; bar of 8.1312 &mdash; a recorded FAIL, not a re-tuned threshold.
- **The engine holds its ceiling.** Peak RSS 3,605 MiB against a 4,096 MiB bar on 12 workers, with the batched 2-D pass 7.009&times; faster than the reference.
- **The gate is 30/0 with 3 of 3 mutants fired across 78 mutant-arm checks.**

## Tier 2 &mdash; the receipts, in context

### 1 The seam: one row per numeral

The seam is the artifact a reviewer audits: 2,427 rows sliced from 49 receipts, named and derived by the rules below, so the page can be checked against the snapshot without a personal checkout.

| seam field | value |
|---|---|
| `gate` | `CFS-G5` |
| `project` | `guppi_gate5` |
| `n_rows` | 2,427 |
| `contract` | `docs/PRE_REGISTRATION.md + amendments A2-A7; harness/g05_gate.sh is the arbiter` |
| `content_sha256` | `17bc9aeacf4066fd2d1c96cb857a4f7cb57e9552601a168bb9ebac89d11ee721` |
| `index/sources` (receipts sliced) | 49 |

| index rule | text |
|---|---|
| `derived_rule` | `derived.* rows are arithmetic on the cited leaves, computed by src/receipt_index.py` |
| `gate_rule` | `gate.* rows are the counts src/gate_receipt.py parsed from the four harness logs; the receipt also pins the log digests, so one run is traceable to the byte text behind those counts` |
| `rows_rule` | `one row per numeric leaf of a source receipt; a numeric-only list contributes .len/.max/.min only; string leaves count only when the whole string is a number` |
| `value_rule` | `repr of the leaf, unrounded; no wall-clock field, so the file is byte-identical across regenerations` |

### 2 The threshold: z_crit from the null's own tail

The pre-registration fixes a floor of 6, a tail-fit level of 0.05, and an effective independent-draw count of 1,326 &mdash; 1,200 for the DM arm and 126 for the drift arm, because the arms are not equally correlated; the calibration record's own `n_eff` below is the DM pool it was fitted on. The window grid is 6 pointings &times; 8 windows of 8 blocks (1 &times; 64 for the drift arm).

| seam row | value |
|---|---|
| `pool/n_draws` | 1,200 |
| `pool/n_trials` | 150 |
| `pool/n_pointings` | 6 |
| `pool/windows` | 8 |
| `pool_stats/n` | 1,200 |
| `pool_stats/min` | 4.4081 |
| `pool_stats/median` | 4.589 |
| `pool_stats/p95` | 5.2615 |
| `pool_stats/max` | 5.7711 |

The fit is taken on the tail only &mdash; 60 draws from 5.2615 to 5.7711, exponential `a*exp(-b*z)` with `a`&nbsp;=&nbsp;22.5603 and `b`&nbsp;=&nbsp;3.4573. The empirical check closes the loop: 0 draws exceed the floor, 60 are expected under the fit, and 0 reach `z_crit` &mdash; the tail is thin exactly where the claim needs it thin.

| seam row | value |
|---|---|
| `floor` | 6 |
| `alpha` | 0.05 |
| `n_eff` | 1,200 |
| `zcrit` | 7.392 |
| `zcrit_all_fit` | 7.392 |
| `zcrit_off_fit` | 6.6552 |

### 3 The surrogate that failed the published bar

Amendment A7 asks whether a surrogate pool &mdash; 600 phase-randomised draws, largest 7.5911 &mdash; would have set a different threshold. It would: its own tail fit gives 9.6962, and the bar the amendment published is 1.1&times; the adopted `z_crit` = 8.1312. 9.6962 exceeds it, so the surrogate-pooled threshold is rejected and the adopted value stays 7.392. The surrogate pool itself is not quiet: 384 of its 600 draws clear the floor and 24 clear `z_crit`, which is the honest reason its tail fit sits high &mdash; randomising phase does not remove the RFI structure that inflates the tail.

### 4 The drift arm: why one pooled fit is not allowed

Pooling the drift arm's null draws gives 126 values with median 410.6587 and maximum 786.0187; its tail (672.7372&ndash;786.0187) is far heavier than the DM arm's. A fit on that pooled tail would have set z_crit = 1,048.8383 against the adopted 7.392 &mdash; a bar no astrophysical signal could clear. The arm is therefore calibrated on its own draws (`zcrit_off_fit` = 6.6552), and the drift arm's headline is a veto count, not a z threshold.

### 5 The veto: matched blank-sky windows

A lock survives only if the same trial in the blank-sky window does not show it. On the base arm that is 63 ON locks against 63 OFF locks: 63 vetoed, 0 standing. The locks are not marginal &mdash; their ON z runs 237.94&ndash;552.2 (63 of them) &mdash; they are terrestrial: every one of them sits at a trial whose blank-sky z is exactly 786.0187 (max = min over the set, all of them in blank-sky window 12), the OFF window carries the same tone up to 786.0187, and the 63 lock frequencies span only 383.88 Hz (988,976.5864&ndash;989,360.4687 Hz, out to bin 1,394,292, where blank sky itself reads 96.67&ndash;146.75, trial index 20).

| veto arm | `z_crit` | `n_on_locks` | `n_off_locks` | `n_vetoed` | `n_standing` |
|---|---|---|---|---|---|
| `base` | 7.392 | 63 | 63 | 63 | 0 |
| `base_stale` | 3.437 | 138 | 138 | 138 | 0 |
| `ctrl` | 7.392 | 138 | 63 | 63 | 75 |
| `ctrl_broken` | 7.392 | 138 | 63 | 138 | 0 |
| `ctrl_stale` | 3.437 | 138 | 138 | 138 | 0 |

The control arm is the interesting one: 138 ON locks against only 63 OFF locks, so 63 match and 75 cannot be matched at all. Those 75 are an unmatched-window artefact of the veto's own definition, not 75 detections &mdash; the veto is a matched test, and the receipt prints both sides so the reader can see the asymmetry instead of being handed a verdict.

### 6 The positive control that survives the veto

A veto that killed everything would be worthless, so the campaign plants a beacon: drift 0.06 Hz/s at 2.821 s, width 0.3 s, tone 400 Hz, SNR 0.02, over 4 drift chunks of 64 blocks on 12 workers in 1,089.4 s. The recovery: ON-minus-OFF z delta at least 116.7043 across the beacon footprint, 121.1742 at the adjacent drift trial, and only 0.9752 for the same cell in blank sky, with the recovered bin at most 1 bin from the injected one. The injection is the pre-registered drift control exactly: 0.06 Hz/s at 2.821 s, SNR 0.02. The DM control the same campaign pre-registered is a 6-SNR tone at DM 20, 400 Hz, 0.002 s wide at 0.9 s.

### 7 The engine under a 4 GiB ceiling

The parity run is 16,515,072 blocks &times; 60 DM trials on 12 workers with 0 fdot trials. Peak RSS 3,605 MiB against the 4,096 MiB bar (idle baseline 2,473 MiB). The batched 2-D pass does the reference work in 0.38 s against 2.66 s &mdash; 7.009&times; at normalised difference 3.107e-06, inside the 1e-05 bar. The same workload takes 65.648 s on one worker and 24.985 s on 12 &mdash; 2.627&times;, over a pool of 6 with 64 channels and 11 reps.

Parity is reported twice, because the two numbers answer different questions: the normalised differences against the naive DFT are 6.153451013180551e-13 (DM arm) and 1.6002709229826886e-12 (drift arm) over 1,024 comparisons, while the raw worst-case maxima are 1.2303e-05 in 0.000159254 and 0.0 in float64 &mdash; a raw maximum is only meaningful against the normalised bar, and the receipt keeps both rather than the flattering one.

| seam row | value |
|---|---|
| `engine_1blk:peak_rss_mib` | 142 |
| `engine_1blk:wall_s` | 11.101 |
| `engine_1blk:workers` | 12 |
| `engine_1blk:family_max/dm_full` | 4.4013 |
| `engine_1blk:family_max/drift_full` | 307.6157 |
| `bitorder_probe:fftlen` | 32,768 |
| `bitorder_probe:nchan` | 64 |
| `bitorder_probe:npol` | 4 |
| `bitorder_probe:nt` | 258,304 |
| `bitorder_probe:pfb_over` | 12 |
| `statistic_probes:elapsed_s` | 677.71 |
| `statistic_probes:workers` | 12 |

### 8 The bytes and the slice contract

7 slices staged, 12,963,131,904 bytes = 12.073 GiB, split 6,349,307,904 (role ON) and 6,349,307,904 (role OFF); per slice 0.246&ndash;1.971 GiB. The contract is 2,147,483,648 bytes and 16 blocks per pointing, and the checkpoint counts 6 documented anomalies &mdash; counted, not hidden. The grid the bytes are searched with: drift window 1.4093 s, natural fdot resolution 0.5035 Hz/s, a span of 1.1196 cells stepped by 0.056. The target is named in the receipt's own source annotation: `run/g05_sha256_active.json:target 'HIP 78709 (V=5.9, RA 16:04:03.096 DEC +25:15:27.72)'`.

| slice audit | `bytes` | `blocks_walked` | `header/NBITS` | `header/OBSFREQ` | `header/OBSBW` | `header/RA` | `header/DEC` | `t_slice_s` | `seam_pad_bytes` | `audit_seconds` |
|---|---|---|---|---|---|---|---|---|---|---|
| `A1` | 2,116,435,968 | 64 | 2 | 1,781.25 | 187.5 | 241.0129 | 25.2577 | 5.6427 | 0 | 2.34 |
| `A2` | 2,116,435,968 | 64 | 2 | 1,781.25 | 187.5 | 241.013 | 25.2576 | 5.6427 | 0 | 1.7 |
| `A3` | 2,116,435,968 | 64 | 2 | 1,781.25 | 187.5 | 241.013 | 25.2579 | 5.6427 | 0 | 2.43 |
| `B` | 2,116,435,968 | 64 | 2 | 1,781.25 | 187.5 | 241.0129 | 27.2575 | 5.6427 | 0 | 2.34 |
| `C` | 2,116,435,968 | 64 | 2 | 1,781.25 | 187.5 | 241.013 | 27.2578 | 5.6427 | 0 | 2.15 |
| `D` | 2,116,435,968 | 64 | 2 | 1,781.25 | 187.5 | 241.0128 | 27.2575 | 5.6427 | 0 | 1.8 |
| `PSR_J1543_40089_8bit` | 264,516,096 | 2 | 8 | 1,781.25 | 187.5 | 235.9118 | 9.4881 | 0.1763 | 0 | 0.45 |

### 9 The sweeps

24 sweep legs, each its own receipt, each carrying its own peak RSS and its own family maximum. The `ru_maxrss_mib_unreliable` column is the receipt's own warning that the kernel's high-water mark is not comparable across legs; `peak_rss_mib` is the measured one.

| sweep leg | `family_max` | `peak_rss_mib` | `ru_maxrss_mib_unreliable` | `trials` | `windows` | `window_blocks` | `surrogate` | `wall_s` |
|---|---|---|---|---|---|---|---|---|
| `base_dm_A1` | 4.9203 | 341 | 778.7 | 25 | 8 | 8 | 0 | 234.324 |
| `base_dm_A2` | 5.1035 | 341 | 778.7 | 25 | 8 | 8 | 0 | 229.349 |
| `base_dm_A3` | 4.915 | 341 | 778.7 | 25 | 8 | 8 | 0 | 219.752 |
| `base_dm_B` | 5.1098 | 341 | 778.7 | 25 | 8 | 8 | 0 | 219.764 |
| `base_dm_C` | 5.603 | 341 | 778.7 | 25 | 8 | 8 | 0 | 220.379 |
| `base_dm_D` | 5.7711 | 342 | 778.7 | 25 | 8 | 8 | 0 | 221.935 |
| `base_drift_A1` | 379.0085 | 3,605 | 3,605 | 21 | 1 | 64 | 0 | 1,409.556 |
| `base_drift_A2` | 552.1953 | 3,586 | 3,589.7 | 21 | 1 | 64 | 0 | 1,250.614 |
| `base_drift_A3` | 479.5658 | 3,588 | 3,590.2 | 21 | 1 | 64 | 0 | 1,250.165 |
| `base_drift_B` | 786.0187 | 3,628 | 3,627.7 | 21 | 1 | 64 | 0 | 1,051.039 |
| `base_drift_C` | 528.0863 | 3,628 | 3,627.7 | 21 | 1 | 64 | 0 | 1,050.797 |
| `base_drift_D` | 533.7445 | 3,628 | 3,627.5 | 21 | 1 | 64 | 0 | 1,107.171 |
| `ctrl_dm_A1` | 127.7593 | 436 | 778.7 | 25 | 8 | 8 | 0 | 209.266 |
| `ctrl_dm_A2` | 125.0786 | 436 | 778.7 | 25 | 8 | 8 | 0 | 209.872 |
| `ctrl_dm_A3` | 124.1468 | 436 | 778.7 | 25 | 8 | 8 | 0 | 206.755 |
| `ctrl_drift_A1` | 379.0211 | 3,629 | 3,628.6 | 21 | 1 | 64 | 0 | 1,083.002 |
| `ctrl_drift_A2` | 552.2468 | 3,624 | 3,624.3 | 21 | 1 | 64 | 0 | 1,074.917 |
| `ctrl_drift_A3` | 479.5905 | 3,624 | 3,624.4 | 21 | 1 | 64 | 0 | 1,165.132 |
| `surro_dm_B` | 6.6974 | 357 | 778.7 | 25 | 8 | 8 | 1 | 173.07 |
| `surro_dm_C` | 6.4172 | 357 | 778.7 | 25 | 8 | 8 | 1 | 174.197 |
| `surro_dm_D` | 7.5911 | 357 | 778.7 | 25 | 8 | 8 | 1 | 178.214 |
| `surro_drift_B` | 384.514 | 3,624 | 3,623.8 | 21 | 1 | 64 | 1 | 1,113.541 |
| `surro_drift_C` | 317.8272 | 3,627 | 3,627.3 | 21 | 1 | 64 | 1 | 1,090.573 |
| `surro_drift_D` | 1,052.7434 | 3,624 | 3,623.6 | 21 | 1 | 64 | 1 | 952.309 |

### 10 The gate and its mutants

30 checks passed, 0 failed; 3 of 3 expected mutants fired, inside 78 mutant-arm checks. A mutant arm must exit non-zero &mdash; a gate that cannot be broken is not a gate.

| arm | `checks` | `failures` | `exit_code_expected` | `log_bytes` |
|---|---|---|---|---|
| `base` | 30 | 0 | 0 | 2,306 |
| `shuffle` | 20 | 0 | 1 | 1,682 |
| `stale` | 29 | 0 | 1 | 2,422 |
| `veto` | 29 | 0 | 1 | 2,488 |

### 11 The censuses

Two censuses say the same thing from different directions. The impulse census over 4 blocks per pointing (29.01 s each) reaches z&nbsp;=&nbsp;5.2032 ON and 5.3638 OFF, with 0 ON and 0 OFF blocks above z&nbsp;=&nbsp;20 &mdash; the sky is not quiet, in either window. The spectral census finds 14 hot ON bins and 6 hot OFF bins above z&nbsp;=&nbsp;20, of which 0 are shared (0 of the ON set): interference, present in both windows, which is precisely what the matched veto is built to remove.

### 12 Honest boundaries

- The seam is an index, not the engine output: 2,427 rows sliced from 49 receipts. The receipts are the source of truth; the seam is what a reviewer reads first.
- The drift arm's raw statistic is RFI-dominated (`family_max` 307.6157 on a single block, 786.0187 in the blank-sky window). The claim is 0 standing after the matched veto, never "the drift statistic is clean".
- The A7 surrogate result is a FAIL and is published as one (9.6962 against the 8.1312 bar). Nothing was re-tuned to make it pass.
- `wall_s`, `elapsed_s` and `audit_seconds` are telemetry from one machine, not claims; `ru_maxrss_mib_unreliable` is flagged unreliable by the receipt itself.
- The 75 standing locks on the control arm are unmatched trials (138 ON against 63 OFF), not candidates.
- The 6 documented anomalies are counted here and enumerated in the checkpoint receipt, not on this page.
- The contract is `docs/PRE_REGISTRATION.md + amendments A2-A7; harness/g05_gate.sh is the arbiter`. This page is a rendering of the seam; where the two disagree, the gate wins.

## Related

- [CFS Gate 4](cfs-gate4.md) &mdash; the multi-rotation fold and its receipted null.
- [ChemGym-v2](chemgym-v2.md) &mdash; the same laws applied to chemistry benches.
- [The Laws of Governed Compaction](omp-governance.md) &mdash; the rules this page is built under.
- [Projects](projects.md) &mdash; what is running, what is closed.

_Apache License 2.0 &middot; built by `src/tools/make_docs_pages.py` from `harness/vendor/guppi_gate5/run/receipts.json`._

