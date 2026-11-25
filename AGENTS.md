# AGENTS.md — Repository Map for Coding Agents

This repository publishes the **Oh-My-Pi (OMP) Governance Scaffold** (`spoon-t8i/omp-governance-scaffold`).

## Directory Layout
- `src/spine/` — Sanitized Position-Zero governance spine (`AGENTS.md`, `CORE.md`, `STATE.template.md`, `surprise_policy.json`).
- `src/hooks/` — Lifecycle interception hooks, including the `S1–S4` Pre-Compaction Sieve (`pre_compact_sieve.py`).
- `src/gates/` — Standalone deterministic verification gates (`spine_check.py`, `entail_check.py`, `plan_check.py`, `fact_store.py`, `regime_sensor.py`, `surprise_check.py`, `merkle_compactor.py`, `gate_exec.py`, `gate_freeze.py`, `checkpoint.sh`).
- `src/tools/` — Line-bounded surgical editor and context telemetry tools (`edit_file.py`, `impact_analysis.py`, `query_expand.py`, `rehydrate.py`, `session_telemetry.py`, `tok_probe.py`).
- `docs/` — GitHub Pages source (`index.html`, `_layouts/default.html`, `llms.txt`, `llms-full.txt`, and technical guides).

## Conventions
- Never commit local session databases (`*.db`), personal home paths (`/home/...`), or credentials.
- All documentation pages in `docs/*.md` use `layout: default` and are mirrored into `docs/llms-full.txt`.
