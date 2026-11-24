#!/usr/bin/env python3
"""impact_analysis.py - what a change reaches, and the smallest gate set that proves it.

Why this exists: the spine's --run-harness runs every runnable script in the corpus on
every change. Most of that is unrelated to the file that moved, so the honest answer to
"what must I re-run?" costs 128 s of waiting or a guess. This builds the reference graph
the spine already implies and answers the question from the graph.

Its gate: harness/impact_harness.py.

Modes
  impact_analysis.py PATH [PATH...] [--roots R ...] [--json] [--timings F] [--baseline S]
  impact_analysis.py ... --stats                    # add the graph cost line
  impact_analysis.py --git [--roots R ...]        # git's own modified set, per root
  A PATH may be a file (any path shape: absolute, root-relative, or a bare basename that
  is unique inside a root) or a directory, which expands to every corpus file under it.

Output, in the order a caller acts on it
  MODIFIED          the resolved inputs
  REQUIRED_GATES    runnable verification scripts that reach a modified file - run these
  IMPACTED_TOOLS    non-gate scripts that reach one - re-read or re-run by hand
  CITING_RECORDS    text that names a modified file - prose, or a table like
                    harness/timings.json, that may now be stale
  LEDGER_MENTIONS   append-only ledgers that name one - counted, never listed to update
  SUMMARY           selected vs total gate count; with a timing table, the estimated
                    seconds, the saving, and how many of those gates the table prices
  STATS (--stats)   graph edges, files, elapsed ms, unresolved/ambiguous/unparsable counts.
                    Off by default: elapsed varies run to run, and the default text output
                    is meant to be diffed against another run of the same corpus.

Semantics
  * An edge X -> Y means "X refers to Y", so a change to Y reaches X. Edges come from a
    python import (parsed with ast), a js/mjs require/import specifier, any path-shaped
    token in any text file (string, comment, markdown sentence), and a DAG.json node's
    gate command or artifacts.
  * A token resolves only to a file that exists inside a --root. Unresolved tokens are
    dropped, never guessed. A basename that matches two files in one root is dropped as
    ambiguous: a false edge costs a needless 30 s gate, a wrong one costs trust.
  * Forward chaining walks code only: a python/js/shell file or a DAG.json. A markdown
    note, a .txt artifact or an archived copy is a terminal - it is reported as a citing
    record, but the chain never passes through it. Without that rule one note citing
    another note forwards the closure into everything: a query_expand.py edit reached
    16 gates through six notes nobody re-runs a gate for. Cycles terminate.
  * Scratch is never an answer: a file under out_*/ or archive/ is a throwaway probe, a
    gate's own output or a retired copy, so it is dropped from every bucket. A probe that
    imports the changed file is not a consumer anyone has to re-run.
  * A basename that matches both a live file and an archived copy resolves to the live
    one: `spine_check.py` inside a live script means the live script, not the retired copy.
  * A DAG.json is a statement about what to run, not just another file: editing the plan,
    or editing evidence a plan node names, selects that node's declared gates - even a
    gate no import reaches, such as a shell wrapper the plan invokes. Only a gate command
    declares; an artifact is a reference, so it marks the plan stale without selecting.
  * A script is a gate if it lives under harness/, or its name says what it verifies
    (harness|verify|selftest|test_|_test|check_|_check), or a DAG.json gate command names
    it. Everything else that is reached is a tool, not a gate.
  * Corpus = every text file under --roots with query_expand.py's skip rules (sessions/,
    cache/, build*/, run/ artifacts, node_modules, binaries, files over 2 MB). The scan is
    wider than harness/ and tools/ on purpose: an engine source is a node too, or the
    chain from source to harness has nowhere to pass through. With no --roots the default
    is the spine's project registry (spine_check.ROOTS through query_expand.DEFAULT_ROOTS,
    FT-48/R3), so this walker sees the same directories as every other. No second list.

Exit codes
  0  ok, including nothing impacted; a bare run prints usage
  2  usage: unknown flag, a named path that cannot be read or resolves to nothing, --git
     with no git working tree among --roots, or a root set that yields no readable file
"""
import argparse
import ast
import json
import os
import pathlib
import re
import subprocess
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from query_expand import (DEFAULT_ROOTS, TEXT_EXT, list_files, read_text)  # noqa: E402

