#!/usr/bin/env python3
"""OMP vNext plan validator — the enforcement layer for multi-hour DAG work.

Same design rule as spine_check.py: a rule the model has to remember is a rule it
eventually breaks; a rule this script checks is a rule nobody has to remember.
This covers the project-work half that spine_check does not reach: spine_check
gates the spine, plan_check gates the nodes.

A plan is one JSON file named DAG.json in the project home. The shape is the one
already used by governance-eval/DAG.json and omp-governance-experiments/DAG_R4.json,
with three additions that those files lacked:

  "gate":        an exact shell command, run in the project home, that must pass
                 before the node may be called done. Prose "verification" text
                 describes what done means; the gate is what proves it.
  "gate_negative": an exact shell command that must FAIL. --prove-gate ID runs the gate
                 and this together and records `gate_proof`: a gate that also passes on a
                 folder nobody touched measures nothing, and only the second side shows it.
  "gate_result": recorded by --run-gate: {cmd, exit, at, at_iso, output, commit,
                 receipt}. A
                 node cannot be done while its gate is unrun, failed, or recorded
                 against a different command. `receipt` is the coordinate
                 (archive/gates/<name>.log:L1-L<N>@<sha256[:12]>) of the archived run:
                 every line the gate printed, hash-pinned, so a number quoted from the

  "artifact":      a node whose gate consumes decoder, parser or library bytes pins
                 them: {"path": "rel/file.py:L200-L600", "sha256": "...", "rerun_ok":
                 true, "seeds": "run/freeze-seed.txt"}. --pin ID writes the digest of
                 the bytes as they stand; validate then fails the plan if those bytes
                 change (the freeze is broken, not drifted) and fails a node whose
                 declared consumers closed after the artifact's last read-only re-run.
                 --run-gate ID --read-only re-executes a gate that writes only beside
                 the artifact, never inside it, and stamps the pin.
                 --repin ID1[,ID2...] --why REASON is the authorized break of a broken
                 freeze. --pin refuses while ANY other node reports a violation, so two
                 artifacts that both moved deadlock each other (NF-17) and the only way
                 out was importing this module to rewrite artifact.sha256 behind the CLI.
                 --repin re-executes each named node's gate (must exit 0) and its
                 gate_negative (must exit nonzero) before any pin moves, writes every pin
                 in one atomic save, and appends an immutable `repin_audit` entry
                 {nodes, old_sha, new_sha, why, at_iso}. An empty --why, or any unnamed
                 node carrying a violation, exits 2 without writing.
                 one-line `output` summary has an artifact behind it.
  "gate_attempts": the last GATE_ATTEMPTS_KEEP --run-gate runs on that node, newest last:
                 {exit, at, commit, output, receipt}. A re-run that fails does not erase the run
                 that closed the node; it is kept here and reported as an ATTEMPT note.
  "actions":     [{node, action, outcome, at}] — the agent-level loop detector's
                 data. A server-side reasoning guard sees repeated tokens; it
                 cannot see the same action re-attempted with the same outcome.

Readiness is computed, never stored: only open/running/failed/done are recorded,
and blocked/ready come from the dependency graph. Stored readiness is what goes
stale when a dependency is reopened.

Checks (violations, exit 1)
  1. duplicate node ids; unknown dependency ids; dependency cycles.
  2. status outside open|running|failed|done (todo/blocked accepted as open).
  3. a node done with no evidence.
  4. a CLOSED node whose gate was never run, exited nonzero, or whose gate_result
     was recorded for a different command than the declared gate. An open node with
     an unrun gate is normal work, not a violation. A node stays closed when a *later*
     re-run fails: it stands on the run that passed, and the failed attempt is reported
     as an ATTEMPT note naming its receipt. A node that declares gate_negative also
     needs a gate_proof verdict of PROVEN from --prove-gate: a closure resting on a gate
     nobody falsified is a violation, and a VACUOUS proof is a violation too.
  5. precedence: a node done/running while a dependency is not done.
  6. agent-level loop: the same (node, action, outcome, kind) recorded LOOP_REPEAT
     times, or an A/B oscillation repeated LOOP_REPEAT cycles. Kinds are the five outcome
     kinds in tools/gate_exec.py (pass|fail|timeout|launch-error|no-output), so three runs
     cut off at the timeout and three genuine verdicts of 1 are not the same repeated action.
Notes (not failures)
  7. STALE: a closed node whose gate ran against a tree that has since moved in a
     way the gate could have caught. The row names the commit it ran against, so
     that commit..HEAD is the comparison; gate output, the plan file, archived
     receipts and prose date only the node that claims them. A row with no recorded
     commit falls back to instants, never to a day-string compare against HEAD.

Usage
  tools/plan_check.py [DAG.json ...]        validate + frontier (default: every
                                            project root's DAG.json that exists)
  tools/plan_check.py --frontier [DAG.json ...]   frontier lines only
  tools/plan_check.py --run-gate ID [DAG.json]    run the node's gate, record it,
                                                  close the node on exit 0. Also keeps
                                                  what the run measured: stdout archived
                                                  as a receipt, and every report file the
                                                  run rewrote copied to
                                                  run/evidence/<date>-<node>/
  tools/plan_check.py --prove-gate ID [--plan DAG.json]
  tools/plan_check.py --repin ID1,ID2 --why "REASON" [--plan DAG.json]
                 run the node's gate AND its gate_negative in the isolation boundary; the
                 gate must exit 0 and the negative must exit nonzero, else the node is
                 refused as vacuous. Records gate_proof. Exit 0 proven, 1 vacuous, 2 nothing
                 proven (no negative declared, blocked, or the gate itself did not pass).
  tools/plan_check.py --log ACTION OUTCOME [--node ID] [--outcome-kind K] [DAG.json]
"""
import argparse
import datetime
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import fact_store  # noqa: E402  receipts are read and written by that tool, never forked
import gate_exec  # noqa: E402  one isolation boundary and one outcome taxonomy, shared
                   # with spine_check.py: a gate record that meant "cut off" in one tool and
                   # "the gate said no" in the other is how a leaked tree got read as a verdict.

AGENT = pathlib.Path(__file__).resolve().parent.parent
ROOTS = [
    AGENT,
    pathlib.Path.home() / "neon_rush",
    pathlib.Path.home() / "omp-governance-experiments",
    pathlib.Path.home() / "context-recovery-experiments",
    pathlib.Path.home() / "governance-eval",
    pathlib.Path.home() / "Projects/morphofractal",
    pathlib.Path.home() / "Projects/amber_ascii",
    pathlib.Path.home() / "Projects/ascii_city",
    pathlib.Path.home() / "Projects/loom",
    pathlib.Path("/mnt/data/strata-workspace/Strata"),
]
# Vocabulary is the corpus's own (governance-eval/DAG.json, DAG_R4.json): open,
# todo, in_progress, done, and the two words generated plans reach for - pending,
# executed - plus closed. blocked/ready are computed, never stored. A synonym costs one
# line; an unreadable plan means a frontier no one prints.
STATUS = {"open": "open", "todo": "open", "blocked": "open", "pending": "open",
          "running": "running", "in_progress": "running",
          "failed": "failed", "done": "done", "closed": "done", "executed": "done"}
LOOP_REPEAT = 3
# Closure rests on the run that passed; the runs since then are history, kept bounded so that
# re-gating a node cannot grow its record without limit.
GATE_ATTEMPTS_KEEP = 12
# OMP_TODAY pins the stamp so a re-run of a gate reproduces the record it rewrites;
# with the variable unset nothing changes.
TODAY = os.environ.get("OMP_TODAY") or datetime.date.today().isoformat()
# The instant of this run, on the same UTC clock as the receipt filename. `at` and
# `closed` are human local days; staleness needs an instant, and a day is exactly
# the thing that cannot tell 21:34 from 02:47 the next morning.
NOW_UTC = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def load(path):
    plan = json.loads(path.read_text(encoding="utf-8"))
    raw_nodes = plan.get("nodes", [])
    if not isinstance(raw_nodes, list) or any(
            not isinstance(n, dict) or not n.get("id") for n in raw_nodes):
        raise ValueError("nodes are not a list of id-keyed objects - a plan dialect "
                         "this validator does not read")
    nodes = {n["id"]: dict(n) for n in raw_nodes}
    for node in raw_nodes:
        # `dependencies` is what the generated plans call it; deps is what ROOTS calls it.
        deps = list(node.get("deps") or node.get("dependencies") or [])
        nodes[node["id"]].setdefault("_deps", []).extend(deps)
    for edge in plan.get("edges") or []:          # DAG_R4.json pair form
        src, dst = edge[0], edge[1]
        if dst in nodes:
            nodes[dst].setdefault("_deps", []).append(src)
    for node in nodes.values():
        node["_deps"] = list(dict.fromkeys(node.get("_deps", [])))
    return plan, nodes


def cycle_ids(nodes):
    """Kahn's algorithm; whatever is left is inside a cycle."""
    indeg = {nid: len([d for d in node["_deps"] if d in nodes]) for nid, node in nodes.items()}
    order, queue = [], [nid for nid, d in indeg.items() if d == 0]
    while queue:
        nid = queue.pop()
        order.append(nid)
        for other, node in nodes.items():
            if nid in node["_deps"]:
                indeg[other] -= 1
                if indeg[other] == 0:
                    queue.append(other)
    return sorted(set(nodes) - set(order))


# Lock 3 of the multi-gate DAG (notes/multi-gate-dag.md): freezing a gate's artifact and
# freezing its re-execution are different rules. The bytes are write-frozen; the run stays
# read-rerunnable, because a later gate that rewrites a caller invalidates the closure an
# earlier gate recorded. Only the first rule is safe on its own, so both are checked here.
SEEDS_MIN_DEFAULT = 5
HEX_RE = re.compile(r"^[0-9a-f]+$")


# STALE asks whether the tree a gate ran against moved in a way that could falsify it.
# Day strings cannot answer that, and neither can HEAD's date. Three clocks write this
# file: gate_result.at is a LOCAL day (date.today()), a receipt coordinate's filename is a
# UTC stamp (fact_store.write_receipt stamps with datetime.now(utc)), git's %cd is the
# committer's LOCAL day. LOOM showed the cost: its 16 gates ran 2026-11-04 21:34 local
# (receipt plan-loom-G6_20261105T033417610576.log, report ts 2026-11-05T03:34:17.442Z), the
# closeout commit landed 2026-11-05 02:47 local, and all 16 rows were called stale over a
# five-hour gap in which nothing but prose and gate output moved. So the comparison is
# tree-to-tree - the row names the commit it ran against - and the question is what moved.
# a run writes these wherever they land; they date nothing. Bytecode is the interpreter's
# copy of a source file the gate already ran: importing a harness rewrites it, so re-running
# any one gate re-dates every closed node that shares that package. 36 of the 73 STALE rows
# on 2026-11-11 were `bench/__pycache__/score.cpython-314.pyc` in cityblock-perf.
GENERATED_EXT = {"log", "png", "jpg", "jpeg", "gif", "webp", "svg", "mp4", "webm", "wav",
                 "pyc", "pyo", "pyd"}


