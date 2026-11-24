#!/usr/bin/env python3
"""OMP vNext spine validator — the enforcement layer.

Design rule: a rule that costs tokens every turn is a rule the model has to
remember. A rule this script checks is a rule nobody has to remember. Every
governance requirement that survived the 2026-10-11 audits lives here as code.

Checks
  1. AGENTS.md @-includes resolve, and the injected closure is reported.
  2. Absolute paths referenced by the spine + PERM index exist.
  3. Project-relative references (harness/, game/, serve/, ...) resolve against
     known project roots, plus the directory named on the same line.
  4. Harness scripts named by the spine resolve their local imports.
     (The defect that actually bit this project: harness/p3_verify.js required
     ./game/sim.js, a module that never existed.)
  5. Loopback port claims in the docs match the real listener.
  6. Retired-vocabulary residue: exact phrases naming machinery the runtime no
     longer has. Warning unless --strict-retired.
  7. STATE.md schema: required headings present, every path the Recovery section
     names resolves (relative to the spine home, globs must match), file within its
     token cap. A recovery path that does not resolve is the one spine failure that
     cannot be worked around from inside a session.
  8. Every closed project (STATE.md "closed" marker) has a durable perm-record.
  9. Injected token budget: AGENTS.md + its @-include closure, measured by the
     live tokenizer (--fast estimates from characters; no server call).
 10. Injection freshness: a session freezes its injected spine at start, so a
     spine edited after that start is superseded for that session. Report the
     drift (note, not violation - disk cannot prove what a live session read).
 11. Claim referents + expiry: every measured quantity in the injected spine
     must name the thing that measured it (a script, harness, command or
     experiment id) and stay within CLAIM_TTL_DAYS. This is the defect that
     cost the most in this corpus: numbers kept as prose outlive their test.
     A claim that also states a general behavioral predicate ("caching exists",
     "reuse is content-keyed") fails on expiry instead of warning: a stale
     predicate is what gets acted on, not just quoted.
     The referent half is a presence test, never an execution test: it fails a claim
     that names nothing re-runnable, and it cannot tell whether the named artifact
     still says what the claim says (F15, notes_f15.md).
 12. Closure hash: sha256 of the exact injected closure text, so a session can
     tell whether the spine it was injected with is the spine on disk.
 13. STATE.md rollup pointers (FC-06 Concept A, tools/merkle_compactor.py): every
     folded cluster must re-hash to its Merkle root over the EVENTS.md vault, resolve
     exactly its declared child count (0/0 is never a pass), and keep its [dead:]
     citations dead. A spine that carries a folded ledger cannot drift from it silently.
 13. Project harness imports: every harness script under a project root resolves its
     local imports, not only the ones the spine names. Check 4 was written for the
     p3_verify.js defect and still missed it: the broken file was never named by a
     record, so a record-first traversal cannot see it (round 12, GATE G F1).
 14. Durable index coverage: every file in notes/ is named by PERM.md or by the
     injected spine. An unindexed finding is re-derived: the 2026-10-04 audit's answer
     was recomputed twice on 2026-10-11 because nothing pointed at the note holding it
     (GATE G F7/F12). Warning unless --strict-index.
 15. Gate declaration severity (--run-harness): a nonzero exit fails the spine only when the
     script is *declared* - named by operative text, or the whole gate of a closed plan node.
     A script a ledger merely mentions is still executed and still reported, as a note: a
     CLI helper under a retired project's harness/ dir is not a gate, and its usage error is
     not a spine violation (governance-eval/harness/shots.py, 2026-10-13).
 16. Fact-store anchors and receipts: every active or contested fact in memory.db must still
     hash to the sha256 of the source slice it names, and the archived gate output that
     justified a promotion must still hash to the pin its ledger row carries (Item F15,
     tools/fact_store.py). Check 11 only asks whether a claim names something re-runnable;
     this asks whether the named bytes still say it, and whether the evidence that proved it
     still reads. A stale anchor or a broken receipt fails the spine until --repin re-hashes
     it or --transition moves the fact.
 17. Gate self-description (--run-harness): the sweep runs a script the way the script says,
     reading `# spine-gate: ARGS`, `# spine-python: PATH` and `# spine-timeout: N` from its
     first 40 lines. --run-harness used to launch every named script as `<this python> script`
     and grade the result: a probe-only HTTP gate ran as a forever-server and timed out, and a
     venv-bound harness was scored ModuleNotFoundError under the spine's interpreter while its
     own venv passed it. A gate's runtime is one fact and the script holds it; GATE_ARGS stays
     the spine-side override, and a marker path that does not exist falls back rather than
     claiming a run that cannot happen.
 18. Gate receipts (--run-harness): every gate the sweep touches appends one row to
     `run/harness_loop.log` — stamp, tree head, gate path, rc, wall seconds, and the score
     in the shape the spine writes it (`224/0`), or a dash when the run produced no counts.
     A spine line stating a score can bind to that row (fact_store `--check-spine`, the
     cited route), which is the difference between "this file exists" and "this suite ran
     here". `run/` is gitignored, so a receipt is box-local liveness evidence; the durable
     home of a score stays EVENTS.md or a perm-record.
 19. Load pre-flight (--run-harness): a receipt is only evidence if the box was free to make it
     true, so a sweep started while the 1-minute load average is above `SWEEP_LOAD_GUARD` defers
     itself - it runs no gate and appends no row, leaving the last honest receipt standing. The
     run is clean (exit 0) with a NOTE, because a busy box is not a spine violation.

 20. L2A/L2B seam (--surprise): this gate is feedforward - it fires because a boundary happened
     (a spine edit, a commit, a compaction) and it audits structure. `tools/surprise_check.py` is
     the feedback half: it sleeps at zero token cost and wakes only when a receipt disagrees with
     the disk it claims to describe (a cited SHA no repo resolves, an evidence path that is gone,
     a gate stamped green on a tree the script has since outgrown, a launcher repeated without a
     pass). `--surprise` runs it against the spine and lets a fired detector fail this run, so a
     boundary can report a broken receipt even when every structural check above passes.
     OMP_SURPRISE=0 skips the probe and prints the skip, so a boundary that skipped the audit
     never reads like one that ran it and slept; an unusable policy is reported as ERROR,
     never as a pass.
Exit 0 = clean, 1 = at least one violation.
"""

import argparse
import ast
import datetime
import glob
import hashlib
import json
import os
import pathlib
import re
import shlex
import sqlite3
import subprocess
import sys
import time

AGENT = pathlib.Path(os.environ.get("SPINE_DIR", pathlib.Path.home() / ".omp" / "agent"))
# The spine's *words* live in AGENT, and SPINE_DIR may point them at a fixture tree so a
# gate can be tested without touching the live spine. The *machinery* does not move with
# them: launching fact_store.py or plan_check.py through AGENT made a fixture sweep look
# for tools that only exist beside this file.
TOOL_HOME = pathlib.Path(__file__).resolve().parent

# One store, one name for it, and a report that says which store it read. The 2026-11-11
# incident: a leaked `export FACT_STORE_DB=/tmp/vd4/v.db` left the live spine_check
# auditing a scratch database, so a printed FAIL described rows the live store never held.
# A number about a store is worth only its binding, so the binding is resolved once,
# printed in every report, and refused when ambient state would silently swap it.
STORE_OVERRIDE = None  # the --store value, set by authority_firewall


def live_store() -> pathlib.Path:
    """The canonical store of the injected spine: beside the real tools/ directory."""
    return (TOOL_HOME.parent / "memory.db").resolve()


def spine_is_live() -> bool:
    """The words under inspection are the injected ones, not a fixture tree."""
    return AGENT.resolve() == TOOL_HOME.parent.resolve()


def store_binding(cli_store=None):
    """(path, kind) for the store this run reads: --store, then FACT_STORE_DB, then the
    store beside the spine root. An empty variable is unset, which is how fact_store.py
    already treats it - one knob, one meaning, no third behaviour.
    """
    if cli_store:
        return pathlib.Path(cli_store).expanduser().resolve(), "--store explicit"
    env = (os.environ.get("FACT_STORE_DB") or "").strip()
    if env:
        p = pathlib.Path(env).expanduser().resolve()
        return p, ("canonical" if p == live_store() else "env FACT_STORE_DB")
    return (AGENT / "memory.db").resolve(), "root default"


def authority_firewall(args) -> int:
    """LIVE words and FIXTURE data never mix silently, in either direction.

    Live spine plus an ambient non-canonical FACT_STORE_DB: the run would print violations
    and receipts about a store the spine does not own (2026-11-11, /tmp/vd4/v.db). Fixture
    spine plus the canonical live store: a fixture's words would demote real claims. Both
    exit 2 before a gate runs or a byte is written. Pairing a root with a chosen store is
    legal but has to be deliberate, so it goes on the command line as --store. --facts-off
    opens no store at all, so there is nothing to misreport and the ambient value is named
    in the report rather than refused.
    """
    global STORE_OVERRIDE
    path, kind = store_binding(args.store)
    STORE_OVERRIDE = args.store
    # No child inherits a store by accident: every fact_store.py this run spawns is handed the
    # resolved path as --db. Pinning FACT_STORE_DB here made the CLI's two-name warning fire
    # inside the very run that set it, and the fact-anchor parse scored that stderr notice as a
    # violation (2026-11-11); overwriting the caller's environment for the whole child tree is
    # also the act this gate exists to catch in someone else's process.
    if args.facts_off:
        return 0
    if spine_is_live() and kind == "env FACT_STORE_DB":
        print(f"AUTHORITY_MISMATCH: live spine_check invoked with non-canonical"
              f" FACT_STORE_DB={path}", file=sys.stderr)
        print(f"  live spine plus a scratch store must be stated: --store {path}; for the"
              " live store, unset FACT_STORE_DB", file=sys.stderr)
        return 2
    if not spine_is_live() and args.store is None and path == live_store():
        print(f"AUTHORITY_MISMATCH: fixture spine root {AGENT} invoked against the"
              f" canonical live store {path}; a fixture's words must not adjudicate"
              " live claims", file=sys.stderr)
        print("  give the fixture its own store: FACT_STORE_DB=<fixture db> or --store"
              " <fixture db>", file=sys.stderr)
        return 2
    return 0


SPINE = ["AGENTS.md", "CORE.md", "STATE.md", "EVENTS.md", "PERM.md"]
ONDEMAND = ["OMP_STRUCTURE.md", "IMAGE_GEN.md", "PLATFORM_REF.md",
           "FORGE_FAST_CHANGELOG.md", "perm-record.md"]
DOCS = SPINE + ONDEMAND

# Ledgers record what happened; operative text says what to do. Only operative text
# is scanned for retired phrases - a changelog or event log naming retired machinery
# is doing its job, not leaking dead vocabulary into instructions.
HISTORICAL = {"FORGE_FAST_CHANGELOG.md", "EVENTS.md"}

# Rule-bearing files vs ledgers: injection freshness only means anything for the
# files that define how a session behaves.
GOVERNANCE_FILES = {"AGENTS.md", "CORE.md"}
LEDGERS = {"STATE.md", "EVENTS.md", "PERM.md"}

# Runtime liveness is an operative assertion, not a ledger entry: a ledger records that a
# service was up when the entry was written, so a dead listener there is history, not a lie.
# Only files that tell a session what is true now can fail on it.
OPERATIVE_DOCS = [n for n in DOCS if n not in LEDGERS and n not in HISTORICAL]
# STATE.md is spine-internal: schema-capped and rollup-checked as a ledger, yet injected as
# instruction. A dead service in the active bullet list is read as current fact by every
# prompt, so runtime liveness is graded against it as operative text.
RUNTIME_DOCS = OPERATIVE_DOCS + ["STATE.md"]

# Measured by `python3 tools/spine_check.py --all-costs` on 2026-11-14: AGENTS 25 + CORE 1,827
# + STATE 611 = 2,463 tok - above the 1,800 ideal, 37 tok under MAX. The price this line quoted
# (CORE 764 + STATE cap 900 = 1,689) priced a spine that no longer exists and hid a real
# inconsistency: with CORE at 1,827 the nominal STATE ceiling can never bind, because
# 25 + 1,827 + 780 = 2,632 exceeds MAX_TOKENS. STATE.md is graded instead against the room the
# closure actually leaves - state_budget() gives 2,500 - 1,852 = 648 tok here.
IDEAL_TOKENS = 1800
MAX_TOKENS = 2500
EMERGENCY_TOKENS = 4000
# FC-06 (2026-10-19): cap retuned 900 -> 620 from the first compaction pass (measured 565).
# W5-B (2026-10-20): 620 was a hair trigger (8 tok headroom at 612). Replaced with a
# hysteresis band mirroring the aggregate IDEAL/MAX design - the hard ceiling locks the
# compaction gain (780 < 900 permanently) while 168 tok of working headroom stays open.
STATE_IDEAL_TOKENS = 650   # advisory compaction NOTE, non-failing
STATE_MAX_TOKENS = 780     # hard failure ceiling
STATE_HEADINGS = ["# STATE", "## Active", "## Done", "## Next", "## Recovery"]

# Load pre-flight (2026-11-06, notes/2026-11-06_governance-eval-spine-closure.md §6). Two
# harnesses failed at 22:35Z by losing a wall-clock race to the inference engine mid-sweep and the
# ledger recorded those flaps as gate verdicts. Absolute load, not a CPU-count multiple: the 12
# threads here are shared with the engine by design, the engine sits at ~700 % by design, and that
# exact state is the flap condition - a headroom-relative guard would never fire.
SWEEP_LOAD_GUARD = float(os.environ.get("SPINE_LOAD_GUARD", "8.0"))
SWEEP_LOAD_PATH = pathlib.Path("/proc/loadavg")


