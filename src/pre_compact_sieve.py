#!/usr/bin/env python3
"""pre_compact_sieve.py - what you cannot safely guess after a context eviction.

Compaction takes the file text out of the transcript and leaves the intention behind. The
dangerous moment is the next `edit_file.py` call: an `old` block re-emitted from memory
looks plausible and fails, or worse, matches the wrong thing. This sieve prints the small
set of facts that must be re-read or re-verified instead of recalled, in four sections,
and stays inside a token budget so it can be run before every compaction, cheaply.

  [EVICTED_FILE - must reread before edit_file.py] <path> (L<N>, <12hex>, <note>)
  STATE.md rows carry their own tail: -> spine: reread in the same call for edit_file.py
  [FALSIFIED_IN_SESSION] things that were proven wrong or failed here
  [ACTIVE_WORKING_SET] what is dirty in the repo right now
  [DISCOVERED_CONTRACTS] grounded API/behaviour claims with the referent that proves them

Two addresses ride along, because the frozen session's cost is a READ it does not know it can
avoid: a dirty file carries the changed line ranges (@L120-140), so the reread is the hunk and
not the whole file; and a repo wired to DAG.json is named with its counts and ready frontier, so
the session runs plan_check --frontier instead of reading a 3.4 k-line plan raw.

Sources are files and databases that already exist, never the transcript:

  * `git status --short` for the working set, and for the dirty-file half of the evict list.
  * `memory.db` (tools/fact_store.py) for grounded claims: `status='active'` facts are the
    discovered contracts; a fact whose pinned byte slice no longer hashes is ANCHOR-DRIFT,
    and its referent is the single most important file to re-read. Stale, contested and
    falsified facts land under FALSIFIED_IN_SESSION, because a claim the store no longer
    stands behind is exactly the thing a confident guess would resurrect.
  * `~/.omp/stats.db` `tool_calls.is_error` for failed tool calls in the recent window.
    An idle stats.db prints `stats=idle(<age>)` on the SIEVE header line instead of a
    [FALSIFIED_IN_SESSION] row: a quiet witness must never displace real falsifications.
    Nothing daemonizes that store - it advances only when `omp stats` runs - so the sieve
    drives the ingest itself (`sync_stats`) once the store lags by STATS_STALE_S, fail-open.
  * a journal (`~/.omp/run/falsified.jsonl`, `--record` appends) for the claims this
    session disproved and nothing else recorded: a rejected flag, a hypothesis a probe
    killed, a command that exited non-zero for a reason worth remembering.

Output is bounded by --max-tokens (default 200), estimated as ceil(chars/4). When the budget
bites, the sections a re-run rebuilds (contracts from memory.db, the working set from
`git status`) shed their tails before [EVICTED_FILE] does, because a file's sha256 cannot be
rebuilt once the file moves again; every section the budget shortened is named in one
[BUDGET_SPILL] line together with the budget that would show every row, and a lane holding
rows never empties (lane_floors) until the last resort, a hard cut marked TRUNCATED. No
wall-clock appears unless --stamp is given, so the same inputs print the same bytes. Row caps
are floors: they scale xN when --max-tokens is raised to 2N.

Outside `~/.omp/agent`, memory.db is scoped to the repo being sieved: a fact row counts
when its subject starts with the repo name or its referent resolves inside the repo, with
`~/.omp/agent/STATE.md` the one watched exception, so one project's block never spends its
budget on another project's referents.

--check-summary PATH audits a *candidate compaction summary* instead of the repo: every
revision-looking hex token (7-40 hex, holding at least one a-f, content digests excluded) is
re-resolved with `git cat-file -t` across the active repos, and one no repo resolves - or one
that resolves as a blob or tree - is tagged [UNVERIFIED_HASH]. The reason it exists:
perm-record §43 records a compaction summary that cited commit `f7c5628`, which that repo's
git answers "Not a valid object name". A summary may cite a hash, never mint one.
Test: harness/sieve_harness.py.
"""
import argparse
import datetime
import hashlib
import json
import os
import pathlib
import re
import shlex
import sqlite3
import subprocess
import sys

AGENT = pathlib.Path(__file__).resolve().parent.parent
HOME = pathlib.Path.home()
DEFAULT_MEMORY = AGENT / "memory.db"
DEFAULT_STATS = HOME / ".omp" / "stats.db"
DEFAULT_JOURNAL = HOME / ".omp" / "run" / "falsified.jsonl"
# which repo the session is about: operator pin first, then the STATE.md Active rows.
# cwd is the explicit fallback, never a silent one: every resolution is logged.
DEFAULT_PIN = HOME / ".omp" / "run" / "sieve-repo"
DEFAULT_REPO_LOG = HOME / ".omp" / "run" / "sieve-repo.log"
# stats.db has no daemon: omp writes it only when something opens the stats view, so a store
# that has not advanced is a store nobody read, not a clean run - and a store that advanced
# twenty minutes ago is still missing this session's newest failure, which is the row a
# boundary most needs. So the sieve pulls whenever the store lags this far. The ingest is
# cheap (measured 2026-11-08: 0.18 s over 67,575 rows) and the bound below caps the worst case.
# Retune with OMP_SIEVE_STATS_STALE_S / OMP_SIEVE_STATS_SYNC_S, mute the whole lane with
# OMP_SIEVE_STATS_SYNC=0, name the binary with OMP_STATS_CMD.
STATS_STALE_S = 60
STATS_SYNC_TIMEOUT_S = 5.0   # the hook waits 12 s for the sieve; the pull gets a third of it

EVICT_LINES = 5          # most-recent-file entries before "+N more"
FALSIFIED_LINES = 4
WORKING_LINES = 5
CONTRACT_LINES = 4
ROW_SCALE_TOKENS = 200   # caps scale xN at --max-tokens 2N; the fit loop still sheds tails
# Screenshots, gate-evidence trees, WAL sidecars and logs cannot be re-read before
# edit_file.py and must not crowd live code out of the evict block.
NON_EDIT_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".wasm", ".db",
                     ".db-shm", ".db-wal", ".log")
# what gives up its tail lines first when --max-tokens bites: the git mirror is one `git status`
# from being whole again and the live facts are one `fact_store.py --list` away, so both shed
# before the falsifications, and all three shed before the evict list, whose hashes cannot be
# rebuilt after the file moves. Each lane sheds to its floor (lane_floors), never to zero.
SHED_ORDER = ("working", "contracts", "falsified", "evicted")
# evict rows the squeeze protects: the read-before-edit set, and files that vanished
CORE_NOTES = ("dirty", "watched", "spine", "gone", "ANCHOR-DRIFT")
# STATE.md is the spine: re-injected every turn, so it is not a reread-this-file case --
# but edit_file.py still refuses it without a read in the same call, which is what to say.
SPINE_NAMES = ("STATE.md",)
OBJ_CHARS = 68           # how much of a fact's object survives onto the line
CMD_CHARS = 60
PLAN_READY_WORDS = 3   # frontier node ids named before saying how many more there are
HUNK_MAX = 3           # changed-line ranges named per dirty row before "+N"
PLAN_BUDGET_TOKENS = 60  # the ceiling on the plan line's exemption from --max-tokens

BAD_STATUSES = ("stale", "contested", "falsified", "unverified")

# fact_store owns the anchor convention (byte slice of lines start..end, 1-indexed).
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
try:
    import fact_store as FS
except Exception:                                    # pragma: no cover - degrade, never die
    FS = None


class SieveError(Exception):
    pass


def est_tokens(text):
    """Token estimate: the boring one. 4 chars/token, rounded up."""
    return (len(text) + 3) // 4


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def cut(text, n):
    """Bounded one-line form of a claim; the ellipsis says text was dropped."""
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[:max(1, n - 1)].rstrip() + "\u2026"


def human_age(seconds):
    """How old a witness is, in the unit that makes the point."""
    if seconds is None:
        return "?"
    if seconds < 90:
        return f"{int(seconds)}s"
    if seconds < 5400:
        return f"{int(round(seconds / 60))}m"
    if seconds < 172800:
        return f"{round(seconds / 3600.0, 1)}h"
    return f"{round(seconds / 86400.0)}d"


def short_path(path, repo):
    """Shortest unambiguous spelling from here: repo-relative, ~, or absolute."""
    try:
        return str(path.resolve().relative_to(repo.resolve()))
    except (ValueError, OSError):
        pass
    try:
        return "~/" + str(path.resolve().relative_to(HOME))
    except (ValueError, OSError):
        return str(path)


