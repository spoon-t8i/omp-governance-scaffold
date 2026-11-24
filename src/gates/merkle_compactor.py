#!/usr/bin/env python3
"""merkle_compactor.py - lossless Merkle fold of closed STATE.md bullets (FC-06, Concept A).

Adopted from the omp-fractal-crucible prototype after the FC-02 A/B (932->599 tok shadow
STATE.md, 4/4 injected faults caught) and operator signature FC-06. Concept A: fold K
contiguous closed (`- [x]`) bullets under `## Done` into ONE parent receipt bullet. The K
children move verbatim into a vault block appended to EVENTS.md; the parent keeps a
deterministic pointer:

    [rollup:<sha256_12> | children:<K> | domain:<scope> -> EVENTS.md#L<a>-L<b>]

Lossless by construction: child text is stored byte-verbatim in the vault (lines rstripped
once at fold time so the round-trip is stable), and the 12-hex Merkle root binds them, so
verify_rollup can prove the vault still says exactly what STATE.md once said. No abstractive
prose sits anywhere except the fixed parent template, which carries no facts beyond the
counts the pointer itself states (Article II).

Merkle rule (documented, versioned, deterministic):
    leaf(child)  = sha256(b"FRCLEAF1\n" + child_text.encode())
    node(l, r)   = sha256(b"FRCNODE1\n" + l + r)
    odd level    = the last hash is paired with itself (FRCNODE1 over (h, h))
    root12       = hexdigest(root)[:12]
Children are hashed in ledger order (the order STATE.md had them); order is part of the
binding, so a reordered vault is a MERKLE_MISMATCH, not a pass.

verify_rollup(state_path, events_path, memory_db_path=None, root_base=None)
  -> (ok: bool, code: str, detail: dict)
  Fail codes, in check order (Positive Denominator first, nothing is 0/0):
    DENOMINATOR_MISMATCH  K == 0 ("0/0 != PASS"), or resolved_children != K
    MERKLE_MISMATCH       recomputed root != pointer root (mutated/reordered child bytes)
    TOMBSTONE_ALIVE       a [dead:PATH] cited inside a child exists on disk. Absence is
                          checked with os.path.exists only; the file is NEVER opened, so a
                          dead citation cannot raise a missing-file error.
    DB_MISMATCH           the memory.db fc_rollups row is missing or disagrees with
                          the pointer / vault content (only checked when a db is passed).
    POINTER_UNRESOLVED    pointer names a file or range that cannot be sliced.

spine_check.py calls check_rollups() every run; DENOMINATOR_MISMATCH or MERKLE_MISMATCH
is a hard spine failure. A fold that STATE.md carries is a live referent, not a note.

1-hop fractal zoom: zoom(query) returns the exact K children of the rollup whose parent
bullet matches every query token (case-folded substring, deterministic); a child that is
itself a pointer can be zoomed again by calling zoom once more - one hop per call.

Modes
  fold   --state S --events E [--db D]        rewrite STATE.md + vault + db row
  --verify [--state S --events E --db D] [--receipt R]   verify every pointer, JSON out
  --zoom QUERY [--state S --events E]         print the K children, zoom_ms included

fold rewrites STATE.md; it refuses when the chosen Done cluster already carries a
pointer. Run spine_check after folding, and commit before and after.
"""
import argparse
import hashlib
import json
import os
import pathlib
import re
import sqlite3
import sys
import time

LEAF = b"FRCLEAF1\n"
NODE = b"FRCNODE1\n"
POINTER_RE = re.compile(
    r"\[rollup:([0-9a-f]{12}) \| children:(\d+) \| domain:(\S+) -> (\S+)#L(\d+)-L(\d+)\]")
DEAD_RE = re.compile(r"\[dead:([^\]\s]+)\]")
DONE_RE = re.compile(r"^## Done\s*$")
BULLET_RE = re.compile(r"^- \[x\] ")
VAULT_MARK = "<!-- fc-vault:"
ZOOM_BUDGET_MS = 2.0


def h(b):
    return hashlib.sha256(b).digest()


def merkle_root(children):
    """12-hex root over children in ledger order; odd nodes pair with themselves."""
    level = [h(LEAF + c.encode("utf-8")) for c in children]
    if not level:
        return hashlib.sha256(LEAF).hexdigest()[:12]
    while len(level) > 1:
        nxt = []
        for i in range(0, len(level), 2):
            left = level[i]
            right = level[i + 1] if i + 1 < len(level) else level[i]
            nxt.append(h(NODE + left + right))
        level = nxt
    return level[0].hex()[:12]


