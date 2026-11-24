#!/usr/bin/env python3
"""fact_store.py — the chromatic fact store: grounded state, bidirectional transitions.

Design rule, same as spine_check.py: a rule the model has to remember is a rule it
eventually breaks. What this tool makes impossible instead:

  * A fact with no referent. Every fact carries `source_path` + `line_range` + the
    SHA-256 of the exact UTF-8 byte slice those lines occupy. `--check-anchors`
    re-reads the slice and re-hashes it, so a fact whose source moved is found, not
    trusted (Item F15: notes_f15.md — a claim can name a real file and still be wrong).
  * A claim that hardens by repetition. `frequency_class` (how often the claim must be
    re-derived: session < project < architectural < permanent) is separate from
    `status` (whether it currently holds). Promoting anything above `session` with
    status `active` requires a rationale, an evidence referent and a runnable gate; the
    transition is appended to an immutable ledger, so the answer to "why do we believe
    this" is a query (`--why`), not a memory.
  * Uncertainty as a second-class citizen. `contested` is a status with first-class
    storage: competing claims and the command that adjudicates them live in the row,
    and `--query` refuses to print a contested fact without the guardrail banner.
  * A regime change that leaves stale architecture looking true. `--check-regime`
    demotes `architectural`/`permanent`/`project` facts pinned to a different regime to
    `conditional` — "revalidation required" — and prints the gates that revalidate them.
    `project` is in scope because a project fact is exactly one that outlives its session
    (VS-13); `session` facts are excluded — `--evict-session` retires them first.
  * A promotion justified by a line in a transcript. `--run-gate` archives the gate's
    verbatim output under `archive/gates/` and SHA-pins it, and the ledger's
    `evidence_referent` names that slice; `--bind-receipt` moves the fact's anchor onto
    the receipt too, for claims whose referent *was* a transcript line. `--check-evidence`
    re-hashes the archived outputs, so evidence damaged after the promotion is found,
    not trusted — and `--check-anchors` runs it, so the spine refuses a store whose
    receipts no longer read.

Anchor definition (pinned by harness/fact_store_harness.py):
  sha256_pin = sha256 of the contiguous bytes of `source_path` from the first byte of
  line `L<start>` through the last byte of line `L<end>`, lines counted 1-indexed and
  separated by 0x0A only (the same numbering an editor shows), the final line's
  terminator included when the file has one.

Modes (exactly one per call; `--db PATH` may precede any of them):
  --init                                   create/refresh the schema (idempotent)
  --add S P O --class C --source path:Ls-Le [--status st] [--regime pin]
             [--claims JSON] [--gate CMD]              upsert a fact and pin its anchor
  --transition ID_OR_SUBJECT --direction D --to-status S [--to-class C]  (S required for promote/demote, never = current)
             --why W --evidence E --gate G [--run-gate] [--bind-receipt] [--claims JSON]
  --repin ID_OR_SUBJECT --why W --evidence E --gate G [--run-gate] [--bind-receipt]
  --query FTS [--class C] [--include-contested] [--include-retired]
              [--include-falsified] [--limit N] [--json]
  --why ID_OR_SUBJECT                      full causal lineage, oldest first
  --evict-session                          retire session:unverified rows (checkpoint.sh)
  --check-anchors                          exit 1 if any active referent drifted;
                                           also verifies archived gate receipts
  --check-evidence                         exit 1 if an archived gate output is gone,
                                           damaged, or exited nonzero
  --check-spine [--spine-dir DIR]          advisory: every measured-looking number the
                                       spine states (gate scores, comma counts, %) must
                                       bind to a live fact, a file the same spine line
                                       cites, a gate's newest sweep row, or the record; exit 0, prints buckets
  --strict-spine [--spine-dir DIR]         same scan; exit 1 unless every number binds to
                                       a live fact, cited referent or ledger row (the record alone
                                       is reported as weak binding, not accepted)
  --check-regime [--live-pin PIN]          demote cross-regime architectural/
                                       permanent/project claims, exit 1
  --live-pin [PIN]                         print the live regime pin: explicit PIN,
                                       else composed OMP_MODEL_ID|OMP_RUNTIME_ID|probe

Bare (no mode) prints the store census and exits 0, so `--run-harness` can smoke it.

--stale lists every claim sitting in a historical status and prints the command that
re-derives it; --contested-facts prints each contested claim, its rivals and the
adjudicating command. Both are read-only and advisory: a historical claim is not a live
claim, so neither fails a run, and neither may decide which claim wins.
A receipt lost to archive rotation is informational when the current sweep's loop-ledger
row shows that gate exiting 0 with a passing score: the ledger row IS the run, and the
archive is a copy of it. A receipt reporting a failing or checkless run is a failure
whatever the ledger says.
Adjudication is recorded by --transition --corrected-by NAME --correction-reason TEXT
(spine_check --corrected/--confirmed route there): the status stays an evidence verdict,
and who adjudicated, when and why are stamped beside it.

Exit codes: 0 ok / nothing to act on, 1 stale anchors, broken gate evidence, regime
drift or demotions happened, or nothing matched, 2 usage or policy rejection.

Latency: every mode prints the in-process store time in ms, excluding interpreter
start. The <15 ms query and <5 ms anchor-flip budgets are measured against that
number, because a process-spawn figure measures the loader, not the store.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import pathlib

AGENT = pathlib.Path(__file__).resolve().parent.parent
# One name for one knob: spine_check.py and this store's own harness both point FACT_STORE_DB
# at a database. An env var nothing else reads is worse than no env var, because it looks honored.
DEFAULT_DB = pathlib.Path(os.environ.get("FACT_STORE_DB") or (AGENT / "memory.db"))

# The regime this store's facts were measured under. VS-10 (S1) retired the old
# hardcoded value: it kept pinning a regime this box had left (Qwen3.8-125B-IQ3_XXS)
# while the spine stayed green - FG-7. The default is now the line published by
# tools/regime_sensor.py from live observables; OMP_REGIME_PIN still wins, so a
# harness or one-off run pins its own regime explicitly. With neither source,
# --add refuses (guard in upsert) rather than pin a guess.
def _published_regime() -> str:
    try:
        return (AGENT / "run" / "sensors" / "S1.pin").read_text(encoding="utf-8").strip()
    except OSError:
        return ""

DEFAULT_REGIME_PIN = os.environ.get("OMP_REGIME_PIN") or _published_regime()

# CG-10: the live pin for regime checks is COMPOSED per dimension, never one env
# string the operator has to keep true (architecture_inventory.md §5: a stale
# whole-pin equality demoted 70 % of the store at once). model and runtime come
# from OMP_MODEL_ID / OMP_RUNTIME_ID; the device set is probed. A missing
# dimension stays empty and the check fails OPEN - an unseen dimension is not a
# known difference, so a half-known live regime must never demote a store.
def compose_live_pin() -> str:
    model = os.environ.get("OMP_MODEL_ID", "").strip()
    runtime = os.environ.get("OMP_RUNTIME_ID", "").strip()
    # FT-48/R2: an unstated dimension is not an invisible one. regime_sensor
    # derives model and engine from live observables (VS-13's S1); env still
    # wins when the operator states it. Only a dimension neither stated nor
    # derivable stays empty - silently-empty env dims are how a dead engine
    # string survived every sweep (FT-46: 46 of 57 store facts skipped).
    if not (model and runtime):
        try:
            import regime_sensor
        except Exception as exc:
            regime_sensor = None
            print(f"live-pin: regime_sensor unavailable ({exc})", file=sys.stderr)
        if regime_sensor is not None:
            if not model:
                try:
                    model = regime_sensor.derive_model()
                except Exception as exc:
                    print(f"live-pin: model derivation failed ({exc})",
                          file=sys.stderr)
            if not runtime:
                try:
                    runtime = regime_sensor.derive_engine()
                except Exception as exc:
                    print(f"live-pin: engine derivation failed ({exc})",
                          file=sys.stderr)
    try:
        from regime_sensor import derive_gpus
        device = derive_gpus()
    except Exception as exc:
        print(f"live-pin: device probe failed ({exc}); device dimension empty",
              file=sys.stderr)
        device = ""
    missing = [n for n, v in (("OMP_MODEL_ID", model),
                              ("OMP_RUNTIME_ID", runtime)) if not v]
    if missing:
        joined = " and ".join(missing)
        print(f"live-pin: {joined} unset and underivable; live pin incomplete, "
              "regime check fails open on those dimensions", file=sys.stderr)
    return "|".join((model, runtime, device))

# chromatic_schema.md §3: regime is a scoped predicate, not whole-pin equality. A pin
# is model|runtime|device, positional; the scope names which positions a claim depends
# on, so a model swap cannot demote a hardware tuning note and a GPU swap cannot
# demote a model behaviour note. Default 'model' keeps today's demote-on-model-swap.
REGIME_SCOPES = ("global", "hardware", "model", "window", "env")
# FT-48/R2: a model claim is made UNDER an engine, so scope "model" is
# positions 0+1. The comment lives on its own line: inline here it once
# swallowed the hardware entry into the comment and the dict compiled
# without it (KeyError at sweep time).
_SCOPE_POSITIONS = {"global": (), "model": (0, 1), "hardware": (2,),
                    "window": (0, 1), "env": (1, 2)}

def regime_mismatch(pinned: str, live: str, scope: str = "model") -> bool:
    """True when a load-bearing position of the scope differs. A pin too short to
    carry a position the scope needs counts as a mismatch: a truncated pin cannot
    shelter a claim from the dimension it claims to depend on."""
    p, l = pinned.split("|"), live.split("|")
    return any(i >= len(p) or i >= len(l) or p[i] != l[i]
               for i in _SCOPE_POSITIONS[scope])

CLASSES = ("session", "project", "architectural", "permanent")
CLASS_ORDER = {c: i for i, c in enumerate(CLASSES)}
STATUSES = ("unverified", "contested", "active", "conditional", "stale",
            "falsified", "retired")
DIRECTIONS = ("promote", "demote", "contest", "falsify")

# A historical status records what an earlier derivation said. It answers a question about
# the past; it MUST NOT steer the present, and the tool says so wherever it prints one.
HISTORICAL_STATUSES = ("stale", "retired", "falsified")
STATUS_USAGE_NOTE = ("STATUS NOTE   stale/retired/falsified are historical: they record what"
                     " an earlier derivation said and MUST NOT steer current work.")
STATUS_USAGE_FIX = ("            re-derive: --repin ID (the anchor drifted) or --transition ID"
                    " --direction promote --to-status active --why ... --evidence ... --gate"
                    " ... (the claim is re-proved); --superseded-by NEW_ID replaces a retired"
                    " claim outright; --reconcile retires a stale row that a later live claim for"
                    " the same subject+predicate already replaces, which is the common case and"
                    " the one no report used to name. A verdict records --corrected-by NAME"
                    " --correction-reason TEXT; it never carries a status of its own.")
# Only a copy the archive lost outright can be cleared by the ledger: absence is rotation
# damage. A receipt whose bytes are present but wrong (SHA mismatch, no receipt header), or
# whose own run failed or counted nothing, is evidence damage and stays a problem whatever
# the ledger says.
RECEIPT_LOST = ("receipt missing", "receipt unreadable")
LEDGER_ROW_RE = re.compile(r"^(?P<stamp>\S+) head=(?P<head>\S+) gate=(?P<gate>\S+)"
                           r" rc=(?P<rc>-?\d+)\b.*\bscore=(?P<score>\S+)\b"
                           r".*\bsweep=(?P<sweep>\d+)")
PASSING_SCORE_RE = re.compile(r"^\d+/0$")
# Classes whose `active` state is a load-bearing claim, so it must carry proof.
PROVENANCE_CLASSES = ("project", "architectural", "permanent")
# Statuses whose referent must still hash to the pin for the store to be honest.
ANCHORED_STATUSES = ("active", "contested")

# Every state transition names WHY with a machine-findable code before the prose that
# explains it. The 2026-11-11 reachability audit found 247 ledger rows whose rationale was
# prose only: `stale` said a claim stopped being load-bearing but not whether the referent
# was REFUTED by bytes, UNREADABLE, superseded or merely old, and one label carrying all
# four is how a 12-day-old adjudication came to sit under a 27-day-old unreadable referent
# with nothing in the store telling them apart. The code is the axis; the prose stays,
# because a code cannot say which line drifted or which regime ended.
REASON_CODES = {
    "R0_HEALTHY": "the referent re-matched its pin; a label is being withdrawn",
    "R1_CONTENT_DRIFT": "referent readable, its bytes disagree with the pin",
    "R2_REFERENT_UNREADABLE": "referent cannot be read; the pin is undecidable, not refuted",
    "R3_EVIDENCE_LOST": "the archived gate output no longer matches the receipt pin",
    "R4_SUPERSEDED": "a later claim, record or adjudication replaces this one",
    "R5_AGE_DECAY": "the class window elapsed with no revalidation",
    "R6_REGIME_MISMATCH": "the regime the claim was made under no longer holds",
    "R7_RIVAL_CLAIM": "a rival claim of record contradicts this one",
    "R8_REPINNED": "anchor re-stamped against the referent as it now reads",
    "R9_OPERATOR": "operator- or checkpoint-directed change with no automatic trigger",
}

# The rival sentence an unreadable referent records. Absence of evidence is never evidence of
# absence (CORE), so an unreadable referent never takes the R1 stale route: the row is
# contested against the one thing actually in doubt - that its referent can be read - and the
# sweep that can read the slice again heals it back to whatever status the contest buried.
# This text is the heal test, so it stays a single stable string.
R2_CLAIM_MARK = "R2_REFERENT_UNREADABLE"


def reason_code(code: str, prose: str) -> str:
    """`R1_CONTENT_DRIFT: <prose>` - grep finds every drift demotion without a sentence being
    read, and the prose survives because the code cannot say what drifted."""
    if code not in REASON_CODES:
        raise FactError(f"unknown reason code {code!r}; known: {', '.join(REASON_CODES)}")
    return f"{code}: {prose}"


def prefix_for(direction: str, to_state: str, superseded_by: str | None,
               corrected_by: str | None) -> str:
    """The reason an operator-driven transition carries, read off the target state and the
    direction, so no call path can write a ledger row with no reason code at all."""
    if superseded_by or to_state.endswith((":falsified", ":retired")):
        return "R4_SUPERSEDED"
    if direction == "contest" or corrected_by is not None:
        return "R7_RIVAL_CLAIM"          # a verdict adjudicates between rivals
    if direction == "promote" or to_state.endswith(":active"):
        return "R8_REPINNED"
    return "R9_OPERATOR"                 # their --why prose says the rest

SOURCE_RE = re.compile(r"^(?P<path>.+?):L?(?P<start>\d+)(?:-L?(?P<end>\d+))?$")

# Gate receipts: the verbatim output of a gate run, archived, SHA-pinned and
# timestamped. A promotion that ran its gate no longer has to cite a transcript line;
# it cites the slice of the output the run left behind, and `--check-evidence` proves
# those bytes are still the ones that were recorded. tools/plan_check.py and
# tools/entail_check.py import these constants and the read/write functions below, so
# the archive has one format and one implementation.
GATE_ARCHIVE = pathlib.Path(os.environ.get("OMP_GATE_ARCHIVE")
                            or (AGENT / "archive" / "gates"))
RECEIPT_MAGIC = "# OMP-GATE-RECEIPT v1"
RECEIPT_BEGIN = "--- BEGIN OUTPUT ---"
RECEIPT_END = "--- END OUTPUT ---"
# Same anchor grammar as a fact's referent: path:L<start>-L<end>@<hex prefix>.
# A record may abbreviate a long receipt path with a leading ellipsis: loom's notes cite
# `…T042018065766.log:L1-L40@574e21205a6e` for
# `archive/gates/plan--G4A_20261101T042018065766.log`. The marker passes through here and is
# resolved by unique suffix below, so an abbreviated run stays checkable instead of vanishing.
RECEIPT_RE = re.compile(r"([\w./~@…-]+\.log):L(\d+)-L(\d+)@([0-9a-f]{12,64})")
# Only a coordinate the ledger *labels* as evidence is evidence. An `--bind-receipt`
# transition also writes `anchor: <old> -> <new>`, and that old coordinate is history:
# the run it names was legitimately superseded, so checking it would report a repaired
# fact as broken forever.
RECEIPT_LABEL_RE = re.compile(
    r"receipt:\s*([\w./~@-]+\.log:L\d+-L\d+@[0-9a-f]{12,64})")
RECEIPT_HEADER_RE = re.compile(r"^([a-z_]+):\s*(\S.*?)\s*$", re.M)
# The numbers a gate declares about itself. A harness prints `71 checks, 0 fail`; a
# checker prints `checks: 172`; a memory-hungry gate prints `vmhwm_mb: 6051` or the
# `/usr/bin/time -v` form `VmHWM 126.6 MB`. Whatever the phrasing, the receipt records it
# under these leaf names, so a number a record quotes binds to the archived run instead of
# to a summary someone typed into DAG.json (Projects/amber_ascii/perm-record.md §16).
CHECKS_KV_RE = re.compile(r"^checks\s*[:=]\s*(\d+)\s*$", re.M | re.I)
FAIL_KV_RE = re.compile(r"^fail\s*[:=]\s*(\d+)\s*$", re.M | re.I)
CHECKS_TEXT_RE = re.compile(r"(\d+)\s+checks")
FAIL_TEXT_RE = re.compile(r"(\d+)\s+fail")
VMHWM_KV_RE = re.compile(r"^vmhwm_mb\s*[:=]\s*([\d.]+)\s*$", re.M | re.I)
VMHWM_TEXT_RE = re.compile(r"VmHWM\D{0,12}?([\d.]+)\s*MB", re.I)


class FactError(Exception):
    """A caller-facing refusal: usage, policy or an unverifiable argument. Exit 2."""


class AnchorError(Exception):
    """The pinned slice could not be produced: the file cannot be read at all, or the
    range it pins is not there. `ReferentGone` below separates the two, because they refute
    different things."""


class ReferentGone(AnchorError):
    """The file opened and was read, but no longer holds the pinned lines. Reading worked,
    so what came back is evidence: the slice the claim points at is gone, and that refutes
    the claim (R1) the way a mismatch does. It is not the undecidability of a referent that
    could not be opened at all (R2), where neither reading can be checked."""


# --------------------------------------------------------------------------- schema

SCHEMA = """
CREATE TABLE IF NOT EXISTS facts (
    fact_id            TEXT PRIMARY KEY,
    subject            TEXT NOT NULL,
    predicate          TEXT NOT NULL,
    object             TEXT NOT NULL,
    frequency_class    TEXT NOT NULL CHECK (frequency_class IN
                        ('session','project','architectural','permanent')),
    status             TEXT NOT NULL DEFAULT 'unverified' CHECK (status IN
                        ('unverified','contested','active','conditional','stale',
                         'falsified','retired')),
    source_path        TEXT NOT NULL,
    line_range         TEXT NOT NULL,
    sha256_pin         TEXT NOT NULL,
    git_commit         TEXT,
    regime_pin         TEXT NOT NULL,
    competing_claims   TEXT,
    adjudication_gate  TEXT,
    updated_at         TEXT NOT NULL,
    corrected_at       TEXT,
    corrected_by       TEXT,
    correction_reason  TEXT
);
CREATE INDEX IF NOT EXISTS facts_state ON facts (frequency_class, status);

