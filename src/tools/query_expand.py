#!/usr/bin/env python3
"""query_expand.py - deterministic query expansion + ranked line-window retrieval.

Why: `read` wants an exact path:N-M. `grep -rn` answers with the first line that contains
the string, which is rarely the line that explains the thing. This tool expands a query the
way the spine's own vocabulary does - identifier case splits, morphological stems, a small
alias map - scores sliding line windows by correlated term density and proximity, and
prints the top-K ranges ready to hand to `read :N-M`.

Deterministic: no randomness, no network, no model call. Same corpus plus same query gives
byte-identical output; the harness pins that.

Usage
  tools/query_expand.py <query> [--roots p1,p2,...] [--top K] [--window N] [--json]
  tools/query_expand.py <query> --fact-boost [--facts DB]   # memory.db steers ranking
  tools/query_expand.py <query> --facts-off                 # say facts are off; never silently
  tools/query_expand.py <query> --route agent|factstore   # named root set; factstore is the
                                                          # default when --fact-boost is
                                                          # passed without --roots
  tools/query_expand.py --print-roots [--route R]         # list effective roots, exit 0
  tools/query_expand.py <query> --class-idf               # rarity per artifact class (arm A5 control)
  tools/query_expand.py <query> --fact-boost --governance --gov-weights FILE   # capped prior (arm A6)
  tools/query_expand.py <query> ... --governance --show-suppressed             # name the rank
                                                          # denials (rank-vs-offer, CG-29/34);
                                                          # refuses without --governance
  tools/query_expand.py                       # no arguments: usage, exit 0

Defaults
  --roots   ~/.omp/agent,~/Projects/amber_ascii
  --top     5      --window 30 lines (stride window//3)
  --facts   ~/.omp/agent/memory.db   (read only with --fact-boost)
  --facts-off  declare facts deliberately unconsulted; contradicts --fact-boost/--facts
  --route      agent (the default pair); factstore (the agent home alone) is the default
               when --fact-boost is passed without --roots; contradicts an explicit --roots
  --print-roots  print the effective roots and exit 0; refuses a query, never searches,
               never logs
Env
  QUERY_EXPAND_LOG   retrieval log path (default run/logs/retrieval.jsonl under the agent
                     home); empty value disables it; a log failure warns and never fails

Semantics
  * Each query token becomes one concept: a set of surface forms. Scoring is tiered by
    DISTINCT concepts, so a window holding 3 of 3 concepts always outranks one repeating a
    single term - COVERAGE dominates every other weight by size, so concept count is a
    strict tier, not a suggestion.
  * Expansion per token: snake_case <-> camelCase <-> kebab-case splits and joins, token
    parts (>= 3 chars), morphological stems (>= 4 chars, prefix-matched in both
    directions), and the built-in spine alias map (parity/exactGrids/digest/max_abs_diff;
    bloom/halation/box_blur; contention/load/F4.1/B2.1/over41_7).
  * Skipping: .git, __pycache__, node_modules, .venv, venv, cache, dist, build*, and any
    directory named run (generated gate output) unless a run/ path is itself passed as a
    root - that is how you search a gate artifact. Also skipped: any directory named
    sessions, which is the agent's own transcript log; it holds every word recently
    discussed, so it would win every query and answer none of them, and its JSONL costs
    more per line than the rest of the corpus put together. Files outside the
    text-extension set, files that are not valid UTF-8, and files over 2 MB are skipped.
    archive/ is NOT skipped: duplicated old copies are corpus noise the ranking has to
    live with, not something the scanner hides.
  * --fact-boost opens memory.db read-only and adds FACT for every LIVE fact - status
    'active', the only status that may steer retrieval - whose pinned slice intersects the
    window, capped at FACT_CAP. FACT * FACT_CAP < COVERAGE by construction: evidence that a
    region is load-bearing can raise a window inside its own concept tier, it can never buy
    a window into a tier its concept coverage does not reach. conditional / contested rows
    are readable history, not live evidence: a claim whose regime, gate or agreement state
    is not settled must not steer retrieval. unverified / stale / falsified / retired rows
    are never read either. Regime validity is upstream: a wrong-regime claim is demoted off
    'active' before this tool is asked, so the reader never re-derives it. Fail-open, never
    silent: a db that cannot be opened, carries no facts table, or pins a slice that cannot
    be read (malformed line_range, missing file) still ranks - the run prints
    `deps: uncomputed` and names the facts; a live pin anchoring no file in the walk is
    named a dead referent. A boosted text row never renders blank: it shows facts=[ids],
    `facts=-`, or `deps: uncomputed`. --facts-off says so in the output instead of leaving
    the absence to be guessed. A relative source_path resolves against the directory
    holding the db.
  * --route names a root set: agent is today's default pair; factstore is the agent home
    alone, and it is the default whenever --fact-boost runs without --roots - a governed
    query reads the governed tree. An explicit --roots overrides the default but contradicts
    --route: an ignored flag must never read as a choice. Under the factstore route a live
    fact whose referent lies outside the route is dropped from boosting and named
    `suppressed referent` in stderr and json - excluded, never silently.
  * Equal value never falls through to path order while the store is consulted: windows
    tie-break on (state, class, path). The documented state order is active, contested,
    conditional, unanchored; the class order is the schema's durability ladder - permanent,
    architectural, project, session - with classes off the ladder mid, and unanchored last.
    Governance never enters the value; it only orders what already ties.
  * Every json row carries score_terms: the exact components behind its score (coverage,
    rarity, anchor, proximity, and fact and gov when facts were consulted). gov is the
    capped additive prior of --governance; without a weight table it is 0.0, because
    governance without a declared table buys nothing. Their float sum, in key order, is
    the score exactly and unrounded; only the text display rounds. Where a row carries
    gov_clamped, the cap has clipped the prior: the components shown still sum to the
    displayed score, but the raw prior exceeded the bound.
  * Every real query appends one JSONL line (ts, query, route, roots, scanned_files, top,
    fact_ids, wall_ms) to the retrieval log, rotated at 1 MiB. The log lives under run/,
    which the walker skips, so it is never its own corpus; a log failure is a warning,
    never a failed query. --print-roots and the bare usage run write nothing.
  * Paths print absolute so `read` can take them verbatim. A window under a root's `run/`

Exit codes
  0  ok, including zero hits; a bare run prints usage
  2  usage: unknown flag, empty query, a named --roots path that cannot be read, a root
     set that yields no readable text file, contradictory facts flags: --facts-off with
     --fact-boost/--facts, or --facts without --fact-boost, or governance contradictions:
     --governance without --fact-boost, --governance without --gov-weights, --gov-weights
     without --governance, --show-suppressed without --governance (an ignored flag must
     not read as a choice). A db that cannot be
     opened is NOT a usage error; see --fact-boost.
"""
import argparse
import bisect
import datetime
import functools
import json
import math
import os
import pathlib
import re
import sqlite3
import sys
import time