def owns_path(rel, own):
    return any(rel == o or rel.startswith(o + "/") for o in own)


def evidence_move(rel, own):
    """The committed path that dates a closed node's evidence, or None.

    Gate output and bookkeeping move because a run happened: its reports, the environment
    snapshot it writes, the evidence copies, its figures, the bytecode the interpreter
    dropped beside a module it imported, the archived receipts, and the plan
    file it records itself in. Charging a gate for its own run is what called all 16 LOOM
    rows stale five hours after they passed. Prose is no gate's input, and a `run/`
    directory is where a run drops what it produced, so both date only the node that
    claims that path as its artifact; a baseline a gate READS belongs in bench/.
    Anything else is code, a fixture or a baseline, and is fair game for every node.

    The path is the return value because coverage cannot be known statically: two projects
    showed a genuinely stale gate blamed on whatever the newest commit happened to drop, a
    control log in one case and a scratch probe in the other. Naming the file lets the
    reader judge the claim instead of trusting the word covers.
    """
    if rel == "DAG.json" or rel.startswith(("archive/", "run/report-", "run/evidence/",
                                            "run/shots-")):
        return None
    if "__pycache__/" in rel:
        return None            # the interpreter's copy of source this gate already ran
    leaf = rel.rsplit("/", 1)[-1]
    if "." in leaf and leaf.rsplit(".", 1)[-1].lower() in GENERATED_EXT:
        return None            # a gate's own logs and pictures are a run, not a change
    # Any run/ directory, at any depth: a run drops its per-run weights, its control
    # log and the scratch probe it wrote to make its own comparison in there, and
    # charging closed nodes for those dated the corpus's largest block of rows on the
    # run that recorded them - a control log in chromatic-gravity (bench/run/fit/
    # ctrl.json, which c7599fc wrote beside nine per-run weight files and fifty fit
    # databases) and a scratch probe in omp-fractal-crucible (run/retune_query.py,
    # added by its closeout commit beside the run/chromatic_table_old.json it wrote).
    # Leaf-level only was the earlier rule, and it is what kept the nested case alive:
    # a control log under a run/ directory is run output, not a placed artifact. A
    # node still answers for a run/ path it claims as its own artifact.
    if "run" in rel.split("/")[:-1]:
        return rel if owns_path(rel, own) else None
    if rel.endswith(".md") or rel.startswith("notes/"):
        return rel if owns_path(rel, own) else None
    return rel


def git(home, *args):
    """(stdout, returncode); ("", 1) when git cannot be run at all."""
    try:
        proc = subprocess.run(["git", "-C", str(home), *args], capture_output=True, text=True)
        return proc.stdout.strip(), proc.returncode
    except OSError:
        return "", 1


def parse_instant(text):
    """Stamp -> aware UTC datetime, None for anything unparseable.

    Aware ISO-8601 is used as written; a naive stamp is read as UTC, because a report's `ts`
    is toISOString(); a receipt filename stamp (20261105T033417...) is UTC by construction.
    A bare date becomes that day's LOCAL midnight - the earliest instant the day can name -
    so a coarse field can never claim recency it did not measure.
    """
    text = str(text or "").strip()
    if not text:
        return None
    stamp = re.match(r"^(\d{8}T\d{6})", text)
    if stamp:
        try:
            return datetime.datetime.strptime(stamp.group(1), "%Y%m%dT%H%M%S").replace(
                tzinfo=datetime.timezone.utc)
        except ValueError:
            return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):  # before fromisoformat, which reads a
        return datetime.datetime(*map(int, text.split("-"))).astimezone()  # day as UTC
    try:
        dt = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            day = datetime.date.fromisoformat(text[:10])
        except ValueError:
            return None
        return datetime.datetime(day.year, day.month, day.day).astimezone()
    return dt if dt.tzinfo else dt.replace(tzinfo=datetime.timezone.utc)


def receipt_instant(coord):
    """The archive's own UTC stamp of a run, read out of the receipt coordinate.

    The coordinate is archive/gates/<name>_<stamp>.log:L1-L<N>@<sha12>. The stamp is taken
    from the wall clock when the log is written, which makes it the one field that cannot
    drift from when the gate actually ran - `at` is a day, and a hand-recorded row can say
    anything about that day.
    """
    found = re.search(r"(\d{8}T\d{6})", str(coord or ""))
    return parse_instant(found.group(1)) if found else None


def gate_run_instant(node):
    """When the evidence a closed node stands on was last true.

    The latest defensible stamp wins: the UTC instant --run-gate recorded, the receipt
    filename's UTC stamp, the local day in `at`/`closed`, a read-only re-execution's
    rerun_at. Taking only `at` let a correct 21:34 run read as a morning.
    """
    result = node.get("gate_result") or {}
    art = node.get("artifact") if isinstance(node.get("artifact"), dict) else {}
    pinned = art.get("pinned") if isinstance(art.get("pinned"), dict) else {}
    found = [parse_instant(result.get("at_iso")), receipt_instant(result.get("receipt")),
             parse_instant(result.get("at")), parse_instant(node.get("closed")),
             parse_instant(pinned.get("rerun_at"))]
    stamps = [f for f in found if f]
    return max(stamps) if stamps else None


def node_paths(home, node):
    """What the node claims as its own: the pinned artifact target and the seed ledger."""
    art = node.get("artifact") if isinstance(node.get("artifact"), dict) else {}
    out = set()
    for spec in (art.get("path"), art.get("seeds")):
        target, _start, _end = artifact_target(home, spec)
        if target is None:
            continue
        try:
            out.add(str(target.relative_to(home)))
        except ValueError:
            out.add(str(target))
    return out


def commit_log(home, rev=None, limit=64):
    """[(sha, aware instant, changed paths)] newest first; [] when home is not a repo root.

    `rev` restricts the walk (a range, when the row names the tree it ran against); `limit`
    bounds the unbounded fallback. A merge prints no paths under the default diff, so it is
    never the commit that dates a gate; the parent whose content moved is in the same walk.

    `home` is resolved before the toplevel comparison: `plan_check.py DAG.json` run from
    inside the project hands us `.`, and comparing that with git's absolute toplevel answered
    [] - every relative invocation switched the staleness lane off and printed `graph ok`
    over real drift (47 hidden STALE rows across the corpus, probed 2026-11-05). A directory
    that is no repo still answers [], and validate() marks that case out loud.
    """
    home = pathlib.Path(home).resolve()
    top, rc = git(home, "rev-parse", "--show-toplevel")
    if rc or pathlib.Path(top) != home:
        return []
    spec = rev if rev else f"-{limit}"
    args = ["log", spec, "--format=%x00%h%cI", "--name-only"] if rev else \
        ["log", spec, "--format=%x00%h%cI", "--name-only"]
    out, rc = git(home, *args)
    if rc:
        return []
    entries = []
    for chunk in out.split("\x00"):
        lines = [line.strip() for line in chunk.splitlines() if line.strip()]
        if not lines:
            continue
        when = parse_instant(lines[0][7:])
        if when:
            entries.append((lines[0][:7], when, [p.strip("/") for p in lines[1:]]))
    return entries


def staleness_note(nid, node, home, label, history):
    """Why a closed node's evidence no longer describes the tree, or None.

    The row names the tree its gate ran against (`gate_result.commit`), so that is the
    comparison: what moved from there to HEAD, and did any of it bear on the gate. When the
    row names no commit - hand-recorded evidence, or a plan older than the field - fall back
    to instants, the newest dated commit in history against the latest stamp the row can
    defend. A recorded commit that HEAD does not contain is its own finding: the evidence
    points at a tree nobody can check out.
    """
    result = node.get("gate_result") or {}
    own = node_paths(home, node)
    recorded = str(result.get("commit") or "").strip()
    head, rc = git(home, "rev-parse", "HEAD")
    if not rc and head and recorded:
        sha, rc = git(home, "rev-parse", "--verify", recorded + "^{commit}")
        if rc:
            return (f"STALE {nid}: gate ran against {recorded}, which {label} does not "
                    f"contain; the evidence names a tree that cannot be checked out")
        if sha == head:
            return None
        if git(home, "merge-base", "--is-ancestor", sha, head)[1]:
            return (f"STALE {nid}: gate ran against {recorded[:7]}, which is not an ancestor "
                    f"of HEAD in {label}; the tree the evidence describes is not this tree")
        for later, _when, paths in commit_log(home, f"{sha}..HEAD"):
            hit = next((p for p in paths if evidence_move(p, own)), None)
            if hit:
                return (f"STALE {nid}: gate ran against {recorded[:7]}, and {hit} moved in "
                        f"{later[:7]} after that run - the evidence describes older code, "
                        f"re-run --run-gate {nid} --read-only")
        return None
    at = gate_run_instant(node)
    for later, when, paths in history:
        hit = next((p for p in paths if evidence_move(p, own)), None) if at else None
        if hit and at < when:
            return (f"STALE {nid}: gate ran {at.astimezone().date().isoformat()}, newest "
                    f"commit in {label} is {when.astimezone().date().isoformat()} "
                    f"({hit}, {later[:7]}) - the evidence may describe older code")
    return None
def artifact_target(home, spec):
    """`rel/path:L<start>-L<end>` -> (path, start, end). end None = to the last line."""
    text = str(spec or "").strip()
    if not text:
        return None, None, None
    # parsed without a regex on purpose: this interpreter's `re` mis-anchors a
    # single-character capture group before `$` (x(\d)$ matches "x12"), and a silent
    # miss here would freeze the whole file while the plan claims two lines.
    head, sep, tail = text.rpartition(":L")
    if not sep or not tail:
        return pathlib.Path(text), None, None
    start_text, dash, end_text = tail.partition("-")
    if not start_text.isdigit():
        return pathlib.Path(text), None, None
    if dash:
        # the closed form is `:L<start>-L<end>`; the end marker carries its own L.
        end_text = end_text[1:] if end_text[:1] == "L" else end_text
        if not end_text.isdigit():
            return pathlib.Path(text), None, None
    return pathlib.Path(head), int(start_text), int(end_text) if dash else None


def digest_of(path, start=None, end=None):
    """SHA-256 of the whole file, or of the named line range. -> (hex, error)."""
    try:
        data = path.read_bytes()
    except OSError as exc:
        return None, f"unreadable ({exc.strerror})"
    if start is None:
        return hashlib.sha256(data).hexdigest(), None
    # one hasher for every line-range pin in the spine: fact_store.byte_slice keeps the
    # terminating 0x0A, a local split/join would drop it and hash different bytes for the
    # same lines, so a receipt coordinate and an artifact pin would never agree.
    lo = max(int(start), 1)
    hi = int(end) if end else len(data.split(b"\n"))
    if data.endswith(b"\n") and hi >= len(data.split(b"\n")):
        hi = data.count(b"\n")
    try:
        return fact_store.read_pin(path, lo, hi), None
    except fact_store.AnchorError as exc:
        return None, str(exc).split(": ", 1)[-1]


