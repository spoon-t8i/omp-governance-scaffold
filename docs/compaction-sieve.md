[← Back to Home](index.html)

# Compaction Sieve Armor & Signal-to-Noise Engineering

*Licensed under the Apache License, Version 2.0. See `LICENSE` and `NOTICE` in the repository root.*

Most AI coding setups try to solve multi-hour tasks by expanding the **context window** (the total pool of text, measured in tokens, that an AI model can actively read at once). On local hardware, giant context windows often backfire. As logs accumulate, attention degrades, speed drops, and the model forgets critical rules.

This page explains how our **Oh-My-Pi (OMP)** scaffold solves multi-hour autonomy using a tight **65,536-token ceiling** (~43,000 tokens of active working room) paired with deterministic **pre-compaction sieve armor**.

---

## 1. The Signal-to-Noise Law of Local Inference

Every token inside a context window competes for a fixed 100% pool of model attention. When old terminal logs pile up past 64,000 tokens, **softmax dilution** (the gradual fading of focus as attention scores spread across thousands of background tokens) weakens the model's grip on your instructions.

This drop in **attention signal-to-noise ratio (SNR)**—the balance between critical rules and stale chat logs—hits quantized local models hardest:

1. **Protecting the Logit Margin:** Compressing a model to 4-bit precision (`NVFP4` or `Q4`) introduces a small **quantization noise floor** (tiny rounding variances in the model's math). Inside a clean 43k working window (~150–172 KB of text), the **logit margin**—the confidence gap favoring exact syntax like `--self-test` over a typo like `--selftest`—stays well above that noise floor.
2. **Preventing Self-Conditioning Cascades:** Models predict every new word based on everything still sitting in chat history. If a stale error stays inside a 200k window, it acts as a bias that pulls future turns into repeating that same mistake.
3. **Preserving Decode Speed:** Keeping the **KV cache** (stored memory calculations from earlier tokens) capped at 65,536 tokens prevents memory overflow onto slower system RAM, sustaining **54–74.7 tokens per second** over multi-hour runs.

---

## 2. How the 65K Context Budget Is Partitioned

| Context Partition | Token Budget | Role & Persistence Rule |
| :--- | :--- | :--- |
| **Frozen Governance Spine** | **1,704 tokens** | Pinned at Position Zero (`AGENTS.md`, `CORE.md`, `STATE.md`). Never evicted. |
| **Minimal Tool Schemas** | **~4,500 tokens** | Exposes only `read`, `bash`, and `write` (`--no-prewalk`), cutting 30+ unused IDE tools. |
| **Active Working Runway** | **~43,000 tokens** | Holds the current file slice, active test output, and recent reasoning (~150–172 KB). |
| **Compaction Safety Buffer** | **~16,000 tokens** | Headroom reserved so automatic summarization triggers cleanly around ~49.5k–55k tokens. |

Keeping the top of the prompt identical across turns achieves **98.7% median prefix-cache reuse** (reusing pre-computed math for the static prompt header instead of recalculating it on every step).

---

## 3. Anatomy of the `S1–S4` Pre-Compaction Sieve

Right before compaction runs (`55K -> 34K` tokens), `pre_compact_sieve.py` inspects the session and injects up to four deterministic **Sieve Armor** blocks:

* **`S1` (`ACTIVE-DIRECTIVE` & `PRIOR-TASKS-SUPERSEDED`):** Extracts your latest task instructions verbatim and explicitly marks older tasks from earlier in the session as dead.
* **`S2` (`MUTATION-POSTURE` & `LOCKED-PATHS`):** Pins your exact write permissions (`READ-ONLY` vs. `WRITE-APPROVED`) and forbidden directory paths so the model cannot modify protected files after a memory reset.
* **`S3` (`PINNED-ANCHORS` & `PINNED-FLAGS`):** Carries exact specification paths (`specs/*.md`), external verification binaries (such as `ffmpeg`), and hyphen-exact CLI flags (`--self-test`) across the reset boundary.
* **`S4` (`SPIN-LOOP-TRIPWIRE`):** Counts recent command timeouts (`EXIT=124` or `Killed`) and temporary probe scripts, triggering a mandatory **3-Probe Bailout** if a command fails repeatedly.

* **Inactive Cost:** `0 bytes` (`0 tokens`) when no guards trigger.
* **Active Ceiling:** Capped at **137 estimated tokens** (`<= 140` token hard limit), verified by **143 two-sided unit tests** (`106` sieve checks + `37` hook checks).
