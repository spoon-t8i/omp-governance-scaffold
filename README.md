# OMP Governance Scaffold

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Docs: Live Site](https://img.shields.io/badge/Docs-GitHub_Pages-3fb950.svg)](https://spoon-t8i.github.io/omp-governance-scaffold/)
[![Agent Ingestion: llms.txt](https://img.shields.io/badge/AI_Ingestion-llms.txt-58a6ff.svg)](https://spoon-t8i.github.io/omp-governance-scaffold/llms.txt)

**Stop local LLM agents from hallucinating progress and writing self-certifying tests after context compaction.**

Honesty that lives in a system prompt cannot survive the reset that creates the temptation: a freshly compacted local model re-asserts a green build it never ran, writes a test that passes because it asserts nothing, and cites numerals its own receipts contradict. This scaffold moves verification out of the prompt and into exit codes — every claim binds to a receipt on disk, and every gate ships sabotage mutants that must exit `1`, so a silent no-op gate can never certify itself.

### Proof strip — every number below is a receipt in this repo

- **Tiny Position-Zero Spine.** The injected constitution is a Merkle-folded, append-only state ledger (`src/gates/merkle_compactor.py`, `src/spine/`): `1,704` frozen tokens measured against the `IDEAL_TOKENS = 1800` budget in `src/gates/spine_check.py`, so a `65,536`-token local KV cache keeps its `98.7%` prefix-cache hit rate instead of re-reading a prompt that grows every turn.
- **Non-vacuous mutant gates.** Every verification gate ships sabotage mutants that must exit `rc=1` — a silent no-op flag, a numeral the receipt does not carry, a prose leak. Measured on this commit: `harness/seam_gate.sh --gate` **18/0**, `harness/docs_gate.sh --gate` **23/0**, `src/gates/surprise_check.py --self-test` **29/29 mutants** (16 must wake, 13 must stay dormant), `src/tools/session_telemetry.py --self-test` **26/26**.
- **Battle-tested on real science.** Verified end-to-end on published Breakthrough Listen GUPPI radio-astronomy volumes: `CFS-G4` (`34.1 GB`, pre-registered `z_crit = 6.0`, `0` false-positive locks) and `CFS-G5` (`12.07 GiB` across 7 slices, blind ON-vs-blank-sky cadence: `63/63` terrestrial RFI locks vetoed, `0` standing false positives, fitted `z_crit = 7.392`, `2,427` deterministic receipt rows — one per numeral, each carrying the receipt path it was sliced from). Live showcase: [spoon-t8i.github.io/omp-governance-scaffold](https://spoon-t8i.github.io/omp-governance-scaffold/) · [`cfs-g5.html`](https://spoon-t8i.github.io/omp-governance-scaffold/cfs-g5.html).

### 10-second local quickstart (no GPU, no API keys)

```bash
git clone https://github.com/spoon-t8i/omp-governance-scaffold.git && cd omp-governance-scaffold
bash harness/seam_gate.sh --gate                    # promotion seam + CLI contract; mutants must bite
bash harness/docs_gate.sh --gate                    # generated docs can never rot; mutants must bite
python3 src/gates/surprise_check.py --self-test     # 29 two-sided loop/wake mutants
```

---

**Deterministic control-plane governance for long-horizon local AI coding agents.**  
Instead of bloating system prompts with fragile prose rules that erode during context compaction, this scaffold separates **probabilistic token generation** (quantized local MoE models) from **deterministic verification** (standalone Python/Bash `RC=0` gates, `before_compact` lifecycle hooks, and disk-anchored state).

> **Interactive Documentation & Two-Tier Guides:** [**spoon-t8i.github.io/omp-governance-scaffold**](https://spoon-t8i.github.io/omp-governance-scaffold/)  
> **Single-Shot LLM Ingestion Bundle:** [`llms.txt`](https://spoon-t8i.github.io/omp-governance-scaffold/llms.txt) · [`llms-full.txt`](https://spoon-t8i.github.io/omp-governance-scaffold/llms-full.txt)

---

## Closed-Loop Control Architecture

```mermaid
flowchart LR
    P0["Position Zero Spine<br/>AGENTS.md + CORE.md + STATE.md<br/>(Frozen 1,704 Tokens)"] --> OMP["Oh-My-Pi (OMP) Runtime<br/>Minimal 3-Tool Schema<br/>(read · bash · write)"]
    OMP -->|"Context Ceiling<br/>(55K → 34K)"| SIEVE["S1–S4 Pre-Compaction Sieve<br/>pre_compact_sieve.py<br/>(0 B idle · ≤137 tok active)"]
    SIEVE --> OMP
    OMP -->|"Proposed Mutation"| GATES["Deterministic Gates (RC=0)<br/>spine_check · entail_check<br/>plan_check · surprise_check"]
    GATES -->|"Verified Receipt"| STATE["Disk Anchor (STATE.md)<br/>& Merkle Receipt Chain"]
    STATE -.->|"Rehydrate Next Turn"| P0
```

---

## Empirical Receipts & Benchmark Telemetry

Measured across multi-hour autonomous engineering runs (3D WebGL2 engines, bit-exact `ffmpeg` audio editors, and hardware probes) on a tri-GPU Linux workstation (`ASUS ROG Strix X570-E`, `Ryzen 9 5900X`, `94 GiB` DDR4, dual `RTX 5060 Ti 16 GB GDDR7` in CPU-direct `x8/x8` + `Intel Arc A310` display shock absorber, `CachyOS 7.3.5-2`, `Strata v0.1.68`, `Qwen 3.8 Flash-Next NVFP4`):

| Metric | Measured Result | Architectural Mechanism |
| :--- | :--- | :--- |
| **Position-Zero Spine** | **`1,704` tokens** | Frozen `AGENTS.md` (`25` tok) + `CORE.md` (`1,033` tok) + `STATE.md` (`<=650` tok) |
| **Prefix-Cache Hit Rate** | **`98.7%` median** | Immutable Position-Zero prefix + append-only turn discipline |
| **Local Decode Throughput** | **`54 – 74.7 tok/s`** | `180B` total / `6B` active MoE (`NVFP4`) on Blackwell `sm_120` with `11/12` MTP draft acceptance |
| **Working Context Runway** | **`~43,000` tokens** | Inside a `65,536`-token ceiling with deterministic `55K -> 34K` compaction |
| **Compaction Sieve Cost** | **`0 B` idle / `<= 137` tok active** | `S1–S4` AST/regex extraction on OMP `before_compact` hook |
| **Deterministic Gate Suite** | **`584 / 584` passing checks** | Standalone Python/Bash exit-code verifiers (`spine_check.py`, `fact_store.py` `290/290`) |

---

## Inspect the Sieve and Spine Gates Directly

You do not need a multi-GPU workstation to inspect or run the deterministic governance gates and compaction sieve locally (`Python 3.10+`, zero external pip dependencies):

```bash
git clone https://github.com/spoon-t8i/omp-governance-scaffold.git && cd omp-governance-scaffold
python3 src/hooks/pre_compact_sieve.py --help
python3 src/gates/spine_check.py --help
```

---

## Repository Map & Two-Tier Documentation

Every guide on the [GitHub Pages site](https://spoon-t8i.github.io/omp-governance-scaffold/) opens with **Tier 1 (Plain-English Intuition)** followed immediately by **Tier 2 (Systems Engineering & Control-Theoretic Specification)**:

| Path | Description |
| :--- | :--- |
| [`src/spine/`](src/spine/) | Frozen Position-Zero constitution (`AGENTS.md`, `CORE.md`, `STATE.template.md`, `surprise_policy.json`). |
| [`src/hooks/`](src/hooks/) | `S1–S4` Pre-Compaction Sieve (`pre_compact_sieve.py`) hooked into OMP's `before_compact` lifecycle event. |
| [`src/gates/`](src/gates/) | 10 standalone `RC=0` verification gates (`spine_check.py`, `entail_check.py`, `plan_check.py`, `fact_store.py`, `regime_sensor.py`, `surprise_check.py`, `merkle_compactor.py`, `gate_exec.py`, `gate_freeze.py`, `checkpoint.sh`). |
| [`src/tools/`](src/tools/) | Line-bounded surgical editor (`edit_file.py`), static impact analyzer, token probe, and session telemetry utilities. |
| [`docs/`](docs/) | Jekyll GitHub Pages source (`architecture`, `compaction-sieve`, `attractors`, `omp-governance`, `cfs-gate4`, `cfs-g5`, `chemgym-v2`, `workstation`, `projects`, `theory`, `influences`, `llms.txt`, `llms-full.txt` &mdash; the campaign pages are generated from receipts by `src/tools/make_docs_pages.py`, the bundle by `src/tools/bundle_docs.py`). |
| [`harness/`](harness/) | `docs_gate.sh`: the generated pages and `llms-full.txt` can never rot - rebuilds into a scratch copy from vendored `guppi_gate4`, `guppi_gate5` and `chemgym-v2` receipts snapshots and refuses any difference, with mutant arms that must bite. `seam_gate.sh`: the promotion seam (`spine_check.py --check-seam`) grades a throwaway spine fixture under `$TMPDIR` - a numeral the receipt does not carry and a token the project falsified must each exit 1, an entailed row must exit 0 - plus the gate CLIs' `--help`/unknown-flag contract, with mutant arms that must bite. |
| [`AGENTS.md`](AGENTS.md) | Root repository navigation contract for autonomous CLI coding agents. |

---

## License

Published under the [Apache License, Version 2.0](LICENSE). See [NOTICE](NOTICE) for attribution details.