# ------------------------------------------------------- repo scope ----
def git_root(path):
    """The enclosing git worktree root, or None. The marker is rarely where the session
    stands: an agent-tree session works inside the ~/.omp worktree, so `.git` is sought
    up the tree. Ask git first (linked worktrees and submodules keep `.git` as a file),
    then walk ancestors when git is missing or the marker is a bare fixture directory.
    None means git_status() and the dirty set mean nothing at this path."""
    try:
        proc = subprocess.run(["git", "-C", str(path), "rev-parse", "--show-toplevel"],
                              capture_output=True, text=True, timeout=10)
        if proc.returncode == 0 and proc.stdout.strip():
            root = pathlib.Path(proc.stdout.strip())
            if root.is_dir():
                return root
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        cur = pathlib.Path(path).resolve()
    except OSError:
        cur = pathlib.Path(path)
    for cand in (cur, *cur.parents):
        try:
            if (cand / ".git").exists():
                return cand
        except OSError:
            continue
    return None


def is_git(path):
    """True when a git worktree encloses `path`, here or above."""
    return git_root(path) is not None


def active_projects(agent=AGENT):
    """(name, path) for each STATE.md `## Active` row that names a path, in listed order.
    A row like `cityblock-perf Projects/cityblock-perf/` yields the project path; bare
    names yield nothing. One implementation of 'what is this session working on', shared
    by the sieve, its hook and spine_check's scope audit."""
    state = next((p for p in (pathlib.Path(agent) / "memory" / "STATE.md",
                              pathlib.Path(agent) / "STATE.md") if p.is_file()), None)
    if state is None:
        return []
    out, in_active = [], False
    for line in state.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            in_active = line[3:].strip().lower() == "active"
            continue
        m = re.match(r"-\s*\[ ]\s*(\S+)(.*)$", line.strip())  # [x] closed rows never scope
        if not in_active or not m:
            continue
        name, rest = m.group(1), m.group(2)
        for tok in rest.split():
            tok = tok.rstrip("/")
            if "/" not in tok:
                continue
            cand = pathlib.Path(os.path.expanduser(tok))
            if not cand.is_absolute():
                cand = HOME / tok
            try:
                cand = cand.resolve()
            except OSError:
                continue
            if not cand.is_dir():
                continue  # rows name notes and harness files too; only directories scope
            out.append((name, cand))
            break
    return out


def resolve_repo(cli=None, cwd=None, agent=AGENT, env=None):
    """One source of truth for what `--repo` means: OMP_SIEVE_REPO (absolute override),
    then a git cwd - the enclosing worktree root when the session stands in a
    subdirectory of one (the agent tree lives inside the ~/.omp worktree), then the
    operator's run/sieve-repo pin, then the first STATE.md Active row whose path is a
    git repo, then a non-git cwd. A pin never hijacks a git-cwd session - a stale pin
    would otherwise steal every real repo session - and an explicit OMP_SIEVE_REPO still
    outranks a walk-up, so an agent-tree session can be redirected by hand; every
    redirect is logged either way."""
    env = os.environ if env is None else env
    cwd = pathlib.Path(cwd or pathlib.Path.cwd())
    try:
        cwd_r = cwd.resolve()
    except OSError:
        cwd_r = cwd
    enclosing = git_root(cwd_r)
    if enclosing == cwd_r:
        return cwd_r, "cwd(git)", "none"
    pinned = env.get("OMP_SIEVE_REPO")
    where = "env"
    if not pinned:
        pin_file = pathlib.Path(env.get("SIEVE_REPO_PIN_PATH", DEFAULT_PIN)).expanduser()
        where = "pin"
        try:
            pinned = pin_file.read_text(encoding="utf-8").strip() or None
        except OSError:
            pinned = None
    if pinned:
        cand = pathlib.Path(os.path.expanduser(pinned))
        try:
            cand = cand.resolve()
        except OSError:
            cand = pathlib.Path(pinned)
        if cand.is_dir() and is_git(cand):
            return cand, where, where
        # a dead pin must not swallow the STATE chain any more than a live one can
        # hijack a real repo session: fall through, then name the pin as the failure.
    if enclosing is not None:
        # Standing inside a worktree without owning it: sieve the worktree whose dirty
        # set and evicted files the session actually dirties, not whichever project
        # STATE.md happened to list first.
        return enclosing, "cwd(git)", "cwd(walkup)"
    for _name, cand in active_projects(agent):
        if is_git(cand):
            return cand, "state-active", "state-active"
    if pinned:
        return cwd_r, "pin-unusable", "none"
    return cwd_r, "cwd(no-git)", "none"


def log_repo_resolution(repo, source, redirected, cli=None, log_path=None):
    """Every automatic scope decision names itself: an arm that silently sieved the wrong
    tree is the /tmp incident; an unlogged fix is invisible to the next audit."""
    log = pathlib.Path(log_path or os.environ.get("SIEVE_REPO_LOG", DEFAULT_REPO_LOG)).expanduser()
    try:
        log.parent.mkdir(parents=True, exist_ok=True)
        cli_s = f" cli={cli}" if cli else ""
        with log.open("a", encoding="utf-8") as fh:
            fh.write(f"{now_iso()} repo={repo} source={source} redirected={redirected}{cli_s}\n")
    except OSError:
        pass  # logging never costs the session its block


# ---------------------------------------------------------------- git ----
def git_status(repo):
    """`git status --short` lines, or None outside a repo / without git."""
    try:
        proc = subprocess.run(["git", "-C", str(repo), "status", "--short",
                               "--untracked-files=all"],
                              capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return [ln for ln in proc.stdout.splitlines() if ln.strip()]


def status_paths(rows):
    out = []
    for ln in rows:
        body = ln[2:] if len(ln) > 2 else ""
        if " -> " in body:                            # R old -> new / C src dst
            body = body.split(" -> ")[-1]
        body = body.strip().strip('"')
        if body:
            out.append(body)
    return sorted(set(out))


def non_editable(path, repo):
    """Binary/archive/log files and gate-evidence trees (`run/evidence/*`, `run/shots-*`)."""
    name = path.name.lower()
    if any(name.endswith(s) for s in NON_EDIT_SUFFIXES):
        return True
    try:
        parts = path.resolve().relative_to(repo.resolve()).parts
    except (ValueError, OSError):
        return False
    return len(parts) >= 2 and parts[0] == "run" and (
        parts[1] == "evidence" or parts[1].startswith("shots-"))


# ------------------------------------------------------------- facts ----
def open_ro(path):
    if not path.exists():
        return None
    try:
        return sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error:
        return None


def resolve_referent(source_path):
    p = pathlib.Path(source_path).expanduser()
    return p if p.is_absolute() else AGENT / p


def line_range(text):
    m = re.match(r"^L?(\d+)(?:-L?(\d+))?$", (text or "").strip())
    if not m:
        return None, None
    return int(m.group(1)), int(m.group(2) or m.group(1))


def anchor_state(path, rng, pin):
    """match | drift | gone | nopin: does the file still hold the bytes the fact cites?"""
    if FS is None:
        return "unknown"
    start, end = line_range(rng)
    if start is None:
        return "nopin"
    try:
        return "match" if FS.read_pin(path, start, end) == pin else "drift"
    except Exception:
        return "gone"


def read_facts(db, scope=None):
    """(active, bad) facts; both lists of dicts, empty when the store is absent.

    `scope(rec)` keeps only the rows belonging to the repo being sieved; None keeps all.
    "seq" is insertion order, so newest-recorded can sort newest-first.
    """
    conn = open_ro(db)
    if conn is None:
        return None, None
    try:
        rows = conn.execute(
            "SELECT rowid, fact_id, subject, predicate, object, frequency_class, status, "
            "source_path, line_range, sha256_pin FROM facts").fetchall()
    except sqlite3.Error:
        conn.close()
        return [], []
    conn.close()
    active, bad = [], []
    for (seq, fid, subj, pred, obj, klass, status, spath, rng, pin) in rows:
        rec = {"seq": seq, "fact_id": fid, "subject": subj, "predicate": pred,
               "object": obj, "class": klass, "status": status, "source_path": spath,
               "line_range": rng, "sha256_pin": pin}
        if scope is not None and not scope(rec):
            continue
        (active if status == "active" else bad).append(rec)
    # retired facts are history, not something a caller might mistake for live
    bad = [r for r in bad if r["status"] in BAD_STATUSES]
    return active, bad


# ------------------------------------------------------------- stats ----
def stats_age(db):
    """Seconds since stats.db last advanced; None when the store does not exist.

    The newest row, not the file's mtime: an ingest that carried nothing still leaves the
    newest row where it was, and that is the age that matters. mtime is only the fallback for
    a store whose table cannot be read. Row units follow failed_calls - ms above 1e11.
    """
    now = datetime.datetime.now(datetime.timezone.utc).timestamp()
    try:
        age = now - db.stat().st_mtime
    except OSError:
        return None
    conn = open_ro(db)
    if conn is not None:
        try:
            newest = conn.execute("SELECT MAX(timestamp) FROM tool_calls").fetchone()[0]
        except sqlite3.Error:
            newest = None
        conn.close()
        if newest:
            unit = 1000 if newest > 10 ** 11 else 1
            age = max(0.0, now - newest / unit)
    return age


def sync_stats(db, stale_after=None, timeout=None):
    """Pull omp's stats ingest when the store has fallen behind; fail-open, always.

    `omp stats` is the only writer stats.db has and it runs only when someone opens the stats
    view, so a store that has not advanced is a witness nobody read, and a store that advanced
    twenty minutes ago still has not seen this session's last failure: the lane would report a
    clean run while the current session was failing calls. This is the pull, driven from the
    one place that reads the store. Best-effort by construction: a
    missing binary, a timeout or a non-zero exit leaves the file exactly as it was and the
    lane prints whatever it already held, because the sieve runs inside the compaction hook's
    own timeout budget. Output is discarded for the same reason. The row caps
    (FALSIFIED_LINES) stay the hard ceiling, so a freshly ingested store spends budget on its
    newest rows and never starves the other lanes.
    """
    limit = STATS_STALE_S if stale_after is None else stale_after
    wait = STATS_SYNC_TIMEOUT_S if timeout is None else timeout
    try:
        limit = float(os.environ.get("OMP_SIEVE_STATS_STALE_S", limit))
        wait = float(os.environ.get("OMP_SIEVE_STATS_SYNC_S", wait))
    except ValueError:
        pass                                    # a bad knob falls back, never fails the sieve
    if os.environ.get("OMP_SIEVE_STATS_SYNC") == "0":
        return "off"
    age = stats_age(db)
    if age is not None and age <= limit:
        return "fresh"
    cmd = shlex.split(os.environ.get("OMP_STATS_CMD", "omp")) + ["stats", "--json"]
    try:
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=wait, check=False)
    except (OSError, ValueError, subprocess.SubprocessError):
        return "failed"
    return "synced"