EXIT_USAGE = 2
# Append-only ledgers name everything they ever discussed; they are records, not documents
# to keep in step with the code. spine_check.py calls the same pair HISTORICAL.
LEDGERS = {"EVENTS.md", "FORGE_FAST_CHANGELOG.md"}
SPINE_DOCS = {"AGENTS.md", "CORE.md", "STATE.md", "PERM.md"}
PATH_TOKEN_RE = re.compile(
    r"[A-Za-z0-9_.\-/]*[A-Za-z0-9_\-]\.(?:py|js|mjs|cjs|ts|json|jsonl|md|sh|yml|yaml|html|css|toml)")
TEMPLATE_RE = re.compile(r"[*${}\[\]|]")
GATE_NAME_RE = re.compile(r"harness|verify|selftest|test_|_test|check_|_check", re.I)
LIST_CAP = 12
# Only these node kinds carry the chain onward. Everything else is a record about a file,
# not a consumer of it: docs, notes, artifacts, configs, and anything under archive/ or an
# out_*/runs artifact directory (a retired copy is not a live gate).
CODE_EXT = {".py", ".js", ".mjs", ".cjs", ".ts", ".sh"}
ARTIFACT_DIR_RE = re.compile(r"^out(?:_|$)|^runs?$")
USAGE = __doc__.split("Exit codes")[0].rstrip()


class ImpactError(Exception):
    pass


# ---------------------------------------------------------------- corpus and resolution

class Corpus:
    """Files under the roots, plus the index that turns a token into a file."""

    def __init__(self, roots):
        self.roots = [r.resolve() for r in roots]
        self.files = []
        self.by_name = {}
        for root in self.roots:
            found = list_files([root])
            self.files.extend(found)
            for path in found:
                self.by_name.setdefault(path.name, set()).add(path)
        self.files = sorted(set(self.files))
        self.fileset = set(self.files)
        if not self.files:
            raise ImpactError("no readable text file under --roots")
        self.unresolved = 0
        self.ambiguous = 0

    def root_of(self, path):
        for root in self.roots:
            try:
                path.relative_to(root)
                return root
            except ValueError:
                continue
        return None

    def rel(self, path):
        root = self.root_of(path)
        return str(path.relative_to(root)) if root else str(path)

    def resolve_token(self, token, here):
        """A path-shaped token inside file `here` -> the corpus file it names, or None."""
        if TEMPLATE_RE.search(token) or token.startswith("~"):
            return None
        token = token.lstrip("./") if token.startswith("./") else token
        if token.startswith("/"):
            cand = pathlib.Path(token)
            return cand if cand in self.fileset else None
        root = self.root_of(here) or (self.roots[0] if self.roots else None)
        tried = []
        if "/" in token:
            tried = [here.parent / token]
            if root is not None:
                tried.append(root / token)
        else:
            # An archived copy of a script is not what a live file means by `spine_check.py`:
            # drop scratch candidates first, then judge ambiguity on what is left.
            matches = sorted(c for c in self.by_name.get(token, ()) if not is_scratch(c))
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                self.ambiguous += 1
                return None
            return None
        for cand in tried:
            cand = cand.resolve()
            if cand in self.fileset:
                return cand
        self.unresolved += 1
        return None

    def resolve_input(self, spec):
        """A caller's path -> corpus files. A directory expands; a basename must be unique."""
        direct = pathlib.Path(spec)
        if direct.is_dir():
            inside = [f for f in self.files if _is_under(f, direct.resolve())]
            if not inside:
                raise ImpactError(f"{spec}: no corpus file under this directory")
            return inside
        cwd_hit = None
        if direct.is_file():
            path = direct.resolve()
            if path in self.fileset:
                return [path]
            # A same-named file in the cwd that isn't in the corpus must not shadow the
            # corpus hit: keep searching, and only report the shadow if nothing matches.
            cwd_hit = path
        # root-relative or bare basename
        found = []
        for root in self.roots:
            cand = (root / spec)
            if cand.is_dir():
                found.extend(f for f in self.files if _is_under(f, cand.resolve()))
            elif cand.exists() and cand.resolve() in self.fileset:
                found.append(cand.resolve())
        if found:
            return sorted(set(found))
        matches = sorted(self.by_name.get(pathlib.Path(spec).name, ()))
        if len(matches) == 1:
            return matches
        if len(matches) > 1:
            raise ImpactError(f"{spec}: basename matches {len(matches)} files: "
                              f"{', '.join(self.rel(m) for m in matches[:LIST_CAP])}")
        if cwd_hit is not None:
            raise ImpactError(f"{spec}: {cwd_hit} exists but is outside --roots and no "
                              f"corpus file shares that name")
        raise ImpactError(f"{spec}: cannot be read under --roots")