def sweep_load1():
    """The 1-minute load average, or None when it cannot be read - never a silent zero."""
    try:
        return float(SWEEP_LOAD_PATH.read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def sweep_deferral(load, guard, force=False, targeted=False):
    """Pre-flight decision -> (defer, note).

    Unreadable load runs anyway and says so: a missing number is not evidence of an idle box. A
    targeted `--only` gate or `--force` runs hot with a note, because the operator asked for that
    verdict now; the unattended full sweep is the case that appends dozens of receipts.
    """
    if load is None:
        return False, (f"load average unreadable ({SWEEP_LOAD_PATH}): the guard cannot price the "
                       "box, running anyway")
    if load <= guard:
        return False, None
    if force or targeted:
        return False, (f"load {load:.1f} > guard {guard:.1f}, running anyway: these gates were "
                       "asked for now")
    return True, (f"load average {load:.1f} > SWEEP_LOAD_GUARD {guard:.1f}: sweep deferred, no gate "
                  "run, no receipt written (--force runs it hot)")

# Exact phrases, not generic words: "gate" is legitimate (phase gates),
# "CLOSE VALIDATOR" names machinery that no longer exists.
RETIRED_TERMS = [
    "FSM transitions", "FSM ownership", "attention ownership", "work trail",
    "close validator", "circuit breaker", "Phase Zero", "budget governance",
    "adaptive scope", "PWCC",
]

ROOTS = [
    AGENT,
    pathlib.Path.home() / "neon_rush",
    pathlib.Path("/mnt/data/strata-workspace/Strata"),
    # Strata 0.2.16+ moved serve/ and tools/ under terminal/; guard with is_dir() so both layouts work.
    *[p for p in (pathlib.Path("/mnt/data/strata-workspace/Strata/terminal"),) if p.is_dir()],
    pathlib.Path.home() / "Projects/morphofractal",
    pathlib.Path.home() / "Projects/amber_ascii",
    # amber_particles is a live fork, not a scratch copy: its receipts and harnesses are cited by
    # path in EVENTS, so leaving it out made every line about it read as a dead path.
    pathlib.Path.home() / "Projects/amber_particles",
    pathlib.Path.home() / "Projects/ascii_city",
    pathlib.Path.home() / "Projects/chromatic-gravity",
    pathlib.Path.home() / "Projects/game-archive",
    pathlib.Path.home() / "Projects/chemgym-v2",
    pathlib.Path.home() / "Projects/loom",
    pathlib.Path.home() / "Projects/aquarium_sim",
    pathlib.Path.home() / "Projects/chemgym-recon",
    pathlib.Path.home() / "Projects/chromatic_earthworm",
    pathlib.Path.home() / "Projects/chromatic_fermi_phase7",
    pathlib.Path.home() / "Projects/cityblock-perf",
    pathlib.Path.home() / "Projects/governance-outcome-trial",
    pathlib.Path.home() / "Projects/heart_sim",
    pathlib.Path.home() / "Projects/kindi",
    pathlib.Path.home() / "Projects/lantern-godot",
    pathlib.Path.home() / "Projects/omp-constitutional",
    pathlib.Path.home() / "Projects/omp-failure-taxonomy",
    pathlib.Path.home() / "Projects/omp-fractal-crucible",
    pathlib.Path.home() / "Projects/omp-vnext-synthesis",
    pathlib.Path.home() / "Projects/omp_surprise_tournament",
    pathlib.Path.home() / "Projects/orbital_mandel",
    pathlib.Path.home() / "Projects/seam_reconstitute_crucible",
    pathlib.Path.home() / "Projects/secular_drift_crucible",
    pathlib.Path.home() / "Projects/tsforge-sandbox",
    pathlib.Path.home() / "Projects/verifier_rot_crucible",
    pathlib.Path.home() / "Projects/verlet-bridge",
    pathlib.Path.home() / "Projects/xa-cd-audio",
    pathlib.Path.home(),
]

# Roots that get *scanned* (check 13). `home` is in ROOTS only as a resolution
# fallback; rglob over it is not a check, it is a filesystem walk. A function, not a
# module constant, so a harness that repoints ROOTS repoints the scan too.
def scan_roots():
    return [r for r in ROOTS if r != pathlib.Path.home()]


SKIP_DIRS = {".git", ".venv", "__pycache__", "build", "build-vision", "node_modules", "archive"}

# The left boundary is a lookbehind, not \b: `game-archive/tools/serve.py` has a word
# boundary before `archive`, so \b let the scanner harvest `archive/tools/serve.py` out of a
# hyphenated project's path and report it unresolved (EVENTS.md 2026-11-05).
REL_RE = re.compile(
    r"(?<![A-Za-z0-9_.-])(?:harness|game|serve|tools|docs|src|include|bench|shots|modules|notes|archive)/"
    r"[A-Za-z0-9_./-]+\.(?:js|md|png|py|json|html|sh)\b"
)
ABS_RE = re.compile(r"/(?:home|mnt|opt|usr)/[A-Za-z0-9_./-]+")
PORT_RE = re.compile(r"127\.0\.0\.1:(\d{2,5})")
# Shorthand form ("hgka-web :8777") - the spine's own habit. Scanned, but only ever a
# NOTE: the shape also occurs as an HTTP verb or a line reference, so it cannot carry a
# violation. It exists so a dead service named in operative text stops being invisible.
PORT_SHORT_RE = re.compile(r"\b[\w.-]{3,}\s+:(\d{4,5})\b")
PERM_RE = re.compile(r"^\s*[-*]?\s*\[[^\]]*\]\(([^)]+/(?:perm-record|PERM)[^)]*)\)")
CLOSED_RE = re.compile(r"^\*\*Status:\*\*.*closed", re.M | re.I)


def text_of(name):
    p = AGENT / name
    return p.read_text(encoding="utf-8") if p.exists() else None


def read_docs():
    for name in DOCS:
        yield name, text_of(name)


def injected_closure():
    """AGENTS.md plus every @-include, transitively."""
    order = ["AGENTS.md"]
    seen = set(order)
    queue = list(order)
    while queue:
        name = queue.pop(0)
        body = text_of(name)
        if body is None:
            continue
        for m in re.finditer(r"^@([A-Za-z0-9_./-]+)$", body, re.M):
            target = m.group(1)
            if target not in seen:
                seen.add(target)
                order.append(target)
                queue.append(target)
    return order


def check_includes():
    bad = []
    body = text_of("AGENTS.md") or ""
    for m in re.finditer(r"^@([A-Za-z0-9_./-]+)$", body, re.M):
        if not (AGENT / m.group(1)).exists():
            bad.append(f"AGENTS.md @-include missing: @{m.group(1)}")
    return bad


def check_abs_paths():
    bad = []
    for name, text in read_docs():
        if text is None:
            continue
        if name in HISTORICAL:
            continue  # same rule as retired vocabulary: a ledger names files as they were on the
                      # day it was written. Deleting one later is not a false entry.
        seen = set()
        for m in ABS_RE.finditer(text):
            raw = m.group(0)
            if raw in seen:
                continue
            seen.add(raw)
            if text[m.end():m.end() + 1] == "<":
                continue  # template placeholder (path_<ts>.png), not an existence claim
            path = raw.rstrip("/.,:")
            if path.endswith((".js", ".py")):
                continue  # handled by check_rel_paths
            if not pathlib.Path(path).exists():
                bad.append(f"{name}: dangling absolute path -> {path}")
    return bad


def line_roots(line):
    roots = list(ROOTS)
    for raw in ABS_RE.findall(line):
        p = pathlib.Path(raw.rstrip(".,:"))
        if not p.is_dir():
            p = p.parent
        for _ in range(4):
            roots.append(p)
            if p.parent == p:
                break
            p = p.parent
    return roots


def check_rel_paths():
    """A relative path resolves if it exists under a root the same line names, so a key that
    resolved once is not evidence for the next line: roots are per-line, and a `seen` set
    keyed on the path alone suppressed a later line's dead path because an earlier line had
    resolved the same text elsewhere."""
    bad = []
    resolved = set()
    for name, text in read_docs():
        if text is None:
            continue
        for line in text.splitlines():
            for raw in REL_RE.findall(line):
                key = raw.rstrip(".,:")
                if key in resolved:
                    continue
                if any((root / key).exists() for root in line_roots(line)):
                    resolved.add(key)
                    continue
                bad.append(f"{name}: unresolved project path -> {key}")
    return bad


def retired_vocabulary_files():
    files = [(name, AGENT / name) for name in DOCS]
    mods = AGENT / "modules"
    if mods.is_dir():
        files += [(f"modules/{p.name}", p) for p in sorted(mods.glob("*.md"))]
    return files


def check_retired_vocabulary():
    hits = []
    for name, path in retired_vocabulary_files():
        if not path.exists() or name in HISTORICAL:
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            low = line.lower()
            for term in RETIRED_TERMS:
                if term.lower() in low:
                    hits.append(f"{name}:{lineno}: {term}")
    return hits


def proc_net_tcp_listeners():
    inodes = {}
    for f in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            lines = pathlib.Path(f).read_text().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            parts = line.split()
            if len(parts) < 10 or parts[3] != "0A":
                continue
            inodes.setdefault(int(parts[1].split(":")[1], 16), parts[9])
    return inodes


def pid_for_inode(inode):
    for proc in pathlib.Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        try:
            for fd in (proc / "fd").iterdir():
                try:
                    if fd.resolve().name == f"socket:[{inode}]":
                        return proc.name, (proc / "comm").read_text().strip()
                except OSError:
                    continue
        except OSError:
            continue
    return None, None


def check_port_claims():
    """Liveness of every loopback port the docs name, by literal form.

    A full literal (127.0.0.1:PORT) is a claim the reader can act on, so a dead listener in
    operative text is a violation; in a ledger it is history and stays a report line. A
    shorthand claim (name :PORT) is NOTE-only. 2026-10-27: STATE.md asserted
    `hgka-web :8777` while nothing listened, and no rule saw it - the port rule read only
    the full literal form and graded even that as informational.
    """
    notes, problems = [], []
    inodes = proc_net_tcp_listeners()
    resolved = {}

    def listener_of(port):
        if port not in resolved:
            pid, comm = (None, None)
            if port in inodes:
                pid, comm = pid_for_inode(inodes[port])
            resolved[port] = (pid, comm)
        pid, comm = resolved[port]
        return f"pid {pid} ({comm})" if pid else None

    for name, text in read_docs():
        if text is None:
            continue
        for port in sorted({int(p) for p in PORT_RE.findall(text)}):
            listener = listener_of(port)
            notes.append(f"  claim in {name}: 127.0.0.1:{port} -> actual: {listener or 'NO LISTENER'}")
            if listener is None and name in RUNTIME_DOCS:
                problems.append(f"dead runtime claim: {name} asserts 127.0.0.1:{port}, no listener")
        for port in sorted({int(p) for p in PORT_SHORT_RE.findall(text)}):
            if port in {int(p) for p in PORT_RE.findall(text)}:
                continue
            if listener_of(port) is None and name in RUNTIME_DOCS:
                notes.append(f"  NOTE {name} names :{port} and nothing listens (shorthand claim; "
                             f"verify the service or record it down)")
    return notes, problems


def token_rule(referent):
    """A budget failure must name the check that priced it, the box that carries the cap, and
    the referent that decides - here that referent is a measured cost plus a rule in this file,
    so the operator can re-run the measurement without reading the source."""
    return (f"check=tokenizer_costs  rule=CORE.md \"Cap {STATE_MAX_TOKENS} tok\"  "
            f"referent={referent}  rerun=python3 tools/spine_check.py --all-costs")


def tokenizer_costs(texts):
    """prompt_tokens per string from the live local server; None if unreachable."""
    import json
    import urllib.request

    def served_model():
        """The id the server actually serves; a hardcoded label goes stale."""
        with urllib.request.urlopen(
            "http://127.0.0.1:8080/v1/models", timeout=30
        ) as r:
            return json.load(r)["data"][0]["id"]

    try:
        model = served_model()
    except Exception as exc:
        return {"__error__": f"{type(exc).__name__}: {exc}"}

    def ask(content):
        body = json.dumps(
            {
                "model": model,
                "messages": [
                    {"role": "system", "content": content},
                    {"role": "user", "content": "hi"},
                ],
                "max_tokens": 1,
                "temperature": 0.0,
                "chat_template_kwargs": {"enable_thinking": False},
            }
        ).encode()
        with urllib.request.urlopen(
            urllib.request.Request(
                "http://127.0.0.1:8080/v1/chat/completions",
                data=body,
                headers={"Content-Type": "application/json"},
            ),
            timeout=180,
        ) as r:
            return json.load(r).get("usage", {}).get("prompt_tokens")

    out = {}
    for key, content in texts.items():
        try:
            out[key] = ask(content)
        except Exception as exc:
            out[key] = None
            out["__error__"] = f"{type(exc).__name__}: {exc}"
    return out


def state_budget(costs, closure, limit):
    """STATE.md's ceiling for THIS run: its nominal cap, or the room the rest of the injected
    closure leaves under `limit`, whichever is smaller; the working ideal shrinks with it.

    The nominal per-file ceiling was the only number ever printed beside STATE.md, so a spine
    whose AGENTS.md + CORE.md already cost 1852 of a 2500 tok aggregate could call a 660 tok
    STATE.md "under cap 780" when the closure left it 648 tok. The reader was told to compact a
    file that had no room, and the cap that would actually bind was never named. Charging a
    member the tokenizer did not answer for (None) at 0 is the same approximation the aggregate
    total already makes; an unmeasured STATE.md is shown, never graded.
    Returns (cap, ideal, others_cost, room, measured).
    """
    others = sum(costs.get(n) or 0 for n in closure if n != "STATE.md")
    room = limit - others
    cap = min(STATE_MAX_TOKENS, room)
    return cap, min(STATE_IDEAL_TOKENS, cap), others, room, costs.get("STATE.md")


def state_note_line(cost, cap, ideal, others, room, limit):
    """The advisory line: over the working ideal, inside the effective cap. It names BOTH caps
    and who took the aggregate, because "under cap 780" is the sentence that hid the bound."""
    return (f"  NOTE STATE.md {cost} tok above ideal ({ideal}); effective cap {cap} tok "
            f"(nominal {STATE_MAX_TOKENS}) - the rest of the closure costs {others} tok of "
            f"the {limit} tok aggregate, leaving {room} tok; compaction advised before next "
            "registration")


def state_violation_line(cost, cap, others, room, limit):
    """The failing line and its referent, naming which cap ran out. Over the effective cap is
    a spine problem even when the file sits under its nominal ceiling, so a repair aimed at the
    nominal number cannot fix it - the referent says whether the squeeze or the ceiling bound."""
    which = (f"effective cap {cap} tok = {limit} aggregate less {others} tok spent on the "
             f"rest of the closure (nominal STATE.md cap {STATE_MAX_TOKENS} tok)"
             if cap < STATE_MAX_TOKENS else f"nominal cap {STATE_MAX_TOKENS} tok")
    return (f"STATE.md {cost} tok exceeds its effective {cap} tok cap",
            token_rule(f"STATE.md measured {cost} tok, {which}"))


def effective_cap(costs, closure, limit, price):
    """Which cap the spine's price actually ran out of, and the headroom it leaves.

    The budget section prints an aggregate ideal/max/emergency and a per-file ceiling, and
    from those a reader cannot tell which one bound: a spine whose STATE.md sits at 779 of
    780 tok needs a compaction, a spine at 2400 of 2500 tok needs a spine file cut, and both
    read as "under the limit". Printing the tightest price/cap ratio names the repair.

    A cost the live tokenizer did not answer (None) carries no ratio: an unmeasured file
    must never pose as the binding constraint, or a server outage prints a healthy cap.
    """
    cands = [(limit, price, "injected closure")]
    cap_eff = state_budget(costs, closure, limit)[0]
    for n in closure:
        c = costs.get(n)
        if not c:
            continue
        cands.append((cap_eff if n == "STATE.md" else MAX_TOKENS, c, n))
    cap, cost, name = max(cands, key=lambda t: (t[1] / t[0]) if t[0] else 0.0)
    return name, cap, cost, cap - cost


def effective_cap_line(costs, closure, limit, price, approx=False):
    """The one line that answers "what did the spine cost, and what capped it?".

    approx marks a char estimate (--fast, or no tokenizer reachable): the caps are exact,
    the price is not, and a line that hides which number is soft is how an estimate starts
    being quoted as a measurement.
    """
    name, cap, cost, head = effective_cap(costs, closure, limit, price)
    soft = "~" if approx else ""
    tail = "  (char estimate)" if approx else ""
    return (f"  effective cap {cap} tok ({name})  price {soft}{cost} tok  "
            f"headroom {soft}{head} tok{tail}")


def run_surprise_gate(root):
    """Run the L2B feedback gate over `root` at an L2A boundary.

    Returns (report_line, kind, body_lines) with kind in {"dormant","fired","unavailable","skip"}.
    The probe is priced by the char estimator (`--token-url ""`) on purpose: a boundary that had to
    reach a tokenizer to price its own alarm could stall a compaction on a port that is not
    listening, and an alarm that arrives after the boundary is worth nothing. Fail-open is the
    whole design - a governance probe must never be the reason a gate crashed - so every error
    path returns a line and a kind, never raises.
    """
    tool = pathlib.Path(__file__).resolve().parent / "surprise_check.py"
    if not tool.exists():
        return f"  SKIP      no surprise_check.py beside this gate ({tool.parent})", "skip", []
    cmd = [sys.executable, str(tool), "--repo", str(root), "--quiet",
           "--token-url", "", "--max-blocks", "2"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    except Exception as exc:  # timeout, missing interpreter, permission
        return f"  ERROR     L2B probe could not run: {type(exc).__name__}: {exc}", "unavailable", []
    rc = proc.returncode
    if rc == 0:
        return ("  DORMANT   L2B 0 surprises - feedback gate silent on intact receipts",
                "dormant", [])
    if rc == 2:
        why = (proc.stderr or proc.stdout or "").strip().splitlines()
        return (f"  ERROR     L2B unusable: {why[-1][:160] if why else 'exit 2, no message'}",
                "unavailable", [])
    if rc != 1:
        return (f"  ERROR     L2B exit {rc} is not a verdict this seam understands "
                "(0 dormant / 1 fired / 2 unusable)", "unavailable", [])
    body = [ln for ln in (proc.stdout or "").splitlines() if ln.strip()]
    n = sum(1 for ln in body if ln.startswith("[L2B-SURPRISE-INTERRUPT"))
    return (f"  SURPRISE  L2B fired {n} interrupt(s) against {root}", "fired", body)


def verdict_line(problems, budget=340, field=120):
    """The last line, carrying the verdict AND every violation's own words.

    On 2026-11-15 a run printed `result: FAIL (1 violation(s))`, the caller piped it through
    `| tail -2`, and the fault lost its name: the count survived, the check that raised it did
    not, so the failure could only be filed as unexplained. A footer that repeats what each
    violation says cannot be emptied that way - truncating the report costs the detail, never
    the identity. Each entry keeps its leading identifier (`STALE ...`, `STORE ...`,
    `STATE.md ... exceeds`), newlines are flattened so the verdict stays ONE line, and the
    overflow is counted (`+N more`) rather than cut, so the number still matches the run.
    """
    n = len(problems)
    if not n:
        return "\nresult: PASS (0 violation(s))"
    word = "violation" if n == 1 else "violations"
    parts, used, shown = [], 0, 0
    for p in problems:
        text = " ".join((p[0] if isinstance(p, tuple) else str(p)).split())
        if len(text) > field:
            text = text[:field - 3] + "..."
        if shown and used + len(text) + 2 > budget:
            break
        parts.append(text)
        used += len(text) + 2
        shown += 1
    if shown < n:
        parts.append(f"+{n - shown} more")
    return f"\nresult: FAIL ({n} {word}: " + "; ".join(parts) + ")"


def recovery_targets(recovery: str) -> list:
    """Every path a Recovery bullet names, in order.

    The old parser took the bullet's first whitespace token, so "perm-record.md; PERM.md;
    notes/" checked one path and checked it under the wrong name (the trailing semicolon
    became part of the filename, and rehydrate reported a false MISSING on 2026-10-14).
    Entries are ';'-separated, may be backticked, may be globs, may end in punctuation.
    """
    out = []
    for line in recovery.splitlines():
        line = re.sub(r"^\s*[-*]\s*", "", line).strip()
        if not line or line.startswith("#"):
            continue
        for chunk in line.split(";"):
            target = chunk.strip().strip("`*").strip().rstrip(",;.")
            if target and " " not in target:
                out.append(target)
    return out


def recovery_resolves(target: str) -> bool:
    """Absolute paths as written; relative ones against the spine home, then the known
    project roots (STATE.md names `Projects/*/perm-record.md` relative to home, not to
    the spine home). A glob has to match at least one file."""
    if target.startswith("/"):
        candidates = [pathlib.Path(target)]
    else:
        seen, candidates = set(), []
        for root in [AGENT, *ROOTS]:
            if str(root) not in seen:
                seen.add(str(root))
                candidates.append(root / target)
    for path in candidates:
        if any(ch in target for ch in "*?["):
            if glob.glob(str(path)):
                return True
        elif path.exists():
            return True
    return False


def check_state_schema():
    bad, notes = [], []
    body = text_of("STATE.md")
    if body is None:
        return ["STATE.md missing"], notes
    for heading in STATE_HEADINGS:
        if heading not in body:
            bad.append(f"STATE.md missing required heading: {heading}")
    if "## Recovery" in body:
        for target in recovery_targets(body.split("## Recovery", 1)[1]):
            if not recovery_resolves(target):
                bad.append(f"STATE.md recovery path missing: {target}")
    notes.append(f"  headings ok, {len(body.splitlines())} lines")
    return bad, notes


def check_rollups():
    """FC-06 Concept A: STATE.md rollup pointers must resolve against the vault.

    Recomputes each Merkle root over the EVENTS.md child lines, enforces the declared
    denominator (a dropped child is DENOMINATOR_MISMATCH; children:0 never passes), and
    confirms every [dead:] citation inside a child is absent on disk - checked by
    existence only, the file is never opened. Absence of pointers is legitimate (nothing
    folded yet) and reported as 0 pointers scanned; a pointer that fails is a violation.
    """
    state = AGENT / "STATE.md"
    events = AGENT / "EVENTS.md"
    text = state.read_text(encoding="utf-8", errors="replace") if state.is_file() else ""
    if "rollup:" not in text:
        return [], ["  rollup pointers: 0 folded clusters (nothing to verify)"]
    tools_dir = str(TOOL_HOME)
    if tools_dir not in sys.path:
        sys.path.insert(0, tools_dir)
    import merkle_compactor
    ok, code, detail = merkle_compactor.verify_rollup(str(state), str(events),
                                                      root_base=str(AGENT))
    results = detail.get("results", [])
    passed = sum(1 for r in results if r.get("ok"))
    note = (f"  rollups verified {passed}/{len(results)} scanned of "
            f"{detail.get('pointers', 0)} pointer(s): roots, denominators, tombstones")
    if ok:
        return [], [note]
    failed = detail.get("failed", {})
    return [f"rollup {code} at {failed.get('anchor', '?')}: "
            f"{failed.get('error', '')} | {note.strip()}"], []


def check_closed_projects():
    """A closed project must have a durable record outside the chat."""
    bad = []
    state = text_of("STATE.md") or ""
    perm = text_of("PERM.md") or ""
    for m in re.finditer(r"/(?:home|mnt)/[A-Za-z0-9_./-]+/", state):
        root = m.group(0).rstrip("/")
        p = pathlib.Path(root)
        if not p.is_dir():
            continue
        closed = bool(CLOSED_RE.search(state.split(root, 1)[1][:400]))
        if not closed:
            continue
        records = list(p.glob("perm-record*.md"))
        if not records:
            bad.append(f"closed project without durable record: {root}/perm-record.md")
        elif root.split("/")[-1] not in perm:
            bad.append(f"closed project not indexed in PERM.md: {root}")
    return bad


# Module names a harness is expected to import locally.
LOCAL_HINTS = {"sim", "best", "car", "track", "traffic", "hazards", "driver", "prng",
               "render", "input", "loop", "main", "winjob", "setup"}


SESSION_START_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2})-(\d{2})-(\d{2})")