EXIT_USAGE = 2
# NF-1 (2026-11-19) came from two registries: this list was ad-hoc two entries while
# spine_check.ROOTS named the projects, so a directory could be visible to one walker and
# invisible to another. FT-48/R3 closed the second list: the registry is spine_check.ROOTS,
# minus the home resolution fallback (scan_roots() is that same exclusion). Unimportable
# spine_check fails loud - falling back to a dead list is how NF-1 started.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
try:
    import spine_check as _spine_check
except ImportError as exc:
    raise SystemExit(f"ERROR registry: spine_check unimportable: {exc}") from exc
DEFAULT_ROOTS = tuple(str(r) for r in _spine_check.scan_roots())
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv", "dist", ".mypy_cache",
             ".pytest_cache", ".ruff_cache", "cache"}
# sessions/ is the agent's own transcript log, not a project artifact: it contains every
# word recently discussed, so it would win every query and answer none of them.
SKIP_NAMES = {"sessions"}
SKIP_PREFIXES = ("build",)
RUN_DIR = "run"
TEXT_EXT = {".py", ".js", ".mjs", ".cjs", ".ts", ".md", ".json", ".jsonl", ".sh", ".bash",
            ".yml", ".yaml", ".txt", ".toml", ".cfg", ".ini", ".html", ".css", ".glsl",
            ".gd", ".c", ".h", ".cpp", ".rs", ".go", ".sql", ".xml", ".svg", ".csv", ".json5"}
MAX_FILE_BYTES = 2_000_000

# Score weights. COVERAGE dominates: the largest possible non-coverage contribution is
# RARITY*concepts*FREQ_CAP + ANCHOR*ANCHOR_CAP + PROXIMITY, far below COVERAGE, so the
# concept count is a strict lexicographic tier.
COVERAGE = 1000.0
RARITY = 1.0
FREQ_CAP = 3
ANCHOR = 20.0
ANCHOR_CAP = 3
PROXIMITY = 60.0
PREVIEW_LEN = 90
MATCHED_CAP = 10
# --fact-boost: one step per live fact pinned over the window, capped so FACT * FACT_CAP
# stays below COVERAGE and the strict concept tier survives evidence.
FACT = 100.0
FACT_CAP = 3
FACT_SHOWN = 3
LIVE_FACT_STATUS = ("active",)  # VS-33: only active steers; load_fact_spans says why
DEFAULT_FACT_DB = str(pathlib.Path.home() / ".omp" / "agent" / "memory.db")
AGENT_HOME = pathlib.Path(__file__).resolve().parent.parent
ROUTES = {"agent": DEFAULT_ROOTS, "factstore": (str(AGENT_HOME),)}
# schema contract C1: the tie-break reads exactly these three statuses; load says why
GOV_FACT_STATUS = ("active", "contested", "conditional")

# The rank-vs-offer matrix (Projects/chromatic-gravity/notes/rank-vs-offer.md, CG-29):
# a file the governed store pins ONLY with facts outside this trio - unverified, stale,
# falsified, retired - is listed off the ranker: it appears in no result row while the
# suppression is named. One rankable fact anywhere in the file restores it (perm-record
# holds F-03 active beside retired F-14 and stays rankable). conditional and contested
# rank WITH FLAG; expired is derived at clock time and rides the same list when the
# governance columns exist (§2.3: advisory, never a stored status).
RANKABLE_FACT_STATUS = frozenset(GOV_FACT_STATUS)
# CG-05 documented orders: active, contested, conditional, unanchored; then the durability
# ladder (chromatic_schema Y); classes off the ladder mid; unanchored last.
STATE_RANK = {"active": 0, "contested": 1, "conditional": 2}
STATE_UNANCHORED = 3
CLASS_RANK = {"permanent": 0, "architectural": 1, "project": 2, "session": 3}
CLASS_MID = 4
CLASS_UNANCHORED = 9
# CG-21 capped additive prior (gravity_model K6): the Y ladder is a per-window max, the
# penalties accumulate, and the sum is clamped to the weight table's GOV_CAP - which
# tools/check_weights.py pins below the tier step, so the prior shifts near-ties and can
# never cross a concept tier. Zero weights therefore cannot separate a zero-coverage
# window from a covered one: the coverage floor is structural, not weighted.
LOG_DEFAULT = AGENT_HOME / "run" / "logs" / "retrieval.jsonl"
LOG_MAX_BYTES = 1024 * 1024
LINE_RANGE_RE = re.compile(r"^L(\d+)-L(\d+)$")

STOP = {"a", "an", "the", "and", "or", "but", "if", "so", "as", "of", "to", "in", "on", "at",
        "by", "for", "with", "from", "into", "about", "is", "are", "was", "were", "be", "been",
        "being", "it", "its", "this", "that", "these", "those", "there", "here", "when",
        "where", "which", "what", "how", "why", "who", "whom", "do", "does", "did", "not",
        "no", "yes", "my", "our", "your", "their", "we", "you", "they", "he", "she", "can",
        "will", "should", "would", "could", "than", "too", "very", "some", "any", "all",
        "just", "also", "get", "gets", "give", "gives", "show", "shows", "tell", "tells"}

# Longest-first so one rule fires per stem step; a stem must itself be >= MIN_STEM long.
SUFFIXES = ("ization", "ational", "ation", "ement", "ment", "ness", "ings", "ing", "ism",
            "ist", "ies", "ied", "ers", "est", "er", "ed", "es", "ion", "al", "ly", "s")
MIN_STEM = 4
MIN_PART = 3

# The spine's own vocabulary, bidirectional. Deliberately small: an alias is a claim about
# this corpus, not general knowledge, and every entry is checkable from a record here.
ALIASES = (
    ("parity", "exactgrids", "exact_grid", "digest", "digests", "max_abs_diff", "maxabsdiff"),
    ("bloom", "halation", "box_blur", "boxblur"),
    ("contention", "load", "loads", "f4.1", "b2.1", "over41_7", "over41"),
)
ALIAS_OF = {}
for _group in ALIASES:
    for _term in _group:
        ALIAS_OF[_term] = frozenset(_group)