def rstrip_lines(text):
    return "\n".join(line.rstrip() for line in text.split("\n"))


def split_bullets(lines):
    """Lines -> [(start, end)] bullet spans: `- [x] ...` plus its indented continuations."""
    spans = []
    i, n = 0, len(lines)
    while i < n:
        if BULLET_RE.match(lines[i]):
            j = i + 1
            while j < n and lines[j].startswith(" ") and lines[j].strip():
                j += 1
            spans.append((i, j - 1))
            i = j
        else:
            i += 1
    return spans


def find_done_cluster(lines):
    """The first `## Done` section's contiguous `- [x]` cluster, or None."""
    start = None
    for idx, line in enumerate(lines):
        if DONE_RE.match(line):
            start = idx
            break
    if start is None:
        return None
    sec = []
    for idx in range(start + 1, len(lines)):
        if lines[idx].startswith("## "):
            break
        sec.append(idx)
    spans = split_bullets([lines[i] for i in sec])
    return [(sec[a] + 1, sec[b] + 1) for a, b in spans] or None  # 1-based


def group_children(vault_lines):
    """Vault slice -> verbatim children: new child at `^- \\[`, indented lines continue."""
    children = []
    for line in vault_lines:
        if BULLET_RE.match(line) or (line.startswith("- ") and not line.startswith("-  ")):
            children.append(line)
        elif children and (line.startswith(" ") or line.strip() == ""):
            if line.strip() == "":
                continue
            children[-1] += "\n" + line
        elif line.strip():
            raise ValueError(f"vault line outside any child: {line[:60]!r}")
    return children


def parse_pointers(state_text):
    out = []
    for m in POINTER_RE.finditer(state_text):
        out.append({"root": m.group(1), "k": int(m.group(2)), "domain": m.group(3),
                    "file": m.group(4), "lo": int(m.group(5)), "hi": int(m.group(6))})
    return out


def resolve_events(pointer, events_path):
    p = pathlib.Path(pointer["file"])
    if not p.is_absolute():
        p = pathlib.Path(events_path).parent / p
    return p


def tombstone_check(children, root_base):
    """[dead:PATH] inside children must be ABSENT on disk. Absence is checked with
    os.path.exists only; the file is NEVER opened, so a dead citation inside a rolled-up
    entry cannot raise a missing-file error (FC-02 fault 4)."""
    checked, alive = 0, []
    for text in children:
        for m in DEAD_RE.finditer(text):
            checked += 1
            cited = m.group(1)
            cands = [pathlib.Path(cited)] if os.path.isabs(cited) else \
                [pathlib.Path(root_base) / cited, pathlib.Path(cited)]
            if any(c.exists() for c in cands):
                alive.append(cited)
    return checked, alive


