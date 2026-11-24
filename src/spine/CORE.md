Rule 0 (OPERATOR OVERRIDE — HIGHEST PRECEDENCE): On "stop", "bailout", "status only", "existing info only", "no more tools", or a question not asking for verification/spine changes, IMMEDIATELY emit ZERO tool calls (no tool use), skip verification/closure gates, answer from existing context (label unverified items [UNVERIFIED/INTERIM]) — overrides all rules.

# OMP vNext — CORE

Mission: a project must survive this session's loss — reasoning: model; state: files; continuity:
tooling; truth > verifiable claim.

## Claims
Tag every claim: [A] measured, [B] source, [C] inference, [D] speculation, [E] hypothesis,
[F] unknown+validation. C-F never as A-B; A/B need receipts, C-F the settling check; never invent a
citation; uncheckable: name the check.
`entail_check.py --claims` audits each tag against the run's receipts.

## Engineering
Correctness, then six-month maintainability: delete dead weight, boring design over abstraction.
Execution kernel: disk beats recap (`git status -sb`); `set -euo pipefail`,
never mask a pipeline (`|| true`, `| head`, `| tail`); write only the explicit scope, scratch in /tmp.

## Artifacts
Injected: AGENTS.md, CORE.md, STATE.md (cap 780 tok, NOTE above 650) — price with --all-costs before
cutting. PERM.md indexes durable records, <project>/perm-record.md each closeout; EVENTS.md and the
vault on demand.

## Enforcement
Rules live in tools, not prose: `--help` = flags, docstring = contract,
`harness/<tool>_harness.py` = test; a script is a gate by declaring `# spine-gate: ARGS` in its first
40 lines. spine_check.py alone grades the spine: exit 0 = clean, a differing closure hash = stale; fix
the artifact or the check, never prose.
plan_check.py: a node closes only on a gate that ran, passed and is newer than its code.
ownership.py: which authority (PLANNING DESIGN CLOSURE STATE EXECUTION, LOCAL/ADVISORY) crossed
reasoning into procedure; read-only. checkpoint.sh: commit spine + projects before risky work, nothing
until revalidation and every sieve pass. rehydrate.py: resume a project — tree, closed nodes, ready
gate, failing attempts, dead branch, spine price. edit_file.py: multi-hunk edits, validated, atomic.
query_expand.py deterministic retrieval feeding `read`: concepts from identifier splits,
suffix stems and spine aliases, scored by correlated term density and proximity into top-K
path:N-M ranges; --fact-boost lifts the window a live fact pins so a dead claim cannot steer
`read`.
impact_analysis.py: what a change reaches, smallest gate set that proves it.
num_provenance.py: audited numbers bind to a declared evidence file or hard-fail.
fact_store.py: claims pin the SHA-256 of the slice that makes them true; drift demotes; only a --repin
gate run restores. pre_compact_sieve.py: re-read list before a collapse.
compaction_clock.py: which clock fired. surprise_check.py: zero-token disk probe, wakes with
[L2B-SURPRISE-INTERRUPT]; governance probes fail open, never stall a compaction or commit.

## Verification
Non-trivial change: run the thing; tool output is evidence, reasoning is not. Never bypass a gate — run
its own CLI, never an imported module or function. Two-sided controls: positive passes, negative fails.
Bind every numeric status to a runtime symbol plus one orthogonal check; a failed probe forks
`modules/FAULT_ISOLATION.md`, never a new launcher. Every STATE.md project has a runnable harness; a bug
keeps its failing-before/passing-after test; tests assert consumer-visible boundaries and error paths,
never wiring or tautologies.

## Recovery
Session can die: `tools/checkpoint.sh` before risky work, `tools/rehydrate.py` after restart; files over
memory. Repeated failure, unknown cause: `modules/FAULT_ISOLATION.md`.

## Ironclad Insight Forge v2.0
Truth > clarity > insight > usefulness > brevity. Abduction: H1/H2/H3 on fit, simplicity, testability,
downside -> action + confidence -> disconfirmers, next check; zero-mutation first. Hidden constraints,
second-order effects, false binaries. Code: tempfile fixtures; rows in == rows out.
