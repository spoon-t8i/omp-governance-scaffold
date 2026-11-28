---
layout: default
title: "ChemGym-v2: Physics-Benched Chemistry Environments - Receipted Bench Suite"
---

[&larr; Back to Home](index.html)

# ChemGym-v2: Five Gymnasium-Style Chemistry Benches, Library-Read Physics, and a SARSA Solver Audited Against Both

*Project `chemgym-v2`. Receipts document `run/receipts.json`, collector `src/make_receipts.py` at commit `894e41b`, bench engine at git pin `b1999db`. Every number below is read from that document by the page generator; none is typed by hand. Licensed under the Apache License, Version 2.0.*

## Tier 1: The High-Level Picture (Plain-English Intuition)

ChemGym-v2 is 5 small chemistry environments in the Gymnasium style (`*-v0` ids): a flash vapor-liquid separator, a liquid-liquid extraction vial, an aqueous speciation beaker, a Wurtz reaction flask, and a jar of vinegar with a spoon of baking soda. Each bench hands an agent labelled action slots (open a valve, tip the jar, read the gas cylinder) and a reward, and every physical quantity it reports - pressure, temperature, moles, litres of gas, IR band height - is computed from published component-data libraries, not fitted curves. The tie is explicit on the acid-carbonate bench: the engine's reaction enthalpy `46,281` J/mol must equal the Hess-law sum over library heats of formation, `46,281` J/mol, to the last digit - and the reaction is endothermic, so the jar goes cold.

The RL question: can a tabular SARSA agent learn the action sequence that reaches a bench's goal, and can any single fixed action slot reach it by shortcut? The receipt answers both. Under a pre-registered rule (`terminated and ret >= max(single + 0.5, 0.75 * solve) and ret > random`), SARSA dominates on **5 of 5** benches. And 11 named shortcut controls - each one a deliberate way the goal could be faked - were each caught by named physics checks: 23 of 23 positive-control mutations caught, every one leaving the engine clean afterwards.

## Tier 2: Systems Engineering, Receipts and Honest Boundaries

### 1 The bench suite and the SARSA verdict (gate `GATE-3 RL benchmark + contract`)

| bench | episodes | random mean | best single-slot mean | SARSA return | steps | terminated | reference solver |
|---|---|---|---|---|---|---|---|
| `AcidCarbonateV2-v0` | 900 | -0.120 | 0.100 | 2.100 | 6 | yes | 2.100 |
| `AqLleExtractV2-v0` | 420 | -0.300 | 0.000 | 3.500 | 4 | yes | 2.250 |
| `AqSpeciationV2-v0` | 220 | 3.362 | 3.985 | 4.554 | 2 | yes | 4.554 |
| `FlashVleV2-v0` | 150 | 1.029 | 1.221 | 2.169 | 3 | yes | 1.967 |
| `WurtzReactV2-v0` | 320 | 0.019 | 1.250 | 2.250 | 5 | yes | 2.456 |

### 2 Contract gate, determinism and guardrails

The GATE-3 harness runs 31 contract checks with 0 failures. The determinism leg re-runs the greedy policy and reproduces the `FlashVleV2-v0` SARSA return exactly (`2.169`), with the learning curve hashed to `521626a71238b5f4`. Thermal guardrails are scored, not clamped: cold `r = 0.0934`, window `r = 1.4558`, over-relief `r = -0.5`. The run took `93.52` s at git pin `b1999db` (receipt date `2026-11-27T19:47:53-0600`).

### 3 The acid-carbonate bench: 158/158 physics and contract checks

One US tablespoon of pickling vinegar (`50.0 kg/m3 acetic acid`) meets one tablespoon of baking soda (`0.0138 kg`, `0.000014787 m3` per tablespoon) in a sealed jar; the CO2 is collected over water. The goal is declared before the run: `0.12 L` of gas and a carboxylate IR band of `0.15`, reached through 6 labelled slots: `pour_vinegar_1tbsp`, `load_soda_tissue_1tbsp`, `seal_jar`, `tip_jar_mix`, `read_gas_cylinder`, `close`.

The headline state the engine reports after the full protocol:

| field | value |
|---|---|
| `A_1555` | 0.2616 |
| `P_head_dry_Pa` | 101,325.0000 |
| `P_head_total_Pa` | 102,960.0057 |
| `T_K_after_tip` | 290.4407 |
| `acetate_mol` | 0.0123 |
| `acetate_molal` | 0.8635 |
| `acid_poured_mol` | 0.0123 |
| `bicarbonate_left_mol` | 0.1520 |
| `cylinder_capacity_mol` | 0.0210 |
| `cylinder_co2_mol` | 0.0064 |
| `cylinder_mol` | 0.0116 |
| `dH_rxn_J_per_mol` | 46,281.0000 |
| `dT_K` | -7.7093 |
| `dissolved_co2_mol` | 0.0007 |
| `extent_mol` | 0.0123 |
| `gas_L_wet` | 0.2773 |
| `gas_co2_L` | 0.1519 |
| `head_gas_mol` | 0.0096 |
| `hess_dH_J_per_mol_from_library` | 46,281.0000 |
| `seal_hold_mol` | 0.0096 |
| `seed` | 1 |
| `vented_mol` | 0.0006 |

### 4 Law 2 counterparty controls: 23/23 caught

The gate ships positive-control mutations: each breaks one physical law inside the engine and must be caught by a named check. 11 of them are named shortcut controls (`(acid_carbonate_physics.mutant_controls_total - 1) / 2` = `11`). Each row is the receipt's own record of how the break was introduced, which checks caught it, and whether the engine was clean after the mutation was reverted:

| control | how it was broken | caught by | clean after |
|---|---|---|---|
| `no_henry_dissolution` | check | F3 dissolved CO2 == Henry fixed point; F7 the liquor takes a real fraction of the gas | yes |
| `seal_hold_removed` | raised inside section_pressure (AssertionError) | C10 seal hold == BACK*V/RT (ideal gas) | yes |
| `pure_co2_front` | check | C3 water seal settles the DRY head at 1 atm; C4 head total == BACK*V_head/(R*T) | yes |
| `clamp_is_a_lock_at_load` | check | D3 open jar: the cylinder collects nothing at all; D5 open jar: escaped is most of the CO2, not a rounding error; H18 cylinder empty, gas lanes zero | yes |
| `exothermic_neutralisation` | check | B12 dH_rxn == Hess sum over CRC Hf; B13 the reaction is ENDOTHERMIC; B17 the jar goes COLD | yes |
| `open_jar_keeps_its_gas` | check | E5 the pour displaces exactly its own volume of air | yes |
| `vat_target` | check | H5 target_score on a clean protocol; H6 every criterion met | yes |
| `free_reagent_on_load` | raised inside section_stoichiometry (AssertionError) | B11 CO2 moles == extent; B4 extent == the acid (limiting); B6 soda left = poured - extent | yes |
| `air_volume_reads_as_co2` | check | E7 CO2 litres == its own partial volume; E8 CO2 litres below the wet total | yes |
| `ir_reads_the_water_band` | check | G1 protonated acid shows nothing at the carboxylate band | yes |
| `cylinder_silently_loses_overflow` | check | D4 open jar: head + escaped + liquor == the CO2 made; D5 open jar: escaped is most of the CO2, not a rounding error | yes |

### 5 Provenance

| engine file | sha256 (16) |
|---|---|
| `chemgym_v2/benches.py` | `28aa8de882cb38a4` |
| `chemgym_v2/core.py` | `e494fc2a50669499` |

| receipt file | mtime (UTC) | dirty | last commit touching |
|---|---|---|---|
| `acid_carbonate_physics` | 2026-11-28T01:46:14Z | no | b6d0cad |
| `gate3_rl_benchmark` | 2026-11-28T01:47:53Z | no | b6d0cad |

Python `3.12.13`; physics receipt stamped `2026-11-28T01:46:14Z`; collector `src/make_receipts.py` (`3842336b4125`), `dirty = no`.

### 6 Honest Boundaries

* **`wall_s` is telemetry, not a claim:** `93.52 s` is runtime cost; the volatility partition keeps it (and every timestamp) out of the promoted STATE.md/PERM.md rows - it lives here because this page is the receipt view.
* **SARSA returns are stored rounded to 3 decimals:** the dominance margin is `0.5`, far above the rounding, so the verdict is not a rounding artefact.
* **23 controls ran, 11 are itemised:** the receipt names the shortcut controls above; the remaining controls are counted in the pass total but not listed in this document.
* **Kitchen scale, single vessel:** ideal-gas headspace over water with Henry dissolution - the physics is real but the bench is a jar, not a plant.
* **Per-bench episode budgets differ** (`150`-`900` episodes): the benchmark compares solvers within a bench, not across benches.

## Related

* [Ten Governance Laws](omp-governance.html) - the laws these gates exercise.
* [CFS Gate 4](cfs-gate4.html) - the same receipt discipline on a radio-astronomy null.

*Licensed under the Apache License, Version 2.0. See `LICENSE` and `NOTICE` in the repository root.*
