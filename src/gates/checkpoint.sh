#!/usr/bin/env bash
# OMP vNext checkpoint gate.
#
# Validate the spine and every project plan, then commit them — and any project roots you pass. This is the
# executable form of "checkpoint before risky operations": you do not have to
# remember the rule, you run this before a refactor, migration, architecture change
# or destructive action. A failing validator refuses the checkpoint.
#
# The fact store joins the lifecycle: an unverified session-scoped claim does not survive a
# checkpoint (it is retired, its ledger kept), and a fact whose referent no longer hashes to
# its pin refuses the checkpoint until it is re-pinned or moved. memory.db is git-ignored, so
# the store is machine state on this box; what it proves still has to reach a text record.
# Each checkpoint also prints how the spine's own measured numbers bind (live fact, a
# file the spine line cites, or the durable record) and names the ones that bind to nothing.
#
# That lifecycle is a MUTATION: --evict-session retires rows and --check-anchors demotes
# drifted ones. --facts-off skips it — spine_check runs with --facts-off and the fact-store
# calls are not made — for a checkpoint that only needs the tree validated, or that must not
# touch the store's state. The skip is printed, never silent: the run says out loud that a
# drifted pin will not be named by it.
#
# Usage: tools/checkpoint.sh "message" [project_dir ...]
#        tools/checkpoint.sh -m "message" [project_dir ...]   (-m is accepted; the message is positional)
#        tools/checkpoint.sh --facts-off "message" [project_dir ...]   (skip the fact lifecycle)
#
# Before the first gate it checks the RUNTIME, not just the spine: if `python3` on PATH is
# older than the code the gates are written in, that used to surface as a SyntaxError from
# inside a gate, after the operator had read whatever the earlier gate printed. The floor is
# the code's own, not a preference — `:=` at tools/fact_store.py:662 parses only on 3.8+.
set -euo pipefail

# Self-location: `$0` first, `${BASH_SOURCE[0]}` only as the sourced fallback. Measured
# 2026-11-19 on this box (bash 5.3.0): the top-level script's BASH_SOURCE[0] comes back as the
# literal "bash" when the script is exec'd by this session's shell, while the same exec through
# `setsid` or `/usr/bin/env`, or from a python parent, reports the real path. A "bash" there made
# dirname return ".", so AGENT resolved to the PARENT of the caller's cwd, the pre-flight looked
# for tools/spine_check.py in a directory with no tools/ in it, and every checkpoint from an
# agent shell refused before any gate ran. `$0` is the invocation path on every bash; the
# fallback keeps `source tools/checkpoint.sh` working, where $0 is the interactive shell.
_self="$0"
[ -f "$_self" ] || _self="${BASH_SOURCE[0]:-}"
if [ ! -f "$_self" ]; then
  echo "checkpoint refused: cannot locate itself (\$0=\"$0\", BASH_SOURCE[0]=\"${BASH_SOURCE[0]:-unset}\"); run it as tools/checkpoint.sh from the agent root" >&2
  exit 1
fi
AGENT="$(cd "$(dirname "$_self")/.." && pwd)"
# A checkpoint means: validate and commit the spine the operator is looking at. Its fact
# calls below say "$AGENT/memory.db" in every refusal message, so an ambient
# FACT_STORE_DB would have the eviction and the anchor check act on a scratch store while
# the message named the live one (2026-11-11). Drop the knob before the first gate; a
# checkpoint of a scratch store is a direct fact_store.py/spine_check.py --store call.
unset FACT_STORE_DB

# Flags are consumed wherever they sit, and a flag that reaches the message slot is still a
# refusal: the -m incident was a flag landing in a positional slot, so the commit subject took
# the flag's shape and the real message was filed as a project dir ("not a git repo,
# skipped"). The message must never begin with '-', and an unknown flag is refused rather than
# quietly treated as the message.
MSG=""
DIRS=()
FACTS=1
while [ $# -gt 0 ]; do
  case "$1" in
    -m) ;;  # accepted, the message is positional
    --facts-off) FACTS=0 ;;
    -*) echo "checkpoint: message must not start with '-' (got \"$1\"); usage: tools/checkpoint.sh [--facts-off] \"message\" [project_dir ...]" >&2; exit 2;;
    *) if [ -z "$MSG" ]; then MSG="$1"; else DIRS+=("$1"); fi ;;
  esac
  shift
done
MSG="${MSG:-spine checkpoint}"