CREATE TABLE IF NOT EXISTS transitions (
    transition_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    fact_id            TEXT NOT NULL REFERENCES facts(fact_id) ON DELETE CASCADE,
    direction          TEXT NOT NULL CHECK (direction IN
                        ('promote','demote','contest','falsify')),
    from_state         TEXT NOT NULL,
    to_state           TEXT NOT NULL,
    why_rationale      TEXT NOT NULL,
    evidence_referent  TEXT NOT NULL,
    harness_gate       TEXT NOT NULL,
    regime_pin         TEXT NOT NULL,
    transitioned_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS transitions_fact ON transitions (fact_id, transition_id);

CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts USING fts5(
    fact_id UNINDEXED, subject, predicate, object,
    frequency_class UNINDEXED, status UNINDEXED);

CREATE TRIGGER IF NOT EXISTS facts_ai AFTER INSERT ON facts BEGIN
    INSERT INTO facts_fts (fact_id, subject, predicate, object, frequency_class, status)
    VALUES (new.fact_id, new.subject, new.predicate, new.object,
            new.frequency_class, new.status);
END;
CREATE TRIGGER IF NOT EXISTS facts_ad AFTER DELETE ON facts BEGIN
    DELETE FROM facts_fts WHERE fact_id = old.fact_id;
END;
CREATE TRIGGER IF NOT EXISTS facts_au AFTER UPDATE OF subject, predicate, object,
        frequency_class, status ON facts BEGIN
    UPDATE facts_fts SET subject = new.subject, predicate = new.predicate,
        object = new.object, frequency_class = new.frequency_class,
        status = new.status
    WHERE fact_id = new.fact_id;
END;
"""


# Additive migration (chromatic_schema.md §4): columns are nullable or DEFAULT'd, so
# existing rows survive untouched; the derived state lives in views, never in a CHECK.
MIGRATION_COLUMNS = [
    ("revalidated_at", "ALTER TABLE facts ADD COLUMN revalidated_at TEXT"),
    ("regime_scope",
     "ALTER TABLE facts ADD COLUMN regime_scope TEXT NOT NULL DEFAULT 'model'"),
    ("residency",
     "ALTER TABLE facts ADD COLUMN residency TEXT NOT NULL DEFAULT 'file'"),
    ("superseded_by", "ALTER TABLE facts ADD COLUMN superseded_by TEXT"),

    ("corrected_at", "ALTER TABLE facts ADD COLUMN corrected_at TEXT"),
    ("corrected_by", "ALTER TABLE facts ADD COLUMN corrected_by TEXT"),
    ("correction_reason", "ALTER TABLE facts ADD COLUMN correction_reason TEXT"),
]
MIGRATION_VIEWS = [
    ("state_view", """CREATE VIEW IF NOT EXISTS state_view AS
SELECT f.fact_id,
       f.status,
       f.frequency_class,
       CASE WHEN f.status = 'retired'                                             THEN 'retired'
            WHEN f.status = 'falsified'                                           THEN 'falsified'
            WHEN f.status = 'stale'                                               THEN 'stale'
            WHEN f.status = 'contested'                                           THEN 'contested'
            WHEN julianday('now') - julianday(coalesce(f.revalidated_at,
                 f.updated_at)) > CASE f.frequency_class
                     WHEN 'project' THEN 30 WHEN 'architectural' THEN 180
                     WHEN 'permanent' THEN 730 ELSE 1e9 END                      THEN 'expired'
            WHEN f.status = 'unverified'                                          THEN 'unverified'
            ELSE 'active' END AS state,
       f.regime_pin, f.regime_scope, f.residency, f.superseded_by,
       f.source_path, f.line_range, f.sha256_pin, f.updated_at
FROM facts f"""),
    ("governance_load",
     """CREATE VIEW IF NOT EXISTS governance_load AS
SELECT state, frequency_class, regime_scope, count(*) n
FROM state_view GROUP BY state, frequency_class, regime_scope"""),
]


def migration_diff(conn: sqlite3.Connection):
    """What --migrate would do: missing columns and views, each as runnable DDL."""
    have = {r[1] for r in conn.execute("PRAGMA table_info(facts)")}
    if not have:
        raise FactError(f"no facts table in this store; run --init first")
    cols = [(n, ddl) for n, ddl in MIGRATION_COLUMNS if n not in have]
    views = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='view'")}
    return cols, [(n, sql) for n, sql in MIGRATION_VIEWS if n not in views]


def apply_migration(conn: sqlite3.Connection):
    """Apply §4.1 then §4.2 in one transaction; idempotent, never a rebuild."""
    cols, views = migration_diff(conn)
    conn.execute("BEGIN")
    try:
        for _name, ddl in cols:
            conn.execute(ddl)
        for _name, sql in views:
            conn.execute(sql)
        conn.execute("COMMIT")
    except Exception:
        conn.rollback()
        raise
    return cols, views


def connect(db: pathlib.Path) -> sqlite3.Connection:
    """Open the store with WAL and FK enforcement; never create the file here."""
    conn = sqlite3.connect(str(db), timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    # WAL + NORMAL is the pairing SQLite documents as crash-safe for committed
    # transactions: only a power loss can drop the last few, and commits stop fsyncing
    # (measured here: 4.9 ms -> 0.03 ms per commit, cold init 45 ms -> 30 ms).
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()
    # A fresh store is born migrated (chromatic_schema.md §5.3): same routine as
    # --migrate --apply, no version table, no separate upgrade ceremony.
    apply_migration(conn)
    conn.commit()


def has_store(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='facts'").fetchone()
    return row is not None


# ----------------------------------------------------------------------- grounding

def fact_id_of(subject: str, predicate: str, object_: str) -> str:
    return hashlib.sha256(
        f"{subject}|{predicate}|{object_}".encode("utf-8")).hexdigest()[:12]


def byte_slice(data: bytes, start: int, end: int) -> bytes:
    """The contiguous bytes of lines `start..end` (1-indexed, 0x0A separated)."""
    if start < 1 or end < start:
        raise AnchorError(f"bad line range L{start}-L{end}")
    total = data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0)
    if end > total:
        # An anchor to lines the file does not have is a false claim at pin time, and a
        # file that later shrinks under a clamped pin would look "unchanged" for free.
        raise ReferentGone(f"file has {total} line(s), cannot pin L{start}-L{end}")
    pos = 0
    for _ in range(start - 1):
        nl = data.find(b"\n", pos)
        if nl < 0:
            raise ReferentGone(f"file has {total} line(s), cannot read line {start}")
        pos = nl + 1
    stop = pos
    for _ in range(end - start + 1):
        nl = data.find(b"\n", stop)
        if nl < 0:
            stop = len(data)
            break
        stop = nl + 1
    if stop == pos:
        raise ReferentGone(f"line {start} is past the end of the file")
    return data[pos:stop]


def read_pin(path: pathlib.Path, start: int, end: int) -> str:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise AnchorError(f"referent anchor unreadable (Item F15): "
                          f"{exc.strerror or exc}") from exc
    return hashlib.sha256(byte_slice(data, start, end)).hexdigest()


def parse_source(text: str) -> tuple[pathlib.Path, int, int]:
    """`path:L10-L12` (or `path:10-12`, or a bare `path:L7`) -> (abs path, s, e)."""
    match = SOURCE_RE.match(text)
    if not match:
        raise FactError(f"--source must look like path:L<start>-L<end>, got {text!r}")
    raw = match.group("path")
    start = int(match.group("start"))
    end = int(match.group("end") or start)
    if end < start:
        raise FactError(f"--source range L{start}-L{end} runs backwards")
    path = pathlib.Path(raw).expanduser()
    if not path.is_absolute():
        path = AGENT / path
    return path, start, end


def rel_of(path: pathlib.Path) -> str:
    try:
        return str(path.resolve().relative_to(AGENT))
    except ValueError:
        return str(path)


_GIT_CACHE: dict[str, str | None] = {}


def git_commit_of(path: pathlib.Path) -> str | None:
    """Short HEAD of the repo containing `path`, or None outside a repo. Never fatal."""
    key = str(path.parent)
    if key not in _GIT_CACHE:
        try:
            proc = subprocess.run(["git", "-C", key, "rev-parse", "--short", "HEAD"],
                                  capture_output=True, text=True, timeout=10)
            _GIT_CACHE[key] = proc.stdout.strip() if proc.returncode == 0 else None
        except (OSError, subprocess.SubprocessError):
            _GIT_CACHE[key] = None
    return _GIT_CACHE[key]


def now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def archive_dir_for(db: pathlib.Path, explicit: str | None) -> pathlib.Path:
    """Receipts live beside the store they justify.

    The default store's receipts are the ones the records and the spine name
    (`archive/gates/`); any other store — a harness temporary, a scratch adjudication —
    gets its own folder next to it, so a test run can never write into the durable
    archive and a destroyed store takes only its own evidence with it.
    """
    if explicit:
        p = pathlib.Path(explicit).expanduser()
        # A file-shaped archive dir used to reach mkdir and raise FileExistsError mid-promotion:
        # the gate had already run, the transaction rolled back, and the operator got a traceback
        # instead of a refusal. Name the problem where the argument resolves, before any write.
        if p.exists() and not p.is_dir():
            sys.exit(f"fact_store: --archive-dir must be a directory: {p} exists and is not one")
        return p
    try:
        same = db.resolve() == DEFAULT_DB.resolve()
    except OSError:
        same = False
    return GATE_ARCHIVE if same else db.parent / f"{db.stem}-gates"


def check_claims(raw: str | None) -> str | None:
    """Validate --claims as a JSON array of competing claims; return compact JSON."""
    if raw is None:
        return None
    try:
        value = json.loads(raw)
    except ValueError as exc:
        raise FactError(f"--claims is not valid JSON: {exc}") from exc
    if not isinstance(value, list) or len(value) < 2:
        raise FactError("--claims must be a JSON array naming at least two claims")
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def require(triple: dict[str, str]) -> None:
    missing = [f"--{k}" for k, v in triple.items() if not (v or "").strip()]
    if missing:
        raise FactError(
            f"promoting above session:active requires {', '.join(missing)} to be "
            f"non-empty; a load-bearing claim must name why, what proves it and how")


GATE_KINDS = ("harness", "bench", "probe", "plan", "script", "cmd")


def gate_prefix(gate: str) -> tuple[str | None, str]:
    """Split an explicit taxonomy prefix from a gate string (CG-28).

    `harness:python3 harness/x_harness.py` names its kind; a bare command does not.
    An identifier-shaped prefix that is not a known kind is refused: the typo
    `harnes:...` must not silently downgrade to a `cmd` gate.
    """
    text = (gate or "").strip()
    head, sep, rest = text.partition(":")
    if sep and rest.strip() and re.fullmatch(r"[a-z_]+", head):
        if head in GATE_KINDS:
            return head, rest.strip()
        raise FactError(f"unknown gate kind {head!r}: a gate prefix must be one of "
                        + ", ".join(GATE_KINDS) + " (notes/gate-taxonomy.md)")
    return None, text


def gate_kind(gate: str | None) -> str:
    """CG-28: every gate string classifies into exactly one taxonomy kind.

    Explicit prefix wins; then the path shape; everything else is `cmd` - a raw
    shell command, which the taxonomy prices as the weakest evidence a promotion
    can cite. An empty gate never reaches here: require() refused it already.
    """
    prefix, text = gate_prefix(gate)
    if prefix:
        return prefix
    low = text.lower()
    if "plan_check.py" in low:
        return "plan"
    if "harness" in low:
        return "harness"
    if "bench/" in low:
        return "bench"
    if "run/probe/" in low:
        return "probe"
    if re.search(r"(^|[\s/=(])tools/[\w.-]+\.py", low):
        return "script"
    return "cmd"


def gate_cmd(gate: str) -> str:
    """The command to actually run: the gate with its taxonomy prefix removed."""
    return gate_prefix(gate)[1]


def gate_fields(text: str) -> list[tuple[str, str]]:
    """The numbers a gate declared about itself, in receipt-header order.

    Values stay strings: `vmhwm_mb: 126.6` truncated to an int is a different measurement,
    and the receipt exists to be read back verbatim. A gate that declares none of these is
    not an error — the header then carries no `checks` field, and a checker looking for one
    is entitled to say the run never counted anything.

    The gate's own summary is the TAIL of its output, and one line cannot be scored
    against another: the last tail line that names a count is the summary, and its
    numbers win together. A whole-text or field-major scan let an embedded line
    outrank it — the fact_store harness, which echoes scenario probes containing
    `checks: 0`, was refused by F9 as declaring zero checks while reporting 213/0
    (2026-10-21). Fallback order per field: summary line, then tail lines, then
    whole-text, so logs whose summary sits elsewhere still parse.
    """
    tail = [line for line in text.splitlines() if line.strip()][-5:]

    def summary_line():
        for line in reversed(tail):
            if any(p.search(line) for p in (CHECKS_KV_RE, CHECKS_TEXT_RE,
                                            FAIL_KV_RE, FAIL_TEXT_RE)):
                return line
        return None

    def declared(kv, tx, hay=None):
        lines = [hay] if hay is not None else tail
        for line in reversed(lines):
            for pattern in (kv, tx):
                if (match := pattern.search(line)):
                    return match.group(1)
        if hay is None:
            for pattern in (kv, tx):
                if (match := pattern.search(text)):
                    return match.group(1)
        return None

    out: list[tuple[str, str]] = []
    summary = summary_line()
    checks = declared(CHECKS_KV_RE, CHECKS_TEXT_RE, summary) if summary else None
    if checks is None:
        checks = declared(CHECKS_KV_RE, CHECKS_TEXT_RE)
    if checks is not None:
        out.append(("checks", checks))
    fail = declared(FAIL_KV_RE, FAIL_TEXT_RE, summary) if summary else None
    if fail is None:
        fail = declared(FAIL_KV_RE, FAIL_TEXT_RE)
    if fail is not None:
        out.append(("fail", fail))
    vmhwm = declared(VMHWM_KV_RE, VMHWM_TEXT_RE)
    if vmhwm is not None:
        out.append(("vmhwm_mb", vmhwm))
    return out


def write_receipt(archive_dir: pathlib.Path, name: str, gate: str, proc,
                  duration_ms: float, regime: str = DEFAULT_REGIME_PIN,
                  filename: str | None = None,
                  commit: str | None = None) -> dict:
    """Archive one gate run verbatim and return the coordinate that names it.

    The file is self-describing — header fields, then the output between two markers —
    so a reader needs no sidecar to know what ran, for how long, on what commit, and so
    tools/entail_check.py can take `checks: 172` or `vmhwm_mb` straight from the bytes.
    Failing runs are archived too: a refused promotion should leave the evidence of why.
    """
    archive_dir.mkdir(parents=True, exist_ok=True)
    when = datetime.datetime.now(datetime.timezone.utc)
    stamp = when.strftime("%Y%m%dT%H%M%S") + f"{when.microsecond:06d}"
    path = archive_dir / (filename or f"{name}_{stamp}.log")
    text_out = (proc.stdout or "") + (proc.stderr or "")
    out = text_out.rstrip("\n").splitlines()
    body = [RECEIPT_MAGIC,
            f"fact_id: {name}",
            f"gate_cmd: {gate}",
            f"exit_code: {proc.returncode}",
            f"duration_ms: {duration_ms:.3f}",
            *(f"{key}: {value}" for key, value in gate_fields(text_out)),
            f"git_commit: {commit or git_commit_of(AGENT / 'CORE.md') or '-'}",
            f"regime_pin: {regime}",
            f"timestamp_utc: {when.strftime('%Y-%m-%dT%H:%M:%SZ')}",
            RECEIPT_BEGIN, *out, RECEIPT_END]
    count = len(body)
    path.write_text("\n".join(body) + "\n", encoding="utf-8")
    pin = read_pin(path, 1, count)
    rel = rel_of(path)
    return {"path": str(path), "rel": rel, "line_range": f"L1-L{count}", "pin": pin,
            "coord": f"{rel}:L1-L{count}@{pin[:12]}", "name": name,
            "exit_code": proc.returncode, "duration_ms": duration_ms}


def receipt_values(text: str) -> dict:
    """The receipt's own header fields, numbers cast. Text before BEGIN OUTPUT."""
    head = text.split(RECEIPT_BEGIN, 1)[0]
    out: dict[str, object] = {}
    for key, val in RECEIPT_HEADER_RE.findall(head):
        try:
            out[key] = float(val)
        except ValueError:
            out[key] = val
    return out


def resolve_abbreviated(raw, roots):
    """Find the one receipt an abbreviated coordinate names. -> (path, "") or (None, why).

    The tail fragment (`…T042018065766.log`) is a *suffix* of the real name
    (`plan--G4A_20261101T042018065766.log`) and is searched under each root's `archive/` tree,
    where archived runs live. Uniqueness is the whole safety argument: a tail matching two
    files is reported ambiguous rather than guessed at.
    """
    frag = raw.lstrip("\u2026").lstrip(".").lstrip("/")
    if not frag.endswith(".log"):
        return None, f"receipt missing ({raw})"
    head, slash, name = frag.rpartition("/")
    found = set()
    for root in roots:
        archive = pathlib.Path(root) / "archive"
        base = archive if archive.is_dir() else pathlib.Path(root)
        try:
            for p in base.rglob(f"*{name}"):
                if not p.name.endswith(name):
                    continue
                if slash and not str(p.relative_to(base)).endswith(frag):
                    continue
                found.add(str(p))
        except OSError:
            continue
    if not found:
        return None, f"receipt missing ({raw})"
    if len(found) > 1:
        return None, f"receipt abbreviation ({raw}) is ambiguous across {len(found)} files"
    return pathlib.Path(next(iter(found))), ""


def verify_receipt(coord: str, roots=()) -> tuple[bool, str, dict]:
    """Re-read the archived gate output a coordinate names. -> (ok, why, header).

    The byte-slice hash is the whole integrity check: bytes that still hash to the pin
    are the run the ledger recorded. A missing file, a pin mismatch and a nonzero exit
    are three different answers, because they call for three different repairs — re-run
    the gate, re-run it and re-bind, or retire the claim.
    """
    match = RECEIPT_RE.search(coord)
    if match is None:
        return False, f"not a receipt coordinate: {coord!r}", {}
    raw, start, end, want = (match.group(1), int(match.group(2)),
                             int(match.group(3)), match.group(4))
    path = pathlib.Path(raw).expanduser()
    if not path.is_absolute():
        for root in (AGENT, *(pathlib.Path(r) for r in roots)):
            if (root / path).is_file():
                path = root / path
                break
        else:
            # exact name failed: an abbreviated coordinate still deserves a real answer
            path, why = resolve_abbreviated(raw, (AGENT, *(pathlib.Path(r) for r in roots)))
            if path is None:
                return False, why, {}
    elif not path.is_file():
        return False, f"receipt missing ({raw})", {}
    try:
        live = read_pin(path, start, end)
    except AnchorError as exc:
        return False, f"receipt unreadable: {exc}", {}
    if not live.startswith(want):
        return False, (f"receipt SHA-256 mismatch: pin {want[:12]} != live "
                       f"{live[:len(want)][:12]}"), {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return False, f"receipt unreadable: {exc}", {}
    if not text.startswith(RECEIPT_MAGIC):
        return False, f"receipt has no {RECEIPT_MAGIC!r} header", {}
    head = receipt_values(text)
    try:
        code = int(float(head.get("exit_code", 1)))
    except (TypeError, ValueError):
        code = 1
    if code != 0:
        return False, f"receipt gate exited {code}", head
    checks = head.get("checks")
    if checks is not None and float(checks) == 0.0:
        return False, ("receipt declares 0 checks (F9): a gate that counted "
                       "nothing cannot certify the claim bound to it"), head
    if checks is None:
        # CG-33 scope rule (gate-taxonomy.md): silence is zero for the harness
        # kind and for no other; a cmd or plan receipt with no count stays as
        # ambiguous as it always was and verifies as before.
        gate = str(head.get("gate_cmd", ""))
        if gate and gate_kind(gate) == "harness":
            return False, ("harness receipt carries no check count (F9/CG-33): "
                           "the run cannot name what it certified"), head
    return True, "", head


def run_gate(gate: str, name: str | None = None,
             archive_dir: pathlib.Path | None = None,
             regime: str = DEFAULT_REGIME_PIN) -> dict | None:
    """Execute a gate, archive its output, and refuse the transition on nonzero exit.

    Archiving is the point. Until now `--run-gate` discarded the output, so a promotion
    could only cite a transcript line, and Item F15 had nothing to re-hash: a damaged
    receipt and an merely old one looked identical. Pass or fail, every run is archived;
    only a passing run is ever bound into evidence.
    """
    started = time.perf_counter()
    try:
        proc = subprocess.run(gate_cmd(gate), shell=True, cwd=str(AGENT),
                              capture_output=True, text=True, timeout=900)
    except subprocess.TimeoutExpired as exc:
        raise FactError(f"gate timed out after 900 s: {gate}") from exc
    duration_ms = (time.perf_counter() - started) * 1000.0
    rec = None
    if archive_dir is not None:
        rec = write_receipt(archive_dir, name or "unbound", gate, proc,
                            duration_ms, regime)
    if proc.returncode != 0:
        tail = ((proc.stdout or "") + (proc.stderr or "")).strip().splitlines()[-3:]
        where = f" (receipt {rec['coord']})" if rec else ""
        raise FactError(f"gate exit {proc.returncode}: {gate}{where}\n    "
                        + "\n    ".join(tail))
    # F9 (CG-33): a clean exit from a gate that declared zero checks certified
    # nothing; the receipt was content-blind. A declared zero refuses, and for the
    # harness kind so does silence: a harness exists to count checks, and a harness
    # run naming none counted nothing. Legacy, plan and bench receipts carry no
    # count field by design and stay as ambiguous as they always were.
    declared = dict(gate_fields((proc.stdout or "") + (proc.stderr or ""))).get("checks")
    where = f" (receipt {rec['coord']})" if rec else ""
    if declared is not None and int(declared) == 0:
        raise FactError(f"gate declared 0 checks (F9): a run that counted "
                        f"nothing cannot certify a promotion{where}")
    if declared is None and gate_kind(gate) == "harness":
        raise FactError(f"harness gate printed no check count (F9/CG-33): a run "
                        f"that never counted anything cannot certify a "
                        f"promotion{where}")
    return rec


def bind_evidence(evidence: str, rec: dict | None, row: sqlite3.Row,
                  bind_receipt: bool) -> tuple[str, str | None]:
    """Append the archived gate output to the evidence string.

    `evidence_referent` is a list of referents in one string, and a receipt coordinate is
    a referent like any other, so this appends instead of replacing: what the operator
    argued, plus the run that proved it. With `--bind-receipt` the fact's anchor moves
    onto the receipt as well, and the transition text keeps the anchor it replaced — the
    migration is itself a claim about the past, and the ledger has to say both halves.
    """
    if rec is None:
        return evidence, None
    coord = rec["coord"]
    text = f"{evidence} | receipt: {coord}"
    if bind_receipt:
        old = f"{row['source_path']}:{row['line_range']}@{row['sha256_pin'][:12]}"
        text += f" | anchor: {old} -> {coord}"
    return text, coord


# ------------------------------------------------------------------------ mutation

def add_fact(conn: sqlite3.Connection, subject: str, predicate: str, object_: str,
             frequency_class: str | None, source: str, status: str | None = None,
             regime: str | None = None, claims: str | None = None,
             gate: str | None = None) -> tuple[dict, bool]:
    path, start, end = parse_source(source)
    try:
        pin = read_pin(path, start, end)
    except AnchorError as exc:
        raise FactError(f"cannot pin {rel_of(path)}:L{start}-L{end}: {exc}") from exc
    fact_id = fact_id_of(subject, predicate, object_)
    existing = conn.execute("SELECT * FROM facts WHERE fact_id = ?",
                            (fact_id,)).fetchone()
    created = existing is None
    if existing is not None:
        # --add creates facts; it never moves one. Re-adding a triple whose referent has
        # moved, or whose class/status differs, would re-pin or re-promote it with no
        # provenance and no ledger row - the exact hole Item F15 exists to close.
        ref = f"{rel_of(path)}:L{start}-L{end}"
        old_ref = f"{existing['source_path']}:{existing['line_range']}"
        if pin != existing["sha256_pin"] or ref != old_ref:
            raise FactError(
                f"fact {fact_id} is already pinned to {old_ref} @ "
                f"{existing['sha256_pin'][:8]}; --add cannot move a referent - "
                f"use --repin {fact_id} --source {ref} with --why/--evidence/--gate")
        for flag, got, want in (("--class", frequency_class, existing["frequency_class"]),
                                ("--status", status, existing["status"])):
            if got is not None and got != want:
                raise FactError(
                    f"fact {fact_id} is already {want}; {flag} {got} is a state change - "
                    f"use --transition {fact_id}, which requires --why, --evidence and "
                    f"--gate and writes the ledger")
    final_class = frequency_class or (existing["frequency_class"] if existing
                                      else "session")
    final_status = status or (existing["status"] if existing else "unverified")
    if final_class not in CLASSES:
        raise FactError(f"--class must be one of {'|'.join(CLASSES)}, got {final_class!r}")
    if final_status not in STATUSES:
        raise FactError(f"--status must be one of {'|'.join(STATUSES)}, got {final_status!r}")
    if final_status == "contested":
        if not claims or not gate:
            raise FactError("status contested needs --claims (JSON array) and --gate "
                            "(the command that adjudicates); uncertainty without a "
                            "rival and a test is decoration")
    effective_regime = regime or DEFAULT_REGIME_PIN
    if not effective_regime:
        raise FactError("no regime pin: run tools/regime_sensor.py (publishes "
                        "run/sensors/S1.pin) or set OMP_REGIME_PIN; a fact written "
                        "under no known regime is FG-7 waiting to happen")
    row = {
        "fact_id": fact_id, "subject": subject, "predicate": predicate,
        "object": object_, "frequency_class": final_class, "status": final_status,
        "source_path": rel_of(path), "line_range": f"L{start}-L{end}",
        "sha256_pin": pin, "git_commit": git_commit_of(path),
        "regime_pin": effective_regime,
        "competing_claims": claims if claims is not None else (
            existing["competing_claims"] if existing else None),
        "adjudication_gate": gate if gate is not None else (
            existing["adjudication_gate"] if existing else None),
        "updated_at": now_iso(),
    }
    conn.execute("""
        INSERT INTO facts (fact_id, subject, predicate, object, frequency_class, status,
                           source_path, line_range, sha256_pin, git_commit, regime_pin,
                           competing_claims, adjudication_gate, updated_at)
        VALUES (:fact_id, :subject, :predicate, :object, :frequency_class, :status,
                :source_path, :line_range, :sha256_pin, :git_commit, :regime_pin,
                :competing_claims, :adjudication_gate, :updated_at)
        ON CONFLICT(fact_id) DO UPDATE SET
            frequency_class = excluded.frequency_class,
            status = excluded.status,
            source_path = excluded.source_path,
            line_range = excluded.line_range,
            sha256_pin = excluded.sha256_pin,
            git_commit = excluded.git_commit,
            regime_pin = excluded.regime_pin,
            competing_claims = excluded.competing_claims,
            adjudication_gate = excluded.adjudication_gate,
            updated_at = excluded.updated_at
    """, row)
    conn.commit()
    return row, created


def last_gate(conn: sqlite3.Connection, fact_id: str, fallback: str) -> str:
    row = conn.execute("SELECT harness_gate FROM transitions WHERE fact_id = ? "
                       "ORDER BY transition_id DESC LIMIT 1", (fact_id,)).fetchone()
    return row["harness_gate"] if row else fallback


def append_transition(conn: sqlite3.Connection, fact_id: str, direction: str,
                      from_state: str, to_state: str, why: str, evidence: str,
                      gate: str, regime: str) -> None:
    conn.execute("""
        INSERT INTO transitions (fact_id, direction, from_state, to_state,
                                 why_rationale, evidence_referent, harness_gate,
                                 regime_pin, transitioned_at)
        VALUES (?,?,?,?,?,?,?,?,?)
    """, (fact_id, direction, from_state, to_state, why, evidence, gate, regime,
          now_iso()))


def resolve(conn: sqlite3.Connection, ident: str) -> sqlite3.Row:
    """A fact by id, or by exact subject when that subject names exactly one fact."""
    row = conn.execute("SELECT * FROM facts WHERE fact_id = ?", (ident,)).fetchone()
    if row is not None:
        return row
    rows = conn.execute("SELECT * FROM facts WHERE subject = ?", (ident,)).fetchall()
    if len(rows) == 1:
        return rows[0]
    if len(rows) > 1:
        raise FactError(f"{ident!r} names {len(rows)} facts; pass the fact_id")
    raise FactError(f"no fact {ident!r} (run --query first; ids are content hashes)")


def transition(conn: sqlite3.Connection, fact_id: str, direction: str,
               to_class: str | None, to_status: str | None, why: str, evidence: str,
               gate: str, run: bool = False, claims: str | None = None,
               regime: str | None = None, bind_receipt: bool = False,
               archive_dir: pathlib.Path | None = None,
               superseded_by: str | None = None,
               corrected_by: str | None = None,
               correction_reason: str | None = None) -> dict:
    row = resolve(conn, fact_id)
    if bind_receipt and not run:
        raise FactError("--bind-receipt needs --run-gate: there is nothing to bind "
                        "until the gate has actually run and been archived")
    if direction not in DIRECTIONS:
        raise FactError(f"--direction must be one of {'|'.join(DIRECTIONS)}, got {direction!r}")
    # Option B (2026-10-20): a direction is an intent, not a target. promote/demote
    # without --to-status used to write a `X -> X` ledger row and report success -
    # a re-stamp masquerading as a transition (the 2026-10-20 revalidation produced
    # eight of them before the census caught it). Require the explicit target; the
    # anti-no-op guard below sits after the state computation so that a class-ladder
    # promotion (project:active -> permanent:active) and a re-contest carrying new
    # rival claims still pass - only a call that changes nothing is a no-op.
    if direction in ("promote", "demote") and to_status is None:
        raise FactError(f"--direction {direction} needs an explicit --to-status: "
                        "without a target the row records "
                        f"{row['status']} -> {row['status']} and proves nothing")
    new_class = to_class or row["frequency_class"]
    new_status = to_status or row["status"]
    if new_class not in CLASSES:
        raise FactError(f"--to-class must be one of {'|'.join(CLASSES)}, got {new_class!r}")
    if new_status not in STATUSES:
        raise FactError(f"--to-status must be one of {'|'.join(STATUSES)}, got {new_status!r}")
    # CG-30: the schema table says `retired -> active: never`, and until this guard
    # that refusal lived only in the design doc - any direction could walk a dead
    # claim back into circulation. Re-learning re-adds a fact with its own provenance.
    if row["status"] == "retired":
        raise FactError(
            f"{row['fact_id']} is retired: retirement is terminal, no transition "
            "leaves it; re-learn the claim with --add so it carries its own provenance")
    if row["status"] == "falsified" and new_status not in ("falsified", "retired"):
        raise FactError(
            f"{row['fact_id']} is falsified: it cannot be revalidated, re-contested "
            f"or re-promoted; falsified -> retired is the only exit, got {new_status!r}")
    from_state = f"{row['frequency_class']}:{row['status']}"
    to_state = f"{new_class}:{new_status}"
    if new_status == "active" and new_class in PROVENANCE_CLASSES:
        require({"why": why, "evidence": evidence, "gate": gate})
    if direction == "contest":
        if not claims or not gate:
            raise FactError("--direction contest needs --claims and --gate: a contested "
                            "record without a rival claim and an adjudicating command "
                            "cannot be resolved, only argued about")
    new_claims = check_claims(claims) if claims is not None else row["competing_claims"]
    new_gate = gate or row["adjudication_gate"]
    if direction == "contest":
        new_gate = gate
    # CG-28: a promotion writes `active`, so its gate must classify into a named
    # taxonomy kind, and the kind is echoed so the operator sees what sort of proof
    # the claim is standing on. Demotions and retirements keep their own bars (why,
    # evidence): they withdraw a proof rather than cite one.
    kind = None
    if new_status == "active" and new_class in PROVENANCE_CLASSES:
        kind = gate_kind(new_gate)
    # CG-32: supersession is recorded at the terminal transition - the retired or
    # falsified row names the fact that replaced it, so --lineage on the old claim
    # shows where the knowledge went instead of only that it died.
    new_superseded = row["superseded_by"]
    if superseded_by is not None:
        target = resolve(conn, superseded_by)
        if target["fact_id"] == row["fact_id"]:
            raise FactError("--superseded-by cannot name the fact itself")
        if new_status not in ("retired", "falsified"):
            raise FactError(f"--superseded-by records a replacement at a terminal "
                            f"transition; {to_state} is not one")
        new_superseded = target["fact_id"]
    if corrected_by is not None and not (correction_reason or "").strip():
        raise FactError("--corrected-by needs --correction-reason: a verdict that does not"
                        " say why is a status change with no record")
    if (new_class == row["frequency_class"] and new_status == row["status"]
            and new_claims == row["competing_claims"]
            and new_gate == row["adjudication_gate"]
            and (corrected_by is None or corrected_by == row["corrected_by"])):
        raise FactError(f"no-op self-transition refused: {direction} leaves {from_state} "
                        "exactly as it is - class, status, claims and gate all unchanged; "
                        "if the intent is a re-stamp, that is what --repin does")
    # --regime on a revalidation re-stamps the fact: it was just measured again, under
    # this regime. Without that, a regime-shift demotion could never be undone.
    new_regime = regime or row["regime_pin"]
    rec = (run_gate(gate, row["fact_id"], archive_dir, new_regime)
           if run else None)
    evidence, receipt = bind_evidence(evidence, rec, row, bind_receipt)
    ts = now_iso()
    sets = ["frequency_class = ?", "status = ?", "competing_claims = ?",
            "adjudication_gate = ?", "regime_pin = ?", "updated_at = ?"]
    vals: list[object] = [new_class, new_status, new_claims, new_gate, new_regime, ts]
    # `revalidated_at` is the age-decay clock (fact_store_harness S16, query_expand's
    # governance ranking), and until now nothing ever wrote it, so every row aged from its
    # first stamp however often it was re-proved. A promote is the event that re-adopts a
    # claim on evidence named in the same call, so it starts the clock; demote, contest and
    # falsify re-check nothing and must leave the stamp where it is rather than launder it.
    if direction == "promote":
        sets.append("revalidated_at = ?")
        vals.append(ts)
    if rec is not None and bind_receipt:
        sets += ["source_path = ?", "line_range = ?", "sha256_pin = ?",
                 "git_commit = ?"]
        vals += [rec["rel"], rec["line_range"], rec["pin"],
                 git_commit_of(pathlib.Path(rec["path"]))]
    if new_superseded != row["superseded_by"]:
        sets.append("superseded_by = ?")
        vals.append(new_superseded)
    if corrected_by is not None:
        # An adjudication stamp, not a status: the status says what the evidence decided,
        # this says who adjudicated and why, so a verdict never needs a status of its own.
        sets.append("corrected_at = ?")
        vals.append(now_iso())
        sets.append("corrected_by = ?")
        vals.append(corrected_by)
        sets.append("correction_reason = ?")
        vals.append(correction_reason or "")
    vals.append(row["fact_id"])
    conn.execute(f"UPDATE facts SET {', '.join(sets)} WHERE fact_id = ?", vals)
    append_transition(conn, row["fact_id"], direction, from_state, to_state,
                      reason_code(prefix_for(direction, to_state, new_superseded,
                                             corrected_by), why),
                      evidence, gate, new_regime)
    conn.commit()
    if corrected_by is not None:
        print(f"VERDICT   {row['fact_id']}  {direction} {from_state} -> {to_state}"
              f"  by {corrected_by}  reason: {correction_reason}")
        print(f"          recorded as a verdict on {row['fact_id']}: --why {row['fact_id']}"
              " shows the ledger row and the adjudicator. STATE.md and EVENTS.md are not"
              " written here.")
    if new_status in HISTORICAL_STATUSES or new_superseded:
        print(STATUS_USAGE_NOTE)
        print(STATUS_USAGE_FIX)
    return {"fact_id": row["fact_id"], "from": from_state, "to": to_state,
            "direction": direction, "regime_pin": new_regime, "receipt": receipt,
            "gate_kind": kind}


def repin(conn: sqlite3.Connection, fact_id: str, why: str, evidence: str, gate: str,
          run: bool = False, regime: str | None = None, bind_receipt: bool = False,
          archive_dir: pathlib.Path | None = None,
          new_source: str | None = None,
          new_scope: str | None = None) -> dict:
    """Re-hash a fact's anchor after its source legitimately moved, then restore it.

    `--regime-scope` on an *active* fact corrects the claim's dependency on the regime
    pin without waiting for a regime change to demote it by accident: a GitHub census
    stamped `model` is a wrong claim about what the number depends on, and refusing the
    repair leaves no supported way to fix it. The scope must actually change - a same-scope
    repin of an active row is still the re-stamp this command refuses.

    `--source path:Lx-Ly` moves the anchor to a new slice: the repair for a fact whose
    claim survived an edit that shifted line numbers. Without it the existing range is
    re-hashed in place.

    `--bind-receipt` re-anchors onto the archived gate output instead of the source file.
    That is the repair for a claim whose referent was a transcript line: the source slice
    is gone or has moved, and the only durable bytes that carry the claim are the ones a
    re-run of the gate leaves in the receipt archive. The new pin is a live hash, so
    Item F15 now guards the evidence rather than nothing.
    """
    row = resolve(conn, fact_id)
    keys = row.keys()
    scope_now = row["regime_scope"] if "regime_scope" in keys else "model"
    if new_scope is not None and new_scope not in REGIME_SCOPES:
        raise FactError(f"--regime-scope must be one of {'|'.join(REGIME_SCOPES)}, "
                        f"got {new_scope!r}")
    if row["status"] not in ("stale", "conditional", "active"):
        raise FactError(f"--repin is for stale, conditional or active facts, "
                        f"{row['fact_id']} is {row['status']}")
    if row["status"] == "active" and new_scope == scope_now:
        raise FactError(f"--repin on active {row['fact_id']} changes nothing: it is already "
                        f"{scope_now}-scope, and a bare re-stamp of an active claim is refused")
    if bind_receipt and not run:
        raise FactError("--bind-receipt needs --run-gate: there is nothing to bind "
                        "until the gate has actually run and been archived")
    if new_source and bind_receipt:
        raise FactError("--repin --source and --bind-receipt choose different anchors")
    from_state = f"{row['frequency_class']}:{row['status']}"
    restored = "contested" if row["competing_claims"] else "active"
    to_state = f"{row['frequency_class']}:{restored}"
    new_regime = regime or row["regime_pin"]
    rec = (run_gate(gate, row["fact_id"], archive_dir, new_regime)
           if run else None)
    if rec is not None and bind_receipt:
        pin = rec["pin"]
        source = pathlib.Path(rec["path"])
        src_path, src_range = rec["rel"], rec["line_range"]
    else:
        anchor = new_source or f"{row['source_path']}:{row['line_range']}"
        path, start, end = parse_source(anchor)
        try:
            pin = read_pin(path, start, end)
        except AnchorError as exc:
            # A re-stamp hashes the referent as it now reads. With no referent there is
            # nothing to hash, and re-stamping the old bytes would launder the label. Both
            # repairs are named because they are the only ways out.
            raise FactError(f"cannot re-pin {anchor}: {exc}; there is nothing to hash"
                            " while the referent cannot be read - repair with --source"
                            " path:Lx-Ly to move the anchor, or --bind-receipt --run-gate"
                            " <gate> to anchor on the gate's own output")
        source = path
        src_path = (str(path.relative_to(AGENT))
                    if str(path).startswith(str(AGENT) + os.sep) else str(path))
        src_range = f"L{start}-L{end}"
    evidence, receipt = bind_evidence(evidence, rec, row, bind_receipt)
    scope_to = new_scope or scope_now
    ts = now_iso()
    conn.execute("""
        UPDATE facts SET sha256_pin = ?, git_commit = ?, status = ?, regime_pin = ?,
                         updated_at = ?, revalidated_at = ?, source_path = ?, line_range = ?,
                         regime_scope = ?
        WHERE fact_id = ?
    """, (pin, git_commit_of(source), restored, new_regime, ts, ts, src_path,
          src_range, scope_to, row["fact_id"]))
    ledger_why = (why if scope_to == scope_now
                  else f"{why} [regime scope {scope_now} -> {scope_to}]")
    append_transition(conn, row["fact_id"], "promote", from_state, to_state,
                      reason_code("R8_REPINNED", ledger_why), evidence, gate, new_regime)
    conn.commit()
    return {"fact_id": row["fact_id"], "from": from_state, "to": to_state,
            "direction": "promote", "sha256_pin": pin, "regime_pin": new_regime,
            "regime_scope": scope_to, "scope_changed": scope_to != scope_now,
            "source_path": src_path, "line_range": src_range, "receipt": receipt}


# ------------------------------------------------------------------------ reading

def query(conn: sqlite3.Connection, text: str, frequency_class: str | None = None,
          include_contested: bool = False, include_retired: bool = False,
          include_falsified: bool = False, limit: int = 20) -> list[sqlite3.Row]:
    if not text.strip():
        raise FactError("--query needs an FTS5 expression, got an empty string")
    if frequency_class and frequency_class not in CLASSES:
        raise FactError(f"--class must be one of {'|'.join(CLASSES)}, got {frequency_class!r}")
    sql = ["SELECT f.* FROM facts_fts JOIN facts f ON f.fact_id = facts_fts.fact_id",
           "WHERE facts_fts MATCH ?"]
    args: list = [text]
    if frequency_class:
        sql.append("AND f.frequency_class = ?")
        args.append(frequency_class)
    hidden = []
    if not include_contested:
        hidden.append("contested")
    if not include_retired:
        hidden.append("retired")
    if not include_falsified:
        hidden.append("falsified")
    if hidden:
        sql.append("AND f.status NOT IN (" + ",".join("?" * len(hidden)) + ")")
        args.extend(hidden)
    sql.append("ORDER BY rank LIMIT ?")
    args.append(max(1, limit))
    try:
        return conn.execute(" ".join(sql), args).fetchall()
    except sqlite3.OperationalError as exc:
        raise FactError(f"FTS5 rejected {text!r}: {exc}") from exc


def format_row(row: sqlite3.Row) -> list[str]:
    line = (f"[{row['fact_id']}] ({row['frequency_class']}:{row['status']}) "
            f"{row['subject']} -> {row['predicate']} -> {row['object']} "
            f"| ref: {row['source_path']}:{row['line_range']} @ {row['sha256_pin'][:8]}")
    out = [line]
    if row["status"] == "contested":
        out.append(f"[EPISTEMIC STATUS: CONTESTED - Competing: "
                   f"{row['competing_claims'] or 'unrecorded'} | Gate: "
                   f"{row['adjudication_gate'] or 'unrecorded'}]")
    return out


def lineage(conn: sqlite3.Connection, ident: str) -> list[tuple[sqlite3.Row, list[sqlite3.Row]]]:
    """Facts matching `ident` (id, then exact subject, then subject substring)."""
    facts = []
    exact = conn.execute("SELECT * FROM facts WHERE fact_id = ?", (ident,)).fetchone()
    if exact:
        facts = [exact]
    else:
        facts = conn.execute("SELECT * FROM facts WHERE subject = ? ORDER BY fact_id",
                             (ident,)).fetchall()
        if not facts:
            like = f"%{ident}%"
            facts = conn.execute(
                "SELECT * FROM facts WHERE subject LIKE ? OR object LIKE ? "
                "ORDER BY fact_id", (like, like)).fetchall()
    return [(f, conn.execute(
        "SELECT * FROM transitions WHERE fact_id = ? ORDER BY transition_id",
        (f["fact_id"],)).fetchall()) for f in facts]


def evict_session(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Retire every session fact that never earned a transition. Ledger is kept."""
    rows = conn.execute("SELECT * FROM facts WHERE frequency_class = 'session' "
                        "AND status = 'unverified' ORDER BY fact_id").fetchall()
    stamp = now_iso()
    for row in rows:
        conn.execute("UPDATE facts SET status = 'retired', updated_at = ? "
                     "WHERE fact_id = ?", (stamp, row["fact_id"]))
        append_transition(conn, row["fact_id"], "demote", "session:unverified",
                          "session:retired",
                          reason_code("R9_OPERATOR",
                                      "session evict at checkpoint: unverified "
                                      "session-scoped claims do not cross a restart"),
                          "tools/checkpoint.sh",
                          "python3 tools/fact_store.py --evict-session",
                          row["regime_pin"])
    conn.commit()
    return rows
def reconcile_stale(conn: sqlite3.Connection) -> tuple[list[dict], int]:
    """Close the gap the checks leave open: a stale claim someone has since re-proved is
    superseded, not a debt still owed a re-derivation.

    `--check-anchors` demotes a claim whose pinned slice drifted. When that claim is later
    re-learned, `--add` writes a second row and the demoted one sits at `stale` forever: the
    sweep keeps naming it, and every later `--stale` report is one row longer than the work
    actually outstanding. A stale row is superseded when a live row (`active`, `contested`
    or `conditional`) for the same subject+predicate was stamped at or after it - the claim
    moved on. Retirement is terminal and supersession is recorded at the terminal transition
    (CG-32), so the row names its replacement in `superseded_by` and `--why ID` walks the
    lineage to it. With no live successor the row stays stale and keeps its name in the
    report, because nobody has proved it since. A second sweep has nothing to do: retired
    rows leave the input set, which is what makes this safe to run at every checkpoint.
    """
    rows = conn.execute("SELECT * FROM facts WHERE status = 'stale' "
                        "ORDER BY fact_id").fetchall()
    live = ("active", "contested", "conditional")
    marks = ",".join("?" * len(live))
    stamp = now_iso()
    moved: list[dict] = []
    for row in rows:
        rep = conn.execute(
            f"SELECT * FROM facts WHERE subject = ? AND predicate = ?"
            f"  AND fact_id != ? AND status IN ({marks}) AND updated_at >= ?"
            " ORDER BY updated_at DESC, fact_id LIMIT 1",
            # bound in placeholder order: passing the stamp ahead of the status marks puts a
            # date in the IN list and 'active' in the timestamp slot, so no successor is ever
            # found and the sweep reports work as outstanding forever.
            (row["subject"], row["predicate"], row["fact_id"], *live,
             row["updated_at"])).fetchone()
        if rep is None:
            continue
        referent = (f"{rep['source_path']}:{rep['line_range']}"
                    if rep["source_path"] and rep["line_range"] else "")
        conn.execute("UPDATE facts SET status = 'retired', superseded_by = ?, "
                     "updated_at = ? WHERE fact_id = ?",
                     (rep["fact_id"], stamp, row["fact_id"]))
        append_transition(
            conn, row["fact_id"], "demote",
            f"{row['frequency_class']}:stale", f"{row['frequency_class']}:retired",
            reason_code("R4_SUPERSEDED",
                        f"reconcile: {rep['fact_id']} carries the same claim and is "
                        f"{rep['status']}, so this row is history, not a re-derivation"
                        " owed"),
            f"superseded by {rep['fact_id']}" + (f" ({referent})" if referent else ""),
            "python3 tools/fact_store.py --reconcile", row["regime_pin"])
        moved.append({"fact_id": row["fact_id"], "class": row["frequency_class"],
                      "subject": row["subject"], "predicate": row["predicate"],
                      "superseded_by": rep["fact_id"],
                      "replacement_status": rep["status"]})
    conn.commit()
    left = conn.execute("SELECT count(*) n FROM facts WHERE status = 'stale'").fetchone()["n"]
    return moved, left



# --- spine liveness (--check-spine / --strict-spine) ---------------------------------
# `--check-anchors` proves a fact still names the bytes it was pinned to. It cannot see the
# reverse gap: the spine states measured numbers (gate scores, corpus counts) with no path
# of their own, so a number can outlive every run that produced it and still sit on disk
# while the store reads clean. Each measured-looking spine number must bind to one of:
#   fact      a live fact - its pinned slice or its own triple
#   cited     a file the same spine line names (the line is its own citation)
#   ledger    the newest sweep row for a gate this line names (run/harness_loop.log)
#   recorded  the durable record: EVENTS.md, PERM.md, notes/, run/, archive/
# `--strict-spine` accepts only fact/cited/ledger/exempt and reports `recorded` as weak, so
# the loose bucket stays visible instead of quietly becoming the standard.
# A gate score the newest row contradicts is not weakly bound, it is wrong, so it stays
# unbound even where a fact or some note also carries the older number.
SPINE_FILES = ("AGENTS.md", "CORE.md", "STATE.md")
SPINE_RECORD_FILES = ("EVENTS.md", "PERM.md")
SPINE_RECORD_DIRS = ("notes", "run", "archive")
SPINE_RECORD_SUFFIXES = (".md", ".json", ".jsonl", ".log", ".txt")
SPINE_RECORD_MAX_BYTES = 4_000_000

# Pattern-matched numbers that are not claims about the world. Keyed by the exact literal,
# so one exemption can never swallow a family of measurements.
SPINE_EXEMPT = {
    "780 tok": "STATE.md cap configured in tools/spine_check.py",
    "2,500": "injected-spine hard limit configured in tools/spine_check.py",
    "1,800": "injected-spine ideal budget configured in tools/spine_check.py",
}

SPINE_NUM_RE = re.compile(
    r"\b\d{2,4}/0\b"             # a harness score, e.g. 89/0
    r"|\b\d{1,3},\d{3}\b"       # a comma-grouped count, e.g. 6,539
    r"|\b\d{1,3}(?:\.\d+)?\s%"  # a measured percentage, e.g. 86.2 %
    r"|\b\d[\d,]{2,}\s+tok\b")  # an explicit token count
SPINE_CITE_RE = re.compile(
    r"[A-Za-z0-9_.\-/{}]+\.(?:md|py|json|jsonl|log|txt|js|mjs|ts|c|cc|cpp|h|hpp|yml|yaml|sh)")
# Only a harness score has a sweep row to prove it, so only a harness score can be
# contradicted by one. The row shape is the one tools/spine_check.py writes: pinning gate,
# rc and wall keeps any other score= in the ledger from being read as a gate receipt.
SPINE_SCORE_RE = re.compile(r"^\d{1,4}/\d{1,2}$")
SPINE_TOKEN_RE = re.compile(r"[A-Za-z0-9_.\-]+")
SPINE_LEDGER_REL = pathlib.Path("run") / "harness_loop.log"
SPINE_LEDGER_ROW_RE = re.compile(
    r"\bgate=(\S+)\s+rc=\d+\s+wall=[\d.]+s\s+score=(\d{1,4}/\d{1,2})\b")


def spine_numbers(text: str) -> list[tuple[int, str, str]]:
    """(line, literal, line text) for every measured-looking number in one spine file."""
    out: list[tuple[int, str, str]] = []
    for n, line in enumerate(text.splitlines(), 1):
        out += [(n, m.group(0), line) for m in SPINE_NUM_RE.finditer(line)]
    return out


# SPINE_CITE_RE's class carries no comma: a comma after `notes/x.md,` would be swallowed
# into the path. So a `{a,b,c}` stem is matched here, its region lifted out of the line, and
# only the brace-free rest goes to SPINE_CITE_RE. Without this the whole brace form reads as
# one garbage path and the line's gate scores bind to nothing.
SPINE_BRACE_RE = re.compile(r"([A-Za-z0-9_.\-/]*)(\{[A-Za-z0-9_,.\-/]+\})([A-Za-z0-9_.\-/]*)")
SPINE_TAIL_EXT_RE = re.compile(
    r"\.(?:md|py|json|jsonl|log|txt|js|mjs|ts|c|cc|cpp|h|hpp|yml|yaml|sh)$")


def named_paths(line: str) -> list[pathlib.Path]:
    """Every path a spine line names, `{a,b,c}` expanded, existence unjudged.

    Existence is the caller's call: `spine_cites` wants files it can open, the ledger route
    wants the name. A spine line names a gate relative to its own project -
    `harness/city_harness.py` for a gate that runs under ascii_city - so a path that does
    not resolve under the agent dir still says which gate its number was about.
    """
    out: list[pathlib.Path] = []
    rest = line
    while True:
        m = SPINE_BRACE_RE.search(rest)
        if not m:
            break
        if SPINE_TAIL_EXT_RE.search(m.group(3)):
            out += [pathlib.Path(f"{m.group(1)}{alt}{m.group(3)}")
                    for alt in m.group(2)[1:-1].split(",") if alt]
        rest = rest[:m.start()] + " " + rest[m.end():]
    out += [pathlib.Path(raw) for raw in SPINE_CITE_RE.findall(rest)]
    return out


def spine_cites(line: str, root: pathlib.Path) -> list[pathlib.Path]:
    """Files a spine line names itself, `{a,b,c}` expanded; paths that do not exist drop."""
    return [root / p for p in named_paths(line) if (root / p).is_file()]


def newest_gate_scores(root: pathlib.Path) -> dict[str, str]:
    """Latest sweep score per gate, read off the loop ledger `run/harness_loop.log`.

    A harness score has no referent but the run that printed it, and the ledger is the log
    of those runs. Newest row per gate, not any row: the ledger only grows, so a score
    matched anywhere in it could be vouched for by a sweep the next one superseded.
    """
    out: dict[str, str] = {}
    try:
        text = (root / SPINE_LEDGER_REL).read_text(errors="replace")
    except OSError:
        return out
    for row_line in text.splitlines():
        row = SPINE_LEDGER_ROW_RE.search(row_line)
        if row:
            out[row.group(1)] = row.group(2)
    return out



def gate_path_in(cmd: str) -> str | None:
    """The gate script one command names, resolved so the loop ledger's spelling and a
    fact's `adjudication_gate` compare honestly: `tools/x.py --frontier` and
    `python3 /abs/tools/x.py` name the same gate; `-` and prose name none."""
    for tok in (cmd or "").split():
        if "/" in tok and tok.endswith((".py", ".mjs", ".js", ".sh", ".ts")):
            p = pathlib.Path(tok)
            return str((p if p.is_absolute() else AGENT / p).resolve())
    return None


def ledger_rows(path: pathlib.Path) -> list[dict]:
    """Every loop-ledger row, oldest first."""
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return []
    return [m.groupdict() for m in
            (LEDGER_ROW_RE.match(l.strip()) for l in text.splitlines()) if m]


def ledger_clears_receipt(ledger: pathlib.Path | None, gate_cmd: str,
                          reason: str) -> dict | None:
    """The loop-ledger row proving a lost receipt is only a lost copy: the newest sweep ran
    THIS gate, exited 0 and scored N/0. Currency is the newest sweep generation (same `head`
    and `sweep` as the file's last row), because an old passing sweep vouches for nothing a
    later sweep contradicts. The row is the authoritative run record; the archive is a copy
    of it, so rotation damage is reported, never demoted. A receipt that itself reports a
    failing or checkless run is never cleared."""
    if ledger is None or not reason.startswith(RECEIPT_LOST):
        return None
    want = gate_path_in(gate_cmd)
    if want is None:
        return None
    rows = ledger_rows(ledger)
    if not rows:
        return None
    newest = rows[-1]
    for row in reversed(rows):
        if gate_path_in(row["gate"]) != want:
            continue
        if (row["rc"] == "0" and PASSING_SCORE_RE.match(row["score"])
                and row["head"] == newest["head"] and row["sweep"] == newest["sweep"]):
            return row
        return None
    return None



def line_gate_scores(line: str, ledger: dict[str, str]) -> dict[str, str]:
    """Ledger scores for the gates one spine line names; empty when it names none.

    Gates match by file stem, because the line's path is relative to its own project and
    will not resolve under the agent dir. Where one stem matches several gates -
    `browser_harness.py` sits in two projects at two scores - the line has to say which
    project itself, scored by how many of the gate's directories it repeats; a tie or no
    hit binds nothing, since guessing the project is the false binding this route is for.
    """
    stems = {p.name for p in named_paths(line) if p.suffix == ".py"}
    if not stems:
        return {}
    tokens = set(SPINE_TOKEN_RE.findall(line))
    out: dict[str, str] = {}
    for stem in stems:
        same = [gate for gate in ledger if pathlib.Path(gate).name == stem]
        if not same:
            continue
        if len(same) > 1:
            hits = {gate: sum(1 for part in pathlib.Path(gate).parts[:-1] if part in tokens)
                    for gate in same}
            best = max(hits.values())
            same = [gate for gate in same if hits[gate] == best]
            if best == 0 or len(same) != 1:
                continue
        out[same[0]] = ledger[same[0]]
    return out


def recorded_corpus(root: pathlib.Path) -> str:
    """The durable record, comma-stripped: the weakest thing a spine number can cite."""
    parts: list[str] = []
    for name in SPINE_RECORD_FILES:
        p = root / name
        if p.is_file():
            parts.append(p.read_text(errors="replace"))
    for name in SPINE_RECORD_DIRS:
        base = root / name
        if not base.is_dir():
            continue
        for p in sorted(base.rglob("*")):
            if (p.is_file() and p.suffix in SPINE_RECORD_SUFFIXES
                    and p.stat().st_size <= SPINE_RECORD_MAX_BYTES):
                parts.append(p.read_text(errors="replace"))
    return "\n".join(parts).replace(",", "")


def _number_in(literal: str, blob: str) -> bool:
    """A gate score binds to its own spelling or to the harness wording that produced it."""
    needle = literal.replace(",", "")
    if needle in blob:
        return True
    if "/" in needle:
        passed, failed = needle.split("/", 1)
        return f"{passed} checks" in blob and f"{failed} fail" in blob
    return False


def check_spine(conn: sqlite3.Connection, root: pathlib.Path) -> tuple[dict, float]:
    """Bind every measured-looking spine number to a fact, a cited file, a row or record."""
    started = time.perf_counter()
    live = conn.execute("SELECT subject, predicate, object, source_path, line_range "
                        "FROM facts WHERE status IN ('active','conditional')").fetchall()
    fact_blobs: list[str] = []
    for row in live:
        path = pathlib.Path(row["source_path"])
        if not path.is_absolute():
            path = root / path
        try:
            start, end = parse_range(row["line_range"])
            slice_text = byte_slice(path.read_bytes(), start, end).decode("utf-8", "replace")
        except (AnchorError, OSError, ValueError):
            slice_text = ""
        fact_blobs.append(" ".join([slice_text, row["subject"], row["predicate"],
                                    row["object"]]).replace(",", ""))
    corpus = recorded_corpus(root)
    ledger = newest_gate_scores(root)
    cited_cache: dict[pathlib.Path, str] = {}
    buckets = {"fact": 0, "cited": 0, "ledger": 0, "recorded": 0, "exempt": 0}
    unbound: list[dict] = []
    weak: list[dict] = []
    scanned = 0
    for name in SPINE_FILES:
        path = root / name
        if not path.is_file():
            continue
        for line_no, literal, line_text in spine_numbers(path.read_text(errors="replace")):
            scanned += 1
            where = f"{name}:{line_no}"
            gates = (line_gate_scores(line_text, ledger)
                     if SPINE_SCORE_RE.fullmatch(literal) else {})
            if literal in SPINE_EXEMPT:
                buckets["exempt"] += 1
            elif gates and literal not in set(gates.values()):
                # The line names its gate and that gate's newest run says otherwise. A fact
                # or a note still carrying the older number does not rescue it: the row is
                # this number's referent, so contradicting it is a false claim, not a weak
                # one - which is why this route runs before the fact and cited routes.
                unbound.append({"where": where, "literal": literal,
                                "line": line_text.strip()[:120],
                                "reason": "superseded: newest ledger row for the named gate "
                                          f"is {' or '.join(sorted(set(gates.values())))}"})
            elif any(_number_in(literal, blob) for blob in fact_blobs):
                buckets["fact"] += 1
            elif any(_number_in(literal, cited_cache.setdefault(
                    cited, cited.read_text(errors="replace").replace(",", "")))
                    for cited in spine_cites(line_text, root)):
                buckets["cited"] += 1
            elif gates:
                buckets["ledger"] += 1
            elif _number_in(literal, corpus):
                buckets["recorded"] += 1
                weak.append({"where": where, "literal": literal,
                             "reason": "bound only by the durable record"})
            else:
                unbound.append({"where": where, "literal": literal,
                                "line": line_text.strip()[:120],
                                "reason": "names no live fact, no cited file, no ledger row, no record"})
    rep = {"scanned": scanned, "buckets": buckets, "unbound": unbound, "weak": weak,
           "exempt_reasons": {k: SPINE_EXEMPT[k] for k in SPINE_EXEMPT}}
    return rep, (time.perf_counter() - started) * 1000.0


def unreadable_contest(conn: sqlite3.Connection, fact_id: str) -> str | None:
    """The status an unreadable-referent contest buried, or None when this row's rivals are a
    real disagreement an operator must adjudicate. The test is the recorded rival pair: a row
    contested because its referent could not be read heals when the referent resolves; a row
    contested against a rival claim does not, because nothing about the referent settling says
    which of the two claims holds.
    """
    stored = conn.execute("SELECT competing_claims FROM facts WHERE fact_id = ?",
                          (fact_id,)).fetchone()
    try:
        rivals = json.loads((stored["competing_claims"] if stored else None) or "")
    except (TypeError, ValueError):
        return None
    if not isinstance(rivals, list) or not any(R2_CLAIM_MARK in str(r) for r in rivals):
        return None
    back = conn.execute("SELECT from_state FROM transitions WHERE fact_id = ?"
                        " AND to_state LIKE '%:contested' AND why_rationale LIKE ?"
                        " ORDER BY transition_id DESC LIMIT 1",
                        (fact_id, R2_CLAIM_MARK + ":%")).fetchone()
    return back["from_state"].split(":")[-1] if back else None


def heal_unreadable(conn: sqlite3.Connection, row: sqlite3.Row, back: str,
                    ref: str) -> str:
    """The referent resolved and re-matched its pin, so the one thing in doubt is gone and the
    claim goes back where the contest found it. A promotion still needs a gate name, and the
    only honest one here is the row's own last gate: what re-runs the claim is unchanged, and
    what decided was the sweep. Returns it so the caller can print what revalidates the row.
    """
    gate = last_gate(conn, row["fact_id"],
                     "python3 tools/fact_store.py --repin " + row["fact_id"])
    ts = now_iso()
    conn.execute("UPDATE facts SET status = ?, competing_claims = NULL, updated_at = ?,"
                 " revalidated_at = ? WHERE fact_id = ?",
                 (back, ts, ts, row["fact_id"]))
    append_transition(conn, row["fact_id"], "promote",
                      f"{row['frequency_class']}:contested",
                      f"{row['frequency_class']}:{back}",
                      reason_code("R0_HEALTHY", f"Referent {ref} resolved and re-matched"
                                  " its pin; the unreadable-referent contest is withdrawn"),
                      ref, gate, row["regime_pin"])
    return gate


def stale_rematching(conn: sqlite3.Connection) -> list[dict]:
    """`stale` rows whose referent now re-matches its pin - reported, never healed. A stale
    label earned by content that disagreed is withdrawn by a gate run, not by a sweep noticing
    the bytes came back, because restoring the claim asserts it is true again. Advisory is the
    right ceiling: a stale label must not sit on a healthy referent with nothing saying so.
    """
    out: list[dict] = []
    for row in conn.execute("SELECT * FROM facts WHERE status = 'stale'"
                            " ORDER BY fact_id"):
        path = pathlib.Path(row["source_path"])
        if not path.is_absolute():
            path = AGENT / path
        try:
            start, end = parse_range(row["line_range"])
            live = read_pin(path, start, end)
        except (AnchorError, ValueError):
            continue
        if live != row["sha256_pin"]:
            continue
        out.append({"fact_id": row["fact_id"],
                    "ref": f"{row['source_path']}:{row['line_range']}",
                    "gate": last_gate(conn, row["fact_id"], "-")})
    return out


def check_anchors(conn: sqlite3.Connection) -> dict:
    """Item F15: re-hash every anchored fact's referent, and say WHICH failure happened.

    Two failures are not one thing. A referent whose bytes are present and disagree with the
    pin is REFUTED by evidence: R1_CONTENT_DRIFT, and `stale` is the honest label. A referent
    that cannot be read refutes nothing - absence of evidence is never evidence of absence
    (CORE) - so that row is not called stale either: it is CONTESTED against the single thing
    actually in doubt, its own unreadable referent, and the next sweep that can read the slice
    heals it back to the status the contest buried (R0_HEALTHY). A `stale` row whose slice
    re-matches is reported and left alone: withdrawing a content-refutation asserts the claim
    is true again, which is a promotion and needs a gate run, so the sweep prints the blessed
    command instead of taking it. The row's last gate names what revalidates it, so a sweep
    tells an operator what to run rather than only what to worry about.
    """
    started = time.perf_counter()
    marks = ",".join("?" * len(ANCHORED_STATUSES))
    rows = conn.execute("SELECT * FROM facts WHERE status IN (" + marks + ") "
                        "ORDER BY fact_id", ANCHORED_STATUSES).fetchall()
    stale: list[dict] = []
    contested: list[dict] = []
    healed: list[dict] = []
    for row in rows:
        path = pathlib.Path(row["source_path"])
        if not path.is_absolute():
            path = AGENT / path
        start, end = parse_range(row["line_range"])
        reason = None
        gone = None
        live = None
        try:
            live = read_pin(path, start, end)
        except ReferentGone as exc:
            gone = str(exc)      # readable file, the pinned slice is not in it any more
        except AnchorError as exc:
            reason = str(exc)    # the bytes cannot be read at all: undecidable, R2
        ref = f"{row['source_path']}:{row['line_range']}"
        if live == row["sha256_pin"]:
            # Readable and matching. If the only rival this row ever carried was its own
            # unreadability, that rival is gone; a row contested against a real rival claim is
            # left for the operator, because the referent settling decides nothing about it.
            back = unreadable_contest(conn, row["fact_id"])
            if back is not None:
                healed.append({"fact_id": row["fact_id"], "ref": ref,
                               "to_state": f"{row['frequency_class']}:{back}",
                               "gate": heal_unreadable(conn, row, back, ref)})
            continue
        if reason is not None:
            # R2: nothing was refuted, so nothing is called stale. Idempotent: an already
            # contested row is reported again, never re-contested or re-stamped.
            gate = last_gate(conn, row["fact_id"],
                             "python3 tools/fact_store.py --repin " + row["fact_id"])
            if row["status"] != "contested":
                claim = f"{row['subject']} {row['predicate']} {row['object']}"
                rivals = json.dumps(
                    [claim,
                     f"{claim} [{R2_CLAIM_MARK} referent {ref} unreadable: {reason}; the"
                     " stored claim and the fact that its referent cannot be read are the"
                     " rivals, and this row becomes decidable again the moment the referent"
                     " resolves]"],
                    ensure_ascii=False, separators=(",", ":"))
                conn.execute("UPDATE facts SET status = 'contested',"
                             " competing_claims = ?, adjudication_gate = ?,"
                             " updated_at = ? WHERE fact_id = ?",
                             (rivals, gate, now_iso(), row["fact_id"]))
                append_transition(conn, row["fact_id"], "contest",
                                  f"{row['frequency_class']}:{row['status']}",
                                  f"{row['frequency_class']}:contested",
                                  reason_code("R2_REFERENT_UNREADABLE",
                                              f"Referent anchor unreadable: {reason}"
                                              f" (Item F15)"),
                                  f"{ref} \u2014 {reason}", gate, row["regime_pin"])
            contested.append({"fact_id": row["fact_id"], "ref": ref, "reason": reason,
                              "gate": gate,
                              "from_state": f"{row['frequency_class']}:{row['status']}"})
            continue
        # R1: readable and disagreeing - the bytes refuted the claim. A slice the file no
        # longer holds lands here too: the read worked, so the absence of those lines IS the
        # evidence, and it is a different thing from a file that could not be opened.
        if gone is not None:
            reason = f"pinned slice no longer in range: {gone}"
            why = reason_code("R1_CONTENT_DRIFT",
                              f"Referent anchor no longer in range (Item F15): {gone}")
        else:
            reason = (f"SHA-256 mismatch: pin {row['sha256_pin'][:12]} != live "
                      f"{(live or '')[:12]}")
            why = reason_code("R1_CONTENT_DRIFT",
                              "Referent anchor SHA-256 mismatch (Item F15)")
        gate = last_gate(conn, row["fact_id"],
                         "python3 tools/fact_store.py --repin " + row["fact_id"])
        conn.execute("UPDATE facts SET status = 'stale', updated_at = ? WHERE fact_id = ?",
                     (now_iso(), row["fact_id"]))
        append_transition(conn, row["fact_id"], "demote",
                          f"{row['frequency_class']}:{row['status']}",
                          f"{row['frequency_class']}:stale", why,
                          f"{row['source_path']}:{row['line_range']} — {reason}", gate,
                          row["regime_pin"])
        stale.append({"fact_id": row["fact_id"], "ref": f"{row['source_path']}:{row['line_range']}",
                      "reason": reason, "gate": gate,
                      "from_state": f"{row['frequency_class']}:{row['status']}"})
    conn.commit()
    return {"scanned": len(rows), "stale": stale, "contested": contested,
            "healed": healed, "rematching": stale_rematching(conn),
            "ms": (time.perf_counter() - started) * 1000.0}


def check_evidence(conn: sqlite3.Connection,
                   archive_dir: pathlib.Path | None = None,
                   ledger: pathlib.Path | None = None,
                   infos: list[dict] | None = None) -> tuple[int, list[dict], float]:
    """Item F15 for the evidence itself: re-hash every active fact's archived gate output.

    Anchors pin a claim to a source slice; this pins it to the run that justified it.
    Only the newest `promote` per fact is checked, because that is the run the current
    state rests on. A broken receipt does not falsify the claim — it may still be true —
    so the fact goes to `conditional` (revalidation required) and not `stale`, and the
    ledger records the demote with the coordinate that failed. One exception: a copy the
    archive lost outright (`receipt missing` / `receipt unreadable`) while the loop
    ledger's newest sweep ran this same gate at rc=0 with a passing score is a lost COPY
    of an authoritative run, so it is returned in `infos` as advisory instead.
    """
    started = time.perf_counter()
    marks = ",".join("?" * len(ANCHORED_STATUSES))
    rows = conn.execute(
        "SELECT f.fact_id, f.frequency_class, f.status, f.regime_pin, f.source_path,"
        "       f.line_range, f.sha256_pin, t.transition_id, t.evidence_referent,"
        "       t.harness_gate"
        "  FROM facts f JOIN transitions t ON t.fact_id = f.fact_id"
        f" WHERE f.status IN ({marks}) AND t.direction = 'promote'"
        " ORDER BY t.transition_id", ANCHORED_STATUSES).fetchall()
    roots = [AGENT] + ([archive_dir] if archive_dir is not None else [])
    newest: dict[str, sqlite3.Row] = {}
    for row in rows:
        newest[row["fact_id"]] = row  # ascending transition_id: last write is the live one
    checked, broken = 0, []
    for fact_id in sorted(newest):
        row = newest[fact_id]
        status = row["status"]
        # Live evidence for a fact is the run it was last promoted on, plus its anchor
        # when that anchor is itself a receipt (Item F15's own anchor check proves the
        # bytes; this proves the run exited zero). Anything else the evidence string
        # names — a transcript line, a superseded coordinate in an `anchor:` note — is
        # provenance, and provenance is allowed to be old.
        anchor = (f"{row['source_path']}:{row['line_range']}"
                  f"@{row['sha256_pin'][:12]}")
        coords = list(dict.fromkeys(
            [m.group(1) for m in
             RECEIPT_LABEL_RE.finditer(row["evidence_referent"] or "")]
            + ([anchor] if RECEIPT_RE.fullmatch(anchor) else [])))
        for coord in coords:
            checked += 1
            ok, why, _head = verify_receipt(coord, roots)
            if ok:
                continue
            sweep_row = ledger_clears_receipt(ledger, row["harness_gate"] or "-", why)
            if sweep_row is not None:
                # The archive is a copy of the run; the loop ledger IS the run. A copy
                # the rotation lost, with the current sweep passing this very gate at
                # rc=0 and a passing score, is reported and never demoted.
                if infos is not None:
                    infos.append({"fact_id": fact_id, "coord": coord, "reason": why,
                                  "gate": sweep_row["gate"],
                                  "stamp": sweep_row["stamp"],
                                  "sweep": sweep_row["sweep"],
                                  "gate_score": sweep_row["score"]})
                continue
            from_state = f"{row['frequency_class']}:{status}"
            if status != "conditional":
                conn.execute("UPDATE facts SET status = 'conditional', updated_at = ?"
                             " WHERE fact_id = ?", (now_iso(), fact_id))
                append_transition(conn, fact_id, "demote", from_state,
                                  f"{row['frequency_class']}:conditional",
                                  reason_code("R3_EVIDENCE_LOST",
                                              "Gate-evidence receipt missing or corrupted "
                                              "(Item F15)"),
                                  coord, row["harness_gate"] or "-", row["regime_pin"])
                status = "conditional"
            broken.append({"fact_id": fact_id, "coord": coord, "reason": why,
                           "from_state": from_state,
                           "gate": row["harness_gate"] or "-"})
    conn.commit()
    return checked, broken, (time.perf_counter() - started) * 1000.0


POSITION_NAMES = ("model", "runtime", "device")


# Mirrors the state_view CASE exactly: the view defines expiry, this copy only
# puts the window in the EXPIRED line so the withdrawal explains itself. If the
# view and this drift, the printed number is wrong while the set of expired facts
# stays the view's; S16.5 in the harness pins the number against the boundary.
EXPIRY_TTL_DAYS = {"project": 30, "architectural": 180, "permanent": 730}


def check_expiry(conn: sqlite3.Connection) -> tuple[int, int, float]:
    """CG-12: advisory age decay. `expired` is derived in state_view and never
    stored; --check-expiry makes the withdrawal explainable. A fact crossing its
    class window earns one `demote -> expired` transition row - keyed to its
    revalidation instant, so a second sweep stays silent and a revalidated fact
    that ages again earns a new row - and no status is touched: advisory means the
    offering states stay exactly as they are. The command never fails; an empty
    store prints the zero line and exits 0.
    """
    started = time.perf_counter()
    rows = conn.execute("""
        SELECT s.fact_id, s.frequency_class, s.status, f.regime_pin,
               coalesce(f.revalidated_at, f.updated_at) AS since,
               round(julianday('now') - julianday(coalesce(f.revalidated_at,
                   f.updated_at)), 1) AS age_d
        FROM state_view s JOIN facts f USING (fact_id)
        WHERE s.state = 'expired' ORDER BY s.fact_id""").fetchall()
    written = 0
    for row in rows:
        window = EXPIRY_TTL_DAYS.get(row["frequency_class"])
        print(f"EXPIRED {row['fact_id']} {row['frequency_class']}:{row['status']} "
              f"{row['age_d']} d since revalidation"
              + (f" > {window} d policy" if window else ""))
        prior = conn.execute("""
            SELECT 1 FROM transitions WHERE fact_id = ? AND to_state LIKE '%:expired'
              AND transitioned_at >= coalesce(?, '') LIMIT 1""",
                             (row["fact_id"], row["since"] or "")).fetchone()
        if prior:
            continue
        append_transition(
            conn, row["fact_id"], "demote",
            f"{row['frequency_class']}:{row['status']}",
            f"{row['frequency_class']}:expired",
            reason_code("R5_AGE_DECAY",
                        f"Age decay: {row['age_d']} d since revalidation exceeds the "
                        f"{row['frequency_class']} window; derived in state_view, status "
                        f"unchanged, advisory"),
            last_gate(conn, row["fact_id"],
                      fallback="--repin with a revalidation gate"),
            "python3 tools/fact_store.py --check-expiry",
            row["regime_pin"])
        written += 1
    if written:
        conn.commit()
    return len(rows), written, (time.perf_counter() - started) * 1000.0


def check_regime(conn: sqlite3.Connection, live_pin: str) -> tuple[int, list[dict], int, float]:
    """Regime drift: a claim measured under another regime is conditional. VS-13
    puts project-class claims in scope: a project fact is the kind that outlives its
    session, and it must die with the regime too; session facts are excluded because
    --evict-session retires them inside their own lifetime."""
    started = time.perf_counter()
    rows = conn.execute("SELECT * FROM facts WHERE frequency_class IN "
                        "('architectural','permanent','project') AND status = 'active' "
                        "ORDER BY fact_id").fetchall()
    demoted: list[dict] = []
    live_parts = live_pin.split("|")
    live_empty = {i for i in range(3)
                  if i >= len(live_parts) or not live_parts[i].strip()}
    skipped = 0
    for row in rows:
        keys = row.keys()
        scope = row["regime_scope"] if "regime_scope" in keys else "model"
        positions = _SCOPE_POSITIONS[scope]
        pinned_parts = row["regime_pin"].split("|")
        # FT-48/R2: an empty live dimension blinds that one position only, and
        # a pin too short to carry a load-bearing position is a mismatch the
        # probe cannot blind (chromatic_schema §3). Only a claim whose EVERY
        # load-bearing position the probe cannot see survives un-compared.
        short = [i for i in positions if i >= len(pinned_parts)]
        blind = [i for i in positions if i in live_empty]
        if positions and len(blind) == len(positions) and not short:
            # CG-10 fail open, now counted against the comparison total.
            skipped += 1
            continue
        if not short and not any(i not in blind
                                 and pinned_parts[i] != live_parts[i]
                                 for i in positions):
            continue
        gate = last_gate(conn, row["fact_id"],
                         "python3 tools/fact_store.py --repin " + row["fact_id"])
        conn.execute("UPDATE facts SET status = 'conditional', updated_at = ? "
                     "WHERE fact_id = ?", (now_iso(), row["fact_id"]))
        append_transition(conn, row["fact_id"], "demote",
                          f"{row['frequency_class']}:active",
                          f"{row['frequency_class']}:conditional",
                          reason_code("R6_REGIME_MISMATCH",
                                      f"Regime pin mismatch (scope {scope}): recorded "
                                      f"{row['regime_pin']} vs live {live_pin} "
                                      f"\u2014 revalidation required"),
                          f"python3 tools/fact_store.py --live-pin {live_pin} "
                          "--check-regime",
                          gate, live_pin)
        compared = "+".join(POSITION_NAMES[i] for i in positions) or "none"
        differs = "+".join(
            POSITION_NAMES[i] for i in positions
            if i >= len(pinned_parts)
            or (i not in blind and pinned_parts[i] != live_parts[i])) or compared
        demoted.append({"fact_id": row["fact_id"],
                        "from_state": f"{row['frequency_class']}:active",
                        "to_state": f"{row['frequency_class']}:conditional",
                        "pinned": row["regime_pin"], "gate": gate, "scope": scope,
                        "compared_dimensions": compared, "differing_dimensions": differs,
                        "subject": row["subject"]})
    conn.commit()
    return len(rows), demoted, skipped, (time.perf_counter() - started) * 1000.0


def parse_range(text: str) -> tuple[int, int]:
    match = re.match(r"^L(\d+)-L(\d+)$", text or "")
    if not match:
        raise FactError(f"stored line_range {text!r} is not L<start>-L<end>")
    return int(match.group(1)), int(match.group(2))



def _last_why(conn: sqlite3.Connection, fact_id: str) -> str:
    """The newest ledger row's rationale: why this claim left the live set."""
    row = conn.execute("SELECT why_rationale, evidence_referent FROM transitions"
                       " WHERE fact_id = ? ORDER BY transition_id DESC LIMIT 1",
                       (fact_id,)).fetchone()
    if row is None:
        return "no ledger row: it entered this status before the ledger existed"
    return f"{row['why_rationale']}  ({row['evidence_referent']})"


def rederive_command(row: sqlite3.Row) -> str:
    """One command that re-derives the claim, in the shape the store will accept."""
    if row["status"] == "stale" and not row["superseded_by"]:
        return ("python3 tools/fact_store.py --repin " + row["fact_id"]
                + ' --why "re-derived: <what changed>" --evidence "<receipt:NAME|transcript>"')
    gate = row["adjudication_gate"] or "<gate script>"
    return ("python3 tools/fact_store.py --transition " + row["fact_id"]
            + " --direction promote --to-status active --class " + row["frequency_class"]
            + ' --why "<what proves it now>" --evidence "<receipt:NAME>" --gate "' + gate
            + '" --corrected-by WHO --correction-reason "WHY this replaces what is recorded"'
            + "   # or --superseded-by NEW_ID to replace it outright")


def stale_scan(conn: sqlite3.Connection, args) -> int:
    """The historical set with the way out of it. Advisory: exits 0 whatever it finds,
    because a stale claim is not a live claim -- but a reader must never act on one, so
    every line names what made it historical and the command that re-derives it."""
    rows = conn.execute(
        "SELECT fact_id, frequency_class, status, source_path, line_range,"
        "       adjudication_gate, superseded_by, updated_at, corrected_at, corrected_by,"
        "       correction_reason"
        "  FROM facts WHERE status IN ('stale','retired','falsified')"
        " ORDER BY status, frequency_class, updated_at DESC").fetchall()
    counts = {s: sum(1 for r in rows if r["status"] == s) for s in HISTORICAL_STATUSES}
    if args.json:
        print(json.dumps([{**dict(r), "_why": _last_why(conn, r["fact_id"]),
                                 "command": rederive_command(r)} for r in rows], indent=1))
        print(f"stale-scan: {len(rows)} claim(s) historical  stale={counts['stale']}"
              f" retired={counts['retired']} falsified={counts['falsified']}  (advisory)")
        return 0
    groups: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        key = row["frequency_class"] if args.class_only else row["status"]
        groups.setdefault(key, []).append(row)
    print("== historical claims (stale / retired / falsified) ==")
    for key in sorted(groups):
        print(f"[{key}]  {len(groups[key])} claim(s)")
        for row in groups[key]:
            if args.gate_only:
                print(f"  {row['fact_id']}  {row['status']}  gate:"
                      f" {row['adjudication_gate'] or '-'}")
                continue
            print(f"  {row['fact_id']}  {row['frequency_class']}:{row['status']}"
                  f"  {row['source_path']}:{row['line_range']}")
            print(f"            reason:    {_last_why(conn, row['fact_id'])}")
            if row["superseded_by"]:
                print(f"            superseded: {row['superseded_by']}")
            if row["corrected_by"]:
                print(f"            verdict:     {row['corrected_by']}"
                      f" at {row['corrected_at']} - {row['correction_reason']}")
            print(f"            command:   {rederive_command(row)}")
    print(STATUS_USAGE_NOTE)
    print(STATUS_USAGE_FIX)
    print(f"stale-scan: {len(rows)} claim(s) historical  stale={counts['stale']}"
          f" retired={counts['retired']} falsified={counts['falsified']}  (advisory)")
    return 0


def contested_scan(conn: sqlite3.Connection, args) -> int:
    """Contested claims, their rivals, and the command that decides. Read-only: deciding
    which rival holds is an operator act, so this scan writes nothing."""
    rows = conn.execute(
        "SELECT fact_id, frequency_class, competing_claims, adjudication_gate,"
        "       source_path, line_range, updated_at, corrected_at, corrected_by,"
        "       correction_reason"
        "  FROM facts WHERE status = 'contested' ORDER BY updated_at DESC").fetchall()
    print("== contested claims ==")
    for row in rows:
        gate = row["adjudication_gate"] or "<gate script>"
        print(f"CONTESTED {row['fact_id']}  {row['frequency_class']}:contested"
              f"  {row['source_path']}:{row['line_range']}  since {row['updated_at']}")
        print(f"          rival(s):  {(row['competing_claims'] or '').strip() or 'none recorded'}")
        if row["corrected_by"]:
            print(f"          verdict:   {row['corrected_by']} at {row['corrected_at']}"
                  f" - {row['correction_reason']}")
        print("          command:   python3 tools/fact_store.py --transition "
              + row["fact_id"] + " --direction falsify --to-status retired --class "
              + row["frequency_class"] + ' --claims "<surviving rival ids>"'
              + ' --why "adjudicated: <which rival holds and why>" --evidence'
                ' "<receipt:NAME|transcript>" --gate "' + gate
              + '" --corrected-by WHO --correction-reason "WHY"')
        print(f"          lineage:   python3 tools/fact_store.py --why {row['fact_id']}")
    if args.json:
        print(json.dumps([dict(r) for r in rows], indent=1))
    print(f"contested-facts: {len(rows)} claim(s) awaiting adjudication; the scan writes"
          " nothing")
    return 0



def census(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute("SELECT frequency_class, status, COUNT(*) n FROM facts "
                        "GROUP BY frequency_class, status "
                        "ORDER BY frequency_class, status").fetchall()
    out = [f"  {r['frequency_class']:14s} {r['status']:12s} {r['n']}" for r in rows]
    # C2: the census GAINS an expired: line, never loses one. Derived, never stored;
    # absent only on a pre-migration store, where the derivation does not exist yet.
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='view' AND name='state_view'"
                    ).fetchone():
        n = conn.execute("SELECT COUNT(*) FROM state_view WHERE state='expired'").fetchone()[0]
        out.append(f"  expired: {n}")
    return out


# ---------------------------------------------------------------------------- CLI

def store_ms(since: float) -> str:
    return f"{since:.1f} ms"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="fact_store.py", description=__doc__.splitlines()[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=str(DEFAULT_DB), help=f"default {DEFAULT_DB}")
    ap.add_argument("--init", action="store_true", help="create the schema (idempotent)")
    ap.add_argument("--add", nargs=3, metavar=("SUBJECT", "PREDICATE", "OBJECT"))
    ap.add_argument("--class", dest="klass", choices=CLASSES)
    ap.add_argument("--source", help="path:L<start>-L<end>")
    ap.add_argument("--status", choices=STATUSES)
    ap.add_argument("--regime",
                    help=f"regime pin to record (default {DEFAULT_REGIME_PIN}); on "
                         f"--transition/--repin it re-stamps after revalidation")
    ap.add_argument("--regime-scope", choices=REGIME_SCOPES,
                    help="which pin dimensions a claim depends on (chromatic_schema "
                         "§3); set with --add/--transition/--repin, default 'model'")
    ap.add_argument("--claims", help="JSON array of competing claims (contested)")
    ap.add_argument("--gate", help="adjudicating / verifying command")
    ap.add_argument("--transition", metavar="FACT_ID")
    ap.add_argument("--direction", choices=DIRECTIONS)
    ap.add_argument("--to-class", dest="to_class", choices=CLASSES)
    ap.add_argument("--to-status", dest="to_status", choices=STATUSES)
    ap.add_argument("--superseded-by", dest="superseded_by",
                    help="fact id that replaces this one, on a terminal transition")
    ap.add_argument("--why", metavar="FACT_ID_OR_SUBJECT",
                    help="lineage query, or the rationale for a transition")
    ap.add_argument("--evidence", help="referent that justifies the transition")
    ap.add_argument("--run-gate", action="store_true",
                    help="execute --gate, archive its output, abort on nonzero exit")
    ap.add_argument("--bind-receipt", action="store_true",
                    help="with --run-gate, anchor the fact on the archived gate output "
                         "instead of the source slice")
    ap.add_argument("--archive-dir",
                    help=f"where gate receipts are written (default {GATE_ARCHIVE} for "
                         f"the default store, <db-dir>/<db-stem>-gates otherwise)")
    ap.add_argument("--repin", metavar="FACT_ID",
                    help="re-hash the anchor of a stale/conditional fact")
    ap.add_argument("--query", metavar="FTS")
    ap.add_argument("--include-contested", action="store_true")
    ap.add_argument("--include-retired", action="store_true")
    ap.add_argument("--include-falsified", action="store_true")
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--evict-session", action="store_true")
    ap.add_argument("--reconcile", action="store_true",
                    help="retire stale facts a newer live row for the same claim"
                         " has superseded; idempotent, safe at every checkpoint")
    ap.add_argument("--migrate", action="store_true",
                    help="report the additive schema diff; --apply runs it")
    ap.add_argument("--apply", action="store_true",
                    help="with --migrate: apply the diff in one transaction")
    ap.add_argument("--check-anchors", action="store_true")
    ap.add_argument("--check-evidence", action="store_true",
                    help="exit 1 if an archived gate output is missing, damaged or "
                         "the gate exited nonzero")
    ap.add_argument("--check-regime", action="store_true",
                    help="demote cross-regime claims against the live pin")
    ap.add_argument("--live-pin", metavar="PIN", nargs="?", const="", default=None,
                    help="the live regime pin: PIN to state it, bare to compose it "
                         "from OMP_MODEL_ID|OMP_RUNTIME_ID|device probe (CG-10)")
    ap.add_argument("--check-expiry", action="store_true",
                    help="CG-12 advisory age decay: report facts past the per-class "
                         "policy window, write a demote ledger row on first crossing, "
                         "change no status, always exit 0")
    ap.add_argument("--check-spine", action="store_true",
                    help="advisory: report spine numbers that bind to no fact, no cited "
                         "referent, no ledger row and no durable record")
    ap.add_argument("--strict-spine", action="store_true",
                    help="same scan, exit 1 unless every number binds to a fact, a cited "
                         "referent or a ledger row")
    ap.add_argument("--spine-dir", metavar="DIR",
                    help="spine root to scan, default the agent dir")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--stale", action="store_true",
                    help="list claims sitting in a historical status (stale/retired/"
                         "falsified), each with what made it historical and the command that"
                         " re-derives it; advisory, always exits 0")
    ap.add_argument("--status-only", action="store_true",
                    help="with --stale: group by status instead of by class")
    ap.add_argument("--class-only", action="store_true",
                    help="with --stale: group by frequency class only")
    ap.add_argument("--gate-only", action="store_true",
                    help="with --stale: one terse line per claim naming its re-derivation gate")
    ap.add_argument("--contested-facts", action="store_true",
                    help="list contested claims with their rival claims and the command that"
                         " adjudicates each; read-only, always exits 0")
    ap.add_argument("--corrected-by", metavar="NAME",
                    help="with --transition: stamp this verdict's adjudicator; the status"
                         " stays an evidence verdict, never a decision of its own")
    ap.add_argument("--correction-reason", metavar="TEXT",
                    help="with --transition --corrected-by: why the verdict was made; required")
    args = ap.parse_args(argv)

    # One knob, one name, and it never names the live store by a second path unnoticed
    # (2026-11-11: a leaked `export FACT_STORE_DB=/tmp/vd4/v.db` made the live spine audit a
    # scratch db). spine_check refuses that pairing outright; here the caller may genuinely
    # mean the scratch store - harnesses aim this CLI at fixture dbs on purpose - so the only
    # case worth interrupting is when the variable resolves to the store beside this file: two
    # names for the live store, and whichever the next reader assumes, one of them is wrong.
    _env_db = (os.environ.get("FACT_STORE_DB") or "").strip()
    if _env_db and (args.db or "") == str(DEFAULT_DB):
        if pathlib.Path(_env_db).expanduser().resolve() == (AGENT / "memory.db").resolve():
            print(f"fact_store: FACT_STORE_DB={_env_db} names the live store by a second"
                  f" path; the live store is {AGENT / 'memory.db'} - unset the variable, or"
                  " point it at the scratch db you mean", file=sys.stderr)

    db = pathlib.Path(args.db).expanduser()
    t0 = time.perf_counter()
    try:
        return dispatch(args, db, t0)
    except FactError as exc:
        print(f"fact_store: {exc}", file=sys.stderr)
        return 2
    except sqlite3.Error as exc:
        print(f"fact_store: sqlite error on {db}: {exc}", file=sys.stderr)
        return 2


def dispatch(args, db: pathlib.Path, t0: float) -> int:
    # --why doubles as the rationale flag of --transition/--repin, so it counts as a
    # mode only when neither of those is present.
    modes = [args.init, args.add, args.transition, args.repin, args.query,
             args.why and not (args.transition or args.repin), args.evict_session, args.reconcile,
             args.check_anchors, args.check_evidence, bool(args.check_regime),
             args.check_expiry, bool(args.check_spine or args.strict_spine),
             args.migrate]
    if sum(1 for m in modes if m) > 1:
        raise FactError("one mode per call (see --help)")
    # --run-gate/--bind-receipt have no effect in --add: adding pins the referent, and the
    # gate runs only inside a transition, which is where the ledger row is written. Accepting
    # them silently left the caller believing a gate had adjudicated an `unverified` row.
    if args.add and (args.run_gate or args.bind_receipt):
        raise FactError("--add runs no gate: --run-gate/--bind-receipt belong to "
                        "--transition, where the gate run and its ledger row are written")
    archive = archive_dir_for(db, args.archive_dir)

    if args.migrate:
        if not db.exists():
            raise FactError(f"no store at {db}; run --init first")
        if args.apply:
            conn = connect(db)
            cols, views = apply_migration(conn)
            intact = conn.execute("SELECT count(*) FROM facts").fetchone()[0]
            conn.close()
            what = (f"+{len(cols)} column(s), +{len(views)} view(s)"
                    if cols or views else "nothing: schema already current")
            print(f"migrate --apply {db} in "
                  f"{store_ms((time.perf_counter() - t0) * 1000)}: {what}; "
                  f"{intact} fact row(s) intact")
            return 0
        # mode=ro: a dry run cannot change a byte, and the receipt proves it.
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        cols, views = migration_diff(conn)
        conn.close()
        for _name, ddl in cols:
            print(f"missing column: {ddl}")
        for name, sql in views:
            print(f"missing view: {name}")
            print(sql)
        if cols or views:
            print("dry run: nothing applied, db untouched (--migrate --apply)")
        else:
            print("schema current; nothing to migrate")
        return 0

    if args.init:
        db.parent.mkdir(parents=True, exist_ok=True)
        conn = connect(db)
        init_schema(conn)
        tables = [r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view') "
            "ORDER BY name").fetchall()]
        conn.close()
        print(f"init {db} in {store_ms((time.perf_counter() - t0) * 1000)}: "
              f"{', '.join(tables)}")
        return 0

    # VS-33: a scan gate that was explicitly asked cannot report rc 0 for a store it
    # never scanned - that is the silent green this whole contract exists to kill.
    # Only evict_session, a housekeeping sweep over nothing, stays rc 0. The spine's
    # default run keeps its own route (absent store = note, naked store = named
    # violation) and never reads this rc as verdict or clean.
    if not db.exists():
        print(f"fact_store: no store at {db}; run --init")
        return 0 if (args.evict_session or args.reconcile) else 2

    conn = connect(db)
    if not has_store(conn):
        print(f"fact_store: {db} has no facts table; run --init")
        conn.close()
        return 0 if (args.evict_session or args.reconcile) else 2

    try:
        if args.add:
            subject, predicate, object_ = args.add
            if not args.source:
                raise FactError("--add needs --source path:L<start>-L<end>")
            row, created = add_fact(conn, subject, predicate, object_, args.klass,
                                    args.source, args.status, args.regime,
                                    check_claims(args.claims), args.gate)
            # --regime-scope means the same thing on every write path: without this the
            # flag is accepted on --add and silently left the fact model-scope.
            if args.regime_scope:
                conn.execute("UPDATE facts SET regime_scope = ? WHERE fact_id = ?",
                             (args.regime_scope, row["fact_id"]))
                conn.commit()
                print(f"  regime scope {args.regime_scope}")
            ms = (time.perf_counter() - t0) * 1000
            verb = "added" if created else "re-pinned"
            print(f"{verb} {row['fact_id']} "
                  f"({row['frequency_class']}:{row['status']}) "
                  f"{row['subject']} -> {row['predicate']} -> {row['object']} "
                  f"| {row['source_path']}:{row['line_range']} @ {row['sha256_pin'][:8]}"
                  f" | git {row['git_commit'] or '-'} | {store_ms(ms)}")
            return 0

        if args.transition:
            if not args.direction:
                raise FactError("--transition needs --direction")
            if not (args.why and args.evidence and args.gate):
                raise FactError("--transition needs --why, --evidence and --gate "
                                "(they are the ledger, not decoration)")
            res = transition(conn, args.transition, args.direction, args.to_class,
                             args.to_status, args.why, args.evidence, args.gate,
                             args.run_gate, args.claims,
                             args.regime or DEFAULT_REGIME_PIN,
                             args.bind_receipt, archive, args.superseded_by,
                             corrected_by=args.corrected_by,
                             correction_reason=args.correction_reason)
            if args.regime_scope:
                conn.execute("UPDATE facts SET regime_scope = ? WHERE fact_id = ?",
                             (args.regime_scope, res["fact_id"]))
                conn.commit()
                print(f"  regime scope {args.regime_scope}")
            ms = (time.perf_counter() - t0) * 1000
            print(f"{res['direction']} {res['fact_id']}: {res['from']} -> {res['to']} "
                  f"| regime {res['regime_pin']} | {store_ms(ms)}")
            if res.get("gate_kind"):
                print(f"  gate kind: {res['gate_kind']} (notes/gate-taxonomy.md)")
            if res["receipt"]:
                print(f"  receipt {res['receipt']}")
            return 0

        if args.repin:
            if not (args.why and args.evidence and args.gate):
                raise FactError("--repin needs --why, --evidence and --gate")
            res = repin(conn, args.repin, args.why, args.evidence, args.gate,
                        args.run_gate, args.regime or DEFAULT_REGIME_PIN,
                        args.bind_receipt, archive, args.source, args.regime_scope)
            if res["scope_changed"]:
                print(f"  regime scope {res['regime_scope']}")
            ms = (time.perf_counter() - t0) * 1000
            print(f"re-pinned {res['fact_id']}: {res['from']} -> {res['to']} "
                  f"@ {res['sha256_pin'][:8]} | regime {res['regime_pin']} "
                  f"| {store_ms(ms)}")
            print(f"  anchor {res['source_path']}:{res['line_range']}")
            return 0

        if args.query:
            rows = query(conn, args.query, args.klass, args.include_contested,
                         args.include_retired, args.include_falsified, args.limit)
            ms = (time.perf_counter() - t0) * 1000
            if args.json:
                payload = [{"fact_id": r["fact_id"],
                            "class": r["frequency_class"], "status": r["status"],
                            "subject": r["subject"], "predicate": r["predicate"],
                            "object": r["object"], "source": r["source_path"],
                            "line_range": r["line_range"], "sha256": r["sha256_pin"],
                            "regime_pin": r["regime_pin"],
                            "competing_claims": r["competing_claims"],
                            "adjudication_gate": r["adjudication_gate"]} for r in rows]
                print(json.dumps({"query": args.query, "rows": payload,
                                  "elapsed_ms": round(ms, 1)}, ensure_ascii=False))
            else:
                for r in rows:
                    print("\n".join(format_row(r)))
                print(f"query {args.query!r}: {len(rows)} fact(s) in {store_ms(ms)}")
            return 0 if rows else 1

        if args.why:
            found = lineage(conn, args.why)
            if args.json:
                payload = [{
                    "fact": {k: f[k] for k in f.keys()},
                    "supersedes": [r["fact_id"] for r in conn.execute(
                        "SELECT fact_id FROM facts WHERE superseded_by = ? "
                        "ORDER BY fact_id", (f["fact_id"],))],
                    "transitions": [{k: t[k] for k in t.keys()} for t in ts],
                } for f, ts in found]
                print(json.dumps(payload, ensure_ascii=False, default=str))
                return 0 if found else 1
            for fact, steps in found:
                print(f"WHY {fact['fact_id']}  {fact['subject']} -> "
                      f"{fact['predicate']} -> {fact['object']}  "
                      f"({fact['frequency_class']}:{fact['status']})")
                print(f"  source {fact['source_path']}:{fact['line_range']} @ "
                      f"{fact['sha256_pin'][:12]}  git {fact['git_commit'] or '-'}  "
                      f"regime {fact['regime_pin']}")
                if fact["superseded_by"]:
                    repl = conn.execute(
                        "SELECT subject FROM facts WHERE fact_id = ?",
                        (fact["superseded_by"],)).fetchone()
                    print(f"  superseded-by: {fact['superseded_by']}"
                          + (f"  {repl['subject']}" if repl else "  (row missing)"))
                for r in conn.execute(
                        "SELECT fact_id, subject FROM facts WHERE superseded_by = ? "
                        "ORDER BY fact_id", (fact["fact_id"],)):
                    print(f"  supersedes: {r['fact_id']}  {r['subject']}")
                if not steps:
                    print(f"  no transitions: added {fact['frequency_class']}:"
                          f"{fact['status']} on {fact['updated_at']}, never promoted")
                    continue
                for i, t in enumerate(steps, 1):
                    print(f"  {i}. {t['transitioned_at']}  [{t['direction']}] "
                          f"{t['from_state']} -> {t['to_state']}")
                    print(f"     why:      {t['why_rationale']}")
                    print(f"     evidence: {t['evidence_referent']}")
                    print(f"     gate:     {t['harness_gate']}")
                    print(f"     regime:   {t['regime_pin']}")
            print(f"lineage: {len(found)} fact(s) in "
                  f"{store_ms((time.perf_counter() - t0) * 1000)}")
            return 0 if found else 1

        if args.evict_session:
            rows = evict_session(conn)
            print(f"evict-session: retired {len(rows)} session:unverified fact(s) in "
                  f"{store_ms((time.perf_counter() - t0) * 1000)}")
            return 0

        if args.reconcile:
            moved, left = reconcile_stale(conn)
            for m in moved:
                print(f"RECONCILE {m['fact_id']}  {m['class']}:stale -> retired" 
                      f"  superseded-by {m['superseded_by']}"
                      f" ({m['replacement_status']})  {m['subject'][:64]}")
            print(f"reconcile: superseded {len(moved)} stale fact(s), "
                  f"{left} still stale, in "
                  f"{store_ms((time.perf_counter() - t0) * 1000)}")
            return 0


        if args.stale:
            return stale_scan(conn, args)

        if args.contested_facts:
            return contested_scan(conn, args)

        if args.check_anchors or args.check_evidence:
            stale: list[dict] = []
            scanned = 0
            ms = 0.0
            if args.check_anchors:
                res = check_anchors(conn)
                scanned, stale, ms = res["scanned"], res["stale"], res["ms"]
                for s in stale:
                    print(f"STALE   {s['fact_id']}  {s['ref']}  {s['reason']}",
                          file=sys.stderr)
                    print(f"        was {s['from_state']} -> stale [R1_CONTENT_DRIFT]; "
                          f"revalidate: {s['gate']}", file=sys.stderr)
                for s in res["contested"]:
                    print(f"CONTEST {s['fact_id']}  {s['ref']}  {s['reason']}",
                          file=sys.stderr)
                    print(f"        was {s['from_state']} -> contested"
                          " [R2_REFERENT_UNREADABLE]: the referent decides this one, not an"
                          " adjudication - fix the path or --repin it", file=sys.stderr)
                for s in res["healed"]:
                    print(f"HEAL    {s['fact_id']}  {s['ref']}  referent resolved ->"
                          f" {s['to_state']} [R0_HEALTHY]", file=sys.stderr)
                for s in res["rematching"]:
                    print(f"REPIN   {s['fact_id']}  {s['ref']}  stale but the slice"
                          f" re-matches its pin [R0_HEALTHY]: not healed, because a content"
                          " refutation is withdrawn by a gate run", file=sys.stderr)
                    print(f"        revalidate: python3 tools/fact_store.py --repin"
                          f" {s['fact_id']} --gate \"<gate>\" --why \"<why>\" --evidence"
                          f" \"{s['ref']}\"", file=sys.stderr)
                print(f"check-anchors: {scanned} anchored fact(s) scanned, "
                      f"{len(stale)} stale in {ms:.1f} ms"
                      f" | {len(res['contested'])} unreadable-referent contested,"
                      f" {len(res['healed'])} healed, {len(res['rematching'])}"
                      " stale-but-re-matching")
            cleared: list[dict] = []
            e_scanned, broken, ems = check_evidence(
                conn, archive, ledger=db.parent / SPINE_LEDGER_REL, infos=cleared)
            for c in cleared:
                print(f"INFO     {c['fact_id']}  {c['coord']}  {c['reason']}")
                print(f"         receipt copy lost, but sweep {c['sweep']} ran {c['gate']}"
                      f" at {c['stamp']} with rc=0 score={c['gate_score']}: the loop ledger"
                      " is the authoritative run record and the archive is its copy")
                print("         informational: not stale, not demoted; --verify"
                      " --bind-receipt re-anchors the copy when this claim is next used")

            for b in broken:
                print(f"EVIDENCE {b['fact_id']}  {b['coord']}  {b['reason']}",
                      file=sys.stderr)
                print(f"         was {b['from_state']} -> conditional; "
                      f"revalidate: {b['gate']}", file=sys.stderr)
            print(f"check-evidence: {e_scanned} archived gate receipt(s) verified, "
                  f"{len(broken)} broken, {len(cleared)} receipt copy(s) lost with the"
                  f" current sweep passing (informational) in {ems:.2f} ms")
            if args.check_evidence:
                for b in broken:
                    print(f"  re-run and --repin --bind-receipt: {b['gate']}",
                          file=sys.stderr)
            return 1 if (stale or broken) else 0

        if args.check_regime or args.live_pin is not None:
            live = (args.live_pin if args.live_pin not in (None, "")
                    else compose_live_pin())
            if not args.check_regime:
                print(live)
                return 0
            scanned, demoted, skipped, ms = check_regime(conn, live)
            if args.json:
                # CG-11: machine-readable sweep evidence; every demotion names the
                # dimensions the scoped predicate compared and the ones that differ.
                print(json.dumps({"at": now_iso(), "live_pin": live,
                                  "scanned": scanned, "compared": scanned - skipped,
                                  "demoted": demoted,
                                  "skipped_empty_dimensions": skipped,
                                  "ms": round(ms, 1)}, indent=1))
                return 1 if demoted else 0
            for d in demoted:
                print(f"DEMOTE  {d['fact_id']}  {d['from_state']} -> {d['to_state']}  "
                      f"pinned {d['pinned']}  scope {d['scope']}  "
                      f"differs at {d['differing_dimensions']}")
                print(f"        revalidate: {d['gate']}")
            print(f"check-regime {live}: {scanned} architectural/"
                  f"permanent/project active fact(s) checked, {scanned - skipped} compared, {len(demoted)} demoted in "
                  f"{ms:.1f} ms"
                  + (f", {skipped} skipped (live pin dimension empty)" if skipped
                     else ""))
            return 1 if demoted else 0

        if args.check_expiry:
            # CG-12: advisory. The count line is the contract; the command exits 0
            # whatever it finds, because an expired claim is an explained
            # withdrawal, not a violation. Failing here would make age a
            # spine-breaking event the schema says it is not.
            n_past, n_rows, ems = check_expiry(conn)
            print(f"check-expiry: {n_past} fact(s) past policy, {n_rows} demote "
                  f"row(s) written in {ems:.1f} ms (advisory)")
            return 0

        if args.check_spine or args.strict_spine:
            # Advisory by default: an unbound spine number is a scheduling gap, not a broken
            # store, and a refusing default trains callers to skip the scan. --strict-spine
            # is the same scan with the weak bucket disallowed, for a checkpoint that wants
            # the stronger rule.
            root = (pathlib.Path(args.spine_dir).expanduser() if args.spine_dir else AGENT)
            rep, sms = check_spine(conn, root)
            b = rep["buckets"]
            print(f"check-spine: {rep['scanned']} measured number(s) in "
                  f"{', '.join(SPINE_FILES)} - {b['fact']} fact, {b['cited']} cited, "
                  f"{b['ledger']} ledger, {b['recorded']} recorded, {b['exempt']} exempt, "
                  f"{len(rep['unbound'])} unbound in {sms:.1f} ms")
            for u in rep["unbound"]:
                print(f"UNBOUND {u['where']}  {u['literal']}  {u['reason']}: {u['line']}",
                      file=sys.stderr)
            if args.strict_spine:
                for w in rep["weak"]:
                    print(f"WEAK    {w['where']}  {w['literal']}  {w['reason']}",
                          file=sys.stderr)
            if args.json:
                print(json.dumps(rep, indent=1))
            return 1 if (args.strict_spine and (rep["unbound"] or rep["weak"])) else 0

        rows = conn.execute("SELECT COUNT(*) n FROM facts").fetchone()["n"]
        led = conn.execute("SELECT COUNT(*) n FROM transitions").fetchone()["n"]
        print(f"fact store {db}: {rows} fact(s), {led} transition(s), "
              f"regime default {DEFAULT_REGIME_PIN}")
        print("\n".join(census(conn)))
        print("modes: --init --add --transition --repin --query --why --evict-session "
              "--check-anchors --check-evidence --check-regime --live-pin "
              "--check-expiry --check-spine --strict-spine")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    try:
        _code = main()
    except BrokenPipeError:
        try:
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        except OSError:
            pass
        _code = 0
    sys.exit(_code)
