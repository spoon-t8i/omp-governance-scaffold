---
layout: default
---

[← Back to Home](index.html)

# Positive & Negative Attractors in Autonomous Coding Loops

*Licensed under the Apache License, Version 2.0. See `LICENSE` and `NOTICE` in the repository root.*

An autonomous AI coding loop behaves like a dynamical control system. Over multi-hour sessions, the loop settles into repeating patterns called **attractors** (stable states that pull system behavior toward them). Without structural guardrails, local models drift into **negative attractors**—repeating traps where small mistakes compound into session failure.

---

## 1. Positive Attractors (Stable Verification Basins)

| Positive Attractor | How It Works | OMP Implementation | Measured Result |
| :--- | :--- | :--- | :--- |
| **Focal Attention Basin** | Caps active working memory so instructions never drown in old terminal logs. | `65,536`-token ceiling (~43k working room), frozen `1,704`-token spine at Position Zero, and 3 tools (`read`, `bash`, `write`). | **54–74.7 tokens/s** decode speed and **98.7%** median prefix-cache reuse. |
| **Clean State-Projection Basin** | Replaces lossy chat summaries with exact extraction of active goals, locks, and flags. | `pre_compact_sieve.py` (`S1–S4` Armor: `0 B` idle, `<=137` tokens active) plus clean-repo working directory tracking. | Survives `55K -> 34K` resets with zero `READ-ONLY` lock loss or cross-task bleed. |
| **External Oracle Grounding** | Forces every code change to pass an independent external program rather than self-written tests. | Checking decoders against `ffmpeg`, `git cat-file`, and SHA-256 byte-pinned receipts (`max|d| = 0`). | Caught 7 stacked `.VAG` audio bugs and a `-6.02 dB` volume math slip missed by local tests. |
| **Receipted Epoch Chain** | Ties state updates to cryptographic file hashes so valid plan changes succeed without silent decay. | `entail_check.py`, `plan_check.py`, and a Two-Commit Protocol (`H1` code commit followed by `H2` receipt commit). | **0.0%** silent drift and **0.0%** false-alarm gridlock across 24,000 simulated resets. |

---

## 2. Negative Attractors (Intercepted Failure Traps)

| Negative Attractor | Failure Pattern | Real Incident Receipt | Hard Circuit Breaker |
| :--- | :--- | :--- | :--- |
| **Boiling-Frog Secular Drift** | Tiny rounding shifts and quiet drops of `"DO NOT"` rules pass single resets yet corrupt state over 25+ resets. | Unconstrained summaries suffered **100%** silent drift; step-by-step delta checks still leaked **37.5%** over 25 resets. | Cryptographic epoch chains plus `entail_check.py` binding every summary number to a disk receipt. |
| **Flag-Drift Spin Loop** | Missing a hyphen in a command flag triggers repeated timeouts while the model guesses unrelated causes. | Typing `--selftest` instead of `--self-test` 6 times launched hidden GUI windows until `EXIT=124` timeout. | `S3` `PINNED-FLAGS` (hyphen-exact) plus `S4` `SPIN-LOOP-TRIPWIRE` (3-Probe Bailout + source read). |
| **Self-Certifying Test Illusion** | Broken code passes its own test suite because the test generator shares the exact same bug. | A legacy audio decoder passed `121/121` checks while playing loop flags as audio because its mirror encoder shared the flaw. | Mandatory external ground-truth oracles (`ffmpeg`) and two-sided mutant tests. |
| **False-Green Listener Trap** | A restart command exits non-zero while an old daemon still holds port `:8080`, tricking readiness checks into passing immediately. | An A/B script passed an invalid `--no-pull` flag (`RC=2`), hit an existing listener (`PID 732182`) in `0s`, and reported a false config reload. | Unmasked exit-code gating (`allow_abbrev=False`), process `STIME` binding, and `EXIT` trap verification. |