# Runtime pre-flight, before any gate or commit. Required gates are the ones whose failure
# refuses the checkpoint; the advisory ones already fail open below, so their absence is
# tolerated here too. `compile` is in-memory: no __pycache__ is written by a check.
PY_FLOOR="${OMP_PY_FLOOR:-3.8}"
if ! command -v python3 >/dev/null 2>&1; then
  echo "checkpoint refused: deps: uncomputed - python3 is not on PATH" >&2
  exit 1
fi
if ! py_pre=$(python3 - "$AGENT" "$PY_FLOOR" <<'PY' 2>&1
import pathlib, sys
root = pathlib.Path(sys.argv[1]) / "tools"
floor = tuple(int(x) for x in sys.argv[2].split("."))
if sys.version_info[:2] < floor:
    print("python3 on PATH is %s; the spine gates need >= %s"
          % (sys.version.split()[0], sys.argv[2]))
    sys.exit(1)
bad = []
for required, name in ((True, "spine_check.py"), (True, "plan_check.py"),
                       (False, "ownership.py"), (False, "fact_store.py")):
    path = root / name
    if not path.is_file():
        if required:
            bad.append("tools/%s is missing, so its gate cannot run" % name)
        continue
    try:
        compile(path.read_text(encoding="utf-8"), str(path), "exec")
    except (OSError, SyntaxError) as exc:
        bad.append("tools/%s: %s: %s" % (name, type(exc).__name__, exc))
if bad:
    print("\n".join(bad))
    sys.exit(1)
PY
); then
  echo "checkpoint refused: the python3 on PATH cannot run the spine gates (floor $PY_FLOOR):" >&2
  printf '%s\n' "$py_pre" >&2
  exit 1
fi

SPINE_ARGS=()
if [ "$FACTS" = 0 ]; then SPINE_ARGS=(--facts-off); fi

if ! python3 "$AGENT/tools/spine_check.py" "${SPINE_ARGS[@]}"; then
  echo "checkpoint refused: spine_check.py reported violations" >&2
  exit 1
fi

if ! python3 "$AGENT/tools/plan_check.py"; then
  echo "checkpoint refused: a project plan is invalid (see plan_check.py output above)" >&2
  exit 1
fi

# Advisory, never a refusal: which decisions have already left the session's head.
python3 "$AGENT/tools/ownership.py" --quiet || true

# Fact-store lifecycle, before the commit so the eviction is part of what gets validated.
# spine_check.py runs --check-anchors too (check 16); the call here is the one that evicts
# session residue and prints the repair command for a drifted anchor.
if [ "$FACTS" = 0 ]; then
  echo "checkpoint: fact lifecycle SKIPPED (--facts-off) - no eviction, no anchor check;" >&2
  echo "  a drifted pin in $AGENT/memory.db will not be named by this run." >&2
elif [ -f "$AGENT/memory.db" ]; then
  python3 "$AGENT/tools/fact_store.py" --evict-session \
    || echo "fact_store: --evict-session refused, continuing" >&2
  if ! anchors_out=$(python3 "$AGENT/tools/fact_store.py" --check-anchors 2>&1); then
    printf '%s\n' "$anchors_out" >&2
    case "$anchors_out" in
      *"no facts table"*|*"no store at"*)
        # VS-33: an unreadable store is a named deps failure, not an anchor drift.
        echo "checkpoint refused: deps: uncomputed - $AGENT/memory.db is not a readable fact store" >&2 ;;
      *)
        echo "checkpoint refused: a fact's referent no longer hashes to its pin (Item F15)" >&2
        echo "  re-pin:  python3 $AGENT/tools/fact_store.py --repin <fact_id> --why W --evidence E --gate G" >&2
        echo "  or move: python3 $AGENT/tools/fact_store.py --transition <fact_id> --direction demote ..." >&2 ;;
    esac
    exit 1
  fi

  # Advisory, never a refusal: an unbound spine number is a scheduling gap - the number can
  # outlive every run that produced it - not a broken store, and a refusing default would
  # train callers to skip the scan. --strict-spine is the opt-in stronger rule.
  python3 "$AGENT/tools/fact_store.py" --check-spine \
    || echo "fact_store: --check-spine unreadable, continuing" >&2
fi

