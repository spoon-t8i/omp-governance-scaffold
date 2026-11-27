---
layout: default
title: "Ten Governance Laws: Turning Agent Prose Into Gates"
---

[&larr; Back to Home](index.html)

# Ten Governance Laws: Turning Agent Prose Into Gates

*Every receipt figure quoted here is read from `guppi_gate4/run/receipts.json` by the page generator. The laws are enforced by tools in `src/` and `/home/snoop/.omp/agent/tools/`, not by reminders in a prompt.*

## Tier 1: The High-Level Picture (Plain-English Intuition)

A long-running coding agent fails in a specific, boring way: it stops checking. Early in a session it runs the test; four hours later it *describes* the test. Prose about diligence costs nothing, so a model under pressure produces more of it. The scaffold therefore moves every rule out of the prompt and into an exit code: a claim is only allowed to reach the summary if a program produced the number, and a node is only allowed to close if a gate ran after the code it covers changed.

The ten laws below are the ones that survived contact with a real campaign &mdash; [CFS Gate 4](cfs-gate4.html), a radio-astronomy measurement whose receipts span 33.6 hours and that ended in a published null result with 5/5 nodes closed and zero unbound numerals.

## Tier 2: The Laws, Their Mechanism, and Their Receipt

### Law 1: A node closes only on a gate that ran, passed, and is newer than its code.

**Mechanism.** `plan_check.py` compares each DAG node’s gate-log timestamp against the newest mtime of the code it covers; a gate older than its code is `stale`, not a pass.

**Receipt.** `plan_check` rc=0: 5/5 nodes done, stale=0, violation=0.

### Law 2: Every gate must be able to fail, or it measures nothing.

**Mechanism.** Two-sided control: each node ships a positive arm (rc=0) and a mutant arm that perturbs one input and must exit rc=1 with a named failure.

**Receipt.** Ten gate arms recorded; the five mutant arms exit rc=1, e.g. `g02_mut` 2 failed / 8 passed, `g03_mut` 2 failed / 15 passed.

### Law 3: Never truncate a gate’s output.

**Mechanism.** Piping a status gate through `| tail -2` hides the very line that reports staleness. The check-count law (`F9`) forces every harness to print `checks: N` as its final line on both arms, so a swallowed verdict is visible as a missing count.

**Receipt.** Gate logs carry passed/failed/checks/rc per arm; `gate_logs_missing` = 0.

### Law 4: Every numeral binds to a receipt path or it does not ship.

**Mechanism.** `entail_check.py` walks each sentence’s numbers and re-derives them from the receipts document; `verify_rows.py` checks the formula behind each derived figure.

**Receipt.** `entail_check` REPORT.md rc=0 (entailed 66, contradicted 0, unbound 0); perm-record.md rc=0 (15 entailed, 0 unbound); `verify_rows` stale=0, formula=0, unbound=0.

### Law 5: Promotion is generated, never typed.

**Mechanism.** The long-term memory files (`STATE.md`, `PERM.md`) are emitted from the receipts document by a generator; `spine_check.py --check-seam` fails if a promoted row is not reproducible from it.

**Receipt.** `spine_check --check-seam` rc=0, violations=0; seam rows STATE.md rc=0 unbound=0, PERM.md rc=0 unbound=0.

### Law 6: Thresholds are pre-registered and immutable mid-run.

**Mechanism.** The bar lives in the harness, not in the engine receipt, so no analysis leg can edit its own pass condition; battery check N11 asserts no input receipt carries a threshold field.

**Receipt.** `z_crit` = 6.0 supplied by the harness; `beacon_full_depth_z_min` = 8.0; the campaign reported a miss of 1.2138 rather than a pass.

### Law 7: Every claim ships with its null counterparty.

**Mechanism.** Blank sky, phase-randomised surrogates, Rayleigh uniformity of peak phases, and an extreme-value (Gumbel) ceiling for the trial count actually run.

**Receipt.** OFF sweep 41 trials / 0 locks, Rayleigh p=0.6443; surrogates 16 trials / 0 locks, p=0.3248; ON excess over the sweep ceiling 4.7862 &minus; 4.6496 = 0.136652 (0.3771 sd).

### Law 8: Resource ceilings are measured per leg and fixed in the engine, never raised in the gate.

**Mechanism.** Peak RSS is read from `VmHWM` for every engine leg and compared against one registered ceiling.

**Receipt.** Ceiling 4,096 MiB; worst leg 3,612.9 MiB, headroom 483.11 MiB. The first run breached the ceiling by 483.11 MiB, and the gate failed it.

### Law 9: State lives in files, not in the model.

**Mechanism.** A pre-compaction sieve pins the live repo, open nodes and gate commands to disk; `rehydrate.py` rebuilds the working set after a restart, so a session death costs context, not truth.

**Receipt.** This campaign survived multiple compactions; the closure receipt (`run/receipts/g05_closure.json`) and the gate archive are the only memory that mattered.

### Law 10: A stale freeze is re-gated, never re-labelled.

**Mechanism.** When HEAD moves under a closed node, the gate is re-run at the new commit and the log re-stamped; prose that merely asserts "still valid" is a governance violation.

**Receipt.** Gate logs carry their own `git_commit` (e.g. `g02_mut` @ 0bc5edb); receipts collected at `3793edb`; `plan_check` stale=0.

## The RSS Drift Story, in Full

The memory ceiling for the campaign was 4,096 MiB. The design assumption was that peak RSS tracks the ingest chunk (8 blocks ≈ 1,024 MiB of raw bytes read at a time), and the small-prefix legs honoured it comfortably (1,760.5 MiB at 16 blocks). The assumption was wrong in a way only a full-depth leg exposes: the folding accumulator holds every dedispersed plane for the whole span, so RSS tracks **span**, not chunk. The counterparty arm breached at 4,579.1 MiB &mdash; 483.1 MiB over the ceiling &mdash; and failed its own battery.