def pin_artifact(path, node_id):
    """--pin ID: record the digest of a node's artifact as the bytes currently stand."""
    problems, _, _, plan, nodes = validate(path)
    blockers = [p for p in problems if not p.startswith(f"{node_id}:")]
    if blockers:
        print("plan has violations; fix them before pinning an artifact:", file=sys.stderr)
        for p in blockers:
            print(f"  VIOLATION {p}", file=sys.stderr)
        # The hint names the whole frozen set, including the node being pinned: the authorized
        # break covers every pin that moved together, so the route printed is the one that works.
        freeze_hint(problems)
        return 1
    node = nodes.get(node_id)
    if node is None:
        print(f"no node {node_id} in {path}", file=sys.stderr)
        return 1
    art = node.get("artifact")
    if not isinstance(art, dict):
        print(f"node {node_id} declares no artifact", file=sys.stderr)
        return 1
    p, start, end = artifact_target(path.parent, art.get("path"))
    if p is None:
        print(f"{node_id}: artifact declares no path", file=sys.stderr)
        return 1
    p = p if p.is_absolute() else path.parent / p
    digest, err = digest_of(p, start, end)
    if digest is None:
        print(f"{node_id}: cannot pin {p}: {err}", file=sys.stderr)
        return 1
    art["sha256"] = digest
    art.setdefault("rerun_ok", True)
    art["pinned"] = {"at": TODAY, "sha256": digest, "rerun_at": TODAY}
    save(path, plan)
    print(f"pinned {node_id}: {art.get('path')} @ {digest[:12]} on {TODAY}")
    print("  write-frozen: those bytes failing the plan means the freeze broke, not drifted")
    return 0


FREEZE_MARK = "no longer matches the bytes it was pinned"


def freeze_hint(problems):
    """Name the authorized way out when a refusal is caused by somebody else's broken freeze.

    A digest mismatch is reported on the node that owns the artifact, and --pin refuses while
    ANY other node carries a violation. Two artifacts that both moved therefore deadlock: each
    node's repair is refused by the other's violation (NF-17, 2026-11-22). Without this line the
    refusal is a dead end, and the operator escapes it by hand-editing artifact.sha256 - the one
    move the freeze exists to make loud.
    """
    frozen = sorted({p.split(":", 1)[0] for p in problems if FREEZE_MARK in p})
    if not frozen:
        return
    print(f"  {len(frozen)} node(s) above are frozen-broken, so each one's --pin is refused by "
          f"the other's violation. The authorized break is: --repin "
          f"{','.join(frozen)} --why \"REASON\". Do not edit artifact.sha256 by hand.",
          file=sys.stderr)


def repin_nodes(path, ids, why, timeout=None, env_extra=(), fsize_mb=None, user=""):
    """--repin ID1[,ID2...] --why REASON: an auditable, gate-verified break of a frozen pin.

    NF-17 is a precedence deadlock inside --pin, not a missing flag. pin_artifact refuses while
    any OTHER node reports a violation, and check_artifact reports a digest mismatch as a
    violation ("the freeze is broken, not drifted"). When two pinned artifacts move together - a
    spine refactor that shifted a slice pinned by two notes - each node's own repair is refused
    by the other's violation, and the only way out was importing plan_check to rewrite
    artifact.sha256 behind the CLI. That escape is the actual failure: a freeze break with no
    trace in the plan.

    --repin restores the three properties the deadlock lost:
      * nothing moves unless every named node's gate exits 0 and its declared gate_negative
        exits nonzero, both run in the same isolation boundary --prove-gate uses;
      * all pins move in one save(), so the plan is never half-repinned;
      * the plan gains an append-only `repin_audit` entry naming nodes, old_sha, new_sha, why,
        at_iso and both exits, so a re-pin is a record rather than an edit.

    Refusals exit 2 and write nothing: empty --why; any unnamed node carrying ANY violation (the
    authorized set has to cover the whole deadlock); a named node that is not done, declares no
    artifact, declares no gate, or has an undone dependency; a gate that fails, in which case no
    pin in the plan moves.

    Exit codes: 0 = pins moved and audit appended; 2 = refused, nothing written.
    """
    if not str(why or "").strip():
        print("--repin needs --why \"REASON\": an unexplained freeze break is "
              "indistinguishable from a breach", file=sys.stderr)
        return 2
    named = list(dict.fromkeys(x.strip() for x in ids if x.strip()))
    if not named:
        print("--repin names no node", file=sys.stderr)
        return 2
    problems, _, summary, plan, nodes = validate(path)
    unknown = [n for n in named if n not in nodes]
    if unknown:
        print(f"--repin names nodes that are not in {path}: {unknown}", file=sys.stderr)
        return 2
    unnamed = [p for p in problems if not any(p.startswith(f"{n}:") for n in named)]
    if unnamed:
        print(f"--repin refuses: {len(unnamed)} violation(s) sit on nodes outside the "
              f"authorized set {named}; re-pinning part of a deadlock hides the other break:",
              file=sys.stderr)
        for p in unnamed:
            print(f"  VIOLATION {p}", file=sys.stderr)
        return 2
    budget = gate_exec.GATE_TIMEOUT_S if timeout is None else float(timeout)
    uid, gid = gate_exec.resolve_user(user) if user else (None, None)
    commit = subprocess.run(["git", "-C", str(path.parent), "log", "-1", "--format=%h"],
                            capture_output=True, text=True).stdout.strip()
    staged = []
    for nid in named:
        node = nodes[nid]
        art = node.get("artifact")
        if not isinstance(art, dict) or not str(art.get("path") or "").strip():
            print(f"--repin {nid}: declares no artifact path, so there is no pin to move; "
                  f"--pin is the route for that node", file=sys.stderr)
            return 2
        if summary["state"].get(nid) != "done":
            print(f"--repin {nid}: state is {summary['state'].get(nid)}, not done. A re-pin "
                  f"re-authorizes a closure; --pin is the route for an unclosed node",
                  file=sys.stderr)
            return 2
        gate = str(node.get("gate") or "").strip()
        if not gate:
            print(f"--repin {nid}: declares no gate, so the pin would move on nothing - an "
                  f"unverified pin is exactly the state NF-17 was reported against",
                  file=sys.stderr)
            return 2
        negative = str(node.get("gate_negative") or "").strip()
        undone = [d for d in node["_deps"] if summary["state"].get(d) != "done"]
        if undone:
            print(f"--repin {nid}: dependencies {undone} are not done", file=sys.stderr)
            return 2
        started = time.perf_counter()
        proc, kind, leftovers = gate_exec.run(gate, path.parent, timeout=budget,
                                              env=tuple(env_extra), uid=uid, gid=gid,
                                              fsize_mb=fsize_mb)
        archive(proc, path, nid, gate, (time.perf_counter() - started) * 1000.0, commit)
        print(f"gate {nid}: exit {proc.returncode} kind={kind}")
        print(f"  {gate_summary(proc.stdout, proc.stderr)}")
        if leftovers:
            print(f"  swept {len(leftovers)} process(es) still in the gate's group")
        if proc.returncode != 0:
            print(f"NOT REPINNED {nid}: its gate exited {proc.returncode}; no pin in this plan "
                  f"moves", file=sys.stderr)
            return 2
        neg_exit = None
        if negative:
            started = time.perf_counter()
            nproc, nkind, nleft = gate_exec.run(negative, path.parent, timeout=budget,
                                                env=tuple(env_extra), uid=uid, gid=gid,
                                                fsize_mb=fsize_mb)
            archive(nproc, path, nid, negative, (time.perf_counter() - started) * 1000.0, commit)
            neg_exit = nproc.returncode
            print(f"gate_negative {nid}: exit {neg_exit} kind={nkind}")
            if nkind == "timeout":
                print(f"NOT REPINNED {nid}: the negative was cut off at {budget:g}s, which "
                      f"proves nothing about the gate", file=sys.stderr)
                return 2
            if neg_exit == 0:
                print(f"NOT REPINNED {nid}: gate_negative exited 0 - a vacuous gate cannot "
                      f"authorize a freeze break", file=sys.stderr)
                return 2
        # Digest AFTER the gate ran: the gate is what re-creates the artifact, and a digest taken
        # beforehand would freeze bytes the run has already overwritten.
        f, start, end = artifact_target(path.parent, art["path"])
        # Same resolution --pin uses: a relative artifact spec is relative to the plan, not to
        # whatever directory plan_check happened to be launched from.
        f = f if f.is_absolute() else path.parent / f
        digest, err = digest_of(f, start, end)
        if digest is None:
            print(f"--repin {nid}: cannot read artifact {art['path']}: {err}", file=sys.stderr)
            return 2
        staged.append({"node": nid, "artifact": art, "old_sha": art.get("sha256", ""),
                       "new_sha": digest, "gate_exit": proc.returncode,
                       "gate_negative_exit": neg_exit})
    # Both sides proved for every named node: move all pins and append the audit in one save.
    raw = {n["id"]: n for n in plan.get("nodes", []) if isinstance(n, dict)}
    for row in staged:
        art = raw[row["node"]].setdefault("artifact", {})
        art["sha256"] = row["new_sha"]
        art["pinned"] = {"at": TODAY, "sha256": row["new_sha"], "rerun_at": TODAY,
                         "repin": str(why).strip()}
    entry = {"nodes": [r["node"] for r in staged],
             "old_sha": {r["node"]: r["old_sha"] for r in staged},
             "new_sha": {r["node"]: r["new_sha"] for r in staged},
             "why": str(why).strip(), "at_iso": NOW_UTC,
             "gate_exit": {r["node"]: r["gate_exit"] for r in staged},
             "gate_negative_exit": {r["node"]: r["gate_negative_exit"] for r in staged}}
    plan.setdefault("repin_audit", []).append(entry)
    save(path, plan)
    for row in staged:
        print(f"REPINNED {row['node']}: {(row['old_sha'] or '(unpinned)')[:12]} -> "
              f"{row['new_sha'][:12]} ({raw[row['node']]['artifact']['path']})")
    print(f"  repin_audit[{len(plan['repin_audit']) - 1}] at {NOW_UTC}: {entry['nodes']}"
          f" \"{entry['why']}\"")
    return 0