# Spine-fact revalidation, before a commit exists. A spine commit moves the very lines active
# claims are pinned to - a doctrine file gets compacted, a tool's gate block gets rewritten - and
# a claim whose anchor no longer hashes is vouched for by nothing. --check-evidence re-hashes
# every archived gate receipt (a broken receipt means the run that justified the claim is gone,
# which an anchor re-hash alone cannot see) and --check-regime catches a claim quoting a regime
# other than the one it was struck in. Anchor drift refuses above; receipt failure refuses here,
# because the evidence behind a spine claim is the thing a checkpoint is for. Repair is a re-hash
# under a passing gate - `--repin ID --gate G --run-gate` - never a bare re-hash, which would
# launder an old claim onto new bytes.
if [ "$FACTS" = 0 ]; then
  echo "checkpoint: spine-fact evidence sweep SKIPPED (--facts-off)" >&2
elif [ -f "$AGENT/memory.db" ]; then
  if ! ev_out=$(python3 "$AGENT/tools/fact_store.py" --check-evidence 2>&1); then
    printf '%s\n' "$ev_out" | grep -v '^ *$' | head -12 >&2
    echo "checkpoint refused: an archived gate receipt behind a spine fact no longer hashes, so" >&2
    echo "  that claim is unjustified bytes. Revalidate under a gate that passes:" >&2
    echo "  python3 $AGENT/tools/fact_store.py --repin <fact_id> --gate GATE --run-gate --why W --evidence E" >&2
    exit 1
  fi
  # Advisory: an off-regime claim is still true of the regime it was struck in, and refusing a
  # checkpoint on a model swap would train callers to skip the sweep. The sweep demotes it to
  # conditional, which is the honest label; --strict-spine is where an operator opts into more.
  python3 "$AGENT/tools/fact_store.py" --check-regime >/dev/null 2>&1 \
    || echo "checkpoint: --check-regime demoted at least one off-regime spine fact to conditional" >&2
fi

# The pre-compaction sieve, per project, before the commit. A context about to collapse is the one
# that loses a spine fact, and a spine commit is exactly such a boundary. --repo PATH is what
# the pre-compact hook never passes, and its own text blames that omission for the timeout; the
# checkpoint supplies it once per project. Each scan reads only files inside its own root, so a
# directory holding no spine is a bounded sub-second scan and the refusal costs nothing next to a
# bad claim carried across the collapse. Fails closed: a project whose sieve cannot be bounded, or
# refuses, is not checkpointable.
if ! command -v timeout >/dev/null 2>&1; then
  echo "checkpoint refused: no 'timeout' on PATH, so no project sieve can be bounded; a project" >&2
  echo "  with an unboundable context cannot be checkpointed" >&2
  exit 1
fi
sieve_bad=""
for dir in "$AGENT" "${DIRS[@]}"; do
  [ -d "$dir" ] || continue
  if ! sv_out=$(timeout 120 python3 "$AGENT/tools/pre_compact_sieve.py" --repo "$dir" --json 2>&1); then
    printf '%s\n' "$sv_out" | tail -4 | sed "s|^|  [$dir] |" >&2
    sieve_bad="$sieve_bad $dir"
  fi
done
if [ -n "$sieve_bad" ]; then
  echo "checkpoint refused: the pre-compaction sieve failed closed for:$sieve_bad" >&2
  echo "  the run that justifies the re-read list is not bounded, so the spine cannot be" >&2
  echo "  checkpointed against it; read the printed reason, bound it, then re-run" >&2
  exit 1
fi

# The spine's own price, named where a session will read it: STATE.md's effective cap is the
# closure minus what the spine's doctrine files cost, and a digest that quotes only the nominal
# cap sends a resumed session to compact the wrong file. Same estimator as the spine gate (rehydrate
# imports spine_check), marked ~ because no tokenizer is reachable from the recovery path.
python3 - "$AGENT" <<'PRICE' || echo "checkpoint: spine price unavailable" >&2
import pathlib, sys
sys.path.insert(0, str(pathlib.Path(sys.argv[1]) / "tools"))
import rehydrate
rehydrate.spine_price_note()
PRICE

git -C "$AGENT" add -A
git -C "$AGENT" commit -q -m "$MSG" || echo "spine: nothing to commit"

for dir in "${DIRS[@]}"; do
  if git -C "$dir" rev-parse --git-dir >/dev/null 2>&1; then
    git -C "$dir" add -A
    git -C "$dir" commit -q -m "$MSG" || echo "$dir: nothing to commit"
  else
    echo "$dir: not a git repo, skipped" >&2
  fi
done

git -C "$AGENT" log --oneline -1