def check_injection_freshness(limit=6, live_window_s=24 * 3600):
    """Sessions freeze their injected closure at start. If the spine was edited
    after a session began, that session is running a superseded spine and no
    disk-side check can see it. Report the drift so it is at least visible."""
    # Only the rule-bearing files count. STATE.md/EVENTS.md are ledgers that get
    # appended to constantly, so every session predates their last write and the
    # comparison would be noise. A superseded *rule set* is the actionable drift.
    newest, newest_name = None, None
    for name in injected_closure():
        if name in LEDGERS:
            continue
        path = AGENT / name
        if path.exists():
            m = path.stat().st_mtime
            if newest is None or m > newest:
                newest, newest_name = m, name
    if newest is None:
        return []
    sess = AGENT / "sessions"
    if not sess.is_dir():
        return []
    seen = set()
    stale, total = [], 0
    now = datetime.datetime.now(datetime.timezone.utc).timestamp()
    for cwd_dir in sorted(sess.iterdir()):
        if not cwd_dir.is_dir():
            continue
        for entry in sorted(cwd_dir.iterdir()):
            m = SESSION_START_RE.match(entry.name)
            if not m:
                continue
            key = (cwd_dir.name, entry.name.split(".", 1)[0])
            if key in seen:
                continue
            seen.add(key)
            total += 1
            start = datetime.datetime(*[int(g) for g in m.groups()],
                                      tzinfo=datetime.timezone.utc).timestamp()
            # Only sessions that could still be running matter: a session that
            # last wrote its transcript days ago is finished, not mis-governed.
            last = max((f.stat().st_mtime for f in entry.rglob("*") if f.is_file()),
                       default=entry.stat().st_mtime)
            if now - last > live_window_s:
                continue
            if start < newest:
                stale.append((start, key[1], cwd_dir.name))
    if not stale:
        return [f" no session active in the last {live_window_s // 3600} h predates the "
                f"last rule edit ({total} session(s) on record)"]
    stale.sort(reverse=True)
    shown = ", ".join(f"{name} @{cwd}" for _, name, cwd in stale[:limit])
    more = f" (+{len(stale) - len(shown.split(', '))} older)" if len(stale) > len(shown.split(", ")) else ""
    edited = datetime.datetime.fromtimestamp(newest, datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return [f" NOTE rules last edited {edited} ({newest_name}); {len(stale)} active "
            f"session(s) started before it and may hold a superseded closure "
            f"({total} on record): "
            f"{shown}{more}"]


# A measured quantity: number + unit. Prose numbers ("12/12", "n=12", "30 d")
# are counts, not measurements, and are not claimed to be re-checkable.
MEASURE_RE = re.compile(r"\d[\d,]*\.?\d*[\s-](s\b|ms\b|tok\b|tokens\b|px/s\b|rad\b|fps\b|%)")
# Something a reader can actually re-run to re-derive the number: a file under a
# project root, or a command. A prose mention ("Round 13", "results12") or a bare
# directory prefix ("tools/", "harness/") is NOT a referent - it lets a claim vouch for
# itself. F15 (2026-10-13) measured the live Round-13 claim satisfying its own
# requirement on the words "Round 13" alone (notes_f15.md, variant M7c).
REFERENT_RE = re.compile(
    r"([\w./-]+\.(py|js|sh|md|jsonl|db|yml|json|html)\b|python3 |node |curl |git log|git ls-files"
    r"|time |/metrics|config\.yml)")
# A wall-time claim is a load-coupled claim. 2026-10-18 measured one store route at 0.4 ms
# in-process and 50 ms as a process, the same spine route at 0.7 s without live pricing and
# 7.9 s with it, and a render budget met at 16 ms idle failing at 36 ms under load - none of
# which the spine said the box was doing (run/probe/run10.json, load average 11.78). So a
# spine bullet quoting a rate or a wall time must name the load, or name that the number is
# in-process. Budgets with no rate word (`timeout 120 s`) are left alone.
LATENCY_WORD_RE = re.compile(
    r"\bp\d\d\b|\blatency\b|\bwall\b|\belapsed\b|\btook\b|\btakes\b|\bslow\b"
    r"|\bfaster\b|\bslower\b|\bper (?:call|run|frame|image)\b|ms/")
LOAD_STATE_RE = re.compile(
    r"\bload(?: ?avg)?\b|\bidle\b|in-process|\bunder load\b|\bresident\b"
    r"|\bbusy box\b|MemAvailable")
TIME_UNIT_RE = re.compile(r"\d[\d,]*\.?\d*\s*(ms\b|s\b)")
DATE_RE = re.compile(r"\b(20\d\d)-(\d\d)-(\d\d)\b")
CLAIM_TTL_DAYS = 30


def claim_blocks(text):
    """Group a spine file into claim units: a bullet or heading plus its wrapped
    continuation lines. A referent named anywhere in the bullet covers the bullet."""
    blocks = []
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        if line.startswith(("#", "- ", "* ", "| ")):
            blocks.append(line.strip())
        elif blocks:
            blocks[-1] += " " + line.strip()
        else:
            blocks.append(line.strip())
    return blocks


PREDICATE_RE = re.compile(
    r"\b(exists?|none|always|never|regardless|byte-identical|content-keyed|keyed|uniform"
    r"|monotonic|saturated|exempt|disabled|enabled|frozen|absent|cached|real|unbounded"
    r"|superseded|frozen)\b|\bno [a-z ]*caching\b")


def closure_hash():
    """sha256 over the exact injected closure text, in closure order."""
    h = hashlib.sha256()
    for name in injected_closure():
        h.update((text_of(name) or "").encode("utf-8"))
    return h.hexdigest()[:16]


def check_claim_referents():
    """Measured claims in the injected spine must carry a referent and not be stale."""
    violations, notes = [], []
    today = datetime.datetime.now(datetime.timezone.utc).date()
    for name in ("AGENTS.md", "CORE.md", "STATE.md"):
        body = text_of(name)
        if body is None:
            # A spine dir that does not hold this file is the closure gate's report; there is
            # no text here to price, and aborting the whole run over it hides every other gate.
            continue
        for block in claim_blocks(body):
            if not MEASURE_RE.search(block):
                continue
            label = f"{name}: {block[:96]}"
            if not REFERENT_RE.search(block):
                violations.append(f"claim without referent - {label}")
                continue
            if TIME_UNIT_RE.search(block) and LATENCY_WORD_RE.search(block) \
                    and not LOAD_STATE_RE.search(block):
                violations.append(f"wall-time claim without load state - {label}")
                continue
            dates = [datetime.date(int(y), int(mo), int(d))
                     for y, mo, d in DATE_RE.findall(block)]
            if dates:
                age = (today - max(dates)).days
                if age > CLAIM_TTL_DAYS:
                    if PREDICATE_RE.search(block):
                        violations.append(
                            f"stale predicate claim ({age} d > {CLAIM_TTL_DAYS}) - {label}")
                    else:
                        notes.append(f"NOTE {CLAIM_TTL_DAYS} d old ({age} d) - {label}")
    return violations, notes


# A referent is an ADDRESS, and the spine's gate for it is a presence test: a bullet that
# cites `spine.py:record_spine_price` in a file that does not exist passes every run of that
# gate (note 2026-11-15 section 5 - the sentence withdrawn there was itself written against a
# module no probe had ever named). Existence is a second, separate question, and it has one
# deterministic answer, so it gets its own probe instead of a session's memory.
SYMBOL_DEF_RES = (
    re.compile(r"^(?:async\s+)?def\s+(?P<n>\w+)\b"),
    re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s+(?P<n>\w+)\b"),
    re.compile(r"^(?:export\s+)?(?:const|let|var)\s+(?P<n>\w+)\b"),
    re.compile(r"^class\s+(?P<n>\w+)\b"),
    re.compile(r"^\s*(?:export\s+)?class\s+(?P<n>\w+)\b"),
)
REFERENT_SKIP = {".git", "archive", "node_modules", "__pycache__", "run", "logs"}


def _referent_roots():
    return [pathlib.Path.cwd(), AGENT, pathlib.Path.home()]


def _find_path(spec):
    """The cited file, tried as given, then under the spine dir and the home dir. The first
    that exists wins; a path that resolves nowhere is the miss this probe exists to name."""
    for base in [pathlib.Path(".")] + _referent_roots():
        cand = (base / spec).resolve() if not spec.startswith(("/", "~")) else pathlib.Path(
            os.path.expanduser(spec))
        if cand.exists():
            return cand
    return None


def _find_symbol(sym, root):
    """Where a name IS defined, so a miss can name the referent the session meant. Bounded
    walk of the spine tree; the same directories the gate itself refuses to descend."""
    want = re.compile(r"^(?:\s*)(?:async\s+)?def\s+" + re.escape(sym) + r"\b|"
                      r"^\s*class\s+" + re.escape(sym) + r"\b|"
                      r"^\s*(?:export\s+)?(?:const|let|var|function)\s+" + re.escape(sym) + r"\b")
    hits = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix not in (".py", ".js", ".mjs", ".sh"):
            continue
        if REFERENT_SKIP & set(path.relative_to(root).parts):
            continue
        try:
            for i, line in enumerate(path.read_text(encoding="utf-8",
                                                   errors="replace").splitlines(), 1):
                if want.match(line):
                    hits.append(f"{path.relative_to(root)}:L{i}")
                    break
        except OSError:
            continue
        if len(hits) >= 3:
            break
    return hits


def resolve_referent(spec):
    """Prove one cited address exists. Accepts `path`, `path:N`, `path:N-M`, `path:symbol`
    and `path::symbol` (the shape the prose in a compaction summary actually uses).
    RESOLVED means the file is on disk and, when named, the line is in range and the symbol
    is defined in THAT file. MISSING_TARGET says which half is absent, and for a symbol it
    names where the name really lives, so the reader can repoint instead of re-guess.
    Returns (verdict, detail)."""
    body = spec.replace("::", ":", 1) if "::" in spec else spec
    path_text, _, tail = body.partition(":")
    tail = tail.strip()
    # the fact store writes ranges as `L104-L108`; prose writes `104-108`. Both are the same
    # address, and a probe that rejects one of them reports a real referent as missing.
    if re.fullmatch(r"[Ll]?\d+(?:-[Ll]?\d+)?", tail):
        tail = tail.replace("L", "").replace("l", "")
    path = _find_path(path_text)
    if path is None:
        return ("MISSING_TARGET", f"{spec} - no such file (tried cwd, {AGENT}, ~)")
    if not tail:
        return ("RESOLVED", f"{path} (file, {len(path.read_text(encoding='utf-8', errors='replace').splitlines())} lines)")
    if re.fullmatch(r"\d+(-\d+)?", tail):
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        lo, _, hi = tail.partition("-")
        lo, hi = int(lo), int(hi or lo)
        if 1 <= lo <= hi <= len(lines):
            return ("RESOLVED", f"{path}:L{lo}-L{hi} (file has {len(lines)} lines)")
        return ("MISSING_TARGET",
                f"{spec} - line {tail} is outside {path.name}, which has {len(lines)} lines")
    text = path.read_text(encoding="utf-8", errors="replace")
    for i, line in enumerate(text.splitlines(), 1):
        for rx in SYMBOL_DEF_RES:
            m = rx.match(line)
            if m and m.group("n") == tail:
                return ("RESOLVED", f"{path}:{tail} at L{i}")
    near = _find_symbol(tail, AGENT)
    where = "; defined at " + ", ".join(near) if near else "; defined nowhere under " + str(AGENT)
    return ("MISSING_TARGET",
            f"{spec} - {tail} is not defined in {path.name}{where}")


TIMEOUT_RE = re.compile(r"^\s*#\s*spine-timeout:\s*(\d+)")


GATE_ARG_RE = re.compile(r"^\s*#\s*spine-gate:\s*(?P<cmd>\S.*?)\s*$")


PYTHON_RE = re.compile(r"^\s*#\s*spine-python:\s*(?P<path>\S.*?)\s*$")


def _script_head(script: pathlib.Path) -> list:
    """A script's first 40 lines, or empty if it cannot be read. A marker must never make
    the sweep fail on a file the runner is about to report on anyway."""
    try:
        return script.read_text(encoding="utf-8", errors="ignore").splitlines()[:40]
    except OSError:
        return []


def declared_args(script: pathlib.Path) -> list:
    """What a script needs to act as its own gate: a `# spine-gate: ARGS` line in its
    first 40 lines (see also `# spine-python:` and `# spine-timeout:`).

    `# spine-timeout:` fixed the budget a script gets; this fixes what it is invoked with.
    STATE.md names `tools/serve.py --probe` and the runner executed the bare path, and a
    bare serve.py is a forever-server rather than a check: --run-harness reported
    `could not run: ... timed out after 120 seconds` against a healthy project, and a
    probe-only gate could not be graded at all.

    Only a marker inside the script counts; flags are never parsed out of prose, the same
    rule GATE_ARGS follows, so a doc sentence cannot steer what --run-harness executes.
    """
    for line in _script_head(script):
        match = GATE_ARG_RE.match(line)
        if match:
            return shlex.split(match.group("cmd"))
    return []


def script_interpreter(script: pathlib.Path) -> str:
    """Which interpreter runs a swept script: a `# spine-python: PATH` line in its first 40
    lines; else `node` for a `.js` script; else this process's own Python. Returns "" when a
    `.js` script has no node on PATH - the caller must report that, never run it anyway.

    The `.js` clause is not hypothetical: the sweep ran neon_rush's two JS harnesses under
    CPython and reported `SyntaxError: invalid character '\u2014' (U+2014)` against files that
    pass 25/25 under node - the em-dashes are inside `//` comments, which Python cannot read and
    node can. A runner that guesses the interpreter from its own identity scores a healthy
    project as broken; the marker stays the override, the suffix is the fallback.

    A script that needs a project venv cannot borrow the spine's interpreter -- chemgym-v2's
    harness imports rdkit, which lives only in /home/snoop/tools/chemgym_venv, so the sweep
    ran it under the system interpreter and scored `ModuleNotFoundError` as a broken project
    while the same file under its own venv passed 158/158. Naming the venv in STATE.md prose
    would leave the sweep's command and the sentence describing it owned by two different
    files; the marker keeps the requirement inside the script that has it.
    """
    for line in _script_head(script):
        match = PYTHON_RE.match(line)
        if match:
            path = pathlib.Path(match.group("path")).expanduser()
            if path.is_file():
                return str(path)
    if script.suffix == ".js":
        import shutil
        return shutil.which("node") or ""
    return sys.executable


def script_timeout(script: pathlib.Path) -> int:
    """Per-script budget: a `# spine-timeout: N` line in the script's first 40 lines, else 120 s.

    A gate whose live work legitimately outlasts the default used to be reported as
    "could not run" - a false violation against a healthy project. The marker is clamped to
    900 s so no script can make --run-harness hang indefinitely.
    """
    try:
        head = script.read_text(encoding="utf-8", errors="ignore").splitlines()[:40]
    except OSError:
        return 120
    for line in head:
        match = TIMEOUT_RE.match(line)
        if match:
            return max(1, min(int(match.group(1)), 900))
    return 120


def operative_scripts():
    """The first clause of runnable_scripts, exposed: what the spine's own text names."""
    scripts = set()
    for name, text in read_docs():
        if text is None or name in HISTORICAL or name == "PERM.md":
            continue
        for line in text.splitlines():
            for raw in REL_RE.findall(line):
                key = raw.rstrip(".,:")
                if not key.endswith((".js", ".py")):
                    continue
                for root in line_roots(line):
                    cand = root / key
                    if cand.is_file():
                        scripts.add(cand.resolve())
    return scripts


BARE_GATE_RE = re.compile(r"^\s*\S+\s+(\S+\.(?:py|js))\s*$")


def declared_gates(plans=None):
    """Scripts that ARE the whole gate of a closed plan node, resolved from that project home.

    A plan's DAG is the one artifact that states what a project's gate is, and unlike a
    ledger entry it does not rotate out of the spine after 60 days. Only a gate written as
    one interpreter call of one script counts: a compound gate (`python3 x.py --arm B >
    out.txt && test -s out.txt`) is plan_check's business - it runs those, records the exit
    code on the node - and executing one bare fragment of one would invent a violation the
    plan never declared.
    """
    import plan_check

    gates = set()
    for dag in (plan_check.default_plans() if plans is None else plans):
        dag = pathlib.Path(dag)
        try:
            data = json.loads(dag.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for node in data.get("nodes", []):
            gate = node.get("gate")
            if not gate or plan_check.STATUS.get(node.get("status"), "open") != "done":
                continue
            match = BARE_GATE_RE.match(gate)
            if not match:
                continue
            candidate = (dag.resolve().parent / match.group(1)).resolve()
            if candidate.is_file():
                gates.add(candidate)
    return gates


def runnable_scripts():
    """Scripts --run-harness executes: what operative text names, what a closed plan node's
    gate declares, plus anything a ledger names under a harness/ directory.

    referenced_scripts() also picks up paths quoted by the ledgers: EVENTS.md names a 720 s
    thermal probe (notes/mb_idle.py) and PERM.md names each project's own tests. Those are
    records of what was run, not gates - executing one made --run-harness hang until the
    per-script timeout, and a CLI helper that needs arguments exits 2 bare. They are still
    smoke-run (a project harness no one re-runs is a harness that has silently broken);
    run_harnesses grades their exit code against declared_gates() + operative text, so a
    mention cannot become a gate.
    """
    scripts = operative_scripts()
    for script in referenced_scripts():
        if "harness" in script.parts:
            scripts.add(script)
    scripts |= declared_gates()
    # The runner is not its own gate: CORE.md names spine_check.py, so --run-harness would
    # otherwise execute a nested copy of itself and report its own verdict as a violation.
    scripts.discard(pathlib.Path(__file__).resolve())
    # And neither is the gate whose process is doing the sweeping. A gate harness may
    # import this module and resolve a live tree in-process; there `__file__` is still
    # spine_check.py, so the sweeping gate stayed in its own set and every fixture sweep
    # re-ran the real gate set inside it - spine_harness.py nested itself to a 120 s
    # cut-off and the fixtures reported that as a violation of a gate nobody chose to
    # run. Process identity, not file name, is what makes a gate the one asking.
    if sys.argv and sys.argv[0]:
        scripts.discard(pathlib.Path(sys.argv[0]).resolve())
    return scripts


def referenced_scripts():
    scripts = set()
    for name, text in read_docs():
        if text is None:
            continue
        for line in text.splitlines():
            for raw in REL_RE.findall(line):
                key = raw.rstrip(".,:")
                if not key.endswith((".js", ".py")):
                    continue
                for root in line_roots(line):
                    cand = root / key
                    if cand.is_file():
                        scripts.add(cand.resolve())
    return scripts


def check_plans():
    """Every project plan validates; the frontier is printed so a restart sees position."""
    import subprocess

    proc = subprocess.run([sys.executable, str(TOOL_HOME / "plan_check.py")],
                          capture_output=True, text=True, timeout=120)
    lines = (proc.stdout + proc.stderr).strip().splitlines()
    problems = [l.strip()[len("VIOLATION "):] for l in lines if l.strip().startswith("VIOLATION")]
    notes = [l.strip() for l in lines if l.strip().startswith(("plan ", "STALE"))]
    if proc.returncode and not problems:
        problems.append(f"plan_check.py exit {proc.returncode}: {lines[-1] if lines else '<no output>'}")
    return notes, problems


def check_regime_wiring():
    """VS-13 / FG-7: a spine PASS must mean no active fact pins a dead regime.

    The two doors FG-7 came in through close here. A missing S1.pin means the regime is
    not being derived at all, and a run that reports PASS cannot claim regime validity:
    that is a violation naming its recovery, not a silent skip. Demotions from
    `fact_store --check-regime` mean this store was certifying claims measured under a
    regime that no longer exists: the demotion stands (honest state) but the run FAILS,
    and each fact revalidates before green again. Delegation, as with the fact anchors:
    one implementation of the rule, the store's own.
    """
    import subprocess

    db = store_binding(STORE_OVERRIDE)[0]
    if not db.exists():
        return ([f"no fact store at {db.name} - tools/fact_store.py --init"], [])
    pin_path = pathlib.Path(os.environ.get("S1_PIN", str(AGENT / "run" / "sensors" / "S1.pin")))
    if not pin_path.exists():
        return ([], [f"regime sensor pin missing: {pin_path} - FG-7 stays open until "
                     "python3 tools/regime_sensor.py --publish runs (Projects/"
                     "omp-vnext-synthesis/run/wiring-vs13.md)"])
    live = pin_path.read_text(encoding="utf-8").strip()
    proc = subprocess.run([sys.executable, str(TOOL_HOME / "fact_store.py"),
                           "--db", str(db), "--live-pin", live,
                           "--check-regime"],
                          capture_output=True, text=True, timeout=120)
    notes, problems = [], []
    for line in proc.stdout.splitlines():
        if line.startswith("check-regime"):
            notes.append(line.strip())
        elif line.startswith("DEMOTE"):
            problems.append(f"regime demotion: {line[len('DEMOTE'):].strip()} - an active "
                            "claim pinned a dead regime; revalidate before trusting it")
    if proc.returncode and not problems:
        problems.append(f"fact_store.py --check-regime exit {proc.returncode}: "
                        f"{(proc.stderr or proc.stdout).strip()[-200:]}")
    return notes, problems


def check_sieve_scope():
    """The 2026-11-05 /tmp incident: a session mid-project resolved its sieve scope
    from cwd, found no repo there, and the boundary shipped a block with no dirty set,
    no evicted files and no project facts. A silent default is the failure mode, so
    every element of the resolution chain gets a line here: the operator pin usable,
    the STATE.md Active chain resolvable to a git repo, the resolver itself answering,
    the audit log being written. Delegation, as with the fact anchors: the sieve owns
    the one precedence; this only asks it what it would decide."""
    sieve = TOOL_HOME / "pre_compact_sieve.py"
    if not sieve.exists():
        return ([], [f"no sieve at {sieve} - the compaction boundary has no source"])
    notes, problems = [], []

    pin = pathlib.Path(os.environ.get("SIEVE_REPO_PIN_PATH",
                                      str(pathlib.Path.home() / ".omp" / "run" / "sieve-repo")))
    if pin.exists():
        target = pathlib.Path(os.path.expanduser(pin.read_text(encoding="utf-8").strip()))
        if target.is_dir() and (target / ".git").exists():
            notes.append(f"pin: {target}")
        else:
            problems.append(f"sieve pin {pin} names no git repo ({target}) - unusable pin "
                            "redirects nothing and masks a stale scope; clear it with "
                            "python3 tools/pre_compact_sieve.py --repo-pin ''")
    else:
        notes.append("pin: none (STATE.md Active chain decides)")

    def run(flag):
        try:
            proc = subprocess.run([sys.executable, str(sieve), flag],
                                  capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return None, f"{sieve.name} {flag}: {exc}"
        return proc, ""

    proc, err = run("--active-project")
    if proc is None:
        return [f"sieve scope: {err}"], [err]
    active = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    if proc.returncode == 0 and active:
        notes.append(f"STATE Active repos: {len(active)} first={active[0].split('\t')[0]}")
    elif "NO_ACTIVE_PROJECT" in proc.stdout:
        problems.append("STATE.md Active rows carry no resolvable git-repo path - a session "
                        "not standing in its repo would sieve the wrong scope again; give "
                        "one Active row a repo path or pin one: --repo-pin PATH")
    else:
        problems.append(f"pre_compact_sieve.py --active-project exit {proc.returncode}: "
                        f"{(proc.stderr or proc.stdout).strip()[-160:]}")

    proc, err = run("--resolve-repo")
    if proc is None:
        problems.append(err)
    elif proc.returncode != 0:
        problems.append(f"pre_compact_sieve.py --resolve-repo exit {proc.returncode}: "
                        f"{(proc.stderr or proc.stdout).strip()[-160:]}")
    else:
        m = re.match(r"^(\S+) source=(\S+) redirected=(\S+)$", proc.stdout.strip())
        if not m:
            problems.append(f"resolver line unparsable: {proc.stdout.strip()[:120]!r}")
        else:
            notes.append(f"resolver: source={m.group(2)} redirected={m.group(3)}")

    rlog = pathlib.Path(os.environ.get("SIEVE_REPO_LOG",
                                       str(pathlib.Path.home() / ".omp" / "run" / "sieve-repo.log")))
    if rlog.is_file():
        notes.append(f"audit: {rlog.name} present")
    else:
        problems.append(f"resolver audit log {rlog} missing after a resolve run - the "
                        "resolution that hijacked a session would again leave no trace")
    return notes, problems


LINE_RANGE_RE = re.compile(r"L(\d+)-L(\d+)")


def fact_deps():
    """VS-33: can the fact store be read at all, and does every active pin parse?

    Returns (state, reason, unreadable_count). `uncomputed` is a dependency state, never a
    silent zero: the default run warns on stderr, an explicit --facts fails the run rc 2.
    Only `active` facts can steer or violate anything, so dead rows cannot make a run loud.
    """
    db = store_binding(STORE_OVERRIDE)[0]
    if not db.exists():
        return "uncomputed", f"cannot open fact db {db}: no such file", 0
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        rows = conn.execute(
            "SELECT fact_id, line_range FROM facts WHERE status = 'active'").fetchall()
        conn.close()
    except sqlite3.Error as exc:
        return "uncomputed", f"fact db {db} carries no readable facts table: {exc}", 0
    bad = [fid for fid, rng in rows if not LINE_RANGE_RE.fullmatch(rng or "")]
    return "live", "", len(bad)



def record_verdicts(args) -> int:
    """The verdict surface, routed through the store: fact_store owns the history columns.
    --corrected stamps the claim historical (stale: it must stop steering work) and records
    who adjudicated and why; --confirmed re-activates a claim and still leaves a dated
    adjudication. authority_firewall resolved the store once and it is handed to fact_store as
    --db here, so the receipt lands in the store the report names. Neither mode writes
    STATE.md or EVENTS.md: those are the operator's, and a verdict is a store record."""
    by = args.verdict_by or "spine_check"
    rc = 0
    vstore, vkind = store_binding(STORE_OVERRIDE)
    print("== verdicts ==")
    print(f"  store:     {vstore}  [{vkind}]  -- every receipt below lands here, and only here")
    items = ([(f, "corrected") for f in (args.corrected or [])]
             + [(f, "confirmed") for f in (args.confirmed or [])])
    for fact_id, kind in items:
        if kind == "corrected":
            verb = ["--direction", "demote", "--to-status", "stale"]
        else:
            verb = ["--direction", "promote", "--to-status", "active"]
        cmd = [sys.executable, str(TOOL_HOME / "fact_store.py"),
               "--db", str(store_binding(STORE_OVERRIDE)[0]),
               "--transition", fact_id, *verb,
               "--why", f"{kind}: {args.verdict_reason}",
               "--evidence", f"spine_check --{kind}",
               "--gate", "tools/spine_check.py --run-harness",
               "--corrected-by", by,
               "--correction-reason", args.verdict_reason]
        proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace",
                              timeout=180, cwd=str(AGENT))
        body = (proc.stdout + proc.stderr).strip().splitlines()
        moved = re.search(r"(\S+:\S+) -> (\S+:\S+)", "\n".join(body))
        states = (f"{moved.group(1)} -> {moved.group(2)}" if moved
                  else "state unchanged")
        print(f"VERDICT   {kind} {fact_id}  {states}  by {by}")
        print(f"          reason:    {args.verdict_reason}")
        print(f"          recorded:  python3 tools/fact_store.py --why {fact_id}"
              " shows the adjudicator, the date and the reason in the transitions ledger")
        for line in body:
            if line.strip().startswith(("STATUS NOTE", "re-derive:", "ERROR",
                                        "fact_store:", "--corrected-by needs")):
                print(f"          {line.strip()}")
        if proc.returncode:
            print(f"          the store refused (rc {proc.returncode}); the claim keeps its"
                  " recorded state")
            for line in (proc.stderr or proc.stdout).strip().splitlines():
                if not line.strip().startswith(("usage:", "fact_store:")):
                    print(f"          {line.strip()}")
        rc = max(rc, proc.returncode)
    return rc



def check_fact_anchors():
    """Item F15: an active or contested fact's referent must still hash to its pin, and the
    archived gate output that justified a promotion must still hash to the pin its ledger
    row carries.

    Delegates to the store's own gate rather than reimplementing the hash here: two
    implementations of one rule is how a checker starts disagreeing with its tool. One
    invocation covers both: `--check-anchors` verifies receipts in the same process, which
    is what keeps a lost anchor from being reported twice - as a stale referent and as a
    broken receipt - and then demoted twice. No store means no grounded claims, which is
    not a violation - it is an unused tool.
    """
    import subprocess

    db = store_binding(STORE_OVERRIDE)[0]
    if not db.exists():
        return ([f"no fact store at {db.name} - tools/fact_store.py --init"], [])

    proc = subprocess.run([sys.executable, str(TOOL_HOME / "fact_store.py"),
                           "--db", str(db), "--check-anchors"],
                          capture_output=True, text=True, timeout=120)
    lines = (proc.stdout + proc.stderr).strip().splitlines()
    notes, problems, i = [], [], 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("STALE"):
            detail = ""
            if i + 1 < len(lines) and lines[i + 1].lstrip().startswith("was "):
                detail = lines[i + 1].strip()
                i += 1
            problems.append(" ".join(x for x in (line.strip(), detail) if x))
            # A drift demotion has two endings and the report has to name both. --repin is the
            # one for bytes that moved under a claim still worth holding; --reconcile is the one
            # for a claim someone has since re-proved, where the row is history and re-deriving
            # it again is wasted work. LOOM 2026-11-09 left 41 rows at stale with the spine
            # naming only the first, so a session with no re-derivation to do still read the
            # report as a queue of owed work.
            notes.append("        fix: fact_store.py --repin ID re-hashes the anchor when the"
                         " bytes are what moved; fact_store.py --reconcile retires a stale row"
                         " that a later live claim for the same subject+predicate already"
                         " replaces")
        elif line.startswith("INFO"):
            # A receipt the archive lost while the current sweep's ledger row shows that
            # gate passing: rotation damage, not a claim that stopped holding. The ledger
            # row is the authoritative run record and the archive is its copy, so this is
            # a note, never a violation.
            notes.append(line.strip())
            if i + 1 < len(lines) and "loop ledger" in lines[i + 1]:
                notes.append(lines[i + 1].strip())
        elif line.startswith(("CONTEST", "HEAL", "REPIN")):
            # The sweep could not read the referent, so it refuted nothing: the row is
            # contested against its own unreadability and turns decidable again the moment the
            # referent resolves. Advisory, never a violation - the claim did not stop being
            # true, only the check on it stopped running. HEAL and REPIN are that same axis
            # resolving, and REPIN names the one command that withdraws a stale label.
            notes.append(line.strip())
            if i + 1 < len(lines) and lines[i + 1].startswith("        "):
                notes.append(lines[i + 1].strip())
                i += 1

        elif line.startswith("EVIDENCE"):
            detail = ""
            if i + 1 < len(lines) and lines[i + 1].lstrip().startswith("was "):
                detail = lines[i + 1].strip()
                i += 1
            problems.append(" ".join(x for x in (line.strip(), detail) if x))
        elif line.startswith("fact_store:"):
            # The store's own advisory: one knob named twice. It stops the run being read as a
            # reproducible spine number, so it is a violation - but nothing was demoted and no
            # anchor drifted, and printing it as STALE sent a reader hunting a demotion that
            # never happened (2026-11-11: the firewall pinned FACT_STORE_DB, this line fired, and
            # the report said the claim went stale).
            problems.append(f"STORE {line.strip()}")
        elif line.startswith(("check-anchors:", "check-evidence:")):
            notes.append(line.strip())
        i += 1
    if proc.returncode and not problems:
        problems.append(f"fact_store.py --check-anchors exit {proc.returncode}: "
                        f"{lines[-1] if lines else '<no output>'}")
    # CG-12: age decay is advisory. An expired claim is a withdrawal the store has
    # already explained, not a violation, so the sweep's count is reported here and
    # never joins `problems`. A store too old for the command is a note, not a fault.
    try:
        xp = subprocess.run([sys.executable, str(TOOL_HOME / "fact_store.py"),
                             "--db", str(db), "--check-expiry"],
                            capture_output=True, text=True, timeout=120)
        summary = next((ln.strip() for ln in xp.stdout.splitlines()
                        if ln.startswith("check-expiry:")), "")
        notes.append(summary if xp.returncode == 0 and summary
                     else f"expiry: not computed (exit {xp.returncode})")
    except (OSError, subprocess.TimeoutExpired) as exc:
        notes.append(f"expiry: not computed ({exc})")
    # An operator adjudication that has not landed is a different thing from the two advisories
    # above: the store is holding two claims that cannot both be true, so nothing downstream can
    # vouch for what it summarises. The predicate is the recorded rival pair, not prose - a row
    # the sweep contested because its referent could not be read carries the store's R2 marker
    # and is NOT this case, which is what kept a 27-day-old dead path in memory.db looking like a
    # verdict the operator owed (2026-11-11).
    unresolved = []
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            unresolved = conn.execute(
                "SELECT fact_id, source_path, line_range, adjudication_gate,"
                " competing_claims FROM facts"
                " WHERE status = 'contested'"
                "   AND coalesce(competing_claims, '') NOT LIKE ?"
                " ORDER BY fact_id", ("%R2_REFERENT_UNREADABLE%",)).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        notes.append(f"adjudications: not read ({exc})")
    for fact_id, src, rng, gate, rivals in unresolved:
        try:
            n = len(json.loads(rivals or "[]"))
        except ValueError:
            n = 0
        problems.append(f"ADJUDICATE {fact_id}  {src}:{rng}  {n} rival reading(s) on file:"
                        " the store is holding claims that cannot both be true, so its"
                        " offering view cannot be trusted for either")
        notes.append(f"        fix: fact_store.py --contested-facts lists both readings"
                     f" of {fact_id}; settle it on the spine: --confirmed {fact_id}"
                     f" --verdict-reason '<what proved it>' --verdict-by <who> re-adopts"
                     f" this reading, or --corrected {fact_id} retires it as history; the"
                     f" loser itself: fact_store.py --transition <loser> --direction"
                     f" falsify --to-status retired --superseded-by {fact_id};"
                     f" gate on file: {gate or 'unset'}")
    return notes, problems


def check_harness_imports():
    scripts = referenced_scripts()
    if not scripts:
        return ["no harness scripts referenced by the spine"], []
    notes, problems = [], []
    for script in sorted(scripts):
        bad = local_imports(script)
        if bad:
            problems.append(f"{script} imports unresolved -> {', '.join(bad)}")
        else:
            notes.append(f"ok {script}")
    return notes, problems


def local_imports(script):
    src = script.read_text(encoding="utf-8", errors="ignore")
    bad = []
    if script.suffix == ".js":
        for target in re.findall(r"""require\(\s*['"]([^'"]+)['"]\s*\)""", src):
            if not target.startswith("."):
                continue  # bare specifier: builtin or installed package
            base = (script.parent / target).resolve()
            candidates = [base, base / "index.js"] + [base.with_suffix(ext)
                      for ext in (".js", ".json", ".mjs", ".cjs")]
            if not any(c.exists() for c in candidates):
                bad.append(target)
    elif script.suffix == ".py":
        try:
            tree = ast.parse(src)
        except SyntaxError as exc:
            return [f"<unparsable: {exc.msg}>"]
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level:
                base = script.parent
                for _ in range(node.level - 1):
                    base = base.parent
                pkg = base / (node.module or "")
                pkg_file = pkg.with_suffix(".py")
                pkg_pkg = pkg / "__init__.py"
                if not (pkg_file.exists() or pkg_pkg.exists()):
                    bad.append(f"{'.' * node.level}{node.module}")
                    continue
                if pkg_file.exists():
                    continue
                for a in node.names:
                    if not ((pkg / f"{a.name}.py").exists() or (pkg / a.name / "__init__.py").exists()):
                        bad.append(f"{node.module}.{a.name}")
            elif isinstance(node, ast.Import):
                for a in node.names:
                    top = a.name.split(".")[0]
                    if (script.parent / f"{top}.py").exists() or (script.parent / top / "__init__.py").exists():
                        continue
                    if top in LOCAL_HINTS:
                        bad.append(a.name)
    return bad


# A verification script is a script whose name says what it is for. The F1 defect sat at the
# project ROOT (neon_rush/p3_verify.js at commit 8a585a4), not under harness/, so a
# harness-directory scan misses the very case the check was written for. Root-level names are
# matched by pattern instead: scanning every .py/.js a project owns would flag library code
# whose imports are not this repo's to resolve.
VERIFY_NAME_RE = re.compile(r"harness|verify|selftest|test_|_test|check_", re.IGNORECASE)


def project_harness_scripts():
    """Verification scripts under a project root, whether or not a record names them."""
    out = []
    for root in scan_roots():
        for path in sorted(root.iterdir()):
            if path.is_file() and path.suffix in (".py", ".js") \
                    and VERIFY_NAME_RE.search(path.name):
                out.append(path)
        hdir = root / "harness"
        if not hdir.is_dir():
            continue
        for path in sorted(hdir.rglob("*")):
            if not path.is_file() or path.suffix not in (".py", ".js"):
                continue
            if any(part in SKIP_DIRS for part in path.parts):
                continue
            out.append(path)
    return out


def check_project_harness_imports():
    bad = []
    for script in project_harness_scripts():
        for target in local_imports(script):
            bad.append(f"{script}: unresolved import -> {target}")
    return bad


def check_index_coverage():
    """notes/ holds the durable findings; PERM.md is the index a later session reads."""
    notes_dir = AGENT / "notes"
    if not notes_dir.is_dir():
        return []
    index = "\n".join(text_of(n) or "" for n in ("AGENTS.md", "CORE.md", "STATE.md", "PERM.md"))
    return [f"notes/{p.name}: named by neither PERM.md nor the injected spine"
            for p in sorted(notes_dir.iterdir()) if p.is_file() and p.name not in index]


def box_pressure():
    """The box as the gate saw it: PSI averages and 1-minute load, read the moment a script
    exits nonzero. A perf gate that fails on a contended machine is a different finding from one
    that fails on a quiet machine, and unless the failure line says which, the only way to triage
    it is to run the gate again -- which is the confound amber_ascii perm-record.md S17 names."""
    parts = []
    for kind in ("cpu", "memory", "io"):
        try:
            line = pathlib.Path("/proc/pressure", kind).read_text(encoding="utf-8").splitlines()[0]
        except OSError:
            continue
        fields = dict(tok.split("=", 1) for tok in line.split()[1:])
        try:
            parts.append(f"{kind} some {float(fields.get('avg10', 0.0)):.2f}%")
        except ValueError:
            continue
    try:
        parts.append("loadavg_1m "
                     + pathlib.Path("/proc/loadavg").read_text(encoding="utf-8").split()[0])
    except OSError:
        pass
    return ", ".join(parts) or "no /proc/pressure"


# Spine gate rule 4: a gate that fails while the box is contended retries exactly once, only on
# CPU pressure, and the attempt that scored is the one credited. Memory and io stalls are printed
# with the failure but do not retry: the rule names CPU some, and a gate that fails on a quiet box
# fails honestly and is never retried into a pass.
LOAD_RETRY_PCT = 20.0


def cpu_some_pct():
    """PSI cpu `some avg10` as a number - the 10-second figure the retry rule names, not the
    one-minute one, because a gate's window is seconds. SPINE_CPU_SOME_PCT pins the value so the
    policy is testable without waiting for a loaded box; every receipt row records `retries=N`, so
    a run that used the seam is visible in the ledger afterwards."""
    try:
        return float(os.environ["SPINE_CPU_SOME_PCT"])
    except (KeyError, ValueError):
        pass
    try:
        line = pathlib.Path("/proc/pressure/cpu").read_text(encoding="utf-8").splitlines()[0]
        fields = dict(tok.split("=", 1) for tok in line.split()[1:])
        return float(fields.get("avg10", 0.0))
    except (OSError, ValueError):
        return 0.0


def failing_lines(tail, last, cap=8):
    """The lines that name what failed, not whatever printed last. On 2026-10-18 the spine's
    12-line tail showed `F4.2 p95 frame time <= 1.5 x median ... [PASS]` while the check that
    actually failed sat above the window: the violation read `1 fail` and named nothing.
    Summary and clean-check lines are skipped so the cap holds room for real failures."""
    out = []
    for line in tail:
        s = line.strip()
        if not s or s == last.strip() or "fail" not in s.lower():
            continue
        if re.search(r"\b0 fail", s, re.I) or "[pass]" in s.lower():
            continue
        out.append(s)
        if len(out) >= cap:
            break
    return out


# A declared gate whose test is a flag, not bare execution: CORE.md says num_provenance.py's
# test "is the built-in `--self-test`", and its CLI exits 2 without --report/--evidence, so bare
# execution graded a usage error as a gate failure (rule 15). The runner supplies the flag the
# operative text names. Only scripts listed here get arguments; nothing is inferred from prose.
GATE_ARGS = {"num_provenance.py": ["--self-test"]}

# A probe whose service is down answers "Connection refused", and that is the probe working.
# Runtime liveness belongs to the listener check against operative text (rule 10, RUNTIME_DOCS);
# STATE's game-archive row names the gate and asserts no listener, so grading a refused probe as
# a code violation created a second owner for a question the spine already grades elsewhere, and
# one fired on every sweep of a closed project (tools/serve.py --probe, 2026-11-05). A gate
# output matching this is a note even when the gate is declared; any other nonzero exit still fails.
LIVENESS_REFUSAL = re.compile(r"Connection refused|urlopen error|timed out.*probe|ECONNREFUSED",
                              re.I)

# A gate is credited only by the run this runner started. A 2026-11-05 sweep credited suites
# that had never executed, and reported the runner's own timeout as the suite's failure,
# because credits were harvested from one merged output stream: a suite counted from another
# suite's summary line is a green sweep over untested code. The marker below carries a clock
# only the runner can observe, so a gate's own text cannot quote a future wall start, and a
# gate printing it is forging a credit. A gate that printed nothing is unresolved (rc=127),
# never "ran ok"; a gate that exits 0 while its own summary reports a failing count stays
# unresolved; a run the runner cut off at its budget is the runner's act, not the suite's
# verdict. Budgets are declared the same way: `# spine-timeout: N` in the first 40 lines.
GATE_MARKER = "RUN_HARNESS_START"
GATE_NO_OUTPUT_RC = 127
FAILING_COUNT_RE = re.compile(r"(\d+)\s+fail(?:s|ure|ures|ed)?\b")
CHECK_COUNT_RE = re.compile(r"(\d+)\s+checks?\b", re.I)


def _gate_selected(script, selectors):
    text = str(script)
    return any(text == s or script.name == s or script.stem == s
               or text.endswith("/" + s.rstrip("/")) for s in selectors)


def run_harnesses(only=None):
    """Execute every runnable harness. A nonzero exit is a spine violation only for a script
    the spine or a plan *declared* as a gate; a ledger-mentioned script reports a note.

    `only` narrows the sweep to named gates (`--only`, repeated or comma-separated: a path, a
    file name or a stem). A selector matching nothing is a violation: a typo that runs no gate
    must not read as a clean sweep."""
    import subprocess

    # gate_exec is machinery, not spine words, so it resolves beside this file - the same shape
    # as the merkle_compactor import in the rollup check, because SPINE_DIR may point AGENT at a
    # fixture tree. It owns the process-tree boundary this sweep needs (subprocess.run killed
    # only the leader, so a gate's daemons outlived the sweep and became the CPU load that cut
    # later gates off at their declared budget - the strata-host 307% readings this file reports)
    # and the one outcome vocabulary the receipt rows below carry.
    if str(TOOL_HOME) not in sys.path:
        sys.path.insert(0, str(TOOL_HOME))
    import gate_exec

    declared = operative_scripts() | declared_gates()
    problems, notes = [], []
    every = sorted(runnable_scripts())
    swept_names = {p.name for p in every}
    selectors = [s.strip() for s in (only or []) if s.strip()]
    if selectors:
        for sel in selectors:
            if not any(_gate_selected(p, [sel]) for p in every):
                problems.append(f"--only names no runnable gate: {sel}")
        every = [p for p in every if _gate_selected(p, selectors)]
    # Every gate the sweep touches leaves one receipt line, pass or not, so a score the
    # spine states can cite a real run (`fact_store --check-spine` binds it) and a gate
    # that never ran cannot be missing from the log in silence. Observability only:
    # plan_check still owns node state, this file claims nothing.
    log_path = AGENT / "run" / "harness_loop.log"
    try:
        head = subprocess.run(["git", "-C", str(AGENT), "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True).stdout.strip() or "nohead"
    except Exception:
        head = "nohead"

    # One sweep at a time per box. Two sweeps contend for the very resources every gate measures,
    # and a ledger row cannot say which run produced it - a 72/2 seen at 20:49 on 2026-11-06 cost
    # twenty minutes to attribute for exactly that reason. The holder writes its pid; a live holder
    # means the second sweep is refused instead of raced, a dead holder's lock is stale and is
    # taken over, and the same pid re-enters because the harness sweeps repeatedly in one process.
    sweep_id = os.getpid()
    lock_path = AGENT / "run" / "harness_sweep.lock"
    try:
        held_pid = int(lock_path.read_text(encoding="utf-8").split()[0])
    except (OSError, ValueError, IndexError):
        held_pid = 0
    if held_pid and held_pid != sweep_id and pathlib.Path(f"/proc/{held_pid}").exists():
        problems.append(f"a sweep is already running (pid {held_pid}): two sweeps contend for the "
                        "resources every gate measures and their ledger rows cannot be told apart "
                        "- wait for it or stop it, do not race it")
        return notes, problems
    if held_pid != sweep_id:
        try:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            lock_path.write_text(
                f"{sweep_id} {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\n",
                encoding="utf-8")
        except OSError as exc:
            problems.append(f"{lock_path} could not be written: {exc}")

    def receipt(script, rc, secs, summary, retries=1, outcome="", timeout_s=None):
        """One line per gate: the score in the shape the spine writes it (n/m), how many
        attempts it took - the loop ledger says so, not only the note a reader may never open -
        and which outcome kind the run actually was.

        rc alone cannot say it: a cut-off recorded rc=124 here while plan_check recorded rc=124
        for a gate that would not spawn, and an exit 0 whose own summary reported failures is a
        fail wearing a pass. The kinds are gate_exec's (pass|fail|timeout|launch-error|no-output,
        plus skip), the taxonomy kinds-bench's grader keeps per task (kbench/runner.py:180-184,
        296). Rows written before the field carry no outcome= and read as legacy.

        timeout_s= is present only on a cut-off row: the declared budget that killed the run,
        without which "timeout" cannot say whether the gate ran out at 1800 s or at 60 s on a
        loaded box - two different findings, and STATE was carrying the second one off a reading
        of the first (strata-host 307%, 2026-11-13). Appended after outcome= so the row's pinned
        prefix does not move.
        """
        total = CHECK_COUNT_RE.search(summary)
        fails = FAILING_COUNT_RE.search(summary)
        score = f"{total.group(1)}/{fails.group(1)}" if total and fails else "-"
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        one = " ".join(str(summary).split())[:160]
        # retries and sweep are appended last: the row's pinned shape ends at last=<summary>, and a
        # ledger field a check already depends on must not move to teach a new one. `sweep=` says
        # which process produced the row, so rows from overlapping sweeps are never conflated.
        row = (f"{stamp} head={head} gate={script} rc={rc} wall={secs:.1f}s "
               f"score={score} last={one} retries={retries} sweep={sweep_id}"
               + (f" outcome={outcome}" if outcome else "")
               + (f" timeout_s={timeout_s:g}" if timeout_s else "") + "\n")
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(log_path, "a") as fh:
                fh.write(row)
        except OSError as exc:
            problems.append(f"{log_path} could not be written: {exc}")

    for script in every:
        interp = script_interpreter(script)
        if not interp:  # a .js harness on a box with no node: say so, do not misreport it
            if script in declared:
                problems.append(f"{script} is a declared gate but node is not on PATH")
            else:
                notes.append(f"skipped {script}: node is not on PATH")
            receipt(script, "skip", 0.0, "node is not on PATH", outcome="skip")
            continue
        cmd = [interp, str(script)]
        cmd += GATE_ARGS.get(script.name, [])
        # GATE_ARGS first: a spine-named override beats the script's own marker.
        cmd += [] if script.name in GATE_ARGS else declared_args(script)
        budget = script_timeout(script)
        wall_start = time.time()
        # runner-only credit: names the gate and the wall start of the run it produced
        print(f"{GATE_MARKER} {script} {wall_start:.3f}", flush=True)
        started = time.monotonic()
        # gate_exec, not subprocess.run: on a cut-off only the leader died and the tree survived
        # to load the next sweep, and a launch failure reached the bare `except Exception` below
        # where it reads like a verdict. A cut-off is a kind now, so no caller has to re-kill.
        proc, kind, leftovers = gate_exec.run(cmd, script.parent, timeout=budget)
        if leftovers:
            # A gate that leaves processes behind after exiting is a hygiene bug the reader
            # should see: kinds-bench counted 116 survivors in one 36-task run
            # (kbench/sandbox.py:162-171) and this spine's own strata-host load came from them.
            notes.append(f"swept {len(leftovers)} process(es) still in {script}'s process group")
        if kind == "launch-error":
            problems.append(f"{script} could not run: {(proc.stderr or '').strip()[:200]}")
            receipt(script, "error", time.monotonic() - started, "could not spawn",
                    outcome=kind)
            continue
        if kind == "timeout":
            box = box_pressure()
            if script in declared and cpu_some_pct() > LOAD_RETRY_PCT:
                # spine gate rule 4, cut-off case: a declared budget is a wall-clock claim, and
                # CPU pressure is exactly what steals wall clock, so a gate cut off on a loaded
                # box is a box reading and not a broken gate. One retry, same budget; a gate cut
                # off twice is never converted into a pass.
                quiet = box_pressure()
                started = time.monotonic()
                again, akind, _aleft = gate_exec.run(cmd, script.parent, timeout=budget)
                if akind == "timeout":
                    problems.append(f"{script} was cut off at its declared budget of {budget} s "
                                    f"on a loaded box ({box}) and the one permitted retry was cut "
                                    "off at the same budget: two cut-offs, verdict unknown, never "
                                    f"a pass\n    box at retry: {quiet}")
                    receipt(script, "timeout", budget * 2, "cut off twice: verdict unknown",
                            retries=2, outcome=akind, timeout_s=budget)
                    continue
                if akind == "launch-error":
                    problems.append(f"{script} cut off at {budget} s on a loaded box ({box}); the "
                                    f"one permitted retry could not run: "
                                    f"{(again.stderr or '').strip()[:200]}")
                    receipt(script, "error", time.monotonic() - started,
                            "retry could not run after a cut-off", retries=2, outcome=akind)
                    continue
                secs = budget + (time.monotonic() - started)
                r_tail = ((again.stdout or "") + (again.stderr or "")).strip().splitlines()
                r_last = r_tail[-1].strip() if r_tail else "<no output>"
                if again.returncode == 0:
                    notes.append(f"NOTE {script} ran loaded: attempt 1 cut off at {budget} s; "
                                 f"attempt 2 exit 0 ({r_last[:120]}) | box at cut-off: {box}; "
                                 f"box at retry: {quiet}")
                    receipt(script, 0, secs, r_last, retries=2, outcome=akind)
                    continue
                problems.append(f"{script} was cut off at its declared budget of {budget} s on a "
                                f"loaded box ({box}) and the one permitted retry exited "
                                f"{again.returncode}: {r_last[:160]}\n    box at retry: {quiet}")
                receipt(script, again.returncode, secs, "retry after a cut-off: " + r_last,
                        retries=2, outcome=akind)
                continue
            problems.append(f"{script} exceeded its declared budget of {budget} s (wall start "
                            f"{wall_start:.3f}) and was cut off: verdict unknown, not a pass - "
                            f"declare `# spine-timeout: N` in its first 40 lines if it is slow"
                            f"\n    box at cut-off: {box}")
            receipt(script, "timeout", time.monotonic() - started, "cut off: verdict unknown",
                    outcome="timeout")
            continue
        secs = time.monotonic() - started
        retries = 1
        out = (proc.stdout or "") + (proc.stderr or "")
        # A marker forgery is claiming credit for a gate this sweep is crediting. A gate that
        # echoes a marker naming a path OUTSIDE the sweep is printing text, not claiming a run:
        # a harness that sweeps a fixture tree in-process reports its fixture paths on its own
        # stdout, which is what the spine harness does, and on 2026-11-06 the sweep executing it
        # flagged it for forging a credit out of /tmp/spine_harness_*/harness/thing_harness.py.
        # Receipt rows are written only by this runner, so an echoed name the ledger never had
        # cannot buy a credit either.
        forged = [ln for ln in out.splitlines()
                  if GATE_MARKER in ln
                  and any(pathlib.PurePosixPath(tok).name in swept_names
                          for tok in ln.split(GATE_MARKER, 1)[1].split())]
        if forged:
            problems.append(f"{script} printed a runner credit marker ({forged[0][:90]}); "
                            f"only this runner credits a gate")
            # The exit code may be 0; the verdict is a refusal, and the row must not read as a pass.
            receipt(script, proc.returncode, secs, "printed a runner credit marker",
                    outcome="fail")
            continue
        tail = out.strip().splitlines()
        last = tail[-1].strip() if tail else "<no output>"
        if proc.returncode == 0 and not tail:
            msg = (f"{script} exit 0 produced no output under marker {wall_start:.3f}: "
                   f"unresolved rc={GATE_NO_OUTPUT_RC}, not a silent credit")
            (problems if script in declared else notes).append(msg)
            receipt(script, 0, secs, "<no output>", outcome="no-output")
            continue
        if proc.returncode == 0 and any(int(n) for n in FAILING_COUNT_RE.findall(last)):
            msg = (f"{script} exit 0 while its own summary reports a failing count: "
                   f"{last[:160]} - unresolved, not credited")
            (problems if script in declared else notes).append(msg)
            # rc says pass, the gate's own summary says failures: the kind is what the row means.
            receipt(script, 0, secs, last, outcome="fail")
            continue
        if proc.returncode != 0:
            box = box_pressure()
            if (script in declared and cpu_some_pct() > LOAD_RETRY_PCT
                    and not LIVENESS_REFUSAL.search(last + " " + " ".join(tail[-12:]))):
                # spine gate rule 4: exactly one retry, only over LOAD_RETRY_PCT of CPU some, and
                # the attempt that scored is the one credited. A verdict that moved is reported as
                # a loaded box, never converted into a clean pass.
                quiet = box_pressure()
                retries = 2
                started = time.monotonic()
                again, akind, _aleft = gate_exec.run(cmd, script.parent, timeout=budget)
                if akind == "timeout":
                    problems.append(f"{script} exit {proc.returncode} on a loaded box ({box}); the "
                                    f"one permitted retry ran past its {budget} s budget and was "
                                    "cut off: verdict unknown, not a pass")
                    receipt(script, "timeout", secs + time.monotonic() - started,
                            "retry cut off after a loaded failure", retries=2, outcome=akind,
                            timeout_s=budget)
                    continue
                if akind == "launch-error":
                    problems.append(f"{script} exit {proc.returncode} on a loaded box ({box}); the "
                                    f"one permitted retry could not run: "
                                    f"{(again.stderr or '').strip()[:200]}")
                    receipt(script, "error", secs + time.monotonic() - started,
                            "retry could not run after a loaded failure", retries=2,
                            outcome=akind)
                    continue
                secs += time.monotonic() - started
                r_out = (again.stdout or "") + (again.stderr or "")
                r_tail = r_out.strip().splitlines()
                r_last = r_tail[-1].strip() if r_tail else "<no output>"
                if again.returncode == 0:
                    notes.append(f"NOTE {script} ran loaded: attempt 1 exit {proc.returncode} "
                                 f"({last[:120]}); attempt 2 exit 0 ({r_last[:120]})"
                                 f" | box at first failure: {box}; box at retry: {quiet}")
                    receipt(script, 0, secs, r_last, retries=2, outcome=akind)
                    continue
                box = f"{box}; one retry on a loaded box, box at retry: {quiet}"
                proc, tail, last = again, r_tail, r_last
            if script in declared and not LIVENESS_REFUSAL.search(last + " " + " ".join(tail[-12:])):
                # a gate that fails must name the check that failed: a bare summary line
                # ("197 checks, 1 fail") cannot be triaged from a spine run
                named = failing_lines(tail, last)
                detail = "\n".join(f"    {line}" for line in named or tail[-12:])
                problems.append(f"{script} exit {proc.returncode}: {last}"
                                + (f"\n{detail}" if len(tail) > 1 else "")
                                + f"\n    box at failure: {box}")
            else:
                why = ("no gate declares it" if script not in declared
                       else "probe refused: no listener; liveness is the listener check's")
                buried = "; ".join(failing_lines(tail, last, 3))[:160]
                notes.append(f"NOTE {script} exit {proc.returncode} ({why}): "
                             f"{last}" + (f" | failing: {buried}" if buried else "")
                             + f" | box at failure: {box}")
        else:
            notes.append(f"ran ok {script}: {last} in {secs:.1f}s rc=0")
        receipt(script, proc.returncode, secs, last, retries=retries, outcome=kind)
    if held_pid != sweep_id:  # a re-entrant sweep leaves its holder's lock alone
        try:
            lock_path.unlink(missing_ok=True)
        except OSError:
            pass
    return notes, problems


def main():
    ap = argparse.ArgumentParser(description="OMP vNext spine validator")
    ap.add_argument("--strict-retired", action="store_true",
                    help="fail on retired-vocabulary residue")
    ap.add_argument("--strict-index", action="store_true",
                    help="fail on notes/ findings that no index or spine entry can find")
    ap.add_argument("--run-harness", action="store_true",
                    help="execute referenced harness scripts; nonzero exit fails the run")
    ap.add_argument("--emergency", action="store_true",
                    help="allow the emergency token budget instead of MAX")
    ap.add_argument("--facts-off", action="store_true",
                    help="skip the fact-anchor scan (Item F15) - for triage, and for the\n"
                         "                     store's own harness, which points FACT_STORE_DB at a\n"
                         "                     throwaway database and must not mutate the live one")
    ap.add_argument("--all-costs", action="store_true",
                    help="also price the on-demand files")
    ap.add_argument("--surprise", action="store_true",
                    help="at this L2A boundary, also run the L2B feedback gate "
                         "                     (tools/surprise_check.py) over the spine: a detector "
                         "                     that fires fails this run, a dormant one adds one "
                         "                     line; OMP_SURPRISE=0 skips the probe entirely")
    ap.add_argument("--facts", action="store_true",
                    help="check the fact anchors explicitly - that is already the default,\n"
                         "                     and the switch that skips them is --facts-off.\n"
                         "                     Defined so `--facts` cannot abbreviate it: argparse\n"
                         "                     let a bare `--facts` resolve to --facts-off, which\n"
                         "                     reported a clean run having skipped the scan.")
    ap.add_argument("--store", metavar="DB",
                    help="the fact store to audit and adjudicate, stated deliberately."
                         " Required to pair the live spine with any other store than"
                         " memory.db beside it, or a fixture root with the live one: an"
                         " ambient FACT_STORE_DB does neither (AUTHORITY_MISMATCH, rc 2)")
    ap.add_argument("--fast", action="store_true",
                    help="skip the live token pricing (3 server calls, ~3 s) and "
                         "estimate the budget from characters instead")
    ap.add_argument("--only", action="append", metavar="GATE",
                    help="with --run-harness: run only these gates (repeat the flag or "
                         "comma-separate; a path, a file name or a stem); a selector naming "
                         "no runnable gate is a violation")
    ap.add_argument("--check-referent", action="append", default=None, metavar="SPEC",
                    help="prove one cited address exists on disk and exit: 'path', 'path:N', "
                         "'path:N-M', 'path:symbol' or 'path::symbol'. RESOLVED or "
                         "MISSING_TARGET (exit 3) - the presence gate cannot catch a name "
                         "that never existed ('-' reads one spec per line from stdin)")
    ap.add_argument("--list", action="store_true",
                    help="print the gates --run-harness would run, one path per line, and exit "
                         "without running anything")
    ap.add_argument("--force", action="store_true",
                    help="with --run-harness: ignore the load pre-flight and run the sweep hot")
    ap.add_argument("--corrected", action="append", default=None, metavar="FACT_ID",
                    help="adjudicate this fact corrected: stamp it historical in the store"
                         " with the adjudicator and reason recorded; writes no STATE.md or"
                         " EVENTS.md entry and runs no gates")
    ap.add_argument("--confirmed", action="append", default=None, metavar="FACT_ID",
                    help="adjudicate this fact confirmed: re-activate it in the store and"
                         " record who adjudicated, when and why")
    ap.add_argument("--verdict-reason", metavar="TEXT",
                    help="with --corrected/--confirmed: the adjudication; required, because a"
                         " verdict with no reason is a status change with no record")
    ap.add_argument("--verdict-by", metavar="NAME",
                    help="the adjudicator's name for --corrected/--confirmed (default"
                         " spine_check)")
    args = ap.parse_args()

    refused = authority_firewall(args)
    if refused:
        return refused

    if args.list:
        for gate in sorted(runnable_scripts()):
            print(gate)
        return 0

    if args.check_referent:
        specs = [x for x in args.check_referent if x != "-"]
        if len(specs) != len(args.check_referent):
            specs.extend([ln for ln in sys.stdin.read().splitlines() if ln.strip()])
        missing = 0
        for spec in specs:
            verdict, detail = resolve_referent(spec.strip())
            print(f"  {verdict:<14} {detail}")
            missing += verdict == "MISSING_TARGET"
        print(f"referents: {len(specs)} checked, {len(specs) - missing} resolved, "
              f"{missing} missing")
        return 3 if missing else 0
    only = [s for chunk in (args.only or []) for s in chunk.split(",")]
    if only and not args.run_harness:
        ap.error("--only selects gates for --run-harness; pass --run-harness too")

    if args.corrected or args.confirmed:
        if not (args.verdict_reason or "").strip():
            print("--corrected/--confirmed need --verdict-reason: a verdict that does not"
                  " say why is a status change with no record", file=sys.stderr)
            return 2
        return record_verdicts(args)

    problems = check_includes() + check_abs_paths() + check_rel_paths()
    print("== spine integrity ==")
    if problems:
        for p in problems:
            print(f"  DANGLING  {p}")
    else:
        print("  all @-includes, absolute paths and project paths resolve")

    print("== harness imports ==")
    notes, harness_problems = check_harness_imports()
    for n in notes:
        print(f"  {n}")
    for q in harness_problems:
        print(f"  DANGLING  {q}")
    problems += harness_problems

    scanned = project_harness_scripts()
    print("== project harness imports ==")
    proj_problems = check_project_harness_imports()
    for q in proj_problems:
        print(f"  DANGLING  {q}")
    if not proj_problems:
        print(f"  {len(scanned)} harness scripts under the project roots resolve their imports")
    problems += proj_problems

    if args.run_harness:
        print("== harness execution ==")
        defer, load_note = sweep_deferral(sweep_load1(), SWEEP_LOAD_GUARD,
                                          force=args.force, targeted=bool(only))
        if load_note:
            print(f"  NOTE      {load_note}")
        if not defer:
            notes, run_problems = run_harnesses(only)
            for n in notes:
                print(f"  {n}")
            for q in run_problems:
                print("  FAILED    " + q.replace("\n", "\n            "))
            problems += run_problems

    print("== plan state ==")
    plan_notes, plan_problems = check_plans()
    for n in plan_notes:
        print(f"  {n}")
    for q in plan_problems:
        print(f"  VIOLATION {q}")
    problems += plan_problems

    print("== runtime claims ==")
    claim_notes, claim_problems = check_port_claims()
    print("\n".join(claim_notes) if claim_notes else "  no loopback port claims in docs")
    for p in claim_problems:
        print(f"  VIOLATION {p}")
    problems += claim_problems

    print("== retired vocabulary ==")
    residue = check_retired_vocabulary()
    for r in residue:
        print(f"  RESIDUE   {r}")
    if residue:
        print(f"  {len(residue)} phrase(s) naming machinery the runtime no longer has")
        if args.strict_retired:
            problems += [f"retired vocabulary: {r}" for r in residue]
    else:
        print("  none")

    print("== state schema ==")
    schema_problems, schema_notes = check_state_schema()
    for n in schema_notes:
        print(n)
    for p in schema_problems:
        print(f"  VIOLATION {p}")
    problems += schema_problems
    print("== state rollups ==")
    rollup_problems, rollup_notes = check_rollups()
    for n in rollup_notes:
        print(n)
    for p in rollup_problems:
        print(f"  VIOLATION {p}")
    problems += rollup_problems

    print("== durable records ==")
    perm_problems = check_closed_projects()
    for p in perm_problems:
        print(f"  VIOLATION {p}")
    if not perm_problems:
        print("  every closed project in STATE.md has a perm-record and a PERM.md entry")
    problems += perm_problems

    print("== durable index coverage ==")
    unindexed = check_index_coverage()
    for u in unindexed:
        print(f"  UNINDEXED {u}")
    if unindexed:
        print(f"  {len(unindexed)} note(s) no index or spine entry can find - the re-derivation class")
        if args.strict_index:
            problems += [f"unindexed finding: {u}" for u in unindexed]
    else:
        print("  every note is reachable from PERM.md or the injected spine")

    claim_violations, claim_notes = check_claim_referents()
    problems += claim_violations
    for v in claim_violations:
        print(f"  VIOLATION {v}")
    for n in claim_notes:
        print(f"  {n}")
    if not claim_violations and not claim_notes:
        print("  every measured claim in the injected spine names its referent")

    print("== fact anchors ==")
    store_now, store_kind = store_binding(args.store)
    print(f"  store:    {store_now}  [{store_kind}]")
    if args.facts and args.facts_off:
        print("spine_check: ERROR --facts and --facts-off contradict; pick one",
              file=sys.stderr)
        return 2
    if args.facts_off:
        print("  skipped (--facts-off): grounded claims and regime wiring are not "
              "being checked this run")
    else:
        deps, deps_reason, unreadable = fact_deps()
        if deps != "live":
            print(f"WARN deps: uncomputed: {deps_reason}", file=sys.stderr)
            print(f"  deps: uncomputed: {deps_reason} - grounded claims and regime "
                  "wiring are unverifiable this run")
            if args.facts:
                print(f"spine_check: ERROR deps: uncomputed (--facts): {deps_reason}",
                      file=sys.stderr)
                return 2
        elif unreadable:
            print(f"WARN deps: {unreadable} active fact(s) carry an unreadable pin; they "
                  "cannot steer and are excluded from every count", file=sys.stderr)
            print(f"  deps: {unreadable} unreadable pin(s) named, excluded from counts")
        anchor_notes, anchor_problems = check_fact_anchors()
        for n in anchor_notes:
            print(f"  {n}")
        for q in anchor_problems:
            if q.startswith(("STORE ", "EVIDENCE ", "ADJUDICATE ")):
                kind, rest = q.split(" ", 1)
                print(f"  {kind:<9} {rest}")
            else:
                print(f"  STALE     {q}")
        problems += anchor_problems
        regime_notes, regime_problems = check_regime_wiring()
        for n in regime_notes:
            print(f"  {n}")
        for q in regime_problems:
            print(f"  REGIME    {q}")
        problems += regime_problems

    print("== sieve scope ==")
    scope_notes, scope_problems = check_sieve_scope()
    for n in scope_notes:
        print(f"  {n}")
    for q in scope_problems:
        print(f"  SCOPE     {q}")
    problems += scope_problems

    print("== spine closure hash ==")
    order = injected_closure()
    # A closure file the spine dir does not hold is empty text to closure_hash; stat()ing it
    # here raised FileNotFoundError and the report died in its last section, so a spine with
    # no AGENTS.md - fresh fixture, half-restored recovery - printed no verdict at all.
    stamps = [(AGENT / n).stat().st_mtime for n in order if (AGENT / n).exists()]
    when = (datetime.datetime.fromtimestamp(max(stamps), datetime.timezone.utc)
            if stamps else None)
    last = (" no closure file on disk" if when is None
            else f", last edited {when:%Y-%m-%d %H:%M} UTC")
    print(f"  disk {closure_hash()}  ({' + '.join(order)}{last})")
    print("  a session injected with different text is stale: re-read these files before acting")

    print("== injection freshness ==")
    fresh = check_injection_freshness()
    print("\n".join(fresh) if fresh else " no sessions recorded")

    print("== injected token budget ==")
    closure = injected_closure()
    texts = {n: (text_of(n) or "") for n in closure}
    if args.all_costs:
        for n in ONDEMAND:
            if text_of(n) is not None:
                texts[n] = text_of(n)
    costs = ({"__error__": "--fast: live pricing skipped"}
             if args.fast else tokenizer_costs(texts))
    limit = EMERGENCY_TOKENS if args.emergency else MAX_TOKENS
    if "__error__" in costs:
        print(f"  token cost skipped ({costs['__error__']}); char estimate instead")
        for n in closure:
            print(f"  {n:24s} ~{len(texts[n]) // 4:>5} tok  {len(texts[n]):>6} chars")
        total = sum(len(texts[n]) for n in closure) // 4
        print(f"  injected total ~{total} tok (limit {limit})")
        est = {n: len(texts[n]) // 4 for n in closure}
        print(effective_cap_line(est, closure, limit, total, approx=True))
        # The same STATE.md grading the priced branch does, on the estimate: an operator who
        # runs --fast, or a box with no tokenizer, must reach the same verdict, or the flag
        # exists only on the path that can pay for it.
        scap, sideal, sothers, sroom, _ = state_budget(est, closure, limit)
        scost = est.get("STATE.md")
        if scost is not None and scost > scap:
            problem, _detail = state_violation_line(scost, scap, sothers, sroom, limit)
            problems.append(f"{problem} (~ char estimate)")
            print(f"  VIOLATION {problem} (~ char estimate)")
        elif scost is not None and sideal < scost <= scap:
            print(state_note_line(scost, scap, sideal, sothers, sroom, limit))
        if total > limit:
            problems.append(f"injected spine ~{total} tok exceeds {limit}")
    else:
        total = 0
        scap, sideal, sothers, sroom, smeasure = state_budget(costs, closure, limit)
        for n in closure:
            c = costs[n]
            total += c or 0
            cap = scap if n == "STATE.md" else MAX_TOKENS
            flag = "  " if c <= cap else " >"
            if n == "STATE.md" and smeasure is not None and sideal < c <= scap:
                print(state_note_line(c, scap, sideal, sothers, sroom, limit))
            if c > cap:
                if n == "STATE.md":
                    problem, detail = state_violation_line(c, scap, sothers, sroom, limit)
                else:
                    problem = f"{n} {c} tok exceeds its {cap} tok cap"
                    detail = token_rule(f"{n} measured {c} tok, cap {cap} tok")
                problems.append((problem, detail))
                print(f"  VIOLATION {problem}")
                print(f"            {detail}")
            print(f"{flag} {n:24s} {c:>5} tok  {len(texts[n]):>6} chars")
        extra = [f"  (on demand) {n:20s} {costs[n]:>5} tok" for n in ONDEMAND if n in costs]
        for line in extra:
            print(line)
        print(f"  injected total {total} tok  (ideal {IDEAL_TOKENS}, max {MAX_TOKENS}, "
              f"emergency {EMERGENCY_TOKENS})")
        print(effective_cap_line(costs, closure, limit, total))
        if total > limit:
            detail = token_rule(f"closure {'+'.join(closure)} measured {total} tok, "
                                f"limit {limit} tok ({'--emergency' if args.emergency else 'MAX'})")
            problems.append((f"injected spine {total} tok exceeds {limit} tok budget", detail))
            print(f"  VIOLATION injected spine {total} tok exceeds {limit} tok budget")
            print(f"            {detail}")
        elif total > IDEAL_TOKENS:
            print(f"  NOTE above ideal ({IDEAL_TOKENS}); under limit")

    if args.surprise:
        print("== L2A -> L2B surprise seam ==")
        if os.environ.get("OMP_SURPRISE", "1") == "0":
            # The kill switch is a declared choice, so print it. A boundary that skipped its
            # feedback audit must not read in the record like one that ran the audit and slept;
            # the skip costs one line and buys the difference between silence and no evidence.
            print("  SKIP      OMP_SURPRISE=0 - this boundary carries no L2B evidence")
        else:
            line, kind, body = run_surprise_gate(AGENT)
            print(line)
            for ln in body:
                print(f"            {ln}")
            if kind == "fired":
                problems.append("L2B SURPRISE a receipt disagrees with the disk it describes at "
                                f"{AGENT}; run `python3 tools/surprise_check.py` for every class "
                                "and the one-step fix for each")
            elif kind == "unavailable":
                problems.append("L2B UNAVAILABLE the feedback gate did not run, so this boundary "
                                "has no surprise evidence; fix the probe or say so in the record")
    print(verdict_line(problems))
    return 1 if problems else 0


if __name__ == "__main__":
    try:
        _code = main()
    except BrokenPipeError:
        # `--frontier | head` closes the pipe mid-print. The reader got the line it asked for;
        # the traceback was noise. CPython's recipe: point stdout at /dev/null so the flush at
        # interpreter exit does not raise a second time. Exit 1 = the output pipe broke.
        _devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(_devnull, sys.stdout.fileno())
        _code = 1
    sys.exit(_code)
