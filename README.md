# OMP Governance Scaffold & Compaction Sieve Armor

**Live Documentation & Benchmarks:** [https://spoon-t8i.github.io/omp-governance-scaffold/](https://spoon-t8i.github.io/omp-governance-scaffold/)  
**License:** Apache License, Version 2.0 (see `LICENSE` and `NOTICE`)

This repository provides a lightweight, deterministic governance scaffold and pre-compaction sieve for **Oh-My-Pi (OMP)** coding agents. Instead of relying on bloated context windows or fragile prose instructions, it keeps local models fast, focused, and accurate over multi-hour sessions by pairing a **1,704-token frozen governance spine** with a bounded **~43k working runway** and **S1–S4 Sieve Armor**.

---

## Key Architecture & Telemetry Highlights

* **High Signal-to-Noise Ratio (SNR):** Capping the context window at **65,536 tokens** (~43k working room) prevents **softmax dilution** (the gradual fading of instruction focus as old chat logs accumulate) and protects 4-bit quantized (`NVFP4`) **logit margins** against rounding noise.
* **Frozen Governance Spine (`1,704 tokens`):** Pins `AGENTS.md` (25 tok), `CORE.md` (1,033 tok), and `STATE.md` (646 tok) at Position Zero, sustaining **98.7% median prefix-cache reuse** and **54–74.7 tok/s** decode speed.
* **`S1–S4` Pre-Compaction Sieve (`pre_compact_sieve.py`):** Costs `0 bytes` when idle and at most `137 tokens` when active (`<= 140` token ceiling). Extracts active task directives (`S1`), read-only directory locks (`S2`), exact CLI flags (`S3`), and spin-loop tripwires (`S4`) right before history compresses (`55K -> 34K`).
* **Cross-Platform Core:** The governance spine, `S1–S4` sieve (`Python 3.10+`), and JS compaction hooks run on **Linux, macOS, and Windows**, with reference benchmarks measured on CachyOS Linux (`Strata 0.1.68` + `Qwen 3.8 Flash-Next NVFP4` across dual RTX 5060 Ti 32 GB compute + Intel Arc A310 display isolation).

---

## Repository Layout

* `docs/` — Source files for the GitHub Pages site (`index.html`, `compaction-sieve.md`, `attractors.md`, `projects.md`, `theory.md`, `influences.md`, and `llms.txt`).
* `src/` — Sanitized, portable `pre_compact_sieve.py` and hook integration scripts.
* `LICENSE` & `NOTICE` — Apache License 2.0 terms and community attribution notices.