def verify_rollup(state_path, events_path, memory_db_path=None, root_base=None):
    """Verify every rollup pointer in state_path against events_path (+ optional db)."""
    root_base = root_base or str(pathlib.Path(state_path).parent)
    state_text = pathlib.Path(state_path).read_text()
    pointers = parse_pointers(state_text)
    if not pointers:
        return False, "NO_ROLLUPS", {"pointers": 0, "scanned": 0}
    results, fail_code, fail_detail = [], None, None
    for pt in pointers:
        item = {"root": pt["root"], "expected_k": pt["k"], "domain": pt["domain"],
                "anchor": f"{pt['file']}#L{pt['lo']}-L{pt['hi']}"}
        # Positive Denominator before anything else: 0/0 is never a pass.
        if pt["k"] == 0:
            fail_code, item["error"] = "DENOMINATOR_MISMATCH", "0/0 != PASS"
            results.append(item)
            fail_detail = item
            break
        ev = resolve_events(pt, events_path)
        if not ev.is_file():
            fail_code, item["error"] = "POINTER_UNRESOLVED", f"missing {ev}"
            results.append(item)
            fail_detail = item
            break
        ev_lines = ev.read_text().split("\n")
        if pt["lo"] < 1 or pt["hi"] > len(ev_lines) or pt["hi"] < pt["lo"]:
            fail_code, item["error"] = "POINTER_UNRESOLVED", \
                f"range L{pt['lo']}-L{pt['hi']} outside {ev.name} ({len(ev_lines)} lines)"
            results.append(item)
            fail_detail = item
            break
        try:
            children = group_children(ev_lines[pt["lo"] - 1:pt["hi"]])
        except ValueError as exc:
            fail_code, item["error"] = "DENOMINATOR_MISMATCH", str(exc)
            results.append(item)
            fail_detail = item
            break
        item["resolved_children"] = len(children)
        if len(children) != pt["k"]:
            fail_code, item["error"] = "DENOMINATOR_MISMATCH", \
                f"resolved {len(children)} / expected {pt['k']}"
            results.append(item)
            fail_detail = item
            break
        recomputed = merkle_root(children)
        item["recomputed_root"] = recomputed
        if recomputed != pt["root"]:
            fail_code, item["error"] = "MERKLE_MISMATCH", \
                f"recomputed {recomputed} / pointer {pt['root']}"
            results.append(item)
            fail_detail = item
            break
        checked, alive = tombstone_check(children, root_base)
        item["tombstones_checked"] = checked
        if alive:
            fail_code, item["error"] = "TOMBSTONE_ALIVE", ",".join(alive)
            results.append(item)
            fail_detail = item
            break
        if memory_db_path:
            ok, why = db_crosscheck(memory_db_path, pt, children)
            item["db_crosscheck"] = "ok" if ok else why
            if not ok:
                fail_code, item["error"] = "DB_MISMATCH", why
                results.append(item)
                fail_detail = item
                break
        item["ok"] = True
        results.append(item)
    detail = {"pointers": len(pointers), "scanned": len(results), "results": results}
    if fail_code:
        detail["failed"] = fail_detail
        return False, fail_code, detail
    return True, "OK", detail


def db_crosscheck(db_path, pt, children):
    uri = f"file:{db_path}?mode=ro"
    try:
        con = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        return False, f"cannot open {db_path}: {exc}"
    try:
        row = con.execute(
            "SELECT children, domain, anchor, content12 FROM fc_rollups WHERE root=?",
            (pt["root"],)).fetchone()
    except sqlite3.Error as exc:
        con.close()
        return False, f"fc_rollups: {exc}"
    con.close()
    if row is None:
        return False, f"no fc_rollups row for {pt['root']}"
    if row[0] != pt["k"] or row[1] != pt["domain"] or \
            row[2] != f"{pt['file']}#L{pt['lo']}-L{pt['hi']}":
        return False, f"row disagrees: {row[:3]}"
    content12 = hashlib.sha256(("\n".join(children)).encode()).hexdigest()[:12]
    if row[3] != content12:
        return False, f"content12 {row[3]} != vault {content12}"
    return True, "ok"


def zoom(query, state_path, events_path):
    """Deterministic 1-hop expansion: parent bullet matching every query token -> K children."""
    started = time.perf_counter()
    state_text = pathlib.Path(state_path).read_text()
    tokens = [t for t in query.lower().split() if t]
    hit = None
    for line in state_text.split("\n"):
        if "rollup:" not in line:
            continue
        low = line.lower()
        if all(t in low for t in tokens):
            hit = POINTER_RE.search(line)
            break
    if hit is None:
        return {"matched": False, "children": [],
                "zoom_ms": round((time.perf_counter() - started) * 1000, 3)}
    pt = {"root": hit.group(1), "k": int(hit.group(2)), "domain": hit.group(3),
          "file": hit.group(4), "lo": int(hit.group(5)), "hi": int(hit.group(6))}
    ev = resolve_events(pt, events_path)
    ev_lines = ev.read_text().split("\n")
    children = group_children(ev_lines[pt["lo"] - 1:pt["hi"]])
    ms = round((time.perf_counter() - started) * 1000, 3)
    return {"matched": True, "root": pt["root"], "expected_k": pt["k"],
            "resolved_children": len(children), "children": children, "zoom_ms": ms}