def check_artifact(home, nid, node, state, nodes):
    """A pinned artifact either still holds the bytes it was pinned at, or the closure it
    certifies is not trustworthy. A digest mismatch never reads as drift accepted."""
    problems, notes = [], []
    art = node.get("artifact")
    if art is None:
        return problems, notes
    if not isinstance(art, dict):
        return [f"{nid}: artifact must be an object"], notes
    spec = str(art.get("path") or "").strip()
    if not spec:
        problems.append(f"{nid}: artifact declares no path")
    rerun = art.get("rerun_ok")
    if not isinstance(rerun, bool):
        problems.append(f"{nid}: artifact.rerun_ok must be true or false")
    digest = str(art.get("sha256") or "").strip().lower()
    if not HEX_RE.match(digest) or len(digest) not in (12, 64):
        problems.append(f"{nid}: artifact.sha256 {digest!r} is not 12 or 64 hex digits - "
                        f"the repair is --pin {nid}")
    if state.get(nid) != "done":
        return problems, notes
    pinned = art.get("pinned") if isinstance(art.get("pinned"), dict) else {}
    if spec:
        p, start, end = artifact_target(home, spec)
        p = p if p.is_absolute() else home / p
        now, err = digest_of(p, start, end)
        if now is None:
            problems.append(f"{nid}: pinned artifact {spec} is {err}")
        elif not now.startswith(digest):
            problems.append(
                f"{nid}: pinned artifact {spec} no longer matches the bytes it was pinned "
                f"at ({digest[:12]} -> {now[:12]}) - the freeze is broken, not drifted: a "
                f"read-only gate may rewrite its outputs beside the artifact, never inside "
                f"it; re-run --run-gate {nid} --read-only, then --pin {nid} to accept new bytes")
    seeds = str(art.get("seeds") or "").strip()
    if seeds:
        try:
            want = int(art.get("min_seeds") or SEEDS_MIN_DEFAULT)
        except (TypeError, ValueError):
            problems.append(f"{nid}: artifact.min_seeds {art.get('min_seeds')!r} is not a count")
            want = None
        sp = pathlib.Path(seeds)
        sp = sp if sp.is_absolute() else home / sp
        try:
            rows = [l for l in sp.read_text(encoding="utf-8").splitlines()
                    if l.strip() and not l.startswith("#")]
        except OSError as exc:
            problems.append(f"{nid}: seed list {seeds} is unreadable ({exc.strerror})")
        else:
            if want is not None and len(rows) < want:
                problems.append(f"{nid}: freeze proof names {len(rows)} seeds in {seeds}, "
                                f"needs {want} - N=1 explores, it does not freeze")
    consumers = sorted(n for n, other in nodes.items()
                       if nid in other.get("_deps", []) and state.get(n) == "done")
    rerun_at = str(pinned.get("rerun_at") or pinned.get("at") or "")
    if rerun:
        if consumers and rerun_at:
            after = [c for c in consumers if str(nodes[c].get("closed") or "") > rerun_at]
            if after:
                problems.append(
                    f"{nid}: consumers {after} closed after the artifact's last re-run "
                    f"({rerun_at}) - the closure rests on bytes whose callers moved and were "
                    f"never re-executed: --run-gate {nid} --read-only")
    elif not pinned:
        notes.append(f"PIN {nid}: artifact declares rerun_ok false, so this closure can never "
                     f"be re-executed; the node's record must name why")
    return problems, notes


def validate(path):
    """Return (violations, notes, summary, plan, nodes) for one plan file."""
    problems, notes = [], []
    plan, nodes = load(path)
    label = path.parent.name or str(path)

    raw_ids = [n.get("id") for n in plan.get("nodes", [])]
    dupes = sorted({i for i in raw_ids if raw_ids.count(i) > 1})
    for nid in dupes:
        problems.append(f"duplicate node id {nid}")
    for nid, node in nodes.items():
        for dep in node["_deps"]:
            if dep not in nodes:
                problems.append(f"{nid}: dependency {dep} is not a node")
    for nid in cycle_ids(nodes):
        problems.append(f"{nid}: part of a dependency cycle")

    state = {}
    for nid, node in nodes.items():
        stored = str(node.get("status", "open")).strip().lower()
        if stored not in STATUS:
            problems.append(f"{nid}: status {stored!r} is not "
                            f"open|todo|in_progress|running|failed|done")
        state[nid] = STATUS.get(stored, "open")

    # One unbounded git walk per plan, used only by rows that name no commit.
    history = commit_log(path.parent)
    # A plan outside version control cannot date its own evidence. Say it: a quiet lane reads
    # as "nothing moved", which is exactly the claim this lane exists to test.
    lane_off = git(path.parent, "rev-parse", "--show-toplevel")[1] != 0
    closed_gates = 0

    for nid, node in nodes.items():
        deps_done = all(state.get(d) == "done" for d in node["_deps"])
        if state[nid] == "done":
            if not str(node.get("evidence") or "").strip():
                problems.append(f"{nid}: done with no evidence")
            if not deps_done:
                undone = [d for d in node["_deps"] if state.get(d) != "done"]
                problems.append(f"{nid}: done while dependency {undone} is not done")
        if state[nid] == "running" and not deps_done:
            undone = [d for d in node["_deps"] if state.get(d) != "done"]
            problems.append(f"{nid}: running while dependency {undone} is not done")
        # An open node with an unrun gate is normal work. A CLOSED node is the
        # claim being checked: it may not stand without its gate having passed.
        gate = str(node.get("gate") or "").strip()
        if not gate or state[nid] != "done":
            continue
        result = node.get("gate_result") or {}
        recorded = str(result.get("cmd") or "").strip()
        if not result:
            problems.append(f"{nid}: closed without running its gate - {gate}")
            continue
        if recorded != gate:
            problems.append(f"{nid}: gate_result is for {recorded!r}, not the declared gate {gate!r}")
        if int(result.get("exit", 1)) != 0:
            problems.append(f"{nid}: done although its gate exited {result.get('exit')} - the "
                            f"repair is --run-gate {nid}, or reopen the node")
        negative = str(node.get("gate_negative") or "").strip()
        proof = node.get("gate_proof") if isinstance(node.get("gate_proof"), dict) else {}
        if negative:
            # kinds-bench validate.py:47-52: a hidden test that passes on the untouched folder
            # is BROKEN, not green - it found 7 of 36 that graded nothing. Declaring a negative
            # and closing without running both sides is the same untested claim.
            if not proof:
                problems.append(f"{nid}: closed on a gate that was never falsified - it declares "
                                f"gate_negative {negative!r} but carries no gate_proof; run "
                                f"--prove-gate {nid}")
            elif str(proof.get("verdict") or "") != "PROVEN":
                v = str(proof.get("verdict") or "missing")
                if v == "INCONCLUSIVE":
                    reason = (f"was cut off after {proof.get('negative_timeout_s', '?')}s, so it "
                              f"neither passed nor failed")
                else:
                    reason = (f"exited {proof.get('negative_exit')}, so this gate passes on a "
                              f"folder nobody touched")
                problems.append(f"{nid}: gate_proof verdict is {v!r} - the negative "
                                f"{negative!r} {reason}")
            elif str(proof.get("gate") or "").strip() != gate:
                problems.append(f"{nid}: gate_proof was recorded for "
                                f"{str(proof.get('gate') or '').strip()!r}, not the declared gate "
                                f"{gate!r}; re-run --prove-gate {nid}")
            elif str(proof.get("at") or "") < str(result.get("rerun_at") or result.get("at") or ""):
                # The row is green and the verdict says PROVEN; only the dates show the negative
                # was run against an older tree. Proof that predates the run vouches for nothing.
                problems.append(
                    f"{nid}: gate_proof is stale - recorded {proof.get('at') or 'undated'}, the gate "
                    f"last ran {result.get('rerun_at') or result.get('at')}, so this run was never "
                    f"falsified; re-run --prove-gate {nid}")
        closed_gates += 1
        note = staleness_note(nid, node, path.parent, label, history)
        if note:
            notes.append(note)
        # A re-run that failed after the node was closed is a reading of this box, not a
        # retraction of the closure: bars like PSI stall share and loadavg grade the machine
        # while it serves a model, and the project's own record says they fail when it is busy.
        # Keeping the pass silently would be the lie; erasing it would be worse.
        attempts = node.get("gate_attempts")
        if (isinstance(attempts, list) and attempts and isinstance(attempts[-1], dict)
                and int(attempts[-1].get("exit", 0)) != 0 and int(result.get("exit", 1)) == 0):
            a = attempts[-1]
            notes.append(f"ATTEMPT {nid}: gate re-run exited {a.get('exit')} on a node closed by "
                         f"the run at {result.get('receipt') or result.get('at')}; failing output "
                         f"archived at {a.get('receipt') or 'no receipt'} - re-run on a quiet box "
                         f"or reopen the node")

    if lane_off and closed_gates:
        notes.append(f"STALE LANE OFF: {path.parent} is not a git repo, so {closed_gates} "
                     f"closed gate(s) cannot be dated against any tree - no STALE row here "
                     f"proves anything about the code the gate ran against")

    for nid, node in nodes.items():
        ap, an = check_artifact(path.parent, nid, node, state, nodes)
        problems.extend(ap)
        notes.extend(an)

    actions = plan.get("actions") or []
    # Gate records are machine evidence, not reasoning attempts: a node's declared gate is
    # legitimately re-run every time the tree moves under it. The loop detector watches the
    # agent's own --log entries.
    acts = [a for a in actions if not str(a.get("action", "")).startswith("gate ")]
    seen = {}
    for act in acts:
        # The kind belongs in the signature: a run cut off at the timeout and a run that got a
        # verdict both landed as "exit 124", so a leaked process tree looked like a repeated
        # attempt and a distinct failure looked like a copy of an old one.
        key = (str(act.get("node", "")), str(act.get("action", "")), str(act.get("outcome", "")),
               str(act.get("outcome_kind", "")))
        seen[key] = seen.get(key, 0) + 1
    for key, count in sorted(seen.items()):
        if count >= LOOP_REPEAT:
            problems.append(f"loop: action {key[1]!r} on {key[0]} repeated {count}x with the "
                            f"same outcome {key[2]!r}"
                            + (f" kind {key[3]!r}" if key[3] else "")
                            + " - re-running it will not change anything")
    pairs = {}
    for i in range(1, len(acts)):
        a, b = str(acts[i - 1].get("action", "")), str(acts[i].get("action", ""))
        if a and b and a != b:
            pairs[tuple(sorted((a, b)))] = pairs.get(tuple(sorted((a, b))), 0) + 1
    for pair, count in sorted(pairs.items()):
        if count >= 2 * (LOOP_REPEAT - 1):
            problems.append(f"loop: oscillating between {pair[0]!r} and {pair[1]!r} "
                            f"({count} switches, no progress recorded)")

    ready = [nid for nid, node in nodes.items()
             if state[nid] == "open" and all(state.get(d) == "done" for d in node["_deps"])]
    summary = {
        "total": len(nodes),
        "done": sum(1 for s in state.values() if s == "done"),
        "ready": ready,
        "running": [nid for nid, s in state.items() if s == "running"],
        "failed": [nid for nid, s in state.items() if s == "failed"],
        "blocked": [nid for nid, s in state.items() if s == "open" and nid not in ready],
        "state": state,
    }
    return problems, notes, summary, plan, nodes