TOKEN_RE = re.compile(r"[A-Za-z0-9_]+(?:[._-][A-Za-z0-9_]+)*")
CAMEL_RE = re.compile(r"[A-Z]+(?![a-z])|[A-Z][a-z]*|[a-z]+|\d+")
ANCHOR_RE = re.compile(r"^\s*(?:async\s+)?(?:def|class)\s|^\s*#{1,6}\s")
BACKTICK_RE = re.compile(r"`([^`\n]{1,80})`")
USAGE = __doc__.split("Exit codes")[0].rstrip()


class QueryError(Exception):
    pass


def ident_parts(token):
    """snake/kebab/camel boundaries -> the token's pieces."""
    parts = []
    for chunk in re.split(r"[_.\-]+", token):
        if chunk:
            parts.extend(CAMEL_RE.findall(chunk))
    return parts


@functools.lru_cache(maxsize=None)
def ident_forms(token):
    """Case-style variants of one token: snake, kebab, joined, plus its pieces."""
    forms = {token}
    parts = ident_parts(token)
    if len(parts) > 1:
        forms.add("_".join(parts))
        forms.add("-".join(parts))
        forms.add("".join(parts))
        for part in parts:
            if len(part) >= MIN_PART:
                forms.add(part)
    return forms


def morph_stems(token):
    """Suffix-stripped stems, each at least MIN_STEM long."""
    out = set()
    for suffix in SUFFIXES:
        if token.endswith(suffix):
            stem = token[:-len(suffix)]
            if len(stem) >= MIN_STEM:
                out.add(stem)
    return out


def resolve_pin(base, source, line_range):
    """A pin -> (absolute path, start, end), or None when it cannot be resolved."""
    bounds = LINE_RANGE_RE.match(line_range or "") if source else None
    if not bounds:
        return None
    start, end = int(bounds.group(1)), int(bounds.group(2))
    if end < start:
        start, end = end, start
    claimed = pathlib.Path(source)
    key = str((claimed if claimed.is_absolute() else base / claimed).resolve())
    return (key, start, end) if pathlib.Path(key).is_file() else None