Two options existed: raise the ceiling to a number the code happens to hit, or fix the engine. The ceiling is a registered constant, so raising it would have been a threshold edit under pressure &mdash; Law 6 forbids it. `fold_series` was changed to release each plane as its contribution lands (buffer lifetimes only, arithmetic untouched), and the statistic was re-verified bit-for-bit against the pre-fix value (`R_plane` z 4.7862 unchanged). Post-fix maxima: 3,071.2 / 3,118.7 / 3,612.9 MiB.

The receipts the fix invalidated were not deleted and not silently overwritten: they were quarantined under `run/stale_pre_patch/` with a hash manifest, so a `--reuse` cache can never hand back a pre-fix number, and the audit trail keeps both the breach and the fix visible.

## How a Claim Becomes a Gate

| step | artifact | who checks it |
|---|---|---|
| 1. Register | constants + DAG node with a named gate command | `plan_check.py` refuses a node with no gate |
| 2. Measure | engine writes a JSON receipt (no threshold fields inside) | `make_receipts.py` hashes and stamps it |
| 3. Gate | harness runs positive and mutant arms, logs to the gate archive | exit code + `checks: N` line |
| 4. Report | generator emits `REPORT.md` / `perm-record.md` from the receipts document | `entail_check.py` re-derives every numeral |
| 5. Promote | one-line rows into `STATE.md` / `PERM.md` | `verify_rows.py` + `spine_check.py --check-seam` |

## Enforcement Tools

| tool | what it enforces |
|---|---|
| `spine_check.py` | spine integrity; a differing closure hash means a stale artifact; `--check-seam` grades promoted rows |
| `plan_check.py` | a node closes only on a gate that ran, passed and is newer than its code |
| `entail_check.py` | every numeral in a document is entailed by, or contradicted by, the receipts |
| `verify_rows.py` | promoted rows are reproducible and their formulas recompute |
| `fact_store.py` | claims pin the SHA-256 of the slice that makes them true; drift demotes the claim |
| `num_provenance.py` | a numeric status must bind to a declared evidence file or hard-fail |
| `ownership.py` | which authority (PLANNING/DESIGN/CLOSURE/STATE/EXECUTION) crossed reasoning into procedure |
| `impact_analysis.py` | what a change reaches, and the smallest gate set that proves it |
| `pre_compact_sieve.py` | live repo pin, open nodes, gate commands and read locks before history collapses |
| `rehydrate.py` | resume a project from disk: tree, closed nodes, ready gate, failing attempts, dead branch |
| `compaction_clock.py` | which clock fired the collapse |
| `surprise_check.py` | zero-token disk probe; governance probes fail open, never stall a commit |
| `checkpoint.sh` | commit spine + project before risky work, nothing until revalidation |

## Mutant Arms From This Campaign

| arm | node | gate command | passed | failed | rc |
|---|---|---|---|---|---|
| `g01_mut` | CFS-G4-01 | `bash harness/g01_gate.sh --mutant-seam-offset 512` | 14 | 0 | 1 |
| `g01_pos` | CFS-G4-01 | `bash harness/g01_gate.sh` | 14 | 0 | 0 |
| `g02_mut` | CFS-G4-02 | `bash harness/g02_gate.sh --mutant-wrong-p0` | 8 | 2 | 1 |
| `g02_pos` | CFS-G4-02 | `bash harness/g02_gate.sh` | 10 | 0 | 0 |
| `g03_mut` | CFS-G4-03 | `bash harness/g03_gate.sh --mutant-phase-scramble` | 15 | 2 | 1 |
| `g03_pos` | CFS-G4-03 | `bash harness/g03_gate.sh` | 24 | 0 | 0 |
| `g04_mut` | CFS-G4-04 | `bash harness/g04_gate.sh --mutant-inject-weak-beacon` | 24 | 0 | 1 |
| `g04_pos` | CFS-G4-04 | `bash harness/g04_gate.sh` | 19 | 0 | 0 |
| `g05_mut` | CFS-G4-05 | `bash harness/g05_gate.sh --mutant-row-swap` | 1 | 0 | 1 |
| `g05_pos` | CFS-G4-05 | `bash harness/g05_gate.sh` | 20 | 0 | 0 |

Every mutant arm exits `1` by construction (`[ "$MODE" = pos ] || exit 1`) once its battery has confirmed the perturbation bites, so `failed = 0` on a mutant arm is not an inert gate: the spliced-volume auditor inside `g01_mut` fails its own seam and SHA checks before the harness converts that into a passing meta-check, and `g04_mut`'s tally is clean precisely because its meta-check asserts that the driver's null battery (N1&ndash;N3) has been broken by the injected beacon. Each arm's log is archived and indexed by `g05_closure.gates.<arm>`.

## Related

- [CFS Gate 4: the measured null result](cfs-gate4.html) &mdash; every receipt quoted above in context.
- [Positive & Negative Attractors](attractors.html) &mdash; the failure taxonomy these laws intercept.
- [Compaction Sieve Armor](compaction-sieve.html) &mdash; the pre-compaction mechanics behind Law 9.
- [Projects Showcase](projects.html) &mdash; earlier campaigns on the same spine.

*This page is a build artifact: `python3 src/tools/make_docs_pages.py --receipts <receipts.json>` regenerates it from the campaign receipts, so edit the generator, not the page.*

*Licensed under the Apache License, Version 2.0. See `LICENSE` and `NOTICE` in the repository root.*