def _proves(node, plan):
    """What the node's own record says its gate demonstrates."""
    for key in ("verification", "deliverable", "purpose", "objective"):
        text = str(node.get(key) or "").strip()
        if text:
            return text if len(text) <= 200 else text[:197] + "..."
    acceptance = plan.get("acceptance")
    if isinstance(acceptance, list) and acceptance:
        return str(acceptance[0])[:200]
    return ""


def _explain_ready(nid, node, plan, nodes, path):
    """Why this node is next: the action, what its gate proves, what a pass writes back.

    The frontier line says *what* is ready; an agent still had to open the DAG to learn what
    the gate demonstrates, where the evidence lands, and how many times the node has already
    been attempted. That re-reading is a per-turn cost, and --frontier is the command the
    rehydrate path runs, so the answers ride on the line that asks for them.
    """
    gate = str(node.get("gate") or "").strip()
    print(f"    NEXT {nid}  {node.get('name', '')}")
    print(f"      do: build the deliverable, then {sys.executable} "
          f"{pathlib.Path(__file__).resolve()} --run-gate {nid} --plan {path}")
    attempts = [a for a in (node.get("gate_attempts") or []) if isinstance(a, dict)]
    failing = sum(1 for a in attempts if str(a.get("exit")) not in ("0", "None", ""))
    actions = [a for a in (plan.get("actions") or [])
               if isinstance(a, dict) and str(a.get("node", "")) == nid]
    deps = ", ".join(
        f"{d}@{(nodes.get(d, {}).get('gate_result') or {}).get('commit') or 'no-commit'}"
        for d in node["_deps"])
    if gate:
        print(f"      gate: {gate}")
        proves = _proves(node, plan)
        print("      proves: " + (proves if proves else
              "(no verification field - the gate proves a command ran, not a claim; a claim this"
              " node must answer for closes in the fact ledger: fact_store --stale lists what is"
              " owed, --reconcile retires one a later live claim already replaces, and"
              " spine_check --confirmed/--corrected is the verdict surface)"))
        receipt = fact_store.archive_dir_for(fact_store.DEFAULT_DB, None) \
            / f"plan-{path.parent.name}-{nid}_<ts>.log"
        print(f"      pass writes: gate_result{{cmd,exit,at,commit,receipt}} + evidence on "
              f"{nid}; receipt -> {receipt}")
    else:
        print("      gate: (none declared - nothing will prove this node closed)")
    print(f"      deps: {deps or '(none)'}   gate attempts: {len(attempts)} "
          f"({failing} failing)   logged actions: {len(actions)}")
    if actions:
        print(f"      last action: {actions[-1].get('action', '')} "
              f"-> {actions[-1].get('outcome', '')}")
    if node.get("rollback"):
        print(f"      rollback: {str(node['rollback'])[:200]}")
    if not (node.get("gate_result") or {}).get("commit") and not node.get("closed"):
        print("      staleness: the node names no commit yet, so its closure will be dated "
              "against HEAD; name the commit in the evidence to pin it")


def print_frontier(path, summary, explain=False):
    plan, nodes = load(path)
    name = plan.get("project") or path.parent.name
    print(f"  plan {name}: {summary['done']}/{summary['total']} done, "
          f"{len(summary['ready'])} ready, {len(summary['running'])} running, "
          f"{len(summary['failed'])} failed, {len(summary['blocked'])} blocked")
    for nid in summary["ready"]:
        plan_node = next(n for n in plan["nodes"] if n["id"] == nid)
        if explain:
            _explain_ready(nid, nodes[nid], plan, nodes, path)
            continue
        gate = str(plan_node.get("gate") or "").strip()
        print(f"    NEXT {nid}  {plan_node.get('name', '')}"
              + (f"   gate: {gate}" if gate else "   (no gate declared)"))
    for nid in summary["running"]:
        print(f"    IN FLIGHT {nid}" + (f"  {nodes[nid].get('name', '')}" if explain else ""))
    for nid in summary["failed"]:
        line = f"    FAILED GATE {nid} - do not proceed past it"
        if explain:
            last = nodes[nid].get("gate_result") or {}
            line += (f"   exit {last.get('exit')}   output: "
                     f"{str(last.get('output') or '')[:120]}   receipt: "
                     f"{last.get('receipt') or 'none'}")
        print(line)


def _plan_glob():
    for base in (pathlib.Path.home() / "Projects", pathlib.Path.home()):
        for d in sorted(base.glob("*/DAG.json")):
            yield d


def _is_plan_shape(path):
    """True when the file is the dialect this validator reads: id-keyed node list."""
    try:
        nodes = json.loads(path.read_text(encoding="utf-8")).get("nodes")
    except (OSError, ValueError):
        return False
    return isinstance(nodes, list) and all(isinstance(n, dict) and n.get("id") for n in nodes)


def rooted_plans():
    return [r / "DAG.json" for r in ROOTS if (r / "DAG.json").is_file()]


def default_plans():
    """Every readable plan on the box, ROOTS first.

    ROOTS is curated and drifts: cityblock-perf sat ACTIVE in STATE.md with a 5/10 plan
    while no default frontier run ever printed it. Discovery keeps the list equal to the
    corpus; a plan in another dialect is named by skipped_plans(), never quietly dropped.
    """
    rooted = rooted_plans()
    seen = {p.resolve() for p in rooted}
    found = [d for d in _plan_glob() if d.resolve() not in seen and _is_plan_shape(d)]
    return rooted + found


def skipped_plans():
    """DAG.json files on disk this validator cannot read."""
    seen = {p.resolve() for p in rooted_plans()}
    return [d for d in _plan_glob()
            if d.resolve() not in seen and not _is_plan_shape(d)]