def load_fact_spans(db_path):
    """memory.db -> ({absolute path: [(start, end, fact_id)]}, [uncomputed fact ids]).

    A fact is a claim pinned to a byte slice, so that slice is load-bearing: putting it above
    an unclaimed window is what makes `--why` reachable from a search instead of only from
    memory. VS-33 narrowed LIVE to `active` alone: a conditional or contested claim is
    readable history, not live evidence, and a stale or falsified one is worse than absent.
    Regime validity is upstream - the spine demotes wrong-regime rows off `active` before
    this tool is asked - so the reader takes `active` as "live under the current regime"
    rather than re-deriving it. A pin whose slice cannot be read (missing line_range,
    malformed range, missing file) is never a silent drop: the fact id lands in `uncomputed`
    and the caller prints `deps: uncomputed` naming it. Rows arrive in fact_id order so a
    caller's ranking never depends on the db's physical layout.
    """
    db_file = pathlib.Path(db_path).expanduser()
    try:
        db = sqlite3.connect(f"{db_file.resolve().as_uri()}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise QueryError(f"cannot open fact db {db_file}: {exc}")
    try:
        rows = list(db.execute(
            "SELECT fact_id, source_path, line_range FROM facts WHERE status IN ("
            + ",".join("?" * len(LIVE_FACT_STATUS)) + ") ORDER BY fact_id",
            LIVE_FACT_STATUS))
    except sqlite3.Error as exc:
        raise QueryError(f"fact db {db_file} carries no readable facts table: {exc}")
    finally:
        db.close()
    base = db_file.resolve().parent
    spans = {}
    uncomputed = []
    for fact_id, source, line_range in rows:
        resolved = resolve_pin(base, source, line_range)
        if resolved:
            key, start, end = resolved
            spans.setdefault(key, []).append((start, end, fact_id))
        else:
            uncomputed.append(fact_id)
    return spans, uncomputed


def load_fact_gov(db_path):
    """memory.db -> ({absolute path: [(start, end, fact_id, state_rank, class_rank)]}, uncomputed).

    The tie-break read set is exactly the schema contract C1 trio - active, contested,
    conditional - and it never buys value: governance only orders windows whose value
    already ties. VS-33 holds: contested and conditional steer nothing outright, so they
    add no FACT step and appear in no row's facts list; they only decide, deterministically,
    which of two equal windows the reader sees first. A pin that cannot be read is dropped
    quietly here: a dead governance pin orders nothing, and the live loaders already name
    every unreadable pin they carry.
    """
    db_file = pathlib.Path(db_path).expanduser()
    try:
        db = sqlite3.connect(f"{db_file.resolve().as_uri()}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise QueryError(f"cannot open fact db {db_file}: {exc}")
    try:
        rows = list(db.execute(
            "SELECT fact_id, source_path, line_range, status, frequency_class FROM facts "
            "WHERE status IN (" + ",".join("?" * len(GOV_FACT_STATUS)) + ") ORDER BY fact_id",
            GOV_FACT_STATUS))
    except sqlite3.Error as exc:
        raise QueryError(f"fact db {db_file} carries no readable facts table: {exc}")
    finally:
        db.close()
    base = db_file.resolve().parent
    gov = {}
    uncomputed = []
    for fact_id, source, line_range, status, freq_class in rows:
        resolved = resolve_pin(base, source, line_range)
        if resolved:
            key, start, end = resolved
            gov.setdefault(key, []).append(
                (start, end, fact_id, STATE_RANK.get(status, STATE_UNANCHORED),
                 CLASS_RANK.get(freq_class, CLASS_MID)))
        else:
            uncomputed.append(fact_id)
    return gov, uncomputed


def load_gov_referent_statuses(db_path):
    """memory.db -> ({absolute path: [(fact_id, status)]}, uncomputed) over EVERY status.

    The suppression reader deliberately sees what the boost and prior readers filter out:
    the ranker can only refuse a file it was told about. Rows whose pin cannot be read
    are named in `uncomputed`, never silently dropped - an unreadable pin must not turn
    into a free pass.
    """
    db_file = pathlib.Path(db_path).expanduser()
    try:
        db = sqlite3.connect(f"{db_file.resolve().as_uri()}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise QueryError(f"cannot open fact db {db_file}: {exc}")
    try:
        rows = list(db.execute(
            "SELECT fact_id, source_path, line_range, status FROM facts ORDER BY fact_id"))
    except sqlite3.Error as exc:
        raise QueryError(f"fact db {db_file} carries no readable facts table: {exc}")
    finally:
        db.close()
    base = db_file.resolve().parent
    refs = {}
    uncomputed = []
    for fact_id, source, line_range, status in rows:
        resolved = resolve_pin(base, source, line_range)
        if resolved:
            key, _start, _end = resolved
            refs.setdefault(key, []).append((fact_id, status))
        else:
            uncomputed.append(fact_id)
    return refs, uncomputed


def load_gov_weights(path_s):
    """cg-weights-* table -> ({term: value}, cap).

    The engine refuses to invent a prior: --governance must name its table, because an
    unnamed default applied to every governed query is weight laundering. Terms are read
    by name and unknown names are ignored, so a later schema can ship terms this engine
    has not learned; a missing term reads as 0 (no effect), never as a guess. GOV_CAP is
    mandatory and positive: without the bound the prior is unbounded and K6 is void.
    """
    p = pathlib.Path(path_s).expanduser()
    try:
        w = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"ERROR cannot read gov weights {p}: {exc}")
    if not str(w.get("schema", "")).startswith("cg-weights-"):
        raise SystemExit(f"ERROR gov weights {p} carry no cg-weights-* schema tag")
    terms = w.get("terms")
    if not isinstance(terms, dict) or not terms:
        raise SystemExit(f"ERROR gov weights {p} carry no terms table")
    cap = ((w.get("constants") or {}).get("GOV_CAP") or {}).get("value")
    if not isinstance(cap, (int, float)) or isinstance(cap, bool) or cap <= 0:
        raise SystemExit(f"ERROR gov weights {p} carry no positive constants.GOV_CAP.value")
    vals = {}
    for name, term in terms.items():
        v = term.get("value") if isinstance(term, dict) else term
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            vals[name] = float(v)
    return vals, float(cap)


def _gov_ts(value):
    if not value:
        return None
    s = str(value).strip().replace("Z", "+00:00")
    try:
        dt = datetime.datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.timestamp()


def _gov_now():
    s = os.environ.get("GOV_NOW_ISO")
    if s:
        ts = _gov_ts(s)
        if ts is None:
            raise SystemExit(f"ERROR GOV_NOW_ISO is not an ISO timestamp: {s}")
        return ts
    return time.time()


def load_fact_terms(db_path, vals, now, live_pin):
    """Live facts -> ({path: [(start, end, fact_id, y, r, e, p)]}, uncomputed).

    One row per live fact that resolves, carrying what its governance terms are worth
    on a window: y from the class ladder, r for a regime-pin mismatch inside the
    fact's declared scope (CG-10 fail open: a live dimension the pin cannot see
    mismatches nothing), e for age past the class window (the same TTL table
    --check-expiry reads), and a per-path polluted-source flag. A db that cannot be
    read raises, and the caller fails open exactly as --fact-boost does."""
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    import fact_store as _fs
    try:
        conn = sqlite3.connect(f"file:{pathlib.Path(db_path).expanduser()}?mode=ro",
                               uri=True, timeout=5.0)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT fact_id, source_path, line_range, frequency_class, regime_scope,"
            " regime_pin, revalidated_at, updated_at FROM facts"
            " WHERE status IN ('active', 'conditional', 'contested')").fetchall()
        conn.close()
    except sqlite3.Error as exc:
        raise QueryError(f"cannot read governance facts: {exc}")
    uncomputed = 0
    out = {}
    live_parts = live_pin.split("|")
    base = pathlib.Path(db_path).expanduser().resolve().parent
    for row in rows:
        resolved = resolve_pin(base, row["source_path"], row["line_range"])
        if resolved is None:
            uncomputed += 1
            continue
        path, start, end = resolved
        klass = row["frequency_class"] or ""
        y = vals.get("Y_" + klass, 0.0)
        r = 0.0
        scope = row["regime_scope"] if "regime_scope" in row.keys() else "model"
        positions = _fs._SCOPE_POSITIONS.get(scope, ())
        if positions and vals.get("R_mismatch"):
            blind = any(i >= len(live_parts) or not live_parts[i].strip()
                        for i in positions)
            if not blind and _fs.regime_mismatch(row["regime_pin"] or "", live_pin, scope):
                r = vals["R_mismatch"]
        e = 0.0
        ttl = _fs.EXPIRY_TTL_DAYS.get(klass)
        ts = _gov_ts(row["revalidated_at"] or row["updated_at"])
        if ttl is not None and ts is not None and (now - ts) > ttl * 86400.0:
            e = vals.get("E_expired", 0.0)
        p_flag = ("archive/" in path or "artifacts/" in path or "/tmp/" in path
                  or path.endswith(".bak") or path.endswith(".orig"))
        out.setdefault(path, []).append((start, end, row["fact_id"], y, r, e,
                                         1.0 if p_flag else 0.0))
    return out, uncomputed


def filter_to_roots(spans, route_roots):
    """Drop spans whose file lies outside the route; -> (kept, sorted dropped fact ids)."""
    kept, dropped = {}, set()
    for path, pinned in spans.items():
        p = pathlib.Path(path)
        if any(p == r or r in p.parents for r in route_roots):
            kept[path] = pinned
        else:
            dropped.update(entry[2] for entry in pinned)
    return kept, sorted(dropped)


def build_concepts(query):
    """Query text -> ordered concepts, each a set of surface forms plus their stems."""
    tokens = [t.lower() for t in TOKEN_RE.findall(query)]
    tokens = [t for t in tokens if t not in STOP and (not t.isdigit() or len(t) > 1)]
    tokens = list(dict.fromkeys(tokens))
    if not tokens:
        raise QueryError("empty query: every token was a stop word or a bare digit")
    concepts = []
    for token in tokens:
        forms = {token} | ident_forms(token)
        alias = ALIAS_OF.get(token)
        if alias:
            forms |= set(alias)
        stems = set()
        for form in list(forms):
            stems.add(form)
            stems |= morph_stems(form)
        concepts.append({"key": token, "alias": sorted(alias) if alias else [],
                         "forms": forms, "stems": stems})
    return concepts


def under_run_dir(root):
    """True when the root is (or sits inside) a run directory, which is then fair game."""
    return root.name == RUN_DIR or any(p.name == RUN_DIR for p in root.parents)


def list_files(roots):
    """Roots -> sorted text files, with the skip rules applied."""
    files = []
    for root in roots:
        if root.is_file():
            files.append(root)
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            here = pathlib.Path(dirpath)
            dirnames[:] = sorted(
                d for d in dirnames
                if d not in SKIP_DIRS and d not in SKIP_NAMES
                and not any(d.startswith(p) for p in SKIP_PREFIXES)
                and not (d == RUN_DIR and not under_run_dir(root)))
            for name in sorted(filenames):
                files.append(here / name)
    keep = []
    for path in files:
        if path.suffix.lower() not in TEXT_EXT:
            continue
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                continue
        except OSError:
            continue
        keep.append(path)
    return sorted(set(keep))


def read_text(path):
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if b"\0" in raw[:1024]:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def line_forms(line):
    """A line -> the normalized identifier forms it contains.

    Case-style expansion only pays for itself on tokens that could have more than one
    style; a plain lowercase word has exactly one, so it skips the regex work.
    """
    out = set()
    for raw in TOKEN_RE.findall(line):
        low = raw.lower()
        out.add(low)
        if low != raw or "_" in raw or "-" in raw or "." in raw:
            out.update(ident_forms(low))
    return out


def match_forms(concept, file_tokens, ordered):
    """Which of a concept's forms this file actually contains, stem matches included.

    Forward: a corpus token starting with a stem (entail -> entails).
    Exact:   a stem that is itself a corpus token (transliterat -> transliteration query).
    """
    found = {f for f in concept["forms"] if f in file_tokens}
    for stem in concept["stems"]:
        if stem in file_tokens:
            found.add(stem)
        lo = bisect.bisect_left(ordered, stem)
        hi = bisect.bisect_right(ordered, stem + "\uffff")
        found.update(ordered[lo:hi])
    return found


def scan_file(path, concepts):
    """One file -> (lines, entries, per-concept file flags, allowed forms) or None.

    An entry is (line_index, concept_indices, anchored, matched_surface_forms).
    """
    text = read_text(path)
    if text is None:
        return None
    lines = text.split("\n")
    if len(lines) > 1 and lines[-1] == "":
        lines.pop()  # a trailing newline is punctuation, not a blank line
    per_line = [line_forms(line) for line in lines]
    tokens = set().union(*per_line) if per_line else set()
    if not tokens:
        return lines, [], [0] * len(concepts), [set() for _ in concepts]
    ordered = sorted(tokens)
    allowed = [match_forms(concept, tokens, ordered) for concept in concepts]
    form_to_concepts = {}
    for index, forms in enumerate(allowed):
        for form in forms:
            form_to_concepts.setdefault(form, []).append(index)
    entries = []
    flags = [0] * len(concepts)
    for number, forms in enumerate(per_line):
        hits = set()
        for form in forms:
            for index in form_to_concepts.get(form, ()):
                hits.add(index)
        if not hits:
            continue
        anchored = bool(ANCHOR_RE.search(lines[number]))
        if not anchored:
            for quoted in BACKTICK_RE.findall(lines[number]):
                quoted_forms = line_forms(quoted)
                if any(quoted_forms & allowed[i] for i in hits):
                    anchored = True
                    break
        surfaces = {f for f in forms if f in form_to_concepts}
        for index in hits:
            flags[index] = 1
        entries.append((number, tuple(sorted(hits)), anchored, tuple(sorted(surfaces))))
    return lines, entries, flags, allowed


CODE_SUFFIXES = {".py", ".sh", ".mjs", ".js", ".ts", ".c", ".h", ".go", ".rs"}
PROSE_SUFFIXES = {".md", ".markdown", ".txt", ".rst"}


def artifact_class(path):
    """Artifact class of a scanned file (CG-04, control H3): experiment dumps and
    archived copies separate from code and prose by name alone - no store, no state,
    no trust semantics; this control must beat governance on plumbing, not borrow it."""
    norm = path.replace(os.sep, "/").rstrip("/")
    name = norm.rsplit("/", 1)[-1].lower()
    if name.startswith("out_"):
        return "output"
    if "archive" in norm.lower().split("/"):
        return "archive"
    dot = name.rfind(".")
    suffix = name[dot:] if dot > 0 else ""
    if suffix in CODE_SUFFIXES:
        return "code"
    if suffix in PROSE_SUFFIXES:
        return "prose"
    return "other"


def score_window(entries, lo, hi, idf):
    """Score the entries inside one window; -> (score, concepts, inside) or None."""
    inside = entries[lo:hi]
    if not inside:
        return None
    concepts = set()
    counts = {}
    anchored = 0
    for _, hits, is_anchor, _ in inside:
        for index in hits:
            concepts.add(index)
            counts[index] = counts.get(index, 0) + 1
        anchored += 1 if is_anchor else 0
    span = inside[-1][0] - inside[0][0] + 1
    rarity = sum(idf[i] * min(counts[i], FREQ_CAP) for i in concepts)
    terms = {"coverage": COVERAGE * len(concepts),
             "rarity": RARITY * rarity,
             "anchor": ANCHOR * min(anchored, ANCHOR_CAP),
             "proximity": PROXIMITY * (len(inside) / span)}
    # sum() adds left-to-right in insertion order: the same float chain the score always had.
    return sum(terms.values()), concepts, inside, terms


def preview(lines, inside):
    """The window's best anchor line, flattened to one quoted line."""
    chosen = next((n for n, _, anchor, _ in inside if anchor), inside[0][0])
    text = " ".join(lines[chosen].split())
    if len(text) > PREVIEW_LEN:
        text = text[:PREVIEW_LEN - 3].rstrip() + "..."
    return f"{chosen + 1}: {text}".replace('"', "'")


def log_query(entry):
    """Append one retrieval-log line; never fails a query, never stays silent."""
    path = os.environ.get("QUERY_EXPAND_LOG")
    if path == "":
        return
    try:
        target = pathlib.Path(path).expanduser() if path else LOG_DEFAULT
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and target.stat().st_size > LOG_MAX_BYTES:
            stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            target.rename(target.with_name(f"{target.stem}-{stamp}{target.suffix}"))
        with target.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, sort_keys=True) + "\n")
    except OSError as exc:
        print(f"WARN log: retrieval log not written: {exc}", file=sys.stderr)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        # spine_check --run-harness executes every spine-named script bare, and a script the
        # spine names must exit 0 there; a bad query is still a usage error - see the harness.
        print(USAGE)
        return 0
    ap = argparse.ArgumentParser(prog="query_expand.py", add_help=True,
                                 description="deterministic query expansion + ranked windows")
    ap.add_argument("query", nargs="?", help="natural-language or symbol query")
    ap.add_argument("--roots", action="append", default=[],
                    help="comma-separated files/directories to scan (repeatable)")
    ap.add_argument("--top", type=int, default=5, help="how many windows to print (default 5)")
    ap.add_argument("--window", type=int, default=30, help="window size in lines (default 30)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--fact-boost", action="store_true",
                    help="rank windows over live memory.db referents higher")
    ap.add_argument("--facts", default=None,
                    help=f"fact db for --fact-boost (default {DEFAULT_FACT_DB})")
    ap.add_argument("--facts-off", action="store_true",
                    help="declare facts deliberately unconsulted; contradicts "
                         "--fact-boost/--facts")
    ap.add_argument("--route", choices=sorted(ROUTES), default=None,
                    help="named root set; factstore is the default with --fact-boost "
                         "and no --roots; contradicts --roots")
    ap.add_argument("--print-roots", action="store_true",
                    help="print the effective roots and exit 0; never searches, never logs")
    ap.add_argument("--class-idf", action="store_true",
                    help="score rarity with idf conditional on the artifact class "
                         "(output/archive/code/prose/other); defaults off")
    ap.add_argument("--governance", action="store_true",
                    help="apply the capped additive governance prior of --gov-weights to "
                         "windows over live facts; requires --fact-boost")
    ap.add_argument("--gov-weights", default=None, metavar="FILE",
                    help="cg-weights-* table for --governance; required with it, since an "
                         "unnamed default table would be weight laundering")
    ap.add_argument("--show-suppressed", action="store_true",
                    help="print the rank-vs-offer denials for a governed query (CG-34); "
                         "the human output names each denied file even when the list is "
                         "empty; requires --governance, since only the governed ranker denies")
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        return EXIT_USAGE if exc.code else 0
    if args.print_roots and args.query is not None:
        print("ERROR --print-roots lists roots; it does not answer a query", file=sys.stderr)
        return EXIT_USAGE
    if (args.query is None or not args.query.strip()) and not args.print_roots:
        print("ERROR empty query; run with no arguments for usage", file=sys.stderr)
        return EXIT_USAGE
    if args.route is not None and args.roots:
        print("ERROR --route contradicts --roots; the explicit roots decide, say only one",
              file=sys.stderr)
        return EXIT_USAGE
    if args.top < 1 or args.window < 1:
        print("ERROR --top and --window must be >= 1", file=sys.stderr)
        return EXIT_USAGE
    if args.facts_off and (args.fact_boost or args.facts is not None):
        print("ERROR --facts-off contradicts --fact-boost/--facts; pick one", file=sys.stderr)
        return EXIT_USAGE
    if args.facts is not None and not args.fact_boost:
        print("ERROR --facts is only consulted with --fact-boost; pass --fact-boost or "
              "--facts-off, so an ignored flag never reads as a choice", file=sys.stderr)
        return EXIT_USAGE
    if args.gov_weights is not None and not args.governance:
        print("ERROR --gov-weights is only applied with --governance; an ignored weight "
              "table must not read as a choice", file=sys.stderr)
        return EXIT_USAGE
    if args.governance and not args.fact_boost:
        print("ERROR --governance is a prior over consulted facts; pass --fact-boost or "
              "govern nothing", file=sys.stderr)
        return EXIT_USAGE
    if args.show_suppressed and not args.governance:
        print("ERROR --show-suppressed names the governed ranker's denials; without "
              "--governance there is nothing to show; an ignored flag must not read "
              "as a choice", file=sys.stderr)
        return EXIT_USAGE
    gov_weights = gov_cap = None
    if args.governance:
        if args.gov_weights is None:
            print("ERROR --governance must name its table (--gov-weights FILE); a "
                  "default prior on every governed query is weight laundering",
                  file=sys.stderr)
            return EXIT_USAGE
        try:
            gov_weights, gov_cap = load_gov_weights(args.gov_weights)
        except SystemExit as exc:
            print(str(exc), file=sys.stderr)
            return EXIT_USAGE
    facts_db = args.facts or DEFAULT_FACT_DB
    requested = []
    for chunk in args.roots:
        requested.extend(p for p in chunk.split(",") if p.strip())
    if requested:
        route_name = "explicit"
    elif args.route:
        route_name = args.route
    elif args.fact_boost:
        route_name = "factstore"
    else:
        route_name = "agent"
    effective = requested or list(ROUTES[route_name])
    roots = []
    for raw in effective:
        path = pathlib.Path(raw).expanduser()
        try:
            path = path.resolve()
            if not (path.is_file() or path.is_dir()):
                raise OSError("not found")
            if path.is_dir():
                list(path.iterdir())
        except OSError as exc:
            print(f"ERROR cannot read root {raw}: {exc}", file=sys.stderr)
            return EXIT_USAGE
        roots.append(path)
    if args.print_roots:
        for path in roots:
            print(path)
        return 0
    try:
        concepts = build_concepts(args.query)
    except QueryError as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return EXIT_USAGE
    spans, uncomputed, dead_referents = {}, [], []
    gov = {}
    facts_state = "off"
    boost_reason = None
    suppressed = []
    terms_map, gov_now = {}, None
    if args.fact_boost:
        try:
            spans, uncomputed = load_fact_spans(facts_db)
            gov, _ = load_fact_gov(facts_db)
            if args.governance:
                gov_now = _gov_now()
                live_gov_pin = os.environ.get("GOV_LIVE_PIN", "").strip()
                if not live_gov_pin:
                    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
                    import fact_store as _fs
                    live_gov_pin = _fs.compose_live_pin()
                terms_map, _ = load_fact_terms(facts_db, gov_weights, gov_now,
                                               live_gov_pin)
            facts_state = "live"
        except QueryError as exc:
            # Fail-open, never silent: the ranking survives and says it has no evidence.
            # A silent downgrade would read as "the store had nothing", which is FG-7's shape.
            facts_state = "uncomputed"
            boost_reason = str(exc)
            print(f"WARN deps: uncomputed: {exc}", file=sys.stderr)
    if route_name == "factstore" and spans:
        # A governed query on a route narrower than the corpus: exclusions are named,
        # never absorbed into silence.
        spans, suppressed = filter_to_roots(spans, roots)
        gov, _ = filter_to_roots(gov, roots)
        if terms_map:
            terms_map, _ = filter_to_roots(terms_map, roots)

    stride = max(1, args.window // 3)
    started = time.perf_counter()
    files = list_files(roots)
    if not files:
        print(f"ERROR no readable text file under: {', '.join(map(str, roots))}",
              file=sys.stderr)
        return EXIT_USAGE

    scanned_lines = 0
    per_file = []
    by_path = {}
    flags = [0] * len(concepts)
    class_files = {}
    class_flags = {}
    for path in files:
        found = scan_file(path, concepts)
        if found is None:
            continue
        lines, entries, file_flags, allowed = found
        scanned_lines += len(lines)
        for i, flag in enumerate(file_flags):
            flags[i] += flag
        key = str(path)
        klass = artifact_class(key)
        class_files[klass] = class_files.get(klass, 0) + 1
        cdf = class_flags.setdefault(klass, [0] * len(concepts))
        for i, flag in enumerate(file_flags):
            cdf[i] += flag
        per_file.append((key, klass, lines, entries, allowed))
        by_path[key] = lines
    if facts_state == "live":
        # An anchor that cannot be found is a finding (F15): name it, never drop it quietly.
        for fid in sorted(uncomputed):
            print(f"WARN deps: uncomputed: fact {fid} carries an unreadable pin",
                  file=sys.stderr)
        dead_referents = sorted({fid for key, pinned in spans.items() if key not in by_path
                                 for _, _, fid in pinned})
        for fid in dead_referents:
            print(f"WARN dead referent: fact {fid} anchors no file in this walk",
                  file=sys.stderr)
        for fid in suppressed:
            print(f"WARN suppressed referent: fact {fid} lies outside route factstore",
                  file=sys.stderr)
    total_files = len(per_file)
    idf = [1.0 + math.log(total_files / flags[i]) if flags[i] else 0.0
           for i in range(len(concepts))]
    # Class-aware idf (CG-04, control H3): rarity conditional on the artifact class, so a
    # term the prose notes repeat buys nothing inside prose and keeps its full value in
    # code. Numerator and denominator both come from the class, never the store; with the
    # flag off no window reads these vectors and every score is the A0 float chain.
    idf_class = {}
    for klass in sorted(class_files):
        n_c = class_files[klass]
        cdf = class_flags[klass]
        idf_class[klass] = [1.0 + math.log(n_c / cdf[i]) if cdf[i] else 0.0
                            for i in range(len(concepts))]

    candidates = []
    for path, doc_class, lines, entries, allowed in per_file:
        if not entries:
            continue
        pinned = spans.get(path) or spans.get(os.path.realpath(path)) or ()
        # gov reads independently of spans: a purely dead-store tie (contested vs
        # conditional, no live fact) still has to break on governance.
        gov_pinned = gov.get(path) or gov.get(os.path.realpath(path)) or ()
        n = len(lines)
        starts = list(range(0, max(1, n - args.window + 1), stride))
        if n > args.window and starts[-1] + args.window < n:
            starts.append(n - args.window)
        marks = [e[0] for e in entries]
        for start in starts:
            end = min(start + args.window, n) - 1
            lo = bisect.bisect_left(marks, start)
            hi = bisect.bisect_right(marks, end)
            scored = score_window(entries, lo, hi,
                                  idf_class[doc_class] if args.class_idf else idf)
            if scored is None:
                continue
            value, hits, inside, comp = scored
            ids = {f for lo, hi, f in pinned if lo <= end + 1 and hi >= start + 1}
            fact_step = FACT * min(len(ids), FACT_CAP)
            value += fact_step
            comp = dict(comp, fact=fact_step, gov=0.0) if args.fact_boost else comp
            gov_clamped = False
            if args.fact_boost and gov_weights is not None:
                tf_src = terms_map.get(path) or terms_map.get(os.path.realpath(path)) or ()
                tf = [g for g in tf_src if g[0] <= end + 1 and g[1] >= start + 1]
                y = max((g[3] for g in tf), default=0.0)
                r = sum(g[4] for g in tf)
                e = sum(g[5] for g in tf)
                p = (gov_weights.get("P_artifact", 0.0)
                     if any(g[6] for g in tf) else 0.0)
                raw = y + r + e + p
                gov_clamped = raw > gov_cap or raw < -gov_cap
                gov_v = max(-gov_cap, min(gov_cap, raw))
                if gov_v:
                    value += gov_v
                    comp = dict(comp, gov=gov_v)
                    if not gov_clamped:
                        if y:
                            comp["gov_y"] = y
                        if r:
                            comp["gov_r"] = r
                        if e:
                            comp["gov_e"] = e
                        if p:
                            comp["gov_p"] = p
            anchored = [g for g in gov_pinned if g[0] <= end + 1 and g[1] >= start + 1]
            state = min((g[3] for g in anchored), default=STATE_UNANCHORED)
            klass = min((g[4] for g in anchored), default=CLASS_UNANCHORED)
            terms = set()
            for _, _, _, surfaces in inside:
                terms.update(surfaces)
            candidates.append((-value, path, start, end, value, sorted(terms), inside,
                               sorted(ids), state, klass, comp, gov_clamped))
    # Equal value no longer falls through to path order while the store is consulted:
    # (value, state, class, path, start) - governance orders ties, never buys rank.
    candidates.sort(key=lambda c: (c[0], c[8], c[9], c[1], c[2]))

    # CG-29 rank-vs-offer: suppression happens before the top-N slice, so a suppressed
    # referent frees its slot for the next rankable window - it does not merely lose the
    # tie. Only a governed, store-backed query can suppress; A0/A5 never see this path.
    gov_suppressed = []
    if args.governance and facts_state == "live":
        try:
            ref_status, dead_pins = load_gov_referent_statuses(facts_db)
        except QueryError as exc:
            ref_status, dead_pins = {}, []
            print(f"WARN deps: uncomputed: {exc}", file=sys.stderr)
        for fid in dead_pins:
            print(f"WARN pin: dead-referent: {fid} cannot be read; it cannot suppress",
                  file=sys.stderr)
        offered = {c[1] for c in candidates}
        for path, fl in sorted(ref_status.items()):
            if path not in offered:
                # Pinned but not offered by these roots: there was no window
                # to refuse, and naming it would read as surveillance, not a
                # ranking decision. stderr, payload and SUPPRESSED all draw
                # from this one set.
                continue
            if any(st in RANKABLE_FACT_STATUS for _, st in fl):
                continue
            gov_suppressed.append({
                "path": path,
                "facts": sorted(f for f, _ in fl),
                "statuses": sorted({st for _, st in fl}),
                "why": "every fact pinning this file is unrankable "
                       "(rank-vs-offer matrix, CG-29)"})
        if gov_suppressed:
            supp_paths = {s["path"] for s in gov_suppressed}
            candidates = [c for c in candidates if c[1] not in supp_paths]
            for s in gov_suppressed:
                print(f"WARN rank-suppressed: {s['path']} "
                      f"facts={','.join(s['facts'])} statuses={','.join(s['statuses'])}",
                      file=sys.stderr)

    # FT-48/R3 hub-diversity cap: one live fact buys one returned window. Spending is
    # selection-time: a fact is spent only when a window it pins is kept. A window
    # discarded by the overlap filter (stride < window, so neighbours always overlap)
    # or never reached because --top filled must NOT burn the fact, or the fact buys
    # zero windows and the window it pins lands where its bare concepts rank it. The
    # head candidate is re-taxed to the facts still fresh, re-ranked on the same key,
    # and re-considered; suppression (CG-29 rank-vs-offer) is resolved above.
    kept = []
    used = {}
    if args.fact_boost:
        spent = set()
        pool = sorted(candidates, key=lambda c: (c[0], c[8], c[9], c[1], c[2]))
        while pool and len(kept) < args.top:
            cand = pool[0]
            if any(f in spent for f in cand[7]):
                fresh = [f for f in cand[7] if f not in spent]
                step = FACT * min(len(fresh), FACT_CAP)
                value = cand[4] - cand[10]["fact"] + step
                pool[0] = (-value, cand[1], cand[2], cand[3], value, cand[5], cand[6],
                           sorted(fresh), cand[8], cand[9], dict(cand[10], fact=step),
                           cand[11])
                pool.sort(key=lambda c: (c[0], c[8], c[9], c[1], c[2]))
                continue
            pool.pop(0)
            path, start, end = cand[1], cand[2], cand[3]
            if any(start <= k[1] and end >= k[0] for k in used.get(path, [])):
                continue
            used.setdefault(path, []).append((start, end))
            kept.append(cand)
            spent.update(cand[7])
    else:
        for cand in candidates:
            _, path, start, end = cand[:4]
            if any(start <= k[1] and end >= k[0] for k in used.get(path, [])):
                continue
            used.setdefault(path, []).append((start, end))
            kept.append(cand)
            if len(kept) >= args.top:
                break

    elapsed = round((time.perf_counter() - started) * 1000)
    rows = []
    for rank, cand in enumerate(kept, 1):
        _, path, start, end, value, terms, inside, ids, _state, _klass, comp, \
            gov_clamped = cand
        lines = by_path[path]
        row = {"rank": rank, "score": value, "path": path,
               "start_line": start + 1, "end_line": end + 1,
               "matched_terms": terms, "preview": preview(lines, inside),
               "score_terms": comp}
        if args.fact_boost:
            row["facts"] = ids
        if gov_clamped:
            row["gov_clamped"] = True
        rows.append(row)
    log_entry = {
        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "query": args.query, "route": route_name,
        "roots": [str(r) for r in roots], "scanned_files": total_files,
        "top": [f"{r['path']}:{r['start_line']}-{r['end_line']}" for r in rows],
        "fact_ids": sorted({f for r in rows for f in r.get("facts", [])}),
        "wall_ms": elapsed}

    if route_name == "factstore" and args.fact_boost and not args.json:
        print("roots: factstore (--fact-boost default; --print-roots lists, --roots overrides)")
    if args.facts_off and not args.json:
        print("facts: off (--facts-off)")
    if args.json:
        payload = {
            "query": args.query,
            "expanded_terms": {c["key"]: sorted(c["forms"]) for c in concepts},
            "aliases": {c["key"]: c["alias"] for c in concepts if c["alias"]},
            "idf": {c["key"]: round(idf[i], 3) for i, c in enumerate(concepts)},
            "scanned_files": total_files,
            "scanned_lines": scanned_lines,
            "elapsed_ms": elapsed,
            "route": route_name,
            "results": rows}
        if args.governance and facts_state == "live":
            payload["suppressed_referents"] = gov_suppressed
        if args.class_idf:
            payload["class_idf"] = {
                k: {"files": class_files[k],
                    "idf": {c["key"]: round(idf_class[k][i], 3)
                            for i, c in enumerate(concepts) if idf_class[k][i]}}
                for k in sorted(class_files)}
        if args.governance and facts_state == "live" and any(
                r.get("gov_clamped") or r["score_terms"].get("gov") for r in rows):
            payload["governance"] = {
                "weights_file": str(pathlib.Path(args.gov_weights).expanduser()),
                "cap": gov_cap,
                "clamped_windows": sum(1 for r in rows if r.get("gov_clamped")),
                "now": datetime.datetime.fromtimestamp(
                    gov_now, datetime.timezone.utc).isoformat(timespec="seconds"),
            }
        if args.fact_boost:
            if facts_state == "uncomputed":
                payload["fact_boost"] = {"state": "uncomputed",
                                         "db": str(pathlib.Path(facts_db).expanduser()),
                                         "reason": boost_reason}
            else:
                payload["fact_boost"] = {"state": "live",
                                         "db": str(pathlib.Path(facts_db).expanduser()),
                                         "live_facts": sum(len(v) for v in spans.values()),
                                         "weight": FACT, "cap": FACT_CAP,
                                         "uncomputed": sorted(uncomputed),
                                         "dead_referents": dead_referents,
                                         "suppressed": suppressed}
        elif args.facts_off:
            payload["fact_boost"] = {"state": "off", "declared": True}
        print(json.dumps(payload, indent=1))
        log_query(log_entry)
        return 0

    for row in rows:
        terms = row["matched_terms"]
        shown = ",".join(terms[:MATCHED_CAP])
        if len(terms) > MATCHED_CAP:
            shown += f",+{len(terms) - MATCHED_CAP} more"
        claims = ""
        if args.fact_boost:
            # Never blank: the row renders its facts state, not the absence of one.
            if facts_state == "uncomputed":
                claims = "  deps: uncomputed"
            elif row["facts"]:
                names = ",".join(row["facts"][:FACT_SHOWN])
                if len(row["facts"]) > FACT_SHOWN:
                    names += f",+{len(row['facts']) - FACT_SHOWN} more"
                claims = f"  facts=[{names}]"
            else:
                claims = "  facts=-"
        print(f"RANK {row['rank']}/{args.top}  SCORE {row['score']:.2f}  "
              f"{row['path']}:{row['start_line']}-{row['end_line']}  "
              f"matched=[{shown}]  preview=\"{row['preview']}\"{claims}")
    offered = sum(r["end_line"] - r["start_line"] + 1 for r in rows)
    print(f"SCANNED {total_files} files ({scanned_lines} lines) in {elapsed} ms -> "
          f"top {len(rows)} windows ({offered} lines)")
    if args.show_suppressed:
        # CG-34: the denial is a claim, and a claim states its emptiness. The JSON
        # payload carries suppressed_referents unconditionally under --governance.
        if not gov_suppressed:
            print("SUPPRESSED none")
        for s in gov_suppressed:
            print(f"SUPPRESSED {s['path']} facts={','.join(s['facts'])} "
                  f"statuses={','.join(s['statuses'])} why={s['why']}")
    log_query(log_entry)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        # `query_expand.py ... | head -1` is a normal call; silence the flush at shutdown
        # instead of dumping a traceback on the caller.
        try:
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        except OSError:
            pass
        sys.exit(0)
