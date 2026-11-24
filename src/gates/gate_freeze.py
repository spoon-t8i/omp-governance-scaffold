#!/usr/bin/env python3
"""gate_freeze.py - freeze a closed loop's evidence so it outlives the loop.

A closed node's record names the artifacts that make it true. Nothing here
checked that those files still exist, or that they are the bytes measured, and
one harness leg already overwrote a demo receipt that was the only screen-level
evidence a closed node had. This tool is the governance-layer bound on that:
read-only, it resolves every path a closed node's `evidence` or `gate` names,
records SHA-256 + size + line count + the git blob that holds it, and writes one
manifest under run/freeze/<utc>/. A named path that does not exist is reported
and fails the run; a named artifact that git does not hold is reported as
untracked, which is the class that disappears without anyone deciding it should.

  python3 tools/gate_freeze.py                      # spine DAG.json + every Projects/*/DAG.json
  python3 tools/gate_freeze.py --dag <path> [--json] [--quiet]
Exit 0 = every named path resolved; 1 = a named path is missing.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

SPINE = Path(__file__).resolve().parent.parent
HOME = Path.home()
CLOSED = {"closed", "done", "executed"}  # terminal vocabularies in use across the spine
# a named artifact ends in a recording extension; a slash alone is not enough,
# "11/12" and "tick/palette/bloom" appear in evidence prose
EXT = re.compile(r"\.(log|json|jsonl|md|txt|py|mjs|js|html|csv|png|mp4|gbnf|svg|patch|diff)$")
SPLIT = re.compile(r"[\s|,;]+")


def candidate(token: str) -> str | None:
    """Pull a path out of one evidence token, dropping line/anchor suffixes."""
    t = token.strip().strip("()[]{}").strip()
    for pfx in ("receipt:", "receipt", "evidence:", "log:", "gate:"):
        if t.lower().startswith(pfx):
            t = t[len(pfx):].lstrip(" :").strip()
    t = re.split(r":L\d+", t, maxsplit=1)[0]
    t = re.split(r"@[0-9a-f]{6,}", t, maxsplit=1)[0]
    t = t.split("#L")[0].rstrip("./\\,;:").strip()
    if not t or t.startswith(("-", "--", "python3", "http", "$")) or "://" in t:
        return None
    if not EXT.search(t) or t.split("/")[-1].startswith("."):
        return None
    return t


def named_paths(node: dict) -> list[str]:
    out: list[str] = []
    for field in ("evidence", "gate", "outcome"):
        val = node.get(field)
        if not isinstance(val, str):
            continue
        for tok in SPLIT.split(val):
            p = candidate(tok)
            if p and p not in out:
                out.append(p)
    return out


def git_blobs(repo: Path) -> dict[str, str]:
    try:
        r = subprocess.run(["git", "-C", str(repo), "ls-tree", "-r", "HEAD",
                            "--format=%(objectname) %(path)"],
                           capture_output=True, text=True, timeout=60)
    except Exception:
        return {}
    out: dict[str, str] = {}
    for line in r.stdout.splitlines():
        blob, _, path = line.partition(" ")
        if blob and path:
            out[path] = blob
    return out


def head_sha(repo: Path) -> str | None:
    try:
        return subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                              capture_output=True, text=True, timeout=30).stdout.strip() or None
    except Exception:
        return None


def digest(path: Path) -> tuple[str, int, int | None]:
    h = hashlib.sha256()
    lines: int | None = 0
    size = 0
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
            size += len(chunk)
            if lines is not None:
                lines += chunk.count(b"\n")
                if size > (64 << 20):
                    lines = None
    return h.hexdigest(), size, lines


def freeze(dag_path: Path, now: str) -> dict:
    dag = json.loads(dag_path.read_text())
    repo = dag_path.parent
    nodes = dag.get("nodes") or {}
    if not isinstance(nodes, dict):
        nodes = {n.get("id", f"n{i}"): n for i, n in enumerate(nodes)}
    closed = {k: v for k, v in nodes.items() if str(v.get("status", "")).lower() in CLOSED}
    seen: dict[str, int] = {}
    for n in nodes.values():
        s = str(n.get("status", ""))
        seen[s] = seen.get(s, 0) + 1
    blobs = git_blobs(repo)
    artifacts: dict[str, dict] = {}
    for nid, node in sorted(closed.items()):
        for p in named_paths(node):
            rec = artifacts.setdefault(p, {"path": p, "nodes": []})
            rec["nodes"].append(nid)
    tree = None
    for p, rec in artifacts.items():
        # a receipt is repo-relative unless the spine holds it: plan_check writes
        # archive/gates/ under whatever directory ran it, so both roots resolve
        p = str(Path(p).expanduser())
        roots = [Path("/")] if p.startswith("/") else [repo, SPINE, HOME]
        abs_p = next((r / p for r in roots if (r / p).is_file()), None)
        if abs_p is None and "/" not in p:
            # a record that names a bare filename still means a file in the loop
            if tree is None:
                tree = {}
                r2 = subprocess.run(["rg", "--files", str(repo), "-g", "!node_modules",
                                     "-g", "!.git"], capture_output=True, text=True)
                for line in r2.stdout.splitlines():
                    tree.setdefault(Path(line).name, line)
            if p in tree:
                abs_p = repo / tree[p]
        abs_p = abs_p or roots[0] / p
        rec["root"] = ("abs" if p.startswith("/")
                       else "repo" if abs_p.is_file() and abs_p == repo / p
                       else "spine" if abs_p.is_file() and abs_p == SPINE / p
                       else "home" if abs_p.is_file() and abs_p == HOME / p
                       else None)
        if abs_p.is_file():
            sha, size, lines = digest(abs_p)
            inside = abs_p.is_relative_to(repo)
            rec.update(exists=True, sha256=sha, bytes=size, lines=lines,
                       blob=blobs.get(str(abs_p.relative_to(repo))) if inside else None)
            rec["untracked"] = bool(inside) and rec["blob"] is None
        else:
            rec.update(exists=False, sha256=None, bytes=None, lines=None,
                       blob=blobs.get(p), untracked=blobs.get(p) is None)
    missing = sorted(p for p, r in artifacts.items() if not r["exists"])
    untracked = sorted(p for p, r in artifacts.items() if r.get("untracked"))
    return {
        "frozen_utc": now,
        "dag": str(dag_path),
        "repo": str(repo),
        "head": head_sha(repo),
        "nodes_total": len(nodes),
        "nodes_closed": len(closed),
        "status_vocabulary": seen,
        "artifacts_named": len(artifacts),
        "bytes_hashed": sum(r["bytes"] or 0 for r in artifacts.values()),
        "missing": missing,
        "untracked": untracked,
        "artifacts": [artifacts[k] for k in sorted(artifacts)],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="freeze closed-loop evidence into a manifest")
    ap.add_argument("--dag", action="append", default=None,
                    help="DAG.json to freeze (repeatable); default: spine + every Projects/*/DAG.json")
    ap.add_argument("--out", default=str(SPINE / "run" / "freeze"))
    ap.add_argument("--json", action="store_true", help="print the manifest on stdout")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()
    now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    if a.dag:
        dags = [Path(p) for p in a.dag]
    else:
        cands = [SPINE / "DAG.json", *SPINE.glob("Projects/*/DAG.json"),
                 *HOME.glob("Projects/*/DAG.json")]
        dags = [p for p in cands if p.is_file()]
        if not dags:
            print("FAIL: no DAG.json found; pass --dag", file=sys.stderr)
            return 1
    t0 = time.perf_counter()
    reports = []
    for d in dags:
        if not d.is_file():
            print(f"FAIL: no DAG at {d}", file=sys.stderr)
            return 1
        reports.append(freeze(d, now))
    el = time.perf_counter() - t0
    out = Path(a.out) / now
    out.mkdir(parents=True, exist_ok=True)
    for r in reports:
        (out / f"{Path(r['dag']).parent.name}.json").write_text(json.dumps(r, indent=1) + "\n")
    bad = [r for r in reports if r["missing"]]
    if not a.quiet:
        for r in reports:
            by_path = {x["path"]: x["nodes"] for x in r["artifacts"]}
            print(f"== {Path(r['dag']).parent.name}: {r['nodes_closed']}/{r['nodes_total']} closed nodes, "
                  f"{r['artifacts_named']} artifacts named, {r['bytes_hashed'] / 1e6:.1f} MB hashed, "
                  f"{len(r['untracked'])} untracked, {len(r['missing'])} missing  [head {str(r['head'])[:8]}]")
            for p in r["missing"][:20]:
                print(f"   MISSING {p}  <- {', '.join(by_path[p][:3])}")
            for p in r["untracked"][:20]:
                print(f"   untracked {p}  <- {', '.join(by_path[p][:3])}")
            odd = {k: v for k, v in r["status_vocabulary"].items()
                   if k.lower() not in ("done", "closed", "executed", "open")}
            if odd:
                print(f"   NOTE unseen status vocabulary {odd}: counted neither closed nor open")
        print(f"manifests -> {out}  ({el:.2f} s)")
    if a.json:
        print(json.dumps(reports if len(reports) > 1 else reports[0], indent=1))
    if bad:
        print(f"FAIL: {sum(len(r['missing']) for r in bad)} evidence path(s) named by a closed node do not exist",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