def is_scratch(path):
    """A throwaway probe, a gate's own output, or a retired copy: never a consumer, never
    a record anyone has to update."""
    return any(part == "archive" or ARTIFACT_DIR_RE.match(part) for part in path.parts)


def is_traversal(path):
    """Can the chain carry on through this file? Code and DAG.json yes; docs and notes no.

    A note that cites a script is evidence the note may be stale, not a consumer of the
    script: letting one note forward to the next is what dragged 16 gates into a
    query_expand.py edit through six unrelated notes.
    """
    if path.suffix.lower() not in CODE_EXT and path.name != "DAG.json":
        return False
    return not is_scratch(path)


def _is_under(path, directory):
    try:
        path.relative_to(directory)
        return True
    except ValueError:
        return False


# ---------------------------------------------------------------- edge extraction

def python_edges(path, text):
    """ast-parsed imports -> corpus files."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return None  # caller falls back to token scan and reports the file
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                out.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = path.parent
                for _ in range(node.level - 1):
                    base = base.parent
                mods = [(base / (node.module or "")).as_posix()]
                if not node.module:
                    mods = [base / a.name for a in node.names]
            else:
                mods = [(node.module or "")]
                for alias in node.names:
                    mods.append(f"{node.module}.{alias.name}" if node.module else alias.name)
            for mod in mods:
                if isinstance(mod, pathlib.Path):
                    out.append(mod.as_posix())
                else:
                    out.append(mod.replace(".", "/"))
    return out


def js_edges(path, text):
    """Relative require()/import specifiers -> corpus files."""
    out = []
    for spec in re.findall(r"""require\(\s*['"]([^'"]+)['"]\s*\)""", text):
        out.append(spec)
    for spec in re.findall(r"""(?:import|export)[^'"]*?from\s*['"]([^'"]+)['"]""", text):
        out.append(spec)
    return [s for s in out if s.startswith(".")]


def module_candidates(token, here, corpus):
    """A dotted/slash module token -> existing corpus files (package or module form)."""
    root = corpus.root_of(here) or here.parent
    base = token.strip("/").replace(".", "/")
    cands = []
    for start in (here.parent, root):
        for suffix in ("", "/__init__"):
            for ext in (".py",):
                cands.append((start / (base + suffix + ext)).resolve())
    hits = [c for c in dict.fromkeys(cands) if c in corpus.fileset]
    return hits


def dag_edges(path, text):
    """DAG.json -> (text to link, text that names a gate to run). Not the prose.

    A node's artifacts are references worth linking - editing one makes the node's evidence
    stale - but only a gate command is a statement that a script must be run.
    """
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return "", ""
    bits, gate_bits = [], []
    for node in data.get("nodes", []) if isinstance(data, dict) else []:
        if not isinstance(node, dict):
            continue
        gate = node.get("gate")
        if isinstance(gate, str):
            bits.append(gate)
            gate_bits.append(gate)
        artifacts = node.get("artifacts")
        if isinstance(artifacts, str):
            bits.append(artifacts)
        elif isinstance(artifacts, list):
            bits.extend(str(a) for a in artifacts)
    return "\n".join(bits), "\n".join(gate_bits)


def build_graph(corpus):
    """Corpus -> (deps, dependents, declared, stats). deps[X] = files X refers to.

    declared[DAG.json] = the runnable scripts that plan's gate commands name.
    """
    deps = {}
    declared = {}
    unparsable = []
    for path in corpus.files:
        text = read_text(path)
        if text is None:
            continue
        edges = set()
        if path.suffix == ".py":
            imports = python_edges(path, text)
            if imports is None:
                unparsable.append(corpus.rel(path))
            else:
                for token in imports:
                    if token == "__future__":
                        continue
                    for hit in module_candidates(token, path, corpus):
                        if hit != path:
                            edges.add(hit)
        elif path.suffix in (".js", ".mjs", ".cjs"):
            for spec in js_edges(path, text):
                cand = (path.parent / spec).resolve()
                for alt in (cand, cand.with_suffix(".js"), cand.with_suffix(".mjs"),
                            cand / "index.js"):
                    if alt in corpus.fileset and alt != path:
                        edges.add(alt)
        if path.name == "DAG.json":
            scan, gate_scan = dag_edges(path, text)
        else:
            scan = gate_scan = text
        gate_tokens = set(PATH_TOKEN_RE.findall(gate_scan)) if path.name == "DAG.json" else set()
        for token in PATH_TOKEN_RE.findall(scan):
            hit = corpus.resolve_token(token, path)
            if hit is not None and hit != path:
                edges.add(hit)
                if token in gate_tokens and hit.suffix in CODE_EXT:
                    declared.setdefault(path, set()).add(hit)
        if edges:
            deps[path] = edges
    dependents = {}
    for src, targets in deps.items():
        for t in targets:
            dependents.setdefault(t, set()).add(src)
    declared = {k: sorted(v) for k, v in declared.items()}
    stats = {"files": len(corpus.files), "edges": sum(len(v) for v in deps.values()),
             "unresolved_tokens": corpus.unresolved, "ambiguous_tokens": corpus.ambiguous,
             "unparsable": unparsable}
    return deps, dependents, declared, stats


# ---------------------------------------------------------------- classification

def _is_gate_name(path):
    return bool(GATE_NAME_RE.search(path.name))


def is_gate(path, gates):
    return path in gates or _is_gate_name(path) or "harness" in path.parts


def gate_universe(corpus, gates):
    """Every live gate script in the corpus - the denominator for the savings claim."""
    return sorted(p for p in corpus.files
                  if is_traversal(p) and is_gate(p, gates) and p.suffix != ".json")


def chain_from(parent, node):
    """Walk the BFS parent map back to the modified file -> the derivation chain."""
    chain = [node]
    while chain[0] in parent:
        chain.insert(0, parent[chain[0]])
    return chain


def forward_closure(modified, dependents):
    """Every file that reaches a modified one, with the shortest chain to each.

    Records (docs, notes) are collected but never expanded, so the chain stays inside the
    code that actually consumes the change. The modified set is the exception: a changed
    spine doc has to reach the scripts that read it, so seeds expand whatever they are.
    """
    parent = {}
    seen = set(modified)
    frontier = list(modified)
    while frontier:
        nxt = []
        for target in frontier:
            for src in sorted(dependents.get(target, ())):
                if src in seen:
                    continue
                seen.add(src)
                parent[src] = target
                if is_traversal(src):
                    nxt.append(src)
        frontier = nxt
    return {n: chain_from(parent, n) for n in seen if n not in modified}


# ---------------------------------------------------------------- git

def git_modified(roots):
    """--git: modified + deleted + untracked paths per root, with their git status."""
    out = []
    found = False
    for root in roots:
        proc = subprocess.run(["git", "-C", str(root), "status", "--porcelain"],
                              capture_output=True, text=True)
        if proc.returncode:
            continue
        found = True
        for line in proc.stdout.splitlines():
            if len(line) < 4:
                continue
            status, rel = line[:2], line[3:].split(" -> ")[-1].strip('"')
            path = (root / rel).resolve()
            if path.suffix.lower() in TEXT_EXT:
                out.append((path, status))
    if not found:
        raise ImpactError("--git: no git working tree among --roots")
    return out


# ---------------------------------------------------------------- timing

def load_timings(ref):
    try:
        raw = pathlib.Path(ref).read_text(encoding="utf-8")
    except OSError as exc:
        raise ImpactError(f"--timings {ref}: {exc.strerror or exc}")
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise ImpactError(f"--timings {ref}: not JSON ({exc})")
    if not isinstance(data, dict):
        raise ImpactError(f'--timings {ref}: expected a JSON object of path -> seconds')
    absolute, relative = {}, {}
    for key, value in data.items():
        if key.startswith("_"):
            continue  # a stamp or a note about the table, not a script
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ImpactError(f"--timings {ref}: {key} is not a number")
        if os.path.isabs(key):
            absolute[str(pathlib.Path(key).resolve())] = float(value)
        else:
            # A committed table names scripts relative to a root so it stays true on another
            # box; it resolves against --roots, never against the cwd.
            relative[str(pathlib.PurePosixPath(key))] = float(value)
    return absolute, relative


# ---------------------------------------------------------------- main

def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        print(USAGE)
        return 0
    ap = argparse.ArgumentParser(prog="impact_analysis.py", add_help=True,
                                 description="forward dependency closure + minimal gate set")
    ap.add_argument("paths", nargs="*", help="modified file(s) or director(ies)")
    ap.add_argument("--roots", nargs="+", default=list(DEFAULT_ROOTS),
                    help="corpus roots (default: the spine's)")
    ap.add_argument("--git", action="store_true",
                    help="take the modified set from git status in each root")
    ap.add_argument("--timings", metavar="FILE",
                    help="JSON map of script -> measured seconds (absolute, or relative to a "
                         "--root), for the savings estimate")
    ap.add_argument("--baseline", type=float, metavar="S",
                    help="full-run seconds (default: the sum of the timing table)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--stats", action="store_true",
                    help="print the graph cost line (it varies run to run)")
    args = ap.parse_args(argv)

    if not args.paths and not args.git:
        print(USAGE)
        return 0
    try:
        roots = []
        for ref in args.roots:
            root = pathlib.Path(ref).expanduser()
            if not root.exists():
                raise ImpactError(f"--roots {ref}: cannot be read")
            roots.append(root)
        corpus = Corpus(roots)
        started = time.monotonic()
        deps, dependents, declared, stats = build_graph(corpus)
        elapsed_ms = round((time.monotonic() - started) * 1000, 1)

        raw_inputs = []
        if args.git:
            for path, status in git_modified(roots):
                raw_inputs.append((str(path), status))
        for spec in args.paths:
            raw_inputs.append((spec, ""))
        if not raw_inputs:
            raise ImpactError("nothing modified: name a path or use --git")

        modified = []
        for spec, _status in raw_inputs:
            try:
                modified.extend(corpus.resolve_input(spec))
            except ImpactError:
                if args.git:
                    continue  # git lists files the corpus scan skips (binary, run/)
                raise
        modified = sorted(dict.fromkeys(modified))
        if not modified:
            raise ImpactError("no named path resolves to a corpus file")

        reached = forward_closure(modified, dependents)
        gates = {t for targets in declared.values() for t in targets}
        gate_rows, tool_rows, record_rows, ledger_rows = [], [], [], []
        for node, chain in reached.items():
            if is_scratch(node):
                continue  # a probe or a gate's own output is not something to re-run or update
            why = " -> ".join(corpus.rel(p) for p in chain)
            # Runnable code is a gate or a tool; everything else (docs, DAG.json, configs,
            # .gd, .html) is a record that may now be stale.
            if node.suffix in CODE_EXT and is_gate(node, gates):
                gate_rows.append({"script": str(node), "rel": corpus.rel(node), "via": why})
            elif node.suffix in CODE_EXT:
                tool_rows.append({"script": str(node), "rel": corpus.rel(node), "via": why})
            else:
                row = {"record": str(node), "rel": corpus.rel(node), "via": why}
                (ledger_rows if node.name in LEDGERS else record_rows).append(row)
        # A plan node is a statement that a script must be run. Editing the plan, or editing
        # evidence the plan names, makes its declared gates required - including the gates
        # no static import reaches, like a shell wrapper the plan invokes.
        seen_gates = {r["script"] for r in gate_rows}
        for dag in sorted(set(modified) | set(reached)):
            if dag not in declared:
                continue
            origin = (corpus.rel(dag) if dag in modified
                      else " -> ".join(corpus.rel(p) for p in reached[dag]))
            for target in declared[dag]:
                if is_scratch(target) or str(target) in seen_gates:
                    continue
                seen_gates.add(str(target))
                gate_rows.append({"script": str(target), "rel": corpus.rel(target),
                                  "via": f"{origin} declares it as a gate"})
        gate_rows.sort(key=lambda r: r["script"])
        tool_rows.sort(key=lambda r: r["script"])
        record_rows.sort(key=lambda r: r["record"])
        ledger_rows.sort(key=lambda r: r["record"])

        timings, rel_timings = (load_timings(args.timings) if args.timings
                                else ({}, {}))
        priced = bool(timings or rel_timings)

        def est_for(path):
            """Measured seconds: absolute key, then root-relative key, then unique name."""
            if str(path) in timings:
                return timings[str(path)]
            for root in corpus.roots:
                try:
                    rel = str(path.relative_to(root))
                except ValueError:
                    continue
                if rel in rel_timings:
                    return rel_timings[rel]
            named = [v for k, v in rel_timings.items()
                     if pathlib.PurePosixPath(k).name == path.name]
            return named[0] if len(named) == 1 else None

        for row in gate_rows:
            row["est_s"] = est_for(pathlib.Path(row["script"])) if priced else None
        baseline = args.baseline
        if baseline is None and priced:
            baseline = sum(timings.values()) + sum(rel_timings.values())
        # The table prices only the scripts it names, so the denominator has to say so: an
        # unpriced gate is real work the estimate never counted.
        universe = gate_universe(corpus, gates)
        priced_gates = (sum(1 for p in universe if est_for(p) is not None)
                        if priced else None)

        payload = {
            "modified": [corpus.rel(p) for p in modified],
            "modified_abs": [str(p) for p in modified],
            "required_gates": gate_rows,
            "impacted_tools": tool_rows,
            "citing_records": record_rows,
            "ledger_mentions": [r["rel"] for r in ledger_rows],
            "corpus": {"roots": [str(r) for r in corpus.roots], "files": stats["files"],
                       "edges": stats["edges"], "unresolved_tokens": stats["unresolved_tokens"],
                       "ambiguous_tokens": stats["ambiguous_tokens"],
                       "unparsable": stats["unparsable"], "elapsed_ms": elapsed_ms},
            "baseline_s": baseline,
            "priced_gates": priced_gates,
            "gate_universe": len(universe),
            "selected_est_s": (round(sum(r["est_s"] for r in gate_rows if r["est_s"] is not None), 1)
                               if priced else None),
        }
    except ImpactError as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return EXIT_USAGE

    if args.json:
        print(json.dumps(payload, indent=1, sort_keys=True))
        return 0

    print(f"MODIFIED {len(payload['modified'])}: " + ", ".join(payload["modified"]))
    print(f"REQUIRED_GATES {len(gate_rows)}:")
    for n, row in enumerate(gate_rows, 1):
        est = "" if row["est_s"] is None else f"  est {row['est_s']:.1f}s"
        print(f"  {n}. {row['script']}{est}\n       via: {row['via']}")
    print(f"IMPACTED_TOOLS {len(tool_rows)}:")
    for n, row in enumerate(tool_rows, 1):
        print(f"  {n}. {row['script']}\n       via: {row['via']}")
    print(f"CITING_RECORDS {len(record_rows)}:")
    for n, row in enumerate(record_rows[:LIST_CAP], 1):
        print(f"  {n}. {row['record']}\n       cites: {row['via']}")
    if len(record_rows) > LIST_CAP:
        print(f"  ... and {len(record_rows) - LIST_CAP} more (--json lists all)")
    if ledger_rows:
        print(f"LEDGER_MENTIONS {len(ledger_rows)}: "
              + ", ".join(r["rel"] for r in ledger_rows[:LIST_CAP]))
    total_gates = payload["gate_universe"]
    line = f"SUMMARY {len(gate_rows)} of {total_gates} gate scripts selected"
    priced = payload["priced_gates"]
    if priced is None:
        line += "; no timing table (--timings FILE)"
    elif not priced or not baseline:
        # A table measured on another corpus prices nothing in this one; quoting its total
        # would be a saving nobody measured.
        line += f"; 0 of {total_gates} gates priced by that table"
    else:
        est = payload["selected_est_s"]
        line += (f"; est {est:.1f}s vs {baseline:.1f}s priced "
                 f"({(1 - est / baseline) * 100:.1f}% saved)")
        if priced < total_gates:
            line += f"; {priced} of {total_gates} gates priced"
    print(line)
    if args.stats:
        print(f"STATS graph {stats['edges']} edges over {stats['files']} files in "
              f"{elapsed_ms} ms; unresolved {stats['unresolved_tokens']}, ambiguous "
              f"{stats['ambiguous_tokens']}, unparsable {len(stats['unparsable'])}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        # `impact_analysis.py ... | head -1` is a normal call; silence the flush at shutdown
        # instead of dumping a traceback on the caller.
        try:
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        except OSError:
            pass
        sys.exit(0)
