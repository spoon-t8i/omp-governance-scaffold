[← Back to Home](index.html)

# Hardware, Software & Engineering Lineage

*Licensed under the Apache License, Version 2.0. See `LICENSE` and `NOTICE` in the repository root.*

Open-source local AI engineering stands on the shoulders of kernel developers, compiler authors, hardware tinkerers, and systems researchers. This page documents the technical influences and community builders that shaped our **Oh-My-Pi (OMP)** governance scaffold and workstation setup.

---

## 1. Local Inference Engines & Quantized Model Tooling

* **Oh-My-Pi (OMP) Harness:** Provided the fast TypeScript/Node CLI loop, in-process shell execution (`brush`), and lifecycle hook seams (`before_compact`) that make deterministic `S1–S4` sieve injection possible without modifying core engine binaries.
* **Strata Inference Engine (`v0.1.59.1` → `0.1.68`):** Enabled native Blackwell `sm_120` tensor execution, two-GPU expert caching, and host-RAM streaming for large Mixture-of-Experts (MoE) models on consumer hardware.
* **Qwen Team (`Qwen 3.8 Flash-Next`):** Demonstrated that hybrid **Gated DeltaNet + Sparse Attention** architectures with Multi-Token Prediction (MTP) can deliver frontier-grade code synthesis and graduate-level reasoning on a local workstation when protected from context bloat.

---

## 2. Tri-GPU Workstation Architecture & Linux Systems Discipline

* **Display vs. Compute Isolation (Intel Arc A310 + Dual RTX 5060 Ti):** Offloading Wayland/KDE desktop compositing onto a dedicated 4 GB **Intel Arc A310** leaves 100% of our **32 GB GDDR7** dual-GPU pool dedicated to MoE experts, KV cache, and automated WebGL2 visual QA.
* **CachyOS & Btrfs Snapshot Hygiene:** High-frequency kernel scheduling and copy-on-write snapshot rollbacks allowed rapid compiler upgrades and non-destructive testing across multi-hour engineering runs.

---

## 3. Systems Engineering & Verification Philosophy

* **Counterparty Verification & "Two-Sided" Testing:** Pairing probabilistic LLM code generation with independent ground-truth oracles—such as `ffmpeg`, `git cat-file`, Float64 numerical integrators, and SHA-256 slice pins—eliminates self-certifying test illusions.
* **Demoscene & Hardware-Exact Discipline (8088MPH / Amiga / Retro Console Specs):** Reverse-engineering PlayStation 1 CD-ROM XA sectors, 3DO Opera filesystems, and 60 Hz terminal raymarching reinforced respect for exact byte layouts, fixed-point math, and tight frame budgets.
* **Coupled Dynamical Systems & Small-Model Decomposition (Amir Husain / Kindi & Generative Designer 2):** Public technical dialogues and comparative audits of `GD2` and `Kindi` reinforced our separation of probabilistic cognition from deterministic Python/Rust gates.