def save(path, plan):
    # validate() resolves dependencies into a private _deps field; internal state
    # never reaches the plan file.
    body = {k: v for k, v in plan.items() if k != "nodes"}
    body["nodes"] = [{k: v for k, v in n.items() if k != "_deps"}
                     for n in plan.get("nodes", [])]
    # One write, one rename: --repin moves several pins in a single save, and a plan left
    # half-written by a killed process would validate as a graph nobody authorized.
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(body, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


# A gate's own report files are the referents a record quotes, and a live output file is
# accurate the day it is written and contradicted whenever the gate next runs
# (Projects/amber_ascii/perm-record.md §16). --run-gate therefore copies every report the
# run rewrote into run/evidence/<date>-<node>/ before the next run can overwrite it.
EVIDENCE_SUBDIR = pathlib.Path("run") / "evidence"
REPORT_EXT = {".json", ".csv", ".txt", ".log", ".ndjson", ".md"}
MAX_REPORT_BYTES = 8 * 1024 * 1024
MAX_REPORTS = 40

RECEIPT_TAIL_RE = re.compile(r"\s*\|\s*receipt:\s*\S+.*$")


def snapshot_reports(home: pathlib.Path, node_id: str, since: float, declared=(),
                     exclude=()):
    """Copy the reports this run rewrote into run/evidence/<date>-<node>/. -> [record].

    `since` is the wall-clock instant the gate started: a file with an older mtime was not
    touched by this run, and copying it would archive a measurement the run never made.
    A node may declare `"reports": ["run/x.json", ...]` to name its referents exactly;
    otherwise the run/ subtree is swept. Returns [] when there is nothing to keep.
    """
    srcs = []
    for rel in declared:
        p = home / str(rel)
        if p.is_file():
            srcs.append(p)
    if not srcs and (home / EVIDENCE_SUBDIR.parent).is_dir():
        srcs = [p for p in sorted((home / EVIDENCE_SUBDIR.parent).rglob("*"))
                if p.is_file() and p.suffix in REPORT_EXT
                and EVIDENCE_SUBDIR not in p.relative_to(home).parents]
    # a declared seed list is an input the plan reads, not output the gate wrote: keeping it
    # as evidence would claim the run produced the very bytes that constrain it.
    skip = {(home / str(rel)).resolve() for rel in (exclude or ())}
    srcs = [p for p in srcs if p.resolve() not in skip]
    touched = []
    for p in srcs:
        try:
            st = p.stat()
        except OSError:
            continue
        if st.st_mtime >= since - 1.0 and st.st_size <= MAX_REPORT_BYTES:
            touched.append((p, st))
        if len(touched) >= MAX_REPORTS:
            break
    if not touched:
        return []
    stamp = TODAY
    for keep in range(1, 100):
        dest_dir = home / EVIDENCE_SUBDIR / (f"{stamp}-{node_id}" if keep == 1
                                             else f"{stamp}-{node_id}-{keep}")
        if not dest_dir.exists():
            break
    try:
        dest_dir.mkdir(parents=True)
    except OSError:
        return []
    out = []
    for src, st in touched:
        dest = dest_dir / src.name
        try:
            shutil.copy2(src, dest)
            digest = hashlib.sha256(dest.read_bytes()).hexdigest()
        except OSError:
            continue
        out.append({"src": str(src.relative_to(home)), "copy": str(dest.relative_to(home)),
                    "sha256": digest[:12], "bytes": st.st_size})
    return out


def with_receipt(evidence: str, coord: str) -> str:
    """The node's evidence plus this run's receipt, replacing any older coordinate.

    Re-running a gate replaces its evidence; it does not accumulate a chain of stale
    coordinates that a later reader would have to disambiguate by timestamp.
    """
    base = RECEIPT_TAIL_RE.sub("", evidence or "").strip().rstrip("|").strip()
    return f"{base} | receipt: {coord}" if base else f"receipt: {coord}"


def archive(proc, path, node_id, gate, duration_ms, commit):
    """Archive the run beside the durable store's receipts. Never fatal to the gate."""
    if proc is None:
        return None
    try:
        return fact_store.write_receipt(
            fact_store.archive_dir_for(fact_store.DEFAULT_DB, None),
            f"plan-{path.parent.name}-{node_id}", gate, proc, duration_ms,
            commit=commit or None)
    except OSError as exc:
        print(f"  NOTE receipt not archived: {exc}", file=sys.stderr)
        return None


VERDICT_RE = re.compile(r"^GATE\s+\S+\s+(PASS|FAIL)\b")


def gate_summary(stdout, stderr):
    """The gate's own verdict line, taken from stdout; stderr only when stdout said nothing.

    gate_result.output is the sentence a reader trusts to say what the run decided, so it
    must be the verdict and not the noise that trailed it. Node writes advisory warnings --
    the MODULE_TYPELESS_PACKAGE_JSON notice and its "(Use `node --trace-warnings ...`" tail
    -- on stderr *after* the verdict lands on stdout, and taking the last line of the two
    streams concatenated let that warning become the record: loom's G6/G7/G10 rows stored
    the warning while the gate had passed on stdout. Prefer the last line that reads like a
    verdict, then any stdout line, then the last stderr line -- where a gate that died
    before printing anything left its reason -- and only then the empty marker.
    """
    out = [ln.strip() for ln in (stdout or "").splitlines() if ln.strip()]
    for ln in reversed(out):
        if VERDICT_RE.match(ln):
            return ln
    if out:
        return out[-1]
    err = [ln.strip() for ln in (stderr or "").splitlines() if ln.strip()]
    return err[-1] if err else "<no output>"


def run_gate(path, node_id, read_only=False, timeout=None, env_extra=(), fsize_mb=None,
             user="", dry_run=False):
    problems, _, summary, plan, nodes = validate(path)
    # The only repair for a node whose recorded gate failed is to run that gate again, so its own
    # violation must not refuse the run that clears it. Anyone else's problem still does.
    blockers = [p for p in problems if not p.startswith(f"{node_id}:")]
    if blockers:
        print("plan has violations; fix them before running a gate:", file=sys.stderr)
        for p in blockers:
            print(f"  VIOLATION {p}", file=sys.stderr)
        return 1
    node = nodes.get(node_id)
    if node is None:
        print(f"no node {node_id} in {path}", file=sys.stderr)
        return 1
    gate = str(node.get("gate") or "").strip()
    if not gate:
        print(f"node {node_id} declares no gate", file=sys.stderr)
        return 1
    if read_only and not isinstance(node.get("artifact"), dict):
        print(f"--read-only names no purpose on {node_id}: it declares no artifact",
              file=sys.stderr)
        return 1
    undone = [d for d in node["_deps"] if summary["state"].get(d) != "done"]
    if undone:
        print(f"{node_id} is blocked: dependencies {undone} are not done", file=sys.stderr)
        return 1
    started = time.perf_counter()
    # "Did this run rewrite that report?" is an mtime question, and mtime is wall-clock;
    # perf_counter's epoch is arbitrary, so it can never answer it.
    wall_start = time.time()
    # gate_exec, not subprocess.run: the shell died on a cut-off and its whole tree survived
    # it, and a launch failure was recorded as 124 beside a genuine timeout. One boundary for
    # both spine tools - own process group, group kill, RLIMIT_FSIZE on writes.
    budget = gate_exec.GATE_TIMEOUT_S if timeout is None else float(timeout)
    if dry_run:
        # A rehearsal has to answer "what would this run do" without touching the record:
        # no gate, no ledger, no write_back. It prints the same numbers the real run would
        # use, or it rehearses nothing.
        uid, gid = gate_exec.resolve_user(user) if user else (None, None)
        print(f"DRY-RUN {node_id} gate: {gate}")
        print(f"  cwd: {path.parent}")
        print(f"  timeout: {budget:.0f}s"
              f"  fsize: {gate_exec.fsize_bytes(fsize_mb) // (1024 * 1024)} MiB"
              f"  env: {', '.join(env_extra) if env_extra else '(inherited)'}"
              f"  user: {user or os.environ.get('USER', str(os.getuid()))}"
              f"{' (uid ' + str(uid) + ', gid ' + str(gid) + ')' if uid is not None else ''}")
        print("  on a pass this run would write gate_result + write_back and append a ledger row")
        return 0
    uid, gid = gate_exec.resolve_user(user) if user else (None, None)
    proc, kind, leftovers = gate_exec.run(gate, path.parent, timeout=budget,
                                          env=tuple(env_extra), uid=uid, gid=gid,
                                          fsize_mb=fsize_mb)
    exit_code = proc.returncode
    tail = gate_summary(proc.stdout, proc.stderr)
    duration_ms = (time.perf_counter() - started) * 1000.0
    commit = subprocess.run(["git", "-C", str(path.parent), "log", "-1", "--format=%h"],
                            capture_output=True, text=True).stdout.strip()
    # A failing run is archived too: the refusal is the evidence of why the node stayed open.
    rec = archive(proc, path, node_id, gate, duration_ms, commit)
    art = node.get("artifact") if isinstance(node.get("artifact"), dict) else {}
    reports = snapshot_reports(path.parent, node_id, wall_start,
                               node.get("reports") or (),
                               exclude=[art["seeds"]] if art.get("seeds") else ())
    # A failing re-run must not overwrite the run that closed the node; a node that was not
    # closed records the failure, because for it the failure is the current truth.
    prior = node.get("gate_result") or {}
    closed_on = (str(node.get("status") or "").strip() == "done"
                 and int(prior.get("exit", 1)) == 0)
    attempts = node.get("gate_attempts") if isinstance(node.get("gate_attempts"), list) else []
    attempt = {"exit": exit_code, "outcome_kind": kind, "at": TODAY, "at_iso": NOW_UTC,
               "commit": commit, "output": tail[:160]}
    if kind == "timeout":
        # "cut off" without the number is the second half of the same lost claim: the row
        # says the runner stopped it, and only the budget says whether at 1800 s or at 60.
        attempt["timeout_s"] = round(budget, 1)
    if read_only:
        attempt["kind"] = "read-only"
    if rec:
        attempt["receipt"] = rec["coord"]
    node["gate_attempts"] = (attempts + [attempt])[-GATE_ATTEMPTS_KEEP:]
    if exit_code == 0 or not closed_on:
        node["gate_result"] = {"cmd": gate, "exit": exit_code, "outcome_kind": kind,
                               "at": TODAY, "at_iso": NOW_UTC, "output": tail[:400],
                               "commit": commit}
        if rec:
            node["gate_result"]["receipt"] = rec["coord"]
        if reports:
            node["gate_result"]["reports"] = reports
        if read_only:
            # The re-run proves the frozen bytes still do the work under callers
            # that have moved on. It restamps rerun_at only; `at` stays pinned.
            art = node["artifact"]
            pinned = art.get("pinned") if isinstance(art.get("pinned"), dict) else {}
            pinned["rerun_at"] = TODAY
            pinned.setdefault("at", TODAY)
            art["pinned"] = pinned
    plan["nodes"] = [node if n.get("id") == node_id else n for n in plan["nodes"]]
    actions = plan.setdefault("actions", [])
    actions.append({"node": node_id, "action": f"gate {gate}", "outcome": f"exit {exit_code}",
                    "outcome_kind": kind, "at": TODAY})
    if exit_code == 0:
        node["status"] = "done"
        node["closed"] = TODAY
        base = str(node.get("evidence") or "").strip() or f"gate {gate} exit 0: {tail[:300]}"
        node["evidence"] = with_receipt(base, rec["coord"]) if rec else base
    save(path, plan)
    print(f"gate {node_id}: exit {exit_code} kind={kind} at {TODAY} commit {commit or '-'}")
    if kind == "timeout":
        # "kind=timeout" alone reads like the gate timed itself out. The runner cut it off, at
        # the budget that was asked for; without the number the row cannot be re-checked.
        print(f"  the runner cut the gate off at {budget:g}s; it printed no verdict")
    if leftovers:
        # A gate that leaves processes running after it exits is a hygiene bug it should see
        # printed, not a sweep nobody notices: kinds-bench measured 116 survivors in one run.
        print(f"  swept {len(leftovers)} process(es) still in the gate's group: "
              + " ".join(sorted({e["comm"] for e in leftovers})))
    print(f"  {tail}")
    if rec:
        print(f"  receipt {rec['coord']}")
    for r in reports:
        print(f"  kept {r['src']} -> {r['copy']} @ {r['sha256']}")
    if exit_code == 0:
        print("  closed")
    elif closed_on:
        print(f"  still closed on the run at {prior.get('receipt') or prior.get('at')} - this "
              f"attempt failed and is archived; plan_check reports it as ATTEMPT")
    else:
        print("  left open - the gate did not pass")
    return 0 if exit_code == 0 else 1


def prove_gate(path, node_id, timeout=None, env_extra=(), fsize_mb=None, user=""):
    """Prove a gate on both sides - kinds-bench's two-sided fixture check, ported.

    kbench/validate.py:26-52 runs every hidden test twice: on the folder as the agent gets it
    (must FAIL) and on the reference solution (must PASS). Seven of its 36 tests failed that
    audit while looking green. A spine gate has the same failure mode: `test -f out.png` passes
    because last week's run left out.png, and the node closed on a command that cannot tell a
    pass from a nothing.

    --prove-gate runs the gate (must exit 0) and the node's gate_negative (must exit nonzero)
    in the same isolation boundary and records `gate_proof`. A negative that exits 0 is a
    vacuous gate: no PROVEN is written, the attempt is recorded with verdict VACUOUS, and
    validate reports the node as a violation until the negative is fixed - the same contract
    as gate_attempts, where a failing run is kept as evidence rather than erased.

    Both sides run in the same boundary as --run-gate: one --gate-timeout budget each,
    --env, --max-fsize-mb and --user applied to both, so a negative that hangs is a
    timeout and not an infinite wait.

    A negative that is cut off by the budget is neither: the verdict is INCONCLUSIVE, exit 2,
    and validate keeps the node a violation until it is re-proved.

    Exit codes: 0 = proven, 1 = proven false (vacuous), 2 = nothing proven (no gate_negative,
    blocked dependencies, the gate itself did not pass, or the negative timed out).
    """
    problems, _, summary, plan, nodes = validate(path)
    blockers = [p for p in problems if not p.startswith(f"{node_id}:")]
    if blockers:
        print("plan has violations; fix them before proving a gate:", file=sys.stderr)
        for p in blockers:
            print(f"  VIOLATION {p}", file=sys.stderr)
        return 2
    node = nodes.get(node_id)
    if node is None:
        print(f"no node {node_id} in {path}", file=sys.stderr)
        return 2
    gate = str(node.get("gate") or "").strip()
    negative = str(node.get("gate_negative") or "").strip()
    if not gate:
        print(f"node {node_id} declares no gate", file=sys.stderr)
        return 2
    if not negative:
        print(f"SKIP {node_id}: declares no gate_negative, so there is nothing to prove. A "
              f"gate nobody has falsified is not evidence - declare the command that must "
              f"fail (the untouched-folder check, or the assertion the fix introduced), then "
              f"--prove-gate {node_id}", file=sys.stderr)
        return 2
    undone = [d for d in node["_deps"] if summary["state"].get(d) != "done"]
    if undone:
        print(f"{node_id} is blocked: dependencies {undone} are not done", file=sys.stderr)
        return 2
    commit = subprocess.run(["git", "-C", str(path.parent), "log", "-1", "--format=%h"],
                            capture_output=True, text=True).stdout.strip()
    started = time.perf_counter()
    budget = gate_exec.GATE_TIMEOUT_S if timeout is None else float(timeout)
    uid, gid = gate_exec.resolve_user(user) if user else (None, None)
    proc, kind, leftovers = gate_exec.run(gate, path.parent, timeout=budget,
                                          env=tuple(env_extra), uid=uid, gid=gid,
                                          fsize_mb=fsize_mb)
    gate_rec = archive(proc, path, node_id, gate, (time.perf_counter() - started) * 1000.0,
                       commit)
    print(f"gate {node_id}: exit {proc.returncode} kind={kind}")
    if kind == "timeout":
        # 124 from this runner's budget and 124 from a gate's own timeout(1) are the same integer;
        # the row has to say who stopped it, or a patience limit reads as a verdict on the code.
        print(f"  the runner cut the gate off at {budget:g}s; it printed no verdict")
    print(f"  {gate_summary(proc.stdout, proc.stderr)}")
    if leftovers:
        print(f"  swept {len(leftovers)} process(es) still in the gate's group")
    if proc.returncode != 0:
        print(f"NOT PROVEN {node_id}: the gate itself exited {proc.returncode}, so there is "
              f"nothing yet to falsify - fix the gate, then prove it", file=sys.stderr)
        return 2
    started = time.perf_counter()
    nproc, nkind, nleft = gate_exec.run(negative, path.parent, timeout=budget,
                                        env=tuple(env_extra), uid=uid, gid=gid,
                                        fsize_mb=fsize_mb)
    neg_rec = archive(nproc, path, f"{node_id}-negative", negative,
                      (time.perf_counter() - started) * 1000.0, commit)
    print(f"negative {node_id}: exit {nproc.returncode} kind={nkind}")
    print(f"  {gate_summary(nproc.stdout, nproc.stderr)}")
    if nleft:
        print(f"  swept {len(nleft)} process(es) still in the negative's group")
    if nkind == "timeout":
        # A negative that was cut off falsified nothing. "Ran out of patience" is not "the gate
        # can fail", so the verdict is INCONCLUSIVE and the node stays a violation until the
        # negative is fixed or the budget is raised - the honest third state beside
        # PROVEN/VACUOUS, and the one kbench's two-sided check has no answer for.
        verdict = "INCONCLUSIVE"
    else:
        verdict = "PROVEN" if nproc.returncode != 0 else "VACUOUS"
    proof = {"at": TODAY, "at_iso": NOW_UTC, "commit": commit,
             "gate": gate, "gate_exit": proc.returncode, "gate_kind": kind,
             "negative": negative, "negative_exit": nproc.returncode,
             "negative_kind": nkind, "verdict": verdict}
    if nkind == "timeout":
        proof["negative_timeout_s"] = round(budget, 1)
    if gate_rec:
        proof["gate_receipt"] = gate_rec["coord"]
    if neg_rec:
        proof["negative_receipt"] = neg_rec["coord"]
    node["gate_proof"] = proof
    plan["nodes"] = [node if n.get("id") == node_id else n for n in plan["nodes"]]
    plan.setdefault("actions", []).append(
        {"node": node_id, "action": f"prove-gate {gate}", "outcome": verdict.lower(),
         "outcome_kind": ("pass" if verdict == "PROVEN"
                          else "timeout" if verdict == "INCONCLUSIVE" else "fail"),
         "at": TODAY})
    save(path, plan)
    for key in ("gate_receipt", "negative_receipt"):
        if proof.get(key):
            print(f"  receipt {proof[key]}")
    if verdict == "PROVEN":
        print(f"  PROVEN {node_id}: the gate passes and {negative!r} fails, so this gate can "
              f"tell a pass from a nothing")
        return 0
    if verdict == "INCONCLUSIVE":
        print(f"  INCONCLUSIVE {node_id}: the negative {negative!r} was cut off at {budget:g}s, "
              f"so it neither passed nor failed - nothing is proven; raise --gate-timeout or "
              f"fix the negative, then re-run --prove-gate {node_id}", file=sys.stderr)
        return 2
    print(f"  VACUOUS {node_id}: the negative {negative!r} also exited 0 - this gate passes on "
          f"a folder nobody touched. Nothing is recorded as proven; plan_check reports the node "
          f"as a violation until the negative is fixed")
    return 1


def log_action(path, node_id, action, outcome, outcome_kind=""):
    plan, _ = load(path)
    entry = {"node": node_id, "action": action, "outcome": outcome, "at": TODAY}
    if outcome_kind:
        entry["outcome_kind"] = outcome_kind
    plan.setdefault("actions", []).append(entry)
    save(path, plan)
    print(f"logged: {node_id or '-'} {action} -> {outcome}"
          + (f" [{outcome_kind}]" if outcome_kind else ""))
    problems, _, _, _, _ = validate(path)
    for p in problems:
        if p.startswith("loop:"):
            print(f"  VIOLATION {p}", file=sys.stderr)
    return 1 if any(p.startswith("loop:") for p in problems) else 0


# ---- --claims: does a closed node's prose bind to an artifact, or restate itself? ----

SCORE_RE = re.compile(r"\b(\d+)/(\d+)\b")
NUM_RE = re.compile(r"\b\d+(?:\.\d+)?\b")
CITED_RE = re.compile(r"[\w.~/-]+\.(?:md|py|json|jsonl|log|txt|csv|tsv|html|db|sh|js|mjs)"
                      r"(?::L?\d+(?:-L?\d+)?)?")   # the spine's own file:line@sha receipt form
CLAIM_GRADES = ("SUPPORTED", "DERIVED", "UNANCHORED", "UNCHECKABLE")
CORPUS_CAP_BYTES = 4 * 1024 * 1024   # a 22.8 GB transcript is not a corpus, it is an incident


def _read_text(where):
    try:
        if where.is_file() and where.stat().st_size <= CORPUS_CAP_BYTES:
            return where.read_text(encoding="utf-8", errors="replace")
    except OSError:
        pass
    return ""


def claim_corpus(path, extra=()):
    """Every artifact outside a node's own prose that a stated number could bind to.

    A closed node's evidence is a claim about a run; the run's record is what can support it.
    So the corpus is the plan's own recorded gate outputs, the spine's loop ledger, the
    archived gate receipts, the grounded-claim store, every file the claim cites by path, and
    anything named with --claims-corpus. Prose quoting prose is deliberately absent: that is
    how a fabricated tally survives a review - the amber_ascii spine carried "spine harness
    125/125" and "gate_exec wired into both gates" in EVENTS.md with no such file on disk and
    a ledger that said 109/109 (2026-11-13, found by spine_check's DANGLING path scan).

    Returns [(label, text)]. Labels name the artifact so a reader can go and look.
    """
    texts = []
    _, nodes = load(path)          # load() normalizes the list-of-id-keyed-objects dialect
    for nid, node in sorted(nodes.items()):
        if not isinstance(node, dict):
            continue
        rows = [node.get("gate_result") or {}] + [a for a in (node.get("gate_attempts") or [])
                                                  if isinstance(a, dict)]
        for rec in rows:
            if rec.get("output"):
                texts.append((f"{nid}.gate_result", str(rec["output"])))
    # A project's gate receipts hold the stdout its evidence quotes; they are artifacts, not
    # prose, and a number that appears only there is still bound to a run.
    for rec in sorted((path.parent / "archive" / "gates").glob("*.log"))[:200]:
        body = _read_text(rec)
        if body:
            texts.append((f"receipt:{rec.name}", body))
    ledger = AGENT / "run" / "harness_loop.log"
    if _read_text(ledger):
        texts.append(("run/harness_loop.log", _read_text(ledger)))
    try:
        rdir = fact_store.archive_dir_for(fact_store.DEFAULT_DB, None)
    except Exception:
        rdir = None
    if rdir and pathlib.Path(rdir).is_dir():
        for rec in sorted(pathlib.Path(rdir).glob("*.json"))[:200]:
            body = _read_text(rec)
            if body:
                texts.append((f"receipt:{rec.name}", body))
    if _read_text(pathlib.Path(fact_store.DEFAULT_DB)):
        texts.append(("fact_store", _read_text(pathlib.Path(fact_store.DEFAULT_DB))))
    for name in extra or ():
        where = pathlib.Path(name).expanduser()
        body = _read_text(where)
        if not body and where.is_dir():
            for child in sorted(where.glob("**/*"))[:200]:
                cb = _read_text(child)
                if cb:
                    texts.append((f"corpus:{child}", cb))
        elif body:
            texts.append((f"corpus:{where}", body))
    return texts


def _cited_files(line, plan_dir):
    """Files a claim names by path, read if they exist under the plan root or the spine."""
    out = []
    for raw in CITED_RE.findall(line):
        stem = raw.split(":")[0]
        for cand in (pathlib.Path(stem).expanduser(), plan_dir / stem, AGENT / stem):
            body = _read_text(cand)
            if body:
                out.append((f"cited:{cand}", body))
                break
    return out


def _has_token(text, tok):
    """Whole-number match: 109 is in "109/109" and in "score 109", never inside 1098."""
    return re.search(rf"(?<![\w.]){re.escape(tok)}(?![\w])", text) is not None


def grade_claim(line, corpus):
    """(grade, detail) for one claim line, against a corpus of artifacts.

    SUPPORTED  every number on the line appears verbatim in some artifact, and every score pair
               (a "145/145") appears in one same artifact
    DERIVED    the numbers are not verbatim, but a score a/b equals the sum of the score
               rows in one artifact - an aggregate restated correctly
    UNANCHORED a number is in no artifact and is not such an aggregate: the claim came from
               somewhere that is not evidence
    UNCHECKABLE the line states no number, or there is no corpus to bind it to
    """
    nums = list(dict.fromkeys(NUM_RE.findall(line)))
    if not nums:
        return "UNCHECKABLE", "states no number"
    if not corpus:
        return "UNCHECKABLE", "no corpus to bind to"
    misses, binds, paired = [], [], set()
    # A score pair binds to ONE artifact or not at all. "7 checks 0 failures" must not be
    # SUPPORTED by a ledger that happens to hold a 7 on one line and a 0 on another - the spine's
    # loop log contains every small number, so a loose bind grades a fabricated tally as proven.
    # The pair has to sit together in the artifact that supposedly recorded it.
    for a, b in dict.fromkeys(SCORE_RE.findall(line)):
        hit = next((label for label, text in corpus
                    if _has_token(text, a) and _has_token(text, b)), None)
        if hit is None:
            misses += [t for t in (a, b) if t not in misses]
        else:
            binds.append(f"{a}/{b}->{hit}")
            paired |= {a, b}
    for tok in nums:
        if tok in paired:
            continue
        where = [label for label, text in corpus if _has_token(text, tok)]
        if where:
            binds.append(f"{tok}->{where[0]}")
        else:
            misses.append(tok)
    if not misses:
        return "SUPPORTED", ", ".join(binds[:4])
    # An aggregate is a legitimate derivation: 145/145 + 126/126 is one honest 271/271.
    derived = []
    for a, b in SCORE_RE.findall(line):
        if a not in misses and b not in misses:
            continue
        hit = None
        for label, text in corpus:
            rows = SCORE_RE.findall(text)
            if len(rows) >= 2 and (sum(int(x) for x, _ in rows),
                                   sum(int(y) for _, y in rows)) == (int(a), int(b)):
                hit = label
                break
        if hit is None:
            totals = [(int(x), int(y)) for _, t in corpus for x, y in SCORE_RE.findall(t)]
            if len(totals) >= 2 and (sum(x for x, _ in totals), sum(y for _, y in totals)) == \
                    (int(a), int(b)):
                hit = "corpus total"
        if hit:
            derived.append(f"{a}/{b}=sum({hit})")
            misses = [m for m in misses if m not in (a, b)]
    if not misses:
        return "DERIVED", ", ".join(derived + binds[:2])
    return "UNANCHORED", ("no artifact restates " + ", ".join(misses) + " | binds: "
                          + (", ".join(binds[:3]) or "none"))


def check_claims(path, extra=()):
    """Grade every number a closed node states. Returns (rows, problems)."""
    _, nodes = load(path)
    corpus = claim_corpus(path, extra)
    rows, problems = [], []
    for nid, node in sorted(nodes.items()):
        if not isinstance(node, dict):
            continue
        if STATUS.get(str(node.get("status") or "").strip().lower(),
                      "open") != "done" and node.get("closed") is not True:
            continue
        lines = []
        for key in ("evidence", "verification", "deliverable", "note", "notes"):
            val = node.get(key)
            if isinstance(val, str):
                lines += [val]
            elif isinstance(val, list):
                lines += [str(x) for x in val]
        for art in ([node.get("artifact")] if isinstance(node.get("artifact"), dict) else []):
            for key in ("report", "path", "note", "headline"):
                if isinstance(art.get(key), str):
                    lines.append(art[key])
        for line in [ln for ln in lines if ln and ln.strip()]:
            grade, detail = grade_claim(line, corpus + _cited_files(line, path.parent))
            rows.append((nid, grade, detail, line))
            if grade == "UNANCHORED":
                problems.append(f"{nid}: {detail} (claim: {line[:90]})")
    return rows, problems


def print_claims(path, extra=(), verbose=True):
    rows, problems = check_claims(path, extra)
    counts = {g: 0 for g in CLAIM_GRADES}
    for _, grade, _, _ in rows:
        counts[grade] += 1
    print(f"== claims {path.parent.name} ==")
    if verbose:
        for nid, grade, detail, line in rows:
            print(f"  {nid:<12} {grade:<11} {detail}")
    print("  claims: " + ", ".join(f"{counts[g]} {g.lower()}" for g in CLAIM_GRADES)
          + (f"; problems: {len(problems)}" if problems else ""))
    for pr in problems:
        print(f"  UNANCHORED {pr}")
    return rows, problems


def main():
    ap = argparse.ArgumentParser(description="OMP vNext plan validator")
    ap.add_argument("plans", nargs="*", help="DAG.json files (default: every project root's)")
    ap.add_argument("--frontier", action="store_true", help="print the frontier only")
    ap.add_argument("--explain", action="store_true",
                    help="per ready node: the action to take, what its gate proves, what a pass "
                         "writes back and where its receipt lands (with --frontier or a "
                         "full validate)")
    ap.add_argument("--run-gate", metavar="ID", help="run a node's gate and record the result")
    ap.add_argument("--pin", metavar="ID", help="pin a node's artifact bytes (sha256)")
    ap.add_argument("--read-only", action="store_true",
                    help="with --run-gate: the gate writes nothing but evidence "
                         "beside the artifact; stamp its rerun_at, leave its at alone")
    ap.add_argument("--log", nargs=2, metavar=("ACTION", "OUTCOME"),
                    help="append an action+outcome record")
    ap.add_argument("--node", default="", help="node for --log")
    ap.add_argument("--prove-gate", metavar="ID",
                    help="run that node's gate AND its gate_negative: the gate must exit 0 and "
                         "the negative must exit nonzero, else the node is refused as vacuous; "
                         "records gate_proof")
    ap.add_argument("--repin", metavar="ID1[,ID2...]",
                    help="the authorized break of a broken freeze: re-execute each named node's "
                         "gate (must exit 0) and its gate_negative (must exit nonzero), re-pin "
                         "every named artifact from the bytes the gate left, and append a "
                         "repin_audit entry. Required with --why; refuses (exit 2) when --why "
                         "is empty or any unnamed node carries a violation")
    ap.add_argument("--why", default="", metavar="REASON",
                    help="with --repin: why these freezes are being broken; recorded verbatim "
                         "in the plan's repin_audit")
    ap.add_argument("--outcome-kind", default="", choices=sorted(gate_exec.OUTCOME_KINDS),
                    help="with --log: which of the five outcome kinds this action produced; "
                         "unset means a human labelled it and no machine kind was recorded")
    ap.add_argument("--plan", default="",
                    help="DAG.json for --run-gate / --prove-gate / --repin / --log")
    ap.add_argument("--claims", action="store_true",
                    help="grade the numbers a closed node states against the artifacts that "
                         "could support them: supported, derived (an honest aggregate), "
                         "unanchored (in no artifact), unchecked (no number to bind); "
                         "exit 3 when anything is unanchored")
    ap.add_argument("--claims-corpus", action="append", default=[], metavar="PATH",
                    help="extra file or directory --claims may bind numbers to (repeatable)")
    ap.add_argument("--gate-timeout", type=float, default=None, metavar="SECONDS",
                    help="with --run-gate/--prove-gate: wall-clock budget for the gate "
                         f"(default {gate_exec.GATE_TIMEOUT_S}); the row records it as timeout_s")
    ap.add_argument("--env", action="append", default=[], dest="env_extra", metavar="KEY=VAL",
                    help="with --run-gate: extra environment for the gate (repeatable)")
    ap.add_argument("--max-fsize-mb", default=None,
                    help="with --run-gate: RLIMIT_FSIZE for the gate in MiB, 0 for none")
    ap.add_argument("--user", default="",
                    help="with --run-gate: run the gate as NAME or UID")
    ap.add_argument("--dry-run", action="store_true",
                    help="with --run-gate: print the command, the budget, the env and the uid, "
                         "run nothing, write nothing")
    args = ap.parse_args()

    # A mutation mode names its plan explicitly; it never silently falls back to a
    # default plan, which would record a gate result in the wrong project.
    if args.run_gate or args.prove_gate or args.log or args.pin or args.repin:
        chosen = [pathlib.Path(args.plan)] if args.plan else [pathlib.Path(x) for x in args.plans]
        if len(chosen) != 1:
            print("give exactly one DAG.json with --run-gate/--prove-gate/--repin/--log",
                  file=sys.stderr)
            return 1
        target = chosen[0]
        if not target.is_file():
            print(f"no plan at {target}", file=sys.stderr)
            return 1
        # load() raises ValueError for a plan it cannot read (a governance-tooling artifact,
        # a stray JSON typed into --plan). Name the refusal; do not traceback through it.
        try:
            if args.pin:
                return pin_artifact(target, args.pin)
            if args.run_gate:
                return run_gate(target, args.run_gate, read_only=args.read_only,
                                timeout=args.gate_timeout, env_extra=tuple(args.env_extra),
                                fsize_mb=args.max_fsize_mb, user=args.user,
                                dry_run=args.dry_run)
            if args.prove_gate:
                return prove_gate(target, args.prove_gate, timeout=args.gate_timeout,
                                  env_extra=tuple(args.env_extra), fsize_mb=args.max_fsize_mb,
                                  user=args.user)
            if args.repin:
                return repin_nodes(target, args.repin.split(","), args.why,
                                   timeout=args.gate_timeout, env_extra=tuple(args.env_extra),
                                   fsize_mb=args.max_fsize_mb, user=args.user)
            return log_action(target, args.node, args.log[0], args.log[1],
                              outcome_kind=args.outcome_kind)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1

    if args.claims:
        # A read-only report, like --frontier: every plan asked for, or every project's.
        chosen = ([pathlib.Path(args.plan)] if args.plan
                  else [pathlib.Path(x) for x in args.plans] or default_plans())
        bad = 0
        for target in chosen:
            if not target.is_file():
                print(f"  MISSING   {target}")
                bad += 1
                continue
            try:
                _, problems = print_claims(target, extra=args.claims_corpus,
                                       verbose=args.explain or not args.frontier)
            except ValueError as exc:
                # the read-only report keeps going: one unreadable plan is one problem line
                print(f"  {target}: {exc}")
                problems = [f"{target}: {exc}"]
            bad += len(problems)
        return 3 if bad else 0

    # --plan scopes a report to one plan, exactly as it scopes every mutation mode: an operator
    # asking whether one project's node is a violation must not be answered with another
    # project's NOTEs, and a scan-all report cannot be pointed at the plan under review.
    named = [pathlib.Path(args.plan)] if args.plan else []
    targets = named or [pathlib.Path(p) for p in args.plans] or default_plans()
    if not named and not args.plans:
        for skipped in skipped_plans():
            print(f"  NOTE      {skipped}: not a plan this validator reads (nodes are not"
                  f" id-keyed objects), so its frontier is not reported here")
    if not targets:
        print("no DAG.json in any project root", file=sys.stderr)
        return 1
    violations = 0
    for target in targets:
        if not target.is_file():
            print(f"  MISSING   {target}")
            violations += 1
            continue
        try:
            problems, notes, summary, _, _ = validate(target)
        except (ValueError, KeyError, json.JSONDecodeError) as exc:
            print(f"  VIOLATION {target}: unreadable plan ({exc})")
            violations += 1
            continue
        if args.frontier:
            print_frontier(target, summary, explain=args.explain)
            # Printed after the frontier, so `--frontier | head -1` still gets the frontier.
            # A session that follows the sieve's pointer runs this command and nothing else,
            # so this is the only place staleness can reach it: a green frontier over a closed
            # node whose gate has gone stale against the moving tree is the state where the
            # frontier recommends work the corpus already did (LOOM 2026-11-09, nine nodes).
            stale = [n.split()[1].rstrip(":") for n in notes if n.startswith("STALE ")]
            if stale:
                print("STALE:" + " ".join(stale))
            continue
        print(f"== plan {target.parent.name} ==")
        for p in problems:
            print(f"  VIOLATION {p}")
        for n in notes:
            print(f"  {n}")
        if not problems:
            print("  graph ok: no cycles, every closed node carries evidence, "
                  "every declared gate was run and passed")
        print_frontier(target, summary, explain=args.explain)
        violations += len(problems)
    return 1 if violations else 0


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