def do_fold(state_path, events_path, db_path):
    state = pathlib.Path(state_path)
    events = pathlib.Path(events_path)
    state_lines = state.read_text().split("\n")
    while state_lines and state_lines[-1] == "":
        state_lines.pop()
    cluster = find_done_cluster(state_lines)
    if not cluster:
        return 1, "no `## Done` bullet cluster found"
    if any("rollup:" in state_lines[a - 1] for a, _b in cluster):
        return 1, "the chosen Done cluster already carries a rollup pointer; nothing new to fold"
    lo0, hi0 = cluster[0][0] - 1, cluster[-1][1]        # 0-based span of the whole cluster
    children = [rstrip_lines("\n".join(state_lines[a - 1:b])) for a, b in cluster]
    k = len(children)
    root = merkle_root(children)
    ev_lines = events.read_text().split("\n")
    while ev_lines and ev_lines[-1] == "":
        ev_lines.pop()
    vault_lo = len(ev_lines) + 4                       # blank + header + marker, then children
    vault_hi = vault_lo + sum(c.count("\n") + 1 for c in children) - 1
    ev_out = ev_lines + ["",
                         f"## rollup:{root} - folded closed STATE.md Done bullets (FC-06)",
                         f"{VAULT_MARK} root={root} children={k} domain=state:done "
                         f"span=L{vault_lo}-L{vault_hi} -->"]
    for c in children:
        ev_out.extend(c.split("\n"))
    parent = (f"- [x] Done ledger: {k} closed items held verbatim in the vault "
              f"[rollup:{root} | children:{k} | domain:state:done "
              f"-> EVENTS.md#L{vault_lo}-L{vault_hi}]")
    new_state = state_lines[:lo0] + [parent] + state_lines[hi0:]
    _atomic_write(events, "\n".join(ev_out) + "\n")
    _atomic_write(state, "\n".join(new_state) + "\n")
    if db_path:
        content12 = hashlib.sha256("\n".join(children).encode()).hexdigest()[:12]
        con = sqlite3.connect(db_path)
        con.execute("CREATE TABLE IF NOT EXISTS fc_rollups (root TEXT PRIMARY KEY,"
                    " children INTEGER, domain TEXT, anchor TEXT, content12 TEXT)")
        con.execute("INSERT OR REPLACE INTO fc_rollups VALUES (?,?,?,?,?)",
                    (root, k, "state:done", f"EVENTS.md#L{vault_lo}-L{vault_hi}", content12))
        con.commit()
        con.close()
    return 0, json.dumps({"root": root, "children": k,
                          "anchor": f"EVENTS.md#L{vault_lo}-L{vault_hi}",
                          "state_lines": len(state_lines), "new_state_lines": len(new_state)})


def _atomic_write(path, text):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    home = pathlib.Path(os.environ.get("SPINE_DIR", pathlib.Path.home() / ".omp" / "agent"))
    ap = argparse.ArgumentParser(prog="merkle_compactor.py", description=__doc__.split("Modes")[0])
    ap.add_argument("mode", choices=["fold"], nargs="?")
    ap.add_argument("--state", default=str(home / "STATE.md"))
    ap.add_argument("--events", default=str(home / "EVENTS.md"))
    ap.add_argument("--db", default=None)
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--zoom", default=None)
    ap.add_argument("--receipt", default=None)
    args = ap.parse_args(argv)
    if args.verify and args.zoom:
        print("ERROR --verify and --zoom are exclusive", file=sys.stderr)
        return 2
    if args.zoom:
        out = zoom(args.zoom, args.state, args.events)
        out["budget_ms"] = ZOOM_BUDGET_MS
        out["within_budget"] = out["zoom_ms"] <= ZOOM_BUDGET_MS
        print(json.dumps(out, indent=1))
        return 0
    if args.verify:
        ok, code, detail = verify_rollup(args.state, args.events, args.db)
        payload = {"gate": "rollup-verify", "ok": ok, "code": code,
                   "scanned_N": detail["scanned"], "expected_N": detail["pointers"],
                   "passed_k": sum(1 for r in detail.get("results", []) if r.get("ok")),
                   "detail": detail,
                   "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                   "loadavg_1m": round(os.getloadavg()[0], 2)}
        text = json.dumps(payload, indent=1)
        if args.receipt:
            pathlib.Path(args.receipt).parent.mkdir(parents=True, exist_ok=True)
            pathlib.Path(args.receipt).write_text(text + "\n")
        print(text)
        return 0 if ok else 1
    if args.mode == "fold":
        rc, msg = do_fold(args.state, args.events, args.db)
        print(msg)
        return rc
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