def failed_calls(db, minutes, sync=False):
    """Failed tool calls in the window, read from omp's stats.db.

    `sync` drives the ingest first (sync_stats) when the store lags; the caller sets it only
    for the operator's real store, never for a fixture, so a test run cannot be written over
    by the live omp binary.

    `timestamp` is written in milliseconds. The window is computed in whatever unit the
    table actually uses, because a seconds threshold compared against milliseconds matches
    every row ever written and turns one quiet session into a wall of noise.

    None when the DB is absent; otherwise {"rows": [(tool, count)], "total": int,
    "silent_for": seconds since the newest row of any kind}, so an idle witness is reported
    as idle instead of being mistaken for a clean run.
    """
    if sync:
        sync_stats(db)
    conn = open_ro(db)
    if conn is None:
        return None
    now = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
    try:
        newest = conn.execute("SELECT MAX(timestamp) FROM tool_calls").fetchone()[0]
        unit = 1000 if (newest and newest > 10 ** 11) else 1
        since = (now - minutes * 60) * unit
        rows = conn.execute(
            "SELECT tool_name, COUNT(*) FROM tool_calls WHERE is_error = 1 AND timestamp >= ?"
            " GROUP BY tool_name ORDER BY COUNT(*) DESC, tool_name LIMIT 6", (since,)).fetchall()
    except sqlite3.Error:
        conn.close()
        return {"rows": [], "total": 0, "silent_for": None}
    conn.close()
    return {"rows": rows, "total": sum(n for _, n in rows),
            "silent_for": (now - newest // unit) if newest else None}


# ----------------------------------------------------------- journal ----
def read_journal(path):
    """Journal entries newest-first, deduped by claim. Missing file -> []."""
    if not path.exists():
        return []
    out = []
    for ln in path.read_text(encoding="utf-8", errors="replace").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            rec = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict) and str(rec.get("claim", "")).strip():
            out.append(rec)
    seen, newest = set(), []
    for rec in reversed(out):
        key = rec["claim"].strip()
        if key in seen:
            continue
        seen.add(key)
        newest.append(rec)
    return newest


def record(path, claim, cmd=None, rc=None, flag=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {"at": now_iso(), "claim": claim}
    if cmd:
        entry["cmd"] = cmd
    if rc is not None:
        entry["rc"] = rc
    if flag:
        entry["flag"] = flag
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def evict_note(path, note):
    """The evict-list note for a path: STATE.md is spine, everything else is what it is."""
    return "spine" if path.name in SPINE_NAMES else note


# ----------------------------------------------------------- assemble ----
def repo_rel(path, repo):
    """`path` as git names it, or "" when it lives outside the repo."""
    try:
        return path.resolve().relative_to(repo.resolve()).as_posix()
    except (ValueError, OSError):
        return ""


def git_hunk_ranges(repo):
    """rel path -> the changed line ranges, from ONE `git diff --unified=0`. A dirty file names
    the file; the hunk names the ADDRESS to re-read, which is the part a session otherwise
    re-derives by reading the whole file (note 2026-11-15 section 2: the 5.5 k-token re-read was
    a whole-file read when a hunk was wanted). Empty outside a repo; an untracked file has no
    hunks, because there is no committed version to differ from."""
    try:
        proc = subprocess.run(["git", "-C", str(repo), "diff", "--relative", "--unified=0", "--no-color"],
                              capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return {}
    if proc.returncode != 0:
        return {}
    out, cur = {}, None
    for ln in proc.stdout.splitlines():
        if ln.startswith("+++ b/"):
            cur = ln[6:].strip().rstrip("\t")
            continue
        if cur is None or not ln.startswith("@@"):
            continue
        m = re.search(r"\+(\d+)(?:,(\d+))?", ln)
        if not m:
            continue
        start = int(m.group(1))
        span = int(m.group(2) or 1)
        out.setdefault(cur, []).append((start, start + max(span, 1) - 1))
    return out


def hunk_span(ranges):
    """`@L120-140,L980+2` - where to read, at most HUNK_MAX ranges, so a row carries an address
    without becoming the diff it summarises."""
    if not ranges:
        return ""
    parts = [f"L{a}" if a == b else f"L{a}-{b}" for a, b in ranges[:HUNK_MAX]]
    extra = len(ranges) - HUNK_MAX
    return "@" + ",".join(parts) + (f"+{extra}" if extra > 0 else "")


def plan_pointer(repo):
    """One line saying this repo is WIRED to a plan: how far it is, what is ready now, and the
    command that says the rest. A DAG.json runs to thousands of lines, and a session frozen at a
    compaction boundary that keeps only the file's name re-reads it raw to learn what it was
    about to do (note 2026-11-15 section 3: amber_particles/DAG.json, 3417 lines). One frontier
    definition: the same `plan_check.py --frontier` the session would run, in a subprocess with
    a deadline, because validating a plan walks git - 0.7 s for a 28-node plan - and the sieve
    must not stall a compaction. A plan that cannot be summarised still gets a line, naming the
    command that says why: "unreadable" is not "absent". None when the repo carries no plan."""
    path = repo / "DAG.json"
    if not path.is_file():
        return None
    cmd = f"plan_check.py {path} --frontier"   # absolute: runnable from any cwd
    text = plan_check_text(path)
    if text is None:
        return f"plan=DAG.json present, not summarised -> {cmd}"
    head = next((ln for ln in text.splitlines() if re.search(r"\d+/\d+ done", ln)), "")
    m = re.search(r"plan\s+(\S+):\s+(\d+)/(\d+)\s+done,\s+(\d+)\s+ready", head)
    if not m:
        return f"plan=DAG.json present, not summarised -> {cmd}"
    name, done, total, nready = m.group(1), m.group(2), m.group(3), int(m.group(4))
    ids = [ln.split()[1] for ln in text.splitlines() if ln.strip().startswith("NEXT")]
    ready = " ".join(ids[:PLAN_READY_WORDS]) or "-"
    if nready > len(ids):
        ready += f" +{nready - len(ids)}"
    elif len(ids) > PLAN_READY_WORDS:
        ready += f" +{len(ids) - PLAN_READY_WORDS}"
    failed = [ln.split()[2] for ln in text.splitlines()
              if ln.strip().startswith("FAILED GATE")]
    # A closed node whose gate ran against a tree that has since moved is what a green frontier
    # hides, and the session pointed at `--frontier` never sees the note the full report prints.
    stale = next((ln[len("STALE:"):].split() for ln in text.splitlines()
                  if ln.startswith("STALE:")), [])
    return (f"plan={name} {done}/{total} ready:{ready}"
            + (f" stale={','.join(stale[:3])}" if 0 < len(stale) <= 3
               else f" stale={len(stale)}" if stale else "")
            + (f" FAILED:{','.join(failed)}" if failed else "")
            + f" -> {cmd}, never read DAG.json raw")


def plan_check_text(path):
    """The frontier view of one plan, or None when the validator could not run. A subprocess on
    a deadline: validating a plan walks git - 0.7 s for a 28-node plan, and one plan_check pass
    has run 1290 s over a whole corpus - and the sieve must never stall a compaction behind a
    repo walk. Its own function so the pointer's parsing is testable without a git fixture."""
    try:
        proc = subprocess.run([sys.executable, str(AGENT / "tools" / "plan_check.py"),
                               str(path), "--frontier"],
                              capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout


def collect(repo, memory, stats, journal, minutes, watches, stats_pull=False):
    repo = repo.resolve()
    rows = git_status(repo)
    dirty = status_paths(rows) if rows is not None else []
    hunks = git_hunk_ranges(repo)

    # a foreign project's facts describe files this session will not edit; scoping keeps
    # subject-prefixed rows and rows whose referent lives in the repo, STATE.md excepted
    scope = None
    if repo != AGENT:
        spine_path = (AGENT / "STATE.md").resolve()

        def scope(rec):
            if str(rec["subject"]).startswith(repo.name):
                return True
            p = resolve_referent(rec["source_path"])
            return _under(p, repo) or p.resolve() == spine_path
    active, bad = read_facts(memory, scope)
    facts_available = active is not None

    # referents of live facts: the files the session's grounded claims lean on
    referents = {}
    for rec in (active or []):
        p = resolve_referent(rec["source_path"])
        referents.setdefault(p, []).append(rec)
    for rec in (bad or []):
        rec["anchor"] = anchor_state(resolve_referent(rec["source_path"]),
                                     rec["line_range"], rec["sha256_pin"])

    drift, clean_refs = [], []
    for p, recs in referents.items():
        st = {anchor_state(p, r["line_range"], r["sha256_pin"]) for r in recs}
        (drift if ("drift" in st or "gone" in st) else clean_refs).append((p, recs, st))

    evicted = []
    seen_paths = set()

    def add_evict(p, note):
        if non_editable(p, repo):
            return
        try:
            mtime = p.stat().st_mtime
        except OSError:
            mtime = None
        try:
            data = p.read_bytes()
        except OSError:
            evicted.append({"path": short_path(p, repo), "lines": None, "sha12": None,
                            "note": "gone", "mtime": mtime})
            seen_paths.add(str(p))
            return
        if str(p) in seen_paths:
            for row in evicted:
                if row["path"] == short_path(p, repo) and note not in (row["note"] or ""):
                    row["note"] = ((row["note"] or "") + " " + note).strip()
            return
        lines = data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0)
        evicted.append({"path": short_path(p, repo), "lines": lines,
                        "sha12": hashlib.sha256(data).hexdigest()[:12], "note": note,
                        "mtime": mtime,
                        "hunks": hunks.get(repo_rel(p, repo), [])})
        seen_paths.add(str(p))

    def touch(p):
        try:
            return p.stat().st_mtime
        except OSError:
            return 0.0

    # an operator-named file outranks an incidentally dirty one
    for w in watches:
        p = pathlib.Path(w).expanduser()
        add_evict(p if p.is_absolute() else repo / p, evict_note(p, "watched"))
    for p, recs, st in sorted(drift, key=lambda t: (0 if _under(t[0], repo) else 1,
                                                    -touch(t[0]), str(t[0]))):
        tag = "ANCHOR-DRIFT:" + ",".join(sorted({r["subject"] for r in recs})[:2])
        add_evict(p, tag)
    # newest-touched source first: the file edited minutes before an eviction is the one
    # an `old` block gets re-emitted from memory about
    for rel in sorted(dirty, key=lambda r: (-touch(repo / r), r)):
        add_evict(repo / rel, evict_note(repo / rel, "dirty"))
    # a repo's own referents outrank another project's: a session frozen mid-gate must not
    # spend its read-before-edit set on files from a project it is not working in
    for p, recs, st in sorted(clean_refs, key=lambda t: (0 if _under(t[0], repo) else 1,
                                                         -touch(t[0]), str(t[0]))):
        add_evict(p, "fact-referent")

    falsified = []
    for rec in read_journal(journal):
        bits = []
        if rec.get("rc") is not None:
            bits.append(f"rc={rec['rc']}")
        if rec.get("flag"):
            bits.append(str(rec["flag"]))
        head = cut(rec["claim"], OBJ_CHARS)
        tail = (" :: " + cut(rec["cmd"], CMD_CHARS)) if rec.get("cmd") else ""
        falsified.append({"kind": "claimed", "head": head, "tail": tail,
                          "bits": " ".join(bits)})
    for rec in sorted(bad or [], key=lambda r: (r["status"], r["subject"])):
        falsified.append({"kind": "fact:" + rec["status"],
                          "head": cut(f'{rec["subject"]} {rec["predicate"]}', OBJ_CHARS),
                          "tail": (" -> " + cut(rec["object"], 44)) if rec["object"] else "",
                          "bits": "anchor=" + rec.get("anchor", "?")})
    fails = failed_calls(stats, minutes, sync=stats_pull)
    # an idle witness belongs on the header line, not in [FALSIFIED_IN_SESSION] slot 0:
    # at a small budget it displaced every real falsification below it
    stats_idle = None
    if fails and fails["rows"]:
        names = " ".join(f"{n}×{c}" for n, c in fails["rows"][:3])
        falsified.insert(0, {"kind": "tool-errors", "head": names,
                             "tail": f" in last {minutes} min", "bits": f"n={fails['total']}"})
    elif fails is not None and fails.get("silent_for") is not None \
            and fails["silent_for"] > minutes * 60:
        # silence from a witness that has not been written to is not evidence of a clean run
        stats_idle = human_age(fails["silent_for"])

    contracts = []
    # newest-recorded first inside each referent-proximity group: the contracts captured
    # most recently describe the code as it stands now (G6/G7 ahead of G1/G2)
    live = sorted(active or [], key=lambda r: (
        0 if _under(resolve_referent(r["source_path"]), repo) else 1, -r["seq"]))
    for rec in live:
        ref = resolve_referent(rec["source_path"])
        contracts.append({"subject": rec["subject"], "predicate": rec["predicate"],
                          "object": cut(rec["object"], OBJ_CHARS),
                          "ref": f'{short_path(ref, repo)}:{rec["line_range"]}',
                          "class": rec["class"]})

    silent = [] if facts_available else ["memory.db"]
    if fails is None:
        silent.append("stats.db")

    return {
        "repo": str(repo), "repo_name": repo.name,
        "silent_sources": silent,
        "git": rows is not None,
        "working": rows if rows is not None else [],
        "dirty_count": len(dirty),
        "evicted": evicted,
        "falsified": falsified,
        "contracts": contracts,
        "plan": plan_pointer(repo),
        "facts": {"available": facts_available, "active": len(active or []),
                  "suspect": len(bad or []), "anchor_drift": len(drift)},
        "stats_idle": stats_idle,
    }


def _under(path, root):
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


# ------------------------------------------------------------ render ----
def section(name, items, cap):
    """One bracketed header, up to `cap` indented items, then a spill marker.

    cap 0 is the header-only stage of the budget squeeze: the section name and its count
    survive as `x40 shown 0`, because the header is the schema - a bare header still says
    where to look, a silently missing section does not, and a count with no rows under it
    must not read like rows were printed.
    """
    if not items:
        return [f"[{name}] none observed"]
    if cap <= 0:
        return [f"[{name}] x{len(items)} shown 0"]
    lines = [f"[{name}]"]
    for row in items[:cap]:
        lines.append("  " + row)
    if len(items) > cap:
        lines.append(f"  +{len(items) - cap} more")
    return lines


def evict_line(row):
    # the reread rule lives in the section header; a row is path + (L, sha, note) only
    bits = [f'L{row["lines"] if row["lines"] is not None else "?"}', row["sha12"] or "?"]
    if row["note"]:
        bits.append(row["note"])
    note_words = (row["note"] or "").replace(":", " ").split()
    tail = (" -> spine: reread in the same call for edit_file.py" if "spine" in note_words
            else "")
    return (f'{row["path"]}{hunk_span(row.get("hunks"))} ({", ".join(bits)})' + tail)


def falsified_line(row):
    bits = (" " + row["bits"]) if row["bits"] else ""
    return f'{row["kind"]}{bits}: {row["head"]}{row["tail"]}'


def working_line(row):
    return cut(row, 88)


def contract_line(row):
    return f'{cut(row["subject"], 44)} {cut(row["predicate"], 28)}: {row["object"]} @{row["ref"]}'


def section_counts(data):
    return {"evicted": len(data["evicted"]), "falsified": len(data["falsified"]),
            "working": len(data["working"] if data["git"] else []),
            "contracts": len(data["contracts"])}


def evict_floor(data, floor_cap=EVICT_LINES):
    """Evict rows the squeeze keeps before it sheds any of them at all.

    The read-before-edit files and the drifted anchors: their sha256 values are the one
    thing in the block that cannot be rebuilt once the file moves, so they are the last
    lines a token budget should take.
    """
    core = [r for r in data["evicted"]
            if any(t in CORE_NOTES for t in (r["note"] or "").replace(":", " ").split())]
    return min(len(core), floor_cap) if core else (1 if data["evicted"] else 0)


def lane_floors(data, counts):
    """Rows a lane keeps before any lane is allowed to empty.

    Three blocks the pre-floor tree wrote to ~/.omp/run/sieve on 2026-11-09 (the
    0352Z, 0353Z and 0357Z copies, each at --max-tokens 220) rendered `[DISCOVERED_CONTRACTS]
    x34 shown 0` above `contracts 34->0 ... working 4->0`: a header naming a section the budget
    had deleted, which reads as "this repo has no contracts". The same tree at --max-tokens
    3000 prints all 34 rows with no shed line, so the lane held findings and the budget deleted
    them. Two live contracts and one falsification are the minimum that still says what kind of
    thing is missing; the floors bind only under real pressure, and a raised budget returns
    every row (34/34 at 3000 on the live spine, no [BUDGET_SPILL]).
    """
    return {"evicted": 0, "falsified": 1 if counts["falsified"] else 0,
            "working": 1 if (data["git"] and counts["working"]) else 0,
            "contracts": min(2, counts["contracts"])}


def budget_shed(data, caps, max_tokens=ROW_SCALE_TOKENS):
    """Sections the budget pushed below their requested cap: name -> (total, shown)."""
    counts = section_counts(data)
    scale = max(1, max_tokens // ROW_SCALE_TOKENS)
    defaults = {k: v * scale for k, v in
                (("evicted", EVICT_LINES), ("falsified", FALSIFIED_LINES),
                 ("working", WORKING_LINES), ("contracts", CONTRACT_LINES))}
    return {k: (counts[k], caps[k]) for k in defaults
            if caps[k] < defaults[k] and caps[k] < counts[k]}


def full_budget_tokens(data):
    """The --max-tokens a re-run needs to print every row, measured from this same data.

    A pointer that says "re-run for the full list" without a number sends the model back to the
    default budget and the same shed. Rounding up to the next 500 keeps the figure honest as rows
    shift: on the live spine 2500 still shed while 3000 measured spill-free (2026-11-09).
    """
    counts = section_counts(data)
    caps = {k: counts[k] for k in ("evicted", "falsified", "working", "contracts")}
    return max(1000, ((est_tokens(render(data, caps)) + 499) // 500) * 500)


def spill_line(data, caps, max_tokens):
    """Names what the budget took, so a short block never reads as an empty one."""
    shed = budget_shed(data, caps, max_tokens)
    if not shed:
        return ""
    return ("[BUDGET_SPILL] " + " ".join(f"{k} {total}->{shown}"
                                          for k, (total, shown) in sorted(shed.items()))
            + f" to fit {max_tokens} tokens; all rows at {full_budget_tokens(data)}\n")


def render(data, caps):
    facts_field = (str(data["facts"]["active"]) if data["facts"]["available"]
                   else "unavailable")
    head = (f'SIEVE repo={data["repo_name"]} dirty={data["dirty_count"]} '
            f'facts={facts_field} suspect={data["facts"]["suspect"]} '
            f'drift={data["facts"]["anchor_drift"]}')
    if data.get("stats_idle"):
        head += f' stats=idle({data["stats_idle"]})'
    out = [head]
    out += section("EVICTED_FILE - must reread before edit_file.py",
                   [evict_line(r) for r in data["evicted"]], caps["evicted"])
    out += section("FALSIFIED_IN_SESSION", [falsified_line(r) for r in data["falsified"]],
                   caps["falsified"])
    work = ([] if not data["git"] else
            ["clean"] if not data["working"] else [working_line(r) for r in data["working"]])
    if not data["git"]:
        work = [f'no git repo at {data["repo"]}']
    out += section("ACTIVE_WORKING_SET", work, caps["working"])
    out += section("DISCOVERED_CONTRACTS", [contract_line(r) for r in data["contracts"]],
                   caps["contracts"])
    silent = data.get("silent_sources") or []
    if silent:
        # an empty section because the witness could not be read is not a clean bill of health
        out.append(f'[SOURCES_SILENT] {", ".join(silent)} absent or unreadable: the sections '
                   'above are empty for lack of a witness, not because nothing went wrong')
    elif not (data["evicted"] or data["falsified"] or data["contracts"]
              or (data["git"] and data["working"])):
        out.append("[NOTHING_TO_SIEVE] no dirty file, no fact referent, no suspect claim, no "
                   "failed call in the window: nothing here has to be re-read rather than recalled")
    return "\n".join(out) + "\n"


def render_bounded(data, max_tokens):
    scale = max(1, max_tokens // ROW_SCALE_TOKENS)
    caps = {k: v * scale for k, v in
            (("evicted", EVICT_LINES), ("falsified", FALSIFIED_LINES),
             ("working", WORKING_LINES), ("contracts", CONTRACT_LINES))}
    counts = section_counts(data)
    floors_common = lane_floors(data, counts)
    # stage 1 sheds the rebuildable sections and holds the evict list at its read-before-edit
    # core; stage 2 lets that core fall to one row before the text is ever cut mid-line. Neither
    # stage empties a lane that has rows to show: the budget may shorten a section, not delete it.
    for floors in ({**floors_common, "evicted": evict_floor(data)},
                   {**floors_common, "evicted": evict_floor(data, floor_cap=1)}):
        def fits(c):
            # the spill line is part of the disclosure, so it is paid for inside the
            # budget rather than tipping the block over the cut
            est = est_tokens(render(data, c) + spill_line(data, c, max_tokens))
            return est <= max_tokens
        for _ in range(64):
            if fits(caps):
                break
            shed = next((k for k in SHED_ORDER if caps[k] > floors[k]), None)
            if shed is None:
                break
            caps[shed] -= 1
        if fits(caps):
            break
    text = render(data, caps) + spill_line(data, caps, max_tokens)
    if est_tokens(text) > max_tokens:
        room = max(0, max_tokens * 4 - len("[...TRUNCATED]\n"))
        cut = text.rfind("\n", 0, room) if text.rfind("\n", 0, room) > 0 else room
        text = text[:cut] + "\n[...TRUNCATED]\n"
    if data.get("plan"):
        # line 2, after every cut, and the one line exempt from --max-tokens: a wired plan the
        # session cannot see is a plan it re-reads whole - thousands of lines to learn which
        # node is ready - so this line is priced at PLAN_BUDGET_TOKENS and every other line
        # still has to fit the advertised budget. The exemption is asserted, not assumed.
        head, _, rest = text.partition("\n")
        text = head + "\n  " + data["plan"] + "\n" + rest
    return text, caps


# ------------------------------------------------------ session guards ----
# Boundary amnesia has four deterministic shapes, all fixable from the transcript
# without a model: a task directive the summary paraphrases away, a read-only
# posture that flips to write-approved by recall, a CLI flag whose hyphen drifts
# (`--selftest` where the target parses `--self-test`), and a timeout/probe spin
# loop the next turn re-enters. These extractors read only the session's own
# JSONL transcripts through the resolver session_telemetry owns - never a copy
# of its slug rule - and echo verbatim text, so punctuation survives compaction
# byte-for-byte. No transcript, no output: the block costs zero tokens on a
# session with nothing to pin, and every probe fails open.
GUARD_TOKEN_CAP = 140         # the whole guard block's ceiling, in estimated tokens
GUARD_TAIL_BYTES = 4_000_000  # per-transcript read ceiling; the live directive rides the tail
SPIN_TIMEOUTS = 2             # EXIT=124 / timed out / Killed tool results that trip
SPIN_PROBES = 5               # /tmp scratch .py files touched since session start that trip
TASK_HEADER_RE = re.compile(r"\b(?:TASK|STOP|COURSE-CORRECTION):", re.I)
TASK_ANY_RE = re.compile(r"TASK:")
# an explicit prohibition outranks approval vocabulary: "DO NOT modify" contains the
# word modify, and a summary that forgot the "DO NOT" must not restate write consent
READONLY_RES = (re.compile(r"\bREAD-ONLY\b"), re.compile(r"\bDO NOT modify\b", re.I),
                re.compile(r"\bZERO writes\b", re.I), re.compile(r"\bSTRICTLY FROZEN\b"),
                re.compile(r"\bFROZEN files\b"), re.compile(r"\bREAD ONLY\b"))
WRITE_RES = (re.compile(r"\bWRITE-APPROVED\b"), re.compile(r"\bgit add\b"),
             re.compile(r"\bgit commit\b"), re.compile(r"\bAllowed edit targets\b"),
             re.compile(r"\bpatch approved\b|\bapproval to patch\b", re.I),
             re.compile(r"\bcreate a clean commit\b", re.I))
LOCKED_LINE_RE = re.compile(r"read-only paths|strictly frozen|frozen files", re.I)
SPEC_RE = re.compile(r"[\w.~/-]*specs/[\w./-]+\.md")
BIN_RE = re.compile(r"/(?:usr/)?(?:local/)?bin/[\w.+-]+")
FLAG_RE = re.compile(r"--[A-Za-z][A-Za-z0-9_-]*")
SPIN_RES = (re.compile(r"EXIT=124"), re.compile(r"\btimed out\b", re.I),
            re.compile(r"\bKilled\b"))
# messages the harness authored, not the operator: they quote TASK-shaped prose
# too, and counting them would inflate the supersession tally with our own words
INJECTED_MARKS = ("<pre-compact-sieve>", "System-authored compaction context",
                  "POST-COMPACT SEAM LAWS")


def _telemetry_resolver():
    """The one transcript resolver (session_telemetry), or None: the guards fail
    open rather than invent a second slug rule that could drift from the first."""
    try:
        import session_telemetry
        return session_telemetry
    except Exception:
        return None


def transcript_texts(paths):
    """(user_texts, tool_texts, session_start) from transcript files, oldest first.

    Each file is read from its tail (GUARD_TAIL_BYTES): the live directive and the
    newest failures ride the end, and a 12 s hook window cannot json-parse a whole
    multi-session history. A corrupt or partial line is skipped, never fatal - the
    session_start still comes from the file metadata even when its bytes are not read.
    """
    user, tool = [], []
    start = None
    for p in paths:
        try:
            st = os.stat(p)
        except OSError:
            continue
        created = getattr(st, "st_ctime_ns", st.st_mtime_ns)
        start = created if start is None else min(start, created)
        try:
            with open(p, "rb") as h:
                if st.st_size > GUARD_TAIL_BYTES:
                    h.seek(st.st_size - GUARD_TAIL_BYTES)
                    h.readline()  # drop the partial line the seek landed inside
                blob = h.read()
        except OSError:
            continue
        for raw in blob.split(b"\n"):
            if not raw.strip():
                continue
            try:
                row = json.loads(raw)
            except ValueError:
                continue
            msg = row.get("message") if row.get("type") == "message" else None
            if not isinstance(msg, dict):
                continue
            role = msg.get("role")
            blocks = msg.get("content")
            if not isinstance(blocks, list):
                continue
            text = "\n".join(b.get("text", "") for b in blocks
                             if isinstance(b, dict) and b.get("type") == "text")
            if role == "user":
                if text.strip() and not any(m in text for m in INJECTED_MARKS):
                    user.append(text)
            elif role == "toolResult":
                if text.strip():
                    tool.append(text)
    return user, tool, start


def active_directive(user_texts):
    """(directive, task_turn_count) from the latest substantive user message.

    The directive is the first two non-empty lines, only when the message carries a
    TASK:/STOP:/COURSE-CORRECTION marker - ordinary chatter gets no banner, so an
    absent guard costs zero tokens instead of paraphrasing small talk as an order.
    """
    for text in reversed(user_texts):
        if len(text.strip()) < 40:
            continue
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        if not any(TASK_HEADER_RE.search(ln) for ln in lines[:6]):
            return "", sum(1 for t in user_texts if TASK_ANY_RE.search(t))
        return cut(" | ".join(lines[:2]), 240), sum(1 for t in user_texts if TASK_ANY_RE.search(t))
    return "", 0


def mutation_posture(text):
    """READ-ONLY wins over write consent; the negation is the one a summary drops."""
    if any(r.search(text) for r in READONLY_RES):
        return "READ-ONLY"
    if any(r.search(text) for r in WRITE_RES):
        return "WRITE-APPROVED"
    return ""


def locked_line(text):
    for ln in text.splitlines():
        if LOCKED_LINE_RE.search(ln):
            return cut(ln.strip(), 200)
    return ""


def dedup(rows):
    seen, out = set(), []
    for r in rows:
        if r not in seen:
            seen.add(r)
            out.append(r)
    return out


def pinned_tokens(text):
    """Verbatim paths and flags: hyphens and slashes are syntax, not style."""
    return (dedup(SPEC_RE.findall(text))[:6],
            dedup(m.rstrip(".,;:") for m in BIN_RE.findall(text))[:4],
            dedup(FLAG_RE.findall(text))[:10])


def spin_counts(tool_texts, tmp_root, start_ns):
    timeouts = sum(1 for x in tool_texts if any(r.search(x) for r in SPIN_RES))
    probes = 0
    if start_ns is not None:
        try:
            for f in pathlib.Path(tmp_root).glob("*.py"):
                try:
                    if f.stat().st_mtime_ns >= start_ns:
                        probes += 1
                except OSError:
                    pass
        except OSError:
            pass
    return timeouts, probes


def guards_block(agent, session_cwd, transcripts=(), tmp_root="/tmp"):
    """The pinned-directive block appended after the sieve body, or "" (zero tokens)."""
    if transcripts:
        paths = [p for p in transcripts if os.path.isfile(p)]
    else:
        telem = _telemetry_resolver() if session_cwd else None
        if telem is None:
            return ""
        try:
            paths, _how = telem.resolve_history(str(agent), session_cwd, max_files=4)
        except Exception:
            return ""
    if not paths:
        return ""
    user, tool, start_ns = transcript_texts(paths)
    lines = []
    directive, tasks = active_directive(user)
    if directive:
        lines.append("[ACTIVE-DIRECTIVE] " + directive)
        if tasks >= 2:
            lines.append(f"[PRIOR-TASKS-SUPERSEDED] {tasks - 1} earlier TASK block(s) in this "
                         "session - execute ONLY the latest ACTIVE-DIRECTIVE")
        posture = mutation_posture(user[-1])
        if posture:
            lines.append(f"[MUTATION-POSTURE] {posture} per the latest directive - a boundary "
                         "must never flip READ-ONLY <-> write-approved from recall")
        locked = locked_line(user[-1])
        if locked:
            lines.append("[LOCKED-PATHS] " + locked)
        specs, bins, flags = pinned_tokens(user[-1])
        if specs or bins:
            lines.append("[PINNED-ANCHORS] " + " ".join(specs + bins))
        if flags:
            lines.append("[PINNED-FLAGS] " + " ".join(flags))
    timeouts, probes = spin_counts(tool, tmp_root, start_ns)
    if timeouts >= SPIN_TIMEOUTS or probes >= SPIN_PROBES:
        lines.append(f"[SPIN-LOOP-TRIPWIRE] {timeouts} timeouts / {probes} /tmp probes "
                     "detected - verify exact CLI flags against target source "
                     "(e.g. --self-test vs --selftest) or HALT per 3-Probe Bailout")
    # the cap bites width first, then whole rows, never below the two rows that name
    # the task and its posture; a shed banner is honest, a shed hyphen is the drift
    def emit(rows):
        return "".join(r + "\n" for r in rows)
    guard = emit(lines)
    width = max((len(ln) for ln in lines), default=0)
    while est_tokens(guard) > GUARD_TOKEN_CAP and width > 90:
        width = max(90, width - 30)
        lines = [cut(ln, width) for ln in lines]
        guard = emit(lines)
    for tag in ("[PINNED-ANCHORS]", "[PRIOR-TASKS-SUPERSEDED]", "[LOCKED-PATHS]",
                "[PINNED-FLAGS]", "[SPIN-LOOP-TRIPWIRE]"):  # least load-bearing first
        if est_tokens(guard) <= GUARD_TOKEN_CAP or len(lines) <= 2:
            break
        lines = [ln for ln in lines if not ln.startswith(tag)]
        guard = emit(lines)
    return guard


# ------------------------------------------------- summary hashes ----
# Compaction rewrites prose, it does not run git. A summary that carries a commit hash the
# repo never had reads like evidence and is none (perm-record §43: `f7c5628`, which git
# answers "Not a valid object name"), so the hashes in a candidate summary are re-derived
# from the live object database rather than trusted.
HEX_RE = re.compile(r"(?<![0-9A-Za-z])([0-9a-f]{7,40})(?![0-9A-Za-z])")
# A content digest shares the alphabet with a revision, so a token introduced by one is
# never a commit reference. The window is the phrase introducing the token, not the whole
# line: a summary names a receipt hash and a commit hash on the same line.
DIGEST_HINT_RE = re.compile(r"(sha1|sha224|sha256|sha384|sha512|md5|blake2?|b2sum|checksum"
                            r"|digest|hash)[ =:]*$", re.I)
REVISION_TYPES = ("commit", "tag")   # a blob or tree is not something HEAD can be at
SUMMARY_ROWS = 12                     # verified rows listed before a named count


def commit_refs(text):
    """Revision-looking hex tokens in `text`, first-appearance order, digests excluded.

    Lowercase only, because that is what git prints; at least one letter a-f, so a decimal
    run like `20261115` is not mistaken for a citation and reported as a bad one."""
    out = []
    for m in HEX_RE.finditer(text or ""):
        tok = m.group(1).rstrip(".,;:)")
        if not 7 <= len(tok) <= 40 or tok in out or not re.search(r"[a-f]", tok):
            continue
        if DIGEST_HINT_RE.search(text[max(0, m.start() - 24):m.start()]):
            continue
        out.append(tok)
    return out


def active_repos(repo=None, agent=AGENT):
    """Every repo a session may cite a commit from, in probe order: the resolved repo, the
    spine repo, then each STATE.md Active project. None means git found no worktree."""
    out = []
    for cand in (repo, agent, *(path for _, path in active_projects(agent=agent))):
        if cand is None:
            continue
        root = git_root(cand)
        if root is not None and root not in out:
            out.append(root)
    return out


def resolve_refs(refs, repos, run=subprocess.run):
    """ref -> (repo, object type) at the first repo that names it, else None. Presence only:
    it proves the object exists, never that the prose written about it is true."""
    out = {}
    for ref in refs:
        hit = None
        for root in repos:
            try:
                proc = run(["git", "-C", str(root), "cat-file", "-t", ref],
                           capture_output=True, text=True, timeout=10)
            except (OSError, subprocess.SubprocessError):
                continue  # a missing witness is not a verdict on the hash
            if proc.returncode == 0 and proc.stdout.strip():
                hit = (root, proc.stdout.strip().lower())
                break
        out[ref] = hit
    return out


def summary_hashes(text, repos, run=subprocess.run, cap=SUMMARY_ROWS):
    """(rows, counts) for the revisions a candidate summary cites: unverified rows first and
    never shed, verified rows up to `cap` with a named count for the rest, so a long summary
    cannot spend its budget hiding a bad hash."""
    refs = commit_refs(text)
    kinds = resolve_refs(refs, repos, run=run)
    tried = ", ".join(short_path(r, HOME) for r in repos) or "no git repo in scope"
    good, bad = [], []
    for ref in refs:
        hit = kinds[ref]
        if hit and hit[1] in REVISION_TYPES:
            good.append(f"{ref} verified {hit[1]} in {short_path(hit[0], HOME)}")
            continue
        bad.append(f"{ref} [UNVERIFIED_HASH] " + (
            f"resolves as {hit[1]} in {short_path(hit[0], HOME)}, not a commit or tag"
            if hit else f"no active repo resolves it (tried {tried})"))
    rows = bad + good[:cap]
    if len(good) > cap:
        rows.append(f"+{len(good) - cap} more verified")
    return rows, {"refs": len(refs), "verified": len(good), "unverified": len(bad)}


# --------------------------------------------------------------- cli ----
def build_parser():
    p = argparse.ArgumentParser(
        prog="pre_compact_sieve.py",
        description="Print what must be re-read or re-verified before it is guessed at.",
        epilog="Four sections: [EVICTED_FILE] read-before-edit set (dirty files newest-first, "
               "fact referents, --watch paths; ANCHOR-DRIFT marks a fact whose pinned slice "
               "moved; binaries and run/evidence trees excluded); "
               "[FALSIFIED_IN_SESSION] claims disproved here, failed tool calls, dead "
               "witnesses; [ACTIVE_WORKING_SET] live git dirty set, newest first; "
               "[DISCOVERED_CONTRACTS] live facts as subject + predicate + object + referent.\n"
               "Honesty markers: [SOURCES_SILENT] names a source that could not be read, so an "
               "empty section is never mistaken for an empty history; [NOTHING_TO_SIEVE] states "
               "the sources were read and nothing needed re-reading. A lane holding rows keeps "
               "one at every budget above the line-cut floor; [BUDGET_SPILL] names every section "
               "the budget cut, to what depth, and the budget that prints all rows. A STATE.md "
               "row is marked `spine`: the block already carries it, but "
               "edit_file.py still needs a read of it in the same call. An idle stats.db "
               "prints `stats=idle(<age>)` on the SIEVE line instead of a falsified row; "
               "outside ~/.omp/agent, fact queries are scoped to the repo's own rows. A dirty "
               "row carries its changed line ranges (@L120-140) so the reread is the hunk, not "
               "the file; line 2 names the repo's plan frontier when DAG.json exists.\n"
               "No arguments sieves the resolved repo - OMP_SIEVE_REPO, then run/sieve-repo, "
               "then the first STATE.md Active git project, then cwd (run/sieve-repo.log names "
               "which) - against ~/.omp/agent/memory.db and ~/.omp/stats.db, within "
               "--max-tokens (default 200) estimated tokens. "
               "With --check-summary PATH (- for stdin) it audits a candidate compaction "
               "summary instead: every revision-looking hex token is re-resolved with "
               "`git cat-file -t` against the active repos, and one no repo resolves is "
               "tagged [UNVERIFIED_HASH].\n"
               "A transcript-derived guard block may follow the body: [ACTIVE-DIRECTIVE] "
               "with [PRIOR-TASKS-SUPERSEDED] when earlier TASK turns exist, "
               "[MUTATION-POSTURE] and [LOCKED-PATHS] echoing the latest directive verbatim "
               "so read-only never flips to write-approved across a boundary, "
               "[PINNED-ANCHORS]/[PINNED-FLAGS] carrying specs paths and --flags hyphen-exact, "
               "[SPIN-LOOP-TRIPWIRE] on repeated timeouts or /tmp probe pile-ups; <=140 "
               "tokens, zero when nothing fires.\n"
               "Exit 0 unless --record fails, or --check-summary names a hash no active "
               "repo resolves (exit 3).",
    )
    p.add_argument("--repo", default=None,
                   help="project to sieve (default: OMP_SIEVE_REPO, run/sieve-repo pin, "
                        "STATE.md Active project, then cwd)")
    p.add_argument("--active-project", action="store_true",
                   help="print the first STATE.md Active row whose path is a git repo "
                        "(name<TAB>path) and exit - the resolver spine_check audits with")
    p.add_argument("--resolve-repo", action="store_true",
                   help="print '<path> source=<src> redirected=<r>' for the cwd the hook "
                        "passes (default: this cwd), log it, and exit - the hook and "
                        "spine_check resolve through this one call, never a copy")
    p.add_argument("--agent-dir", default=str(AGENT), metavar="DIR",
                   help="where STATE.md lives for scope resolution (default ~/.omp/agent; "
                        "the harness points it at a throwaway spine)")
    p.add_argument("--repo-pin", metavar="PATH", default=None,
                   help="write PATH to run/sieve-repo as the operator pin ('' clears it) and exit")
    p.add_argument("--memory", default=str(DEFAULT_MEMORY), help="fact store (memory.db)")
    p.add_argument("--stats", default=str(DEFAULT_STATS), help="omp stats.db for failed calls")
    p.add_argument("--sync-stats", action="store_true",
                   help="pull omp stats before reading --stats even when it is not the "
                        "default store (the default store is pulled whenever it lags)")
    p.add_argument("--journal", default=str(DEFAULT_JOURNAL), help="falsified-claim journal")
    p.add_argument("--since", type=int, default=90, metavar="MIN",
                   help="window for failed tool calls (default 90)")
    p.add_argument("--max-tokens", type=int, default=200, metavar="N",
                   help="output budget, estimated at 4 chars/token (default 200)")
    p.add_argument("--watch", action="append", default=[], metavar="PATH",
                   help="add a file to the evict list (repeatable)")
    p.add_argument("--record", metavar="CLAIM", help="append a falsified claim to the journal")
    p.add_argument("--cmd", metavar="CMD", help="the command that disproved it, with --record")
    p.add_argument("--rc", type=int, default=None, help="its exit code, with --record")
    p.add_argument("--flag", default=None, help="the rejected flag, with --record")
    p.add_argument("--check-summary", metavar="PATH", default=None,
                   help="audit a candidate compaction summary instead of the repo: verify "
                        "every commit hash it cites with `git cat-file -t` against the "
                        "active repos and tag the unresolvable ones [UNVERIFIED_HASH] "
                        "('-' reads stdin)")
    p.add_argument("--json", action="store_true", help="emit the same data as JSON")
    p.add_argument("--stamp", action="store_true", help="prefix the output with a UTC timestamp")
    p.add_argument("--session-cwd", default=None,
                   help="session cwd whose transcript JSONL feeds the directive/posture/flag/"
                        "spin-loop guard block (default: OMP_SIEVE_SESSION_CWD, set by the "
                        "compaction hook; neither present means no transcript is read and the "
                        "block costs zero tokens)")
    p.add_argument("--session-transcript", action="append", default=[], metavar="PATH",
                   help="explicit transcript JSONL for the guards (repeatable; fixtures and "
                        "tests bypass the cwd slug resolver)")
    p.add_argument("--tmp-root", default="/tmp", metavar="DIR",
                   help="scratch dir the spin-loop tripwire counts .py probes in (default "
                        "/tmp; tests point it at a fixture)")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    journal = pathlib.Path(args.journal).expanduser()
    agent = pathlib.Path(args.agent_dir).expanduser()
    cli_flag = args.repo
    if args.agent_dir != str(AGENT):
        cli_flag = f"{args.repo}[agent-dir={args.agent_dir}]"
    if args.resolve_repo:
        # the hook passes its session cwd; one resolution implementation, logged once
        repo, source, redirected = resolve_repo(cwd=args.repo, agent=agent)
        log_repo_resolution(repo, source, redirected, cli=cli_flag)
        print(f"{repo} source={source} redirected={redirected}")
        return 0
    if args.active_project:
        found = active_projects(agent=agent)
        if not found:
            print("NO_ACTIVE_PROJECT", file=sys.stderr)
            return 1
        for name, path in found:
            print(f"{name}\t{path}")
        return 0
    if args.repo_pin is not None:
        pin = pathlib.Path(os.environ.get("SIEVE_REPO_PIN_PATH", DEFAULT_PIN)).expanduser()
        try:
            pin.parent.mkdir(parents=True, exist_ok=True)
            if args.repo_pin:
                target = pathlib.Path(os.path.expanduser(args.repo_pin)).resolve()
                if not target.is_dir():
                    print(f"ERROR pin target is not a directory: {target}", file=sys.stderr)
                    return 1
                pin.write_text(str(target) + "\n", encoding="utf-8")
                print(f"PINNED {pin} -> {target}")
            else:
                pin.unlink(missing_ok=True)
                print(f"UNPINNED {pin}")
        except OSError as exc:
            print(f"ERROR cannot write {pin}: {exc.strerror or exc}", file=sys.stderr)
            return 1
        return 0
    if args.record:
        try:
            record(journal, args.record, cmd=args.cmd, rc=args.rc, flag=args.flag)
        except OSError as exc:
            print(f"ERROR cannot write {journal}: {exc.strerror or exc}", file=sys.stderr)
            return 1
        print(f"RECORDED {journal}")
        return 0

    if args.repo is not None:
        # a hook that resolved through --resolve-repo passes the path back with the
        # source still attached, so the audit line names the decision, not just --repo
        src = os.environ.get("OMP_SIEVE_RESOLVED")
        if src:
            repo, source, redirected = pathlib.Path(args.repo), src, src
        else:
            repo, source, redirected = pathlib.Path(args.repo), "cli", "none"
    else:
        repo, source, redirected = resolve_repo()
    log_repo_resolution(repo, source, redirected, cli=args.repo)
    if args.check_summary is not None:
        # the summary is prose, so it is read as-is; a missing file is an error, not a verdict
        if args.check_summary == "-":
            text = sys.stdin.read()
        else:
            try:
                text = pathlib.Path(args.check_summary).expanduser().read_text(
                    encoding="utf-8", errors="replace")
            except OSError as exc:
                print(f"ERROR cannot read --check-summary {args.check_summary}: "
                      f"{exc.strerror or exc}", file=sys.stderr)
                return 1
        repos = active_repos(repo=repo, agent=agent)
        rows, counts = summary_hashes(text, repos)
        print(f"SUMMARY_HASHES refs={counts['refs']} verified={counts['verified']} "
              f"unverified={counts['unverified']} repos={len(repos)} "
              f"agent={short_path(agent, HOME)}")
        for line in section("SUMMARY_HASH", rows, len(rows)):
            print(line)
        return 3 if counts["unverified"] else 0
    stats = pathlib.Path(args.stats).expanduser()
    # the ingest is driven only against the operator's real store: a harness fixture, or any
    # other path handed to --stats, must never be written by the live omp binary
    pull = args.sync_stats or stats.resolve() == DEFAULT_STATS.resolve()
    data = collect(repo, pathlib.Path(args.memory).expanduser(),
                   stats, journal, args.since, args.watch, stats_pull=pull)
    if args.json:
        text, caps = render_bounded(data, args.max_tokens)
        body = dict(data)
        body["shown"] = caps
        body["budget_shed"] = budget_shed(data, caps, args.max_tokens)
        body["truncated"] = text.rstrip("\n").endswith("[...TRUNCATED]")
        body["est_tokens"] = est_tokens(text)
        print(json.dumps(body, ensure_ascii=False))
        return 0
    text, caps = render_bounded(data, args.max_tokens)
    if args.stamp:
        text = f"SIEVE asof={now_iso()}\n" + text
    # the guard block rides after the bounded body and is exempt from --max-tokens,
    # like the plan line above it: it is priced at its own GUARD_TOKEN_CAP ceiling
    guard = guards_block(agent, args.session_cwd or
                         os.environ.get("OMP_SIEVE_SESSION_CWD") or "",
                         args.session_transcript, args.tmp_root)
    sys.stdout.write(text + guard)
    return 0


if __name__ == "__main__":
    sys.exit(main())
