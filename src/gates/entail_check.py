#!/usr/bin/env python3
"""entail_check.py - claim/evidence agreement for a project record.

Why: spine_check's claim gate proves a measured claim NAMES a referent; it never opens the
referent (notes_f15.md: the gate is "blind to ... claim-vs-evidence agreement"). This tool
opens it. Every numeric claim in a record that carries a metric word is bound to the
machine-readable artifacts under the project root and compared at the precision the record
itself states. A record that quotes a number its own artifact no longer says is caught here.

This is not a semantic-entailment pass over recaps (ownership.py S5 stays a lower bound): it
compares numbers against artifacts, not prose against prose.

Usage
  tools/entail_check.py --claims TRANSCRIPT --receipts PATH... [--json] [--strict-uncheckable]
  tools/entail_check.py RECORD [--root DIR]... [--evidence FILE]...
                               [--metrics m1,m2] [--json] [--strict]
                               [--min-claims N] [--min-coverage F] [--min-numerals N]
                               [--lines N:M | --section HEADING]

                               --claims mode verdicts: SUPPORTED (every fact the line states
                               appears in a receipt), PARTIAL (some do), UNANCHORED (none
                               do), UNCHECKABLE (the line states no number, score or file
                               name - advisory unless --strict-uncheckable). It proves
                               restatability, never truth: a figure copied into two records
                               is still one figure. A receipt set that reads nothing is a
                               usage error, because a mode that calls every claim supported
                               over an empty corpus is worse than no mode.

Two floors are defaults, not options. The coverage floor - --min-coverage (0.5) of the numerals
must sit in a paragraph that names where they came from: a cited artifact, a receipt coordinate,
a bound claim, or an explicit "no artifact" declaration - and the default claim floor of
--min-claims (5) both wait until --min-numerals (40) numerals, because a three-figure note is not
a record dodging its work. An explicit --min-claims is honoured at any size. An opt-in floor is
a floor you walk past, and `0 CONTRADICTED` over 300 unparsed numerals reads as clean to
everything downstream. Pass --min-coverage 0 to waive the coverage floor.

Two slice flags audit part of a long record without losing its context, for the records whose
whole-record run does not finish (amber_ascii perm-record.md, 1,707 lines, timed out at 1500 s):
--lines N:M audits 1-based inclusive lines N:M; --section HEADING audits the `## ` section whose
heading matches a section id (28 or the §28 spelling) or a heading-text prefix, running to the
next `## ` heading or EOF. Citations, receipts and evidence bases still resolve across the FULL
record; only claims inside the slice are judged, and every verdict keeps its original-file line
number. The default whole-record floors are waived on a slice unless --min-claims or
--min-numerals is passed explicitly.

Evidence, discovered under each --root (default: the record's own directory), cited artifacts
read first because a record's evidence is what the record names:
  DAG.json       gate_result.output summaries ("71 checks, 0 fail", "VmHWM 126.6 MB", "23.7 s")
                 and node status counts
  receipts       any archive/gates/*.log:L1-L<N>@<hash> a node cites is opened with
                 tools/fact_store.verify_receipt, and its header fields (checks, fail,
                 vmhwm_mb, duration_ms, exit_code) become evidence under those names
  run/evidence/  copies --run-gate kept of an earlier run's reports: a figure that old run
                 measured stays entailed after the live output is rewritten, so a number
                 matching neither the live file nor any kept copy is damage, not age
  *.json         every scalar, indexed by leaf key and by dotted path
  source files   the line count of a file a "N lines" claim names in backticks

Verdicts, per claim
  ENTAILED      the metric exists in the evidence and a value agrees at the stated precision
  CONTRADICTED  the metric exists in the evidence and no value agrees
  UNBOUND       no artifact carries the metric - reported, never silently passed
  DECLARED      the paragraph says "no artifact" in its own words, so the figure is
                transcript-only by declaration: a record disowning a figure is a different
                finding from one it simply failed to cite, and a declared figure compared to
                an unrelated leaf in the same unit is a wrong comparison, so it never lands in
                CONTRADICTED either. Scoped to the paragraph, never the file.
  BAR           the number is a declared bar/target/limit, not a measurement - not checked

Claim families (a number with no metric word and no `key = value` form is not extracted; a
`key: value` whose key exists in no artifact is prose, not a claim, and is dropped):
  N checks [ / N fail]     N lines     N nodes     A/B nodes
  N fps                    N ms        N MB        N s
  `key = value`, `key: value`, `key value UNIT`    (key matched against artifact paths/leaves)
  key 0.739066             a decimal stated against a leaf the artifacts carry, unquoted
  `sha12` N                a pin: digest and byte size, checked against the file itself
  A/B                      pair: bound to an artifact object holding B as a denominator

A citation that names lines outranks a leaf lookup: when `x.log:7@pin` or `x.log:L1-L40@pin`
hashes to its pin and the figure is printed on a cited line, the claim is ENTAILED by that line.
Prose reads seconds where a receipt declares milliseconds, so an `s`/`ms` claim also binds the
scaled literal on a cited line; a `key = value` claim never scales.

Exit 0 no CONTRADICTED - 3 >=1 CONTRADICTED or UNDERAUDITED - 2 usage - 4 no readable evidence
"""
import argparse
import fnmatch
import hashlib
import json
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import fact_store  # noqa: E402  receipt coordinates are parsed and pinned by that tool

# A receipt coordinate names the archive this tool writes, which lives under the agent root.
# A project record quoting `archive/gates/<hex>_<ts>.log:L1-L3@sha` is right about the
# coordinate and wrong about the base: the base is not the record's directory. Every loom
# note cites its own gate receipt in its header, and until the tool's root joined the
# candidates none of those figures could bind (notes/G6.md, 2026-11-05).
AGENT_ROOT = pathlib.Path(__file__).resolve().parents[1]

EXIT_USAGE, EXIT_NO_EVIDENCE, EXIT_CONTRADICTED = 2, 4, 3
MAX_ARTIFACT_BYTES = 12 * 1024 * 1024
# The budget has to hold a closed project's own measured outputs: loom carries 16 gate reports,
# its DAG, the gate receipts and the scenario fixtures, and at 60 the fixtures evicted the
# reports - a note then contradicted a fixture because the file that proves it was never read.
MAX_ARTIFACTS = 96
# The record's own cites outrank the directory sweep. A long record cites more files than the
# budget holds, and a receipt the record pinned that fell out of it was answered by whichever
# sibling did get parsed: a true figure contradicted by the file next to it. Named evidence gets
# its own ceiling, wide enough for a project's whole run history, so the cap never chooses for
# the record which of its own citations count.
MAX_NAMED_ARTIFACTS = 4 * MAX_ARTIFACTS
# A closed node cites one receipt per gate run; more than a handful in one node means the
# node is a dumping ground, and reading them all would crowd out the artifacts.
MAX_RECEIPTS_PER_NODE = 4
# A record is not a node: a closed project's record cites one receipt per gate it ran, and a
# tally quoted from the fifth receipt read nothing but the first four is judged blind - the
# note admitted it. Records get a budget the size of a project's run history.
MAX_RECEIPTS_PER_RECORD = 128
# Elements indexed per list before the value is treated as bulk data rather than a list a
# record would quote. Real seed/frames/hash lists in gate evidence are tens, not thousands.
MAX_LIST_ELEMENTS = 1024
# What a sentence can name as its referent. One greedy token class with no alternation:
# a leftmost-first regex settles for "a" as a path before ever trying the coordinate form.
# The kind is decided afterwards - coordinate, `path:163-183`, or a dotted file name.
# a comma-list coordinate (`log:3-9,25,84,86@sha`) is one citation, so a comma continues a token
SCOPE_RE = re.compile(r"[A-Za-z0-9_][\w./:@,-]*")
COORD_TAIL = re.compile(r":L\d+-L\d+@[0-9a-f]{6,}$")
LINE_TAIL = re.compile(r":\d+(?:-\d+)?$")
# a record also pins bytes without the L form: `log:43@sha`, `log:3-8@sha`, `log:3-9,25@sha`.
# Dropping those tokens made a cited receipt invisible, so figures it carried came back UNBOUND
# or were contradicted by a sibling artifact sharing a key.
PINNED_TAIL = re.compile(r"(?:[Ll]?\d+(?:-[Ll]?\d+)?(?:,\s*[Ll]?\d+(?:-[Ll]?\d+)?)*)?"
                         r"@[0-9a-f]{6,}$", re.IGNORECASE)
# a file name is a referent only with a real extension: "0.1.48.1" and a sentence-final
# "...for the run." must never name one
FILE_EXT = {"json", "jsonl", "log", "md", "py", "txt", "csv", "db", "sqlite", "html", "spec"}
# overwrite them. Reading those copies is what makes CONTRADICTED mean damage rather than
# age (Projects/amber_ascii/perm-record.md §16).
ARCHIVED_MARK = "run/evidence/"
SKIP_DIRS = {".git", "node_modules", "__pycache__", "dist", "build", ".venv", "venv"}
# An "A/B" claim binds only when B sits under one of these keys in the same artifact object.
DENOM_KEYS = {"n", "ref_n", "total", "plan", "frames", "nodes", "count", "samples", "fail",
              "checks", "pass"}
# A channel's depth is a quantum, not a population: `1/255` and `2/255` are the byte step and a
# rounding tolerance, so there is no ratio to bind and no artifact counts a population of 255.
# Same status as `code` in the pair family: an expression the record quotes, not a figure it claims.
BIT_DEPTH = {255, 256, 1023, 4095, 65535}
# An operator before a counter noun means the number is an operand or an arrow result inside an
# expression the record is describing (`%3 feeds cc22`, `200→127 clamp`), not a tally any harness
# kept. Bound to the non-space character before the match, so `n = 46 passes` still counts.
OPERAND_BEFORE = set("*%/-+=><&|^~\u2192\u21d2")
# Unit word -> the artifact leaf keys that can carry it. A claim that names its leaf ("p95 85 ms")
# is scoped to that leaf; otherwise the whole unit family is the candidate set.
UNIT_KEYS = {
    "ms": {"ms", "mean", "p50", "p95", "p99", "p99_9", "p99.9", "max", "min", "median"},
    "mb": {"mb", "peakmb", "currentmb", "heapmb", "vmhwm", "rss"},
    "s": {"s", "secs", "seconds", "elapsed", "duration"},
    # derive_fps() writes its 1000/ms values under this leaf; without the entry an fps
    # claim could never bind and would report UNBOUND forever.
    "fps": {"fps", "fpsmedian", "fpsmean", "fps_p50", "fps_p95"},
    "nodes": {"nodes"},
    # a record counts what it enumerated. `checks` is a JSON *list*, so the figure the record
    # states for it is the list length (walk_json registers it); pass/fail are the verdicts
    # counted off that list.
    "checks": {"checks", "pass", "fail", "n"},
    "bytes": {"bytes"},
}
# A receipt header declares a duration in milliseconds; a record quotes seconds. The scale
# factor is the only freedom a seconds claim gets: it must still name a duration key.
MS_TO_S_KEYS = {"duration_ms", "elapsed_ms", "wall_ms", "total_ms", "run_ms"}
# `ink 18.06 %` is `inkFrac: 0.180611` written as a percentage; `rate 3 %` is `ratePct: 3`
# divided by 100. A record quoting one form binds the leaf that holds the other, scaled.
FRAC_SCALES = (("frac", 100.0), ("fraction", 100.0), ("ratio", 100.0),
               ("percent", 0.01), ("pct", 0.01))
# A number next to one of these is a declared bar, not a measurement.
BAR_BEFORE = ("≥", "≤", "<=", ">=", "<", ">", "bar", "target", "limit", "budget", "threshold",
              "at least", "no more", "required", "must")
BAR_AFTER = ("bar", "target", "limit", "budget", "threshold", "required")
# The trailing comma is optional and never the last character: `1,024` is one number, while
# `1, checks failed` is the numeral `1` plus the end of a clause. Taking the comma made the
# numeral `1,`, which matches no artifact, so a clean harness line reported UNBOUND forever.
NUM = r"(?:\d[\d,]*\d|\d)(?:\.\d+)?"
GATE_CHECKS_RE = re.compile(r"(\d+)\s+checks.*?(\d+)\s+fail", re.I)
GATE_MB_RE = re.compile(r"([\d.]+)\s*MB", re.I)
GATE_S_RE = re.compile(r"\(([\d.]+)\s*s[,)]", re.I)
# A record pins the PNG a gate drew as well as the JSON it parsed, so the cited set takes
# images and logs too: they are pinned by digest and size, never walked.
# The optional coordinate tail is part of the cite: `path.log:L1-L40@sha12` names a real
# artifact, and requiring the token to end at the extension dropped every pinned reference a
# record made, so the receipt a note quoted was never read.
CITED_RE = re.compile(r"`([^`\n]{2,120}?\.(?:json|py|js|mjs|md|sh|gd|glsl|ts"
                      r"|png|jpg|jpeg|webp|log|txt|html|css|svg))"
                      r"(?:[:@][^\s`,]{0,44})?`")
FENCE_RE = re.compile(r"^\s*(```|~~~)")
# A preposition, not a measurement key. Artifacts really do carry a leaf named `of` or `x`
# (report-G2.json has one), so the key lane's existence test passes for them and prose ("a
# scale of 13.43", "4 x 0.005", "agrees to 0.19") is judged against a quantity it never
# named, against a value that has nothing to do with the sentence.
STOP_KEYS = {"of", "to", "on", "at", "in", "by", "or", "and", "as", "is", "was", "were",
             "be", "been", "no", "not", "it", "its", "the", "all", "any", "x", "n", "a",
             "an", "per", "for", "from", "into", "over", "under", "than", "then", "with",
             "up", "if", "so"}
# `1.73e-18` is one figure: the exponent is part of the number. Cutting at the dot judged a
# bare mantissa against whatever decimal shared the key word, and reported CONTRADICTED
# against the record's own receipt value.
SCI_RE = re.compile(r"^(?P<m>-?\d+(?:\.\d+)?)[eE](?P<e>[+-]?\d+)$")
NUM_SCI = NUM + r"(?:[eE][+-]?\d+)?"
# A gate receipt is a `.log`, and it is the only artifact carrying a run's own duration_ms,
# checks and fail count. A record quoting those numbers from those bytes deserves the same
# binding a JSON leaf gets, so keyed scalars in text are indexed too - capped, because a log
# is a transcript, not a value table. Markdown is absent on purpose: a record is a claim, and
# one record's figure must not become the evidence that entails another's.
TEXT_KV_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_.]{1,28})\s*[:=]\s*(-?\d+(?:\.\d+)?)")
MAX_TEXT_KV = 400
# Space-separated pairs are read in a second pass with their own cap, so a runner's `key value`
# evidence line cannot crowd the file's own `key: value` pairs out of the index.
MAX_TEXT_KV_SP = 400
# The regex passes read a byte prefix, not the whole file. An rgb probe dump or a
# browser report is a multi-megabyte wall of digits where `key 1.23` matches millions
# of times without indexing anything a record cites (measured: 70+ s in one such file,
# the 1500 s whole-record audit dying there). A receipt's keyed lines live inside this
# cap; past it a file is still pinned by digest and walked for JSON scalars.
MAX_TEXT_SCAN_BYTES = 512 * 1024
# A receipt header reads `GATE G4A PASS 29 checks 0 fail`: value before key, the one ledger
# form TEXT_KV_RE cannot see. Without it a note quoting its own gate's check count reports
# UNBOUND against the very receipt that carries the count.
# A receipt or a JSON runner writes a measurement into prose: an evidence line reads
# `... png_ms_median 28.597, png_frames 60 ...` inside a string value, and a log stamps
# `png_ms_median 28.597` with no colon at all. The keyed walk sees neither, so a record citing
# that evidence line was left UNBOUND against the bytes carrying its own figure. A `key value`
# pair is read from measurement transcripts only - in source or markdown the same shape is a
# sentence, and one record's figure must never become the evidence that entails another's.
# The key head is length-bounded: an unbounded `*` start class turns a long unbroken
# token - a base64 blob inside a browser report, a digit wall inside a dump - into
# quadratic backtracking per start position (measured: one 5 MB file spent more than
# 60 s inside this finditer and killed a 1500 s audit). 30 characters is the longest
# key the receipt shapes were ever written for; a longer run was never a key anyway.
TEXT_KV_SP_RE = re.compile(r"([A-Za-z][A-Za-z0-9_.\-]{0,28}[_.\-][A-Za-z0-9_.\-]{1,28})\s+"
                           r"(-?\d+(?:\.\d+)?)\b")
TEXT_KV_SP_SUFFIX = {".json", ".log", ".txt"}
TEXT_COUNT_RE = re.compile(r"(?P<v>\d[\d,]*)\s+(?P<k>checks|passes|pass|fails?|nodes|lines|frames"
                           r"|steps|shots|arms|scenarios)\b", re.I)
# The nouns that can stand on the numerator side of a receipt footer; `fail`/`fails` is always
# the other side, so it is absent here by design.
# A gate or harness stamps these onto its own report: the wall it waited, the envelope it
# was run inside, its exit code, the harness's own tally. A record quoting one of these
# figures names the gate's bookkeeping, not the quantity the row is about, so a different
# value under one of them is a different subject and cannot call the row wrong.
GATE_ENVELOPE = {"duration_ms", "wall_ms", "wall_time_s", "wall_s",
                 "budget_ms", "budget_s", "timeout_ms", "timeout_s", "exit", "exit_code",
                 "returncode", "checks", "fails", "fail", "passed", "failed"}

# Unit words, so a key's own spelling can be compared with a leaf path's without one
# side's prose token (`wall`, `mean`) vetoing the match.
UNIT_TOKENS = {"ms", "s", "sec", "secs", "seconds", "minutes", "fps", "hz", "kb", "mb"}

# A pinned receipt coordinate carries the lines it read before the field it read
# (`x.log:L1-L18 duration_ms`): the range locates the bytes, the word after it names the quantity.
LOG_LEAF_RANGE = re.compile(r"^L\d+(?:-L?\d+)?(?=[\s.])")

# The words a record uses for a receipt's own envelope. `wall` and `duration` are one quantity
# told two ways; the unit suffix on the leaf is not.
ENVELOPE_SYNONYMS = {"duration": {"wall", "elapsed", "runtime", "took", "ran", "took_s"},
                     "checks": {"tests", "harness", "tally"},
                     "exit": {"status", "code", "rc"}}


TEXT_COUNT_NOUNS = {"checks", "nodes", "lines", "frames", "steps", "shots", "arms",
                    "scenarios", "pass", "passes"}
TEXT_INDEX_SUFFIX = {".log", ".txt", ".js", ".mjs", ".ts", ".css", ".html", ".glsl",
                     ".py", ".sh", ".jsonl", ".csv", ".yaml", ".yml", ".toml"}

# A coverage figure that charges a record for every digit it wears is a joke about
# punctuation: an ISO date, a `:L171-L193` ledger coordinate, a commit sha, a gate id and a
# `sha256` label are structure, not measurement. A numeral counts only when it stands alone.
DATEISH_RE = re.compile(r"\d{4}-\d{2}(?:-\d{2})?(?:[T ]\d{2}:\d{2}(?::\d{2})?)?|\d{2}:\d{2}:\d{2}")
STANDALONE_RE = re.compile(r"(?<![\w.:/-])(?:\d[\d,]*\.\d+|\d{2,}[\d,]*)(?![\w./:-])")


# `13. do the thing` is a numbered list, not a measurement of 13 anything.
LISTISH_RE = re.compile(r"(?m)^[ \t]*\d{1,3}[.)][ \t]+")


# `0/3/5` is a tuple and `24 / 32 / 48 / 64` a series: no artifact holds either as a ratio,
# and charging their digits to the record punishes quoting structure. A two-part `a/b` stays
# in the denominator - that is a ratio claim.
TUPLEISH_RE = re.compile(r"\d+(?:\s*/\s*\d+){2,}")


# A markdown heading's section number is furniture, not a measurement: `### 1.2 The oracle
# draws the shipped geometry` asserts nothing about 1.2. The rest of the heading line is charged.
HEADNUM_RE = re.compile(r"(?m)^[ \t]{0,3}#{1,6}[ \t]+\d+(?:\.\d+)*[ \t.]*")


def denoise(text):
    """The denominator's noise strip, shared by the whole-record and per-line counts."""
    return TUPLEISH_RE.sub(" ", DATEISH_RE.sub(" ", LISTISH_RE.sub("", HEADNUM_RE.sub("# ", text))))


def audit_numerals(text):
    """Every numeral this record is actually claiming, for the coverage denominator."""
    return STANDALONE_RE.findall(denoise(text).replace("/", " "))


# The declaration a paragraph makes about its own figures, shared by the claim extractor (which
# spares a declared figure from comparison) and the coverage sensor (which credits one).
NO_ARTIFACT_RE = re.compile(r"\bno\s+artifact\b", re.I)

# A paragraph that says `.json`, `report-G5.json` or `:L171` has told the reader where its
# digits came from, backticks or no: that is what makes a paragraph checkable.
SOURCE_RE = re.compile(r"\.(?:json|jsonl|log|md|py|mjs|js|png|csv|txt|html|spec|db)\b|:L\d+")


def clean_line(line):
    """Per-line form of the denominator's noise strip; line count never changes."""
    return denoise(line)


BULLET_RE = re.compile(r"^(?:[-*+]|\d+[.)])[ \t]+")
# A markdown table row. Rows cannot carry an inline citation without a fake sixth column, so a
# block inherits the citation of the sentence that introduces it - the nearest non-blank line
# above, and only if that line (or a row) actually names an artifact, a coordinate or an anchor.
# A table nobody introduced still costs, which is the whole point of the measure.
TABLE_ROW_RE = re.compile(r"^\s*\|")


def paragraph_spans(lines):
    """[(first, last)] inclusive assertion units: a blank-line markdown paragraph, and each list
    item inside one. A bullet list is written without blank lines but asserts one thing per item,
    so a citation or a declaration in one item is not a claim about its neighbour's digits."""
    out, start = [], None
    for i, ln in enumerate(lines):
        if ln.strip():
            if start is not None and BULLET_RE.match(ln):
                out.append((start, i - 1))
            if start is None or BULLET_RE.match(ln):
                start = i
        elif start is not None:
            out.append((start, i - 1))
            start = None
    if start is not None:
        out.append((start, len(lines) - 1))
    return out


def attribution(record_text, rows):
    """(accounted, charged, bound, unchecked) numerals.

    Charged: measurement-shaped digits outside fences. Accounted: those in a paragraph an
    auditor can check - one that cites an artifact, names a line coordinate, binds a claim, or
    declares its figures transcript-only. A coverage figure that charges a bare digit in a
    paragraph pinning `report-G5.json` measures prose punctuation, not audit quality: the reader
    can check that digit, which is the whole point. A paragraph that cites nothing and declares
    nothing is the case that must cost something, and it does. A table block is credited by the
    sentence above it, which is where a table's source is written; a block whose intro names
    nothing still costs. The fourth value names those units, largest first: a floor that only
    prints a percentage gets cite-orandom prose.
    """
    lines = strip_fences(record_text)
    entailed = {r["line"] for r in rows if r["verdict"] == "ENTAILED"}
    per_line = {i: len(STANDALONE_RE.findall(clean_line(ln).replace("/", " ")))
                for i, ln in enumerate(lines)}
    charged = sum(per_line.values())

    def cites(ln):
        return bool(CITED_RE.search(ln)) or bool(SOURCE_RE.search(ln))

    table_credited = set()
    i = 0
    while i < len(lines):
        if not TABLE_ROW_RE.match(lines[i]):
            i += 1
            continue
        j = i
        while j < len(lines) and TABLE_ROW_RE.match(lines[j]):
            j += 1
        intro = next((k for k in range(i - 1, -1, -1) if lines[k].strip()), None)
        if any(cites(lines[k]) for k in range(i, j)) \
                or (intro is not None and cites(lines[intro])):
            table_credited.update(range(i, j))
        i = j

    accounted = 0
    unchecked = []
    for a, b in paragraph_spans(lines):
        body = "\n".join(lines[a:b + 1])
        n = sum(per_line.get(i, 0) for i in range(a, b + 1))
        if not n:
            continue
        if (bool(NO_ARTIFACT_RE.search(body)) or bool(CITED_RE.search(body))
                or bool(SOURCE_RE.search(body))
                or any(a + 1 <= x <= b + 1 for x in entailed)
                or any(i in table_credited for i in range(a, b + 1) if per_line.get(i))):
            accounted += n
        else:
            unchecked.append({"lines": [a + 1, b + 1], "numerals": n,
                              "text": " ".join(body.split())[:160]})
    unchecked.sort(key=lambda u: -u["numerals"])
    bound = sum(len(audit_numerals(r["excerpt"])) for r in rows if r["verdict"] == "ENTAILED")
    return accounted, charged, bound, unchecked


class Evidence:
    def __init__(self):
        self.digests = []      # (sha12, size, name) - pins, over every file, parsed or not
        self.by_leaf = {}      # leaf key -> [(value, referent)]
        self._seg_cache = {}   # referent -> leaf segments, see `_segs`
        self._tok_cache = {}   # leaf key -> leaf tokens, see `_toks`
        self.paths = {}        # dotted path -> [(value, referent)]; two artifacts may share a path
        self.objects = []      # [(path, {key: value})] for pair claims
        self.files = []
        self.errors = []
        # file name -> [(key, value, referent)] from receipts and source files, consulted only
        # for a claim whose own sentence names that file
        self.scoped_kv = {}
        # the record under judgement: a gate's own report is its measurement's home, and a leaf
        # of the same name in the next gate's report is that gate's number, not this one's
        self.record = None

    def add(self, leaf, value, referent):
        self.by_leaf.setdefault(str(leaf).lower(), []).append((value, referent))

    def partner(self, key):
        """Leaves holding `key` as a fraction/percent of the same quantity (O(1) tail-indexed)."""
        key = str(key).lower()
        sig = (len(self.paths), len(self.by_leaf))
        if getattr(self, "_partner_sig", None) != sig:
            tail_idx = {}
            for path, entries in self.paths.items():
                tail_idx.setdefault(path.rsplit(".", 1)[-1], []).append((path, entries))
            self._path_by_tail = tail_idx
            self._partner_cache = {}
            self._partner_sig = sig
        cached = self._partner_cache.get(key)
        if cached is not None:
            return list(cached)
        out = []
        for suffix, scale in FRAC_SCALES:
            target = key + suffix
            for path, entries in self._path_by_tail.get(target, ()):
                out.extend((v * scale, f"{path} (x{scale:g})") for v, _ in entries)
            for v, r in self.by_leaf.get(target, ()):
                out.extend([(v * scale, f"{r}:{target} (x{scale:g})")])
        self._partner_cache[key] = out
        return list(out)

    def _segs(self, referent):
        """Undecorated `_`-segments of a referent's leaf, memoized (used by `unit.hit`)."""
        got = self._seg_cache.get(referent)
        if got is None:
            got = [re.sub(r"\[\d+]$", "", x)
                   for x in referent.split(":")[-1].replace(".", "_").split("_")]
            self._seg_cache[referent] = got
        return got

    def _toks(self, key):
        """`_`-tokens of an indexed leaf key, memoized (used by `unit.hit`)."""
        got = self._tok_cache.get(key)
        if got is None:
            got = key.split("_")
            self._tok_cache[key] = got
        return got

    def unit(self, metric, leaf=None):
        """Candidate values for a unit claim, narrowed to a named leaf when the record names one."""
        metric = metric.lower()
        want = (leaf or metric).lower()
        exact = set(UNIT_KEYS.get(metric, set()))

        # Prose is plural where the artifact is singular ("seeds 1337, ..." against
        # "seed": 1337) and the other way round ("N checks" against check_count). The two
        # spellings name the same quantity; the >= 4 guard keeps "ms" from becoming "m".
        alt = (metric[:-1] if metric.endswith("s") else metric + "s") if len(metric) > 3 else metric

        def hit(key, referent):
            # The record's word is usually a segment of the artifact's leaf, not the whole leaf:
            # "p95 33.208 ms" is p95_frame_ms, "median 28.948 ms" is median_frame_ms, and a
            # nested leaf is reachable only through its dotted path ("tone 0.348 ms" is
            # tone.ms_median). Segment equality, never substring: "s" must not match "frames".
            # Without this a record that names its referent honestly reports CONTRADICTED
            # against a value the artifact carries verbatim.
            # A leaf filed as an array element is still that leaf: `frames[0]` is the report's
            # `frames`, and a report that stores its readings in arrays must not become
            # invisible to the word the record uses for it - which is how a plural claim used
            # to reach only another gate's singular `frame` and report damage that was not there.
            # Both forms are memoized per leaf: the index carries tens of thousands of entries
            # and every unit claim in the record probes all of them, so re-splitting and
            # re-`re.sub`-ing the same leaf per claim - not the matching - is what made a
            # 1.4k-line record cost minutes.
            segs = self._segs(referent)
            toks = self._toks(key)
            if leaf:
                return want in segs or want in toks
            return key in exact or metric in segs or metric in toks or alt in segs or alt in toks

        sig = (len(self.by_leaf), sum(len(v) for v in self.by_leaf.values()))
        if getattr(self, "_unit_sig", None) != sig:
            flat = []
            by_word = {}
            by_key_ord = {}
            idx_n = 0
            for k_str, entries in self.by_leaf.items():
                toks_set = set(self._toks(k_str))
                k_ords = []
                for e in entries:
                    flat.append(e)
                    k_ords.append(idx_n)
                    for w in toks_set | set(self._segs(e[1])):
                        by_word.setdefault(w, []).append(idx_n)
                    idx_n += 1
                by_key_ord[k_str] = k_ords
            self._unit_flat = flat
            self._unit_by_word = by_word
            self._unit_by_key = by_key_ord
            self._unit_cache = {}
            self._unit_sig = sig
        ckey = (metric, want if leaf else None)
        cached = self._unit_cache.get(ckey)
        if cached is not None:
            return list(cached)
        ords = set()
        if leaf:
            ords.update(self._unit_by_word.get(want, ()))
        else:
            for ek in exact:
                ords.update(self._unit_by_key.get(ek, ()))
            ords.update(self._unit_by_word.get(metric, ()))
            if alt != metric:
                ords.update(self._unit_by_word.get(alt, ()))
        flat = self._unit_flat
        out = [flat[i] for i in sorted(ords)]
        if metric == "s":
            # `duration 155.5 s` and `duration_ms: 155527.49` are one measurement; without the
            # scale a seconds claim can never bind the place a harness declares its own runtime.
            for k in MS_TO_S_KEYS:
                if k in self.by_leaf:
                    pass
            out.extend((v / 1000.0, f"{r} (ms/1000)")
                       for k, entries in self.by_leaf.items() if k in MS_TO_S_KEYS
                       for (v, r) in entries)
        self._unit_cache[ckey] = out
        return list(out)

    def note(self, text):
        self.errors.append(text)


def to_num(text):
    return float(text.replace(",", ""))


def decimals(text):
    return len(text.split(".", 1)[1]) if "." in text else 0


def sci_value(text):
    """A stated figure as (value, precision, exponent-form). `1.73e-18` carries two decimals of
    mantissa, not five decimals of anything."""
    m = SCI_RE.match(text)
    if m:
        return float(m.group("m")) * 10 ** int(m.group("e")), decimals(m.group("m")), True
    return to_num(text), decimals(text), False


def agree(claim, evidence, precision, sci=False):
    """Compare at the precision the record states: '85 ms' against 85.4 is an agreement.
    An exponent-form figure states a mantissa, and decimal places mean nothing at 1e-18, so it
    agrees within the rounding of that mantissa."""
    if sci:
        if claim == 0 or evidence == 0:
            return claim == evidence
        return abs(evidence - claim) <= abs(claim) * 0.5 * 10 ** -precision * 1.000001
    if precision == 0:
        return round(evidence) == int(claim)
    return round(evidence, precision) == round(claim, precision)


def same_quantity(claim, evidence):
    """Contradiction needs the same quantity, not merely the same word. A key collision far
    away in scale is a figure this artifact never measured, and the honest verdict for that is
    UNBOUND; a real drift (115.5 against 115.27) stays inside the band and still reports."""
    a, b = abs(claim), abs(evidence)
    if a == 0 or b == 0:
        return a == b
    return a <= 2 * b and b <= 2 * a


def code_spans(text):
    """Ranges the record set in inline code. A figure inside one is quoted structure - a probe
    input, an identifier, a superseded number the record is calling out - unless the sentence
    also names the coordinate its bytes live at."""
    return [m.span() for m in re.finditer(r"`[^`\n]+`", text)]


def fmt(value):
    return f"{value:g}"


# ---------------------------------------------------------------- evidence

def walk_json(node, path, ev, referent):
    if isinstance(node, dict):
        scalars = {k: v for k, v in node.items() if isinstance(v, (int, float)) and not isinstance(v, bool)}
        if scalars:
            ev.objects.append((f"{referent}:{path}" if path else referent, scalars))
        for key, val in node.items():
            child = f"{path}.{key}" if path else str(key)
            if isinstance(val, (dict, list)):
                if isinstance(val, list):
                    # the length is the figure a record states when it counts what the artifact
                    # enumerated ("45 checks", "16/16 nodes"); walk_json's dict branch already
                    # indexes scalars, and a list leaf was invisible to it.
                    ev.add(key, float(len(val)), f"{referent}:{child}#len")
                    # The length is only one of the two things a record states about a list.
                    # "seeds 1337, 24601, 777" quotes the elements, and with only #len indexed
                    # every element claim judged CONTRADICTED against the count (1337 vs 3).
                    # Element paths are dotted so Evidence.unit's segment rule sees them under
                    # the key name. Bounded: a 100k-frame array is a data dump, not a list a
                    # record enumerates, and indexing all of it costs more than it binds.
                    for i, item in enumerate(val[:MAX_LIST_ELEMENTS]):
                        if isinstance(item, (int, float)) and not isinstance(item, bool):
                            ev.add(key, float(item), f"{referent}:{child}.{i}")
                    if str(key).lower() == "checks":
                        passed = sum(1 for x in val if isinstance(x, dict) and x.get("pass"))
                        tally = {"checks": float(len(val)), "pass": float(passed),
                                 "fail": float(len(val) - passed)}
                        for leaf, n in tally.items():
                            ev.add(leaf, n, f"{referent}:{child}#tally")
                        # one object holds all three, so "45/45 checks" binds the way a pair
                        # claim is judged: numerator and denominator in the same object
                        ev.objects.append((f"{referent}:{child}#tally", tally))
                walk_json(val, child, ev, referent)
            elif isinstance(val, (int, float)) and not isinstance(val, bool):
                ev.paths.setdefault(child.lower(), []).append((val, f"{referent}:{child}"))
                ev.add(key, val, f"{referent}:{child}")
    elif isinstance(node, list):
        for i, val in enumerate(node):
            if isinstance(val, (dict, list)):
                walk_json(val, f"{path}[{i}]", ev, referent)


def ingest_receipts(blob, origin, roots, ev, limit=MAX_RECEIPTS_PER_NODE):
    """Read the header fields of every receipt coordinate a blob cites.

    Shared by a DAG node's gate evidence and a markdown record's own prose: the receipt is
    the only artifact carrying a harness's declaration of its own size, so a record that
    cites a coordinate deserves the same binding a node gets. One object per receipt, so a
    `N checks, 0 fail` pair resolves inside a single run and cannot borrow a zero from
    another run.

    A coordinate whose bytes no longer hash to its pin is reported and never trusted.
    """
    roots = tuple(roots) + (AGENT_ROOT,)
    for seen, match in enumerate(fact_store.RECEIPT_RE.finditer(blob)):
        if seen >= limit:
            ev.note(f"{origin}: more than {limit} receipts, first {limit} read")
            break
        coord = match.group(0)
        ok, why, head = fact_store.verify_receipt(coord, roots)
        if not head:
            ev.note(f"{origin} receipt {coord}: {why}")
            continue
        if not ok:
            ev.note(f"{origin} receipt {coord}: {why}")
        where = coord.split("@")[0]
        obj = {}
        for key, val in head.items():
            if isinstance(val, float):
                # registered as a path too: a receipt is a first-class referent for a
                # `checks: 83` claim, and a key first seen in another artifact must not
                # strand the receipt value in by_leaf where a path lookup never reaches it
                ev.paths.setdefault(key.lower(), []).append((val, f"{where} {key}"))
                ev.add(key, val, f"{where} {key}")
                obj[key] = val
        if obj:
            ev.objects.append((where, obj))
            # a receipt a record cited is evidence the run was read: the artifact count must
            # not report one file for a record whose numbers all came from receipts
            ev.files.append(where)


def load_receipts(node, path, ev):
    """Header fields of every archived gate run a node cites.

    The receipt is the only artifact carrying a harness's own `checks:` / `vmhwm_mb:`
    declaration. Before 2026-10-18 those numbers existed only in the one-line summary
    --run-gate typed into DAG.json, which nothing could re-check, so a record quoting
    `checks: 172` bound to a hand-written string (Projects/amber_ascii/perm-record.md §16).
    A coordinate whose bytes no longer hash to its pin is reported and never trusted.
    """
    result = node.get("gate_result") or {}
    blob = " ".join(str(v) for v in (node.get("evidence"), result.get("receipt"),
                                     result.get("output")) if v)
    ingest_receipts(blob, f"{path.name}:{node.get('id', '?')}", (path.parent,), ev)


def load_record_receipts(record_text, record, ev, roots=None):
    """Receipts the record itself cites, resolved against its directory and the --roots."""
    ingest_receipts(record_text, record.name, tuple(roots or (record.parent,)), ev,
                    limit=MAX_RECEIPTS_PER_RECORD)


def load_dag(path, data, ev):
    """Gate summaries and node counts: a project's own plan is the referent for both."""
    done = total = 0
    for node in data.get("nodes", []):
        total += 1
        if str(node.get("status", "")).lower() == "done":
            done += 1
        nid = str(node.get("id", "?"))
        out = ((node.get("gate_result") or {}).get("output") or "").strip()
        referent = f"{path.name}:{nid}.gate_result {out!r}"
        gate = {}
        if match := GATE_CHECKS_RE.search(out):
            gate["checks"], gate["fail"] = float(match.group(1)), float(match.group(2))
        if match := GATE_MB_RE.search(out):
            gate["vmhwm"] = float(match.group(1))
        if match := GATE_S_RE.search(out):
            gate["secs"] = float(match.group(1))
        for leaf, value in gate.items():
            ev.add(leaf, value, referent)
        if gate:
            ev.objects.append((f"{path.name}:{nid}.gate", gate))
        load_receipts(node, path, ev)
    if total:
        ev.add("nodes", float(done), f"{path.name}: {done}/{total} nodes done")
        ev.objects.append((f"{path.name}.nodes", {"nodes": float(done), "ref_n": float(total)}))


def pin_artifact(path, ev, raw=None):
    """A pin claim is a digest and a byte size, and both cost nothing over a file whether or not
    it was parsed: the artifact cap must never leave the record's own pins unverifiable."""
    if raw is None:
        try:
            raw = path.read_bytes()
        except OSError as exc:
            ev.note(f"{path}: {exc}")
            return False
    # the full digest: a record may quote 12 or 16 hex characters, and a pin means prefix
    # agreement either way
    ev.digests.append((hashlib.sha256(raw).hexdigest(), len(raw), path.name))
    ev.add("bytes", float(len(raw)), f"{path.name}:bytes")
    return True


def index_text_kv(path, ev, raw):
    """Index the `key=value` and `n checks` pairs a file prints on its own lines.

    A receipt's or source file's figures are consulted only when the record's own sentence names
    that file: prose `turns = 1` in a note and a runtime `turns: 4` in a report measure different
    things, and a shared key is not evidence they agree. A citation is what makes a quoted
    constant bound. A JSON runner keeps the same habit - its per-check `detail` string carries
    the clause's measurement (`G4-2c ... changes_while_gated=5`) while a same-named keyed field
    counts another lane (`registers.changes_while_gated: 2`) - so a parsed artifact is scanned
    for these pairs as well, or a record quoting its own receipt is called wrong by a different
    column of the same file.
    """
    counted = 0
    # capped scan: see MAX_TEXT_SCAN_BYTES; line numbers stay true within the
    # prefix, so a `name:L12 key` citation still names the file's real line
    lines = raw[:MAX_TEXT_SCAN_BYTES].decode("utf-8", errors="replace").splitlines()
    for n, line in enumerate(lines, 1):
        for m in TEXT_KV_RE.finditer(line):
            key = m.group(1).lower()
            if key in STOP_KEYS or counted >= MAX_TEXT_KV:
                continue
            counted += 1
            where = f"{path.name}:L{n} {m.group(1)}"
            ev.scoped_kv.setdefault(path.name, []).append((key, float(m.group(2)), where))
        if counted < MAX_TEXT_KV:
            for m in TEXT_COUNT_RE.finditer(line):
                if counted >= MAX_TEXT_KV:
                    break
                counted += 1
                k = m.group("k").lower()
                key = "fail" if k == "fails" else k
                ev.scoped_kv.setdefault(path.name, []).append(
                    (key, float(m.group("v").replace(",", "")), f"{path.name}:L{n} {key}"))
    # A plan runner stamps a node's evidence line with space-separated pairs (`median_frame_ms
    # 28.157`), and a record citing that node quotes them. Loose enough to read prose, so it runs
    # last, over its own budget, and never displaces the pairs above.
    if path.suffix in TEXT_KV_SP_SUFFIX:
        spaced = 0
        for n, line in enumerate(lines, 1):
            for m in TEXT_KV_SP_RE.finditer(line):
                key = m.group(1).lower()
                if key in STOP_KEYS or spaced >= MAX_TEXT_KV_SP:
                    continue
                spaced += 1
                ev.scoped_kv.setdefault(path.name, []).append(
                    (key, float(m.group(2)), f"{path.name}:L{n} {m.group(1)}"))
    return True


def load_artifact(path, ev):
    try:
        size = path.stat().st_size
    except OSError as exc:
        ev.note(f"{path}: {exc}")
        return False
    if size > MAX_ARTIFACT_BYTES:
        ev.note(f"{path}: {size} B over the {MAX_ARTIFACT_BYTES} B cap, skipped")
        return False
    if path.suffix not in TEXT_INDEX_SUFFIX and path.suffix != ".json":
        # an image or an unparsed binary is pinned by digest and size, never walked
        return pin_artifact(path, ev)
    if path.suffix != ".json":
        # json.loads on source or a receipt indexed nothing and spent the budget emitting an
        # "unreadable JSON" note; keyed scalars are the half of it a record actually quotes.
        raw = path.read_bytes()
        pin_artifact(path, ev, raw)
        ev.files.append(str(path))
        return index_text_kv(path, ev, raw)
    raw = path.read_bytes()
    pin_artifact(path, ev, raw)
    try:
        data = json.loads(raw.decode("utf-8", errors="replace"))
    except ValueError as exc:
        ev.note(f"{path}: unreadable JSON ({exc})")
        return False
    ev.files.append(str(path))
    walk_json(data, "", ev, path.name)
    # A parsed artifact still prints its clause measurements inside strings; see index_text_kv.
    index_text_kv(path, ev, raw)
    if path.name == "DAG.json" and isinstance(data, dict):
        load_dag(path, data, ev)
    return True


def project_root(record):
    """The project a record belongs to: the nearest ancestor that owns the work.

    A note under notes/ cites `run/report-G5.json` relative to the project, not to notes/. With
    only its own directory as an evidence root, every such citation failed to resolve and the
    note was judged against nothing - `ERROR no readable evidence under: .../notes` - so a whole
    gate's figures reported UNBOUND while the tool exited 0. The spine is excluded on purpose:
    it is already a citation base, and walking it would make the entire agent tree every session
    note's evidence.
    """
    here = record.resolve()
    agent = AGENT_ROOT.resolve()
    for cand in (here.parent, *here.parents):
        if cand == agent:
            return here.parent
        if ((cand / ".git").exists() or (cand / "DAG.json").is_file()
                or (cand / "perm-record.md").is_file()):
            return cand
    return here.parent


def cited_paths(text, roots):
    """A record names the artifacts it measured. Read those first: the budget is finite and an
    artifact the record never cites cannot be its evidence."""
    out = []
    for token in CITED_RE.findall(text):
        token = token.strip().lstrip("./")
        named = pathlib.Path(token).expanduser()
        # A record under a project names its own artifacts relatively and the spine's
        # absolutely or relative to the spine; an expanding, rooted search is what lets a note
        # quote an archived receipt instead of dangling that figure UNBOUND.
        bases = [named] if named.is_absolute() else [AGENT_ROOT / named] + [
            root / named for root in roots]
        for candidate in bases:
            if candidate.is_file():
                out.append(candidate)
                break
    return out


def derive_fps(ev):
    """An fps claim binds to 1000/ms over the same perf leaves the record quotes."""
    for leaf in ("mean", "p50", "p95", "max", "median"):
        for value, referent in ev.by_leaf.get(leaf, []):
            if value:
                ev.add("fps", 1000.0 / value, f"1000/{leaf}={value:g} @ {referent}")


def _rank(path):
    """Parse priority: a gate's own report and receipt before the fixtures filed beside them."""
    if path.name.startswith("report-") or path.name == "DAG.json" or "gates" in path.parts:
        return 0
    if any(part in ("evidence", "baselines", "scenarios", "shots", "oracle")
           for part in path.parts):
        return 2
    return 1


def discover(roots, explicit, text, bases=None):
    """Load the evidence under `roots`, resolving citations against `bases`.

    The two sets differ on purpose. `roots` is where a sweep may look for artifacts; `bases`
    is where the record's own relative citations live, and it always holds the record's
    directory. `--root` used to replace it, so a record under a project lost every cite it
    wrote relatively (`archive/h.log:1@pin`) and its figures were judged against whatever
    same-named leaf the named tree happened to carry.
    """
    ev = Evidence()
    named = ([pathlib.Path(p) for p in explicit]
             + cited_paths(text, tuple(bases if bases is not None else roots)))
    swept = []
    for root in roots:
        if not root.is_dir():
            ev.note(f"{root}: not a directory")
            continue
        # sorted() by path puts run/evidence/* and baselines/* ahead of run/report-G*.json, so
        # the parse budget could drop the one file that proves the note in front of it. Measured
        # outputs rank first; scenario fixtures and shot manifests last.
        swept += sorted((p for p in root.rglob("*.json")
                         if p.is_file() and not any(part in SKIP_DIRS for part in p.parts)),
                        key=lambda p: (_rank(p), str(p)))
    seen, named_q, sweep_q = set(), [], []
    for path in named:
        key = str(path.resolve())
        if key in seen:
            continue
        seen.add(key)
        named_q.append(path)
    for path in swept:
        key = str(path.resolve())
        if key in seen:
            continue
        seen.add(key)
        sweep_q.append(path)
    # Named first, sweep with what's left: the budget spends itself on the evidence the record
    # cites, and an unnamed fixture can only ever take the remainder.
    keep = named_q[:MAX_NAMED_ARTIFACTS]
    queued = keep + sweep_q
    budget = max(MAX_ARTIFACTS, len(keep))
    for path in queued[:budget]:
        load_artifact(path, ev)
    unparsed = queued[budget:] + named_q[MAX_NAMED_ARTIFACTS:]
    if unparsed:
        # past the parse budget a file is still pin-checkable: digest and size need no schema
        # walk, so a pin row naming a file outside the cap binds instead of dangling UNBOUND.
        for path in unparsed:
            pin_artifact(path, ev)
        ev.note(f"{len(queued) - budget} more .json under the roots: pinned by digest "
                f"and size, not parsed")
    if len(named_q) > MAX_NAMED_ARTIFACTS:
        ev.note(f"{len(named_q) - MAX_NAMED_ARTIFACTS} cited artifact(s) past the named "
                f"ceiling: pinned, not parsed")
    derive_fps(ev)
    return ev


def line_count(claim_line, roots):
    """A 'N lines' claim names its file in backticks; resolve it against the roots."""
    for name in re.findall(r"`([^`]+)`", claim_line):
        # a coordinate cites a file: `x.log:L1-L40@sha` is a claim about x.log's bytes, and a
        # ledger's own line counts are stated against receipts, which are .log
        name = name.strip().split(":")[0].split("@")[0]
        if not re.search(r"\.(py|js|mjs|md|gd|glsl|json|sh|ts|log|txt|jsonl|csv|mmd)$", name):
            continue
        named = pathlib.Path(name).expanduser()
        bases = [named] if named.is_absolute() else [AGENT_ROOT / named] + [
            root / named for root in roots]
        for candidate in bases:
            if candidate.is_file():
                try:
                    return float(len(candidate.read_text(encoding="utf-8",
                                                         errors="replace").splitlines())), str(candidate)
                except OSError:
                    continue
    return None, None


# ---------------------------------------------------------------- claims

def strip_fences(text):
    """Fenced blocks are commands and dumps, not claims (a manifest's re-derivation section)."""
    out, inside = [], False
    for line in text.splitlines():
        if FENCE_RE.match(line):
            inside = not inside
            out.append("")
            continue
        out.append("" if inside else line)
    return out


# Prose states a run's counters number-first ("371 applied", "240 stepped frames") while the
# artifact spells the key past tense or plural. The mapping is only for irregular spellings;
# Evidence.unit's singular/plural rule covers the regular ones.
COUNTER_NOUNS = {"fed": "feeds", "feeds": "feeds", "applied": "applied",
                 "frame": "frames", "frames": "frames",
                 "clamp": "clamped", "clamped": "clamped",
                 "reject": "rejected", "rejects": "rejected", "rejected": "rejected"}


def claim_families():
    """(metric, regex, kind) in precedence order; an earlier family owns the digits it matched."""
    return [
        # a pin row: a 12-hex digest and the byte size it was taken at. Both halves are checked
        # against the file's own bytes, so agreement means the pin is the artifact on disk.
        ("digest", re.compile(rf"`(?:sha256[:\s]+)?(?P<sha>[0-9a-f]{{12,16}})`"
                             rf"\s*\(?\s*(?P<size>{NUM})(?:\s*bytes)?"), "digest"),
        # `seeds 1337, 24601, 777`: a run's seed list, bound leaf-by-leaf against the evidence.
        ("seed", re.compile(r"(?<![\w.])seeds?[ \t]+((?:\d[\d,]*[\s,]*){2,})(?:\D|$)"), "list"),
        ("checks", re.compile(rf"({NUM})\s+checks[^\n]{{0,4}}?({NUM})\s*fail", re.I), "pair"),
        ("nodes", re.compile(rf"({NUM})\s*/\s*({NUM})\s+nodes", re.I), "pair"),
        ("nodes", re.compile(rf"\b({NUM})\s+nodes\b", re.I), "num"),
        ("checks", re.compile(rf"\b({NUM})\s+checks\b", re.I), "num"),
        ("lines", re.compile(rf"\b({NUM})\s+lines\b", re.I), "num"),
        ("fps", re.compile(rf"\b({NUM})\s+fps\b", re.I), "num"),
        ("ms", re.compile(rf"\b([a-z][a-z0-9_.]*)?\s*({NUM})\s*ms\b", re.I), "unit"),
        ("mb", re.compile(rf"\b([a-z][a-z0-9_.]*)?\s*({NUM})\s*MB\b"), "unit"),
        ("s", re.compile(rf"\b([a-z][a-z0-9_.]*)?\s*({NUM})\s+s\b"), "unit"),
        # `372 fed messages`, `371 applied`, `240 stepped frames`, `1 clamp`: counters named by
        # their noun. Key-gated like every unit claim, so a noun no artifact carries binds
        # nothing instead of matching some unrelated number.
        ("counter", re.compile(rf"\b({NUM})\s+(?:stepped\s+)?"
                               rf"((?:fed|feeds|applied|frames?|clamp(?:ed)?|reject(?:s|ed)?))\b",
                               re.I), "counter"),
        # `minOrbit 0.739066`, `hitFrac: 0.229492`: a decimal stated against a key. The key lane
        # drops any name no artifact carries, so prose ("section 4", "iters 2..10") never binds;
        # a figure welded to a single letter is an identifier, not a measurement: `B1.5` and
        # `V4.3` are bar ids, and reading them as key `b` contradicted the record against
        # whatever primitive happened to be named `b`. A pair needs its separator, and a
        # space-separated one must still be a decimal, or any word before a version number
        # reads as one.
        # the exponent is part of the figure: `growth 1.73e-18` is one number, and cutting at
        # the dot judged a bare mantissa against an unrelated decimal
        ("knownkey", re.compile(r"\b(?P<kb>[a-z][a-z0-9_.]*)(?:\s*[:=]\s*|\s+)(?P<vb>\d+\.\d+"
                                r"(?:[eE][+-]?\d+)?)", re.I), "key"),
        # backticks required: an unquoted "word: number" is prose ("The Python version: 71
        # tests"). Both quoting styles appear in records: `key` = 30 and `key = 30`.
        # A value that runs on into a rate (`dt = 1/60`) or a congruence (`f = 7 (mod 8)`) is a
        # formula, not a scalar: comparing its numerator to an unrelated scalar is the wrong
        # comparison, which is the same reason a bar chart is skipped.
        ("keyvalue", re.compile(
            rf"`(?P<ka>[a-z_][a-z0-9_.]*)\s*[:=]\s*(?P<va>{NUM_SCI})"
            rf"(?!\s*(?:/\s*\d|\(?\s*mod\b))`?"
            rf"|`(?P<kb>[a-z_][a-z0-9_.]*)`\s*[:=]\s*(?P<vb>{NUM_SCI})"
            rf"(?!\s*(?:/\s*\d|\(?\s*mod\b))"), "key"),
        ("pair", re.compile(rf"\b({NUM})\s*/\s*({NUM})\b"), "pair"),
    ]


def sentence_scope(text, start, end):
    """The artifacts this claim's own sentence names, as path or `path:Lx-Ly` strings.

    A receipt coordinate is the record pointing at the exact bytes that prove it, and a
    bare `tools/plan_check.py:163-183` is the same act. When the sentence names one, an
    unrelated artifact that happens to carry the same key is no longer a fair referent:
    `(`checks: 83`, `fail: 0`, `exit_code: 0`)` next to a receipt coordinate means that
    receipt, not some sibling JSON with a `checks` key of its own.
    """
    # `. ` and `; ` end a sentence. A dot inside a number ("1.0516") or inside a file name
    # ("report-G5.json") is followed by a non-space, so it does not. A markdown record hard-wraps
    # mid-sentence, so a newline is a terminator only where a block starts - a blank line, a bullet,
    # a table row, a heading, a rule - and a receipt cited on the next line still belongs to this
    # claim. Wrapping used to cut the sentence at every newline, and the citation the claim lives on
    # fell outside its own scope: the figure came back UNBOUND or bound to a neighbour gate's leaf.
    cut = max(text.rfind(". ", 0, start) + 2, text.rfind("; ", 0, start) + 2,
              text.rfind("\n\n", 0, start) + 2, text.rfind("\n- ", 0, start) + 3,
              _prev_block_start(text, start))
    stop = min((m.start() for m in re.finditer(
        r";|\.(?=\s|$)|\n[ \t]*\n|\n(?=[ \t]*(?:[-*#>|\u2022]|`{3,}|_{3,}))",
        text[end:end + 400])), default=400)  # a record with no terminator past the claim is one sentence
    return _scope_tokens(text[cut:end + stop])


def _prev_block_start(text, start):
    """Just past the last newline before `start` that opens a new block: a bullet, a table row,
    a heading, a rule, a fence.

    A markdown table is one blank-line paragraph, and `. ` / `; ` are often absent from a row
    that cites its receipt in the next cell, so the sentence window reaches up into the row
    above and a claim inherits another run's coordinate. The row it lives on is its own.
    """
    i = start
    while i > 0:
        i = text.rfind("\n", 0, i)
        if i < 0:
            return 0
        if re.match(r"[ \t]*(?:[-*#>|\u2022]|`{3,}|_{3,})", text[i + 1:]):
            return i + 1
    return 0


def _scope_tokens(block):
    """Every token in a block that names an artifact: a receipt coordinate, a line range, a
    pin, or a bare filename with a known extension."""
    out = []
    for m in SCOPE_RE.finditer(block):
        tok = m.group(0).rstrip(".,:)")
        tail = re.split(r"[/]", tok)[-1]
        named = ("." in tail and tail.rsplit(".", 1)[-1] in FILE_EXT)
        # A dotted coordinate names the file BEFORE the colon, not a file called `.p95_ms`:
        # `receipt.json:perf.p95_ms` is the author pointing at the bytes and at the leaf read
        # inside them. The suffix test above takes the leaf's last dot for the extension, so a
        # pinned citation was no citation at all: the claim fell to the whole corpus, a
        # same-named leaf in another run's window judged it, and a row that named its own receipt
        # could never be called wrong - `p95_ms 39.022 ms` cited to the leg holding 41.5 came
        # back UNBOUND while a sibling leg's key contradicted a row naming no file at all.
        head = tail.split(":")[0]
        pinned = head.count(".") == 1 and head.rsplit(".", 1)[-1] in FILE_EXT
        if (COORD_TAIL.search(tok) or LINE_TAIL.search(tok) or PINNED_TAIL.search(tok)
                or (named and "." in tail and tail.count(".") == 1) or pinned):
            out.append(tok)
    return out


def paragraph_scope(text, start, end):
    """The artifacts this claim's sentence names; failing that, its paragraph's.

    The house style files the citation on its own line under the sentence that earned it
    ("... at the cite below"), and a period or a bullet cuts the sentence at the figure. The
    number and the bytes that prove it then sit in different sentences, the pool is the whole
    corpus, and a same-named leaf from another run's window calls this record's reading wrong.
    Inside one paragraph the citation belongs to the figure, whoever put it on which line.
    """
    scoped = sentence_scope(text, start, end)
    if scoped:
        return scoped
    lo = text.rfind("\n", 0, start)
    if text[lo + 1:start].lstrip().startswith("|"):
        # a table row is its own citation. A markdown table is one blank-line paragraph, so
        # the paragraph's tokens let one row's receipt answer, or contradict, the next row's
        # figure - and each row here stands on the run whose coordinate it prints.
        hi = text.find("\n", end)
        return _scope_tokens(text[lo + 1:] if hi < 0 else text[lo + 1:hi])
    return _scope_tokens(_paragraph(text, start, end))


def _paragraph(text, start, end):
    lo = text.rfind("\n\n", 0, start)
    lo = 0 if lo < 0 else lo + 2
    hi = text.find("\n\n", end)
    return text[lo:] if hi < 0 else text[lo:hi]


def is_bar(text, start, end):
    """A number is a bar when a comparison sits right before it ("bar ≤ 6.0", "≥ 24 fps") or
    the word bar/target/limit follows right after its unit ("11.78 fps no bar", "30/30 bars").
    The windows are tight on purpose: the next sentence's bar is not this number's bar."""
    before = text[max(0, start - 12):start].lower()
    after = text[end:end + 12].lower()
    return (any(mark in before for mark in BAR_BEFORE)
            or any(mark in after for mark in BAR_AFTER))


def _path_suffix_cands(ev, key):
    """O(1) order-preserving lookup for [(v, r) for p, e in ev.paths.items() if p == key or p.endswith('.' + key) for v, r in e]."""
    idx = getattr(ev, "_suffix_cands", None)
    if idx is None:
        idx = {}
        for p, entries in ev.paths.items():
            pos = 0
            while True:
                idx.setdefault(p[pos:], []).extend(entries)
                dot = p.find(".", pos)
                if dot < 0:
                    break
                pos = dot + 1
        ev._suffix_cands = idx
    return list(idx.get(key, ()))


def extract(lines, ev):
    """Match over the joined text: a markdown hard-wrap puts "197" and "checks" on separate
    lines, and a per-line scan loses the claim. Offsets map back to the source line."""
    text = "\n".join(lines)
    code = code_spans(text)
    known = set(ev.by_leaf) | set(ev.paths)
    known_suffixes = {
        k[pos:]
        for k in known
        for pos in (0, *(i + 1 for i, ch in enumerate(k) if ch == "."))
    }
    known_lower = {k.lower() for k in known}
    # A paragraph that says "no artifact" is the record declaring its numbers transcript-only.
    # Comparing such a number to an unrelated leaf in the same unit is a wrong comparison, not
    # a contradiction - but the declaration is scoped to its own paragraph, never the file.
    declared, pos = [], 0
    for para in text.split("\n\n"):
        # Tolerate the record's own hard-wrapping: "...bind to no\nartifact" is the
        # same declaration as "...bind to no artifact".
        if NO_ARTIFACT_RE.search(para):
            declared.append((pos, pos + len(para)))
        pos += len(para) + 2
    claims, taken = [], []
    for metric, rx, kind in claim_families():
        for match in rx.finditer(text):
            span = match.span()
            if any(span[0] < t[1] and t[0] < span[1] for t in taken):
                continue
            if kind == "pair":
                # `5/5/8` is a colour triple and `24 / 32 / 48` a series, not a ratio; a ratio
                # inside `code` is an expression the record quotes, not a figure it claims. Each
                # was read as a ratio and reported UNBOUND forever after. The window is either
                # side of the whole `a/b`, because the match itself owns the middle slash.
                if ("/" in text[max(0, span[0] - 5):span[0]]
                        or "/" in text[span[1]:span[1] + 5]
                        or any(s <= span[0] and span[1] <= e for s, e in code)):
                    continue
                if to_num(match.group(2)) in BIT_DEPTH:
                    continue
            if kind == "counter":
                # an arithmetic operand, an arrow result, or either inside quoted code is the
                # record explaining a script, not a count it took
                p = span[0] - 1
                while p >= 0 and text[p] in " \t":
                    p -= 1
                if (p >= 0 and text[p] in OPERAND_BEFORE
                        or any(s <= span[0] and span[1] <= e for s, e in code)):
                    continue
            if kind == "list":
                # one claim per number: a wrong seed in the list names itself, the list does not
                # fail as a unit, and a seed the evidence never ran stays visible as UNBOUND.
                taken.append(span)
                for n in re.finditer(r"\d[\d,]*", match.group(1)):
                    lo = match.start(1) + n.start()
                    nl = text.count("\n", 0, lo)
                    claims.append({"metric": metric, "line": nl + 1, "text": lines[nl],
                                   "excerpt": n.group(0), "key": None, "metric_named": True,
                                   "value": to_num(n.group(0)), "precision": 0,
                                   "declared": any(s <= lo < e for s, e in declared),
                                   "scope": paragraph_scope(text, lo, lo + len(n.group(0)))})
                continue
            taken.append(span)
            groups = match.groups()
            key, value = "", None
            if kind == "key":
                # a key family may name only one (key, value) pair, so the groups are looked up
                # rather than indexed: the unquoted family carries just kb/vb.
                g = match.groupdict()
                key = (g.get("ka") or g.get("kb")).lower()
                value = g.get("va") or g.get("vb")
                # A `key: value` in prose is not a claim about an artifact unless the artifact
                # has that key; dropping it here keeps the tally honest. A preposition that a
                # artifact happens to use as a leaf name is the same trap wearing other clothes.
                if key in STOP_KEYS or not (
                        key in known_suffixes
                        # `ink 18.06 %` states the fraction its artifact filed as `inkFrac`.
                        # The scaled partner is one measurement written the other way, so the
                        # key lane keeps the claim and the keyed lane binds it at x100.
                        or any(key + s in known_lower for s, _ in FRAC_SCALES)):
                    continue
            elif kind == "unit":
                key, value = (groups[0] or "").lower(), groups[1]
                if key in STOP_KEYS or len(key) < 3:
                    key = ""   # "a 1200 ms" is prose, not a claim about a field named "a"
            elif kind == "counter":
                # the noun is the referent's key, so the metric is the noun as the artifact
                # spells it; every counter match names its own, nothing carries over
                key, value = "", groups[0]
                metric = COUNTER_NOUNS[groups[1].lower()]
            elif kind == "digest":
                key, value = "", match.group("size")
            elif kind != "pair":
                value = groups[0]
            # the number's own group: unit families put the optional key word in group 1
            if kind == "key":
                gi = match.re.groupindex["va" if g.get("va") else "vb"]
            else:
                gi = 2 if kind in ("unit", "digest") else 1
            num_span = match.span(gi)
            if is_bar(text, num_span[0], num_span[1]):
                continue
            line = text.count("\n", 0, span[0]) + 1
            row = {"metric": metric, "line": line, "text": lines[line - 1],
                   "excerpt": " ".join(match.group(0).split()), "key": key or None,
                   # a family whose pattern contains the metric word makes the record name its
                   # metric; the bare `a/b` family names nothing, so its object is chosen by
                   # values alone
                   "metric_named": kind == "pair" and metric != "pair"}
            if kind == "pair":
                row["a"], row["b"] = to_num(groups[0]), to_num(groups[1])
                row["precision"] = 0
                # how many digits each half was written with: a fraction is a measured quantity,
                # and a tally of 32 is not the mean 32.211 written as `32.211`
                row["a_decimals"], row["b_decimals"] = decimals(groups[0]), decimals(groups[1])
            elif kind == "digest":
                # the claim is the pair (digest, size); the size is the number the record stated
                row["sha"], row["value"] = match.group("sha"), to_num(value)
                row["precision"] = 0
            else:
                row["value"], row["precision"], row["sci"] = sci_value(value)
            row["declared"] = any(s <= span[0] < e for s, e in declared)
            row["scope"] = paragraph_scope(text, span[0], span[1])
            claims.append(row)
    return sorted(claims, key=lambda c: c["line"])


# A frame rate and a frame time are one measurement in two units: a file that declares its
# p50 in ms holds the claim's fps as 1000/ms, and calling the record wrong because its own
# fps field was never written would blame the record for the artifact's unit choice.
RECIPROCAL = {"fps": "ms", "ms": "fps"}


def _referent_matches(referent, cited, bases=()):
    """Is this reading one the row's own coordinate reaches? `bases` resolve a relative path.

    A bare filename or a glob matches by basename, the way `_in_scope` matches. A coordinate
    spelled as a path points at the bytes at that path, so a same-named file in another
    directory is another run's artifact and cannot answer the row; a coordinate ending in
    `/` reaches everything filed under it.
    """
    if not cited:
        return True
    head = referent.split(":")[0].rsplit("@", 1)[0]
    base = head.rsplit("/", 1)[-1]
    for token in cited:
        want = str(token).split(":")[0].rstrip("/").rsplit("@", 1)[0]
        # a receipt coordinate carries its line range and its field (`x.log:L1-L18 duration_ms`):
        # the file is what was cited, and the leaf must not become the comparison's subject
        want = re.sub(r"^L\d+(?:-L?\d+)?(?=[\s.])", "", want)
        if not want:
            continue
        if want.endswith("/") and head.startswith(want):
            return True
        if "/" not in want and fnmatch.fnmatchcase(base, want):
            return True
        if head == want or fnmatch.fnmatchcase(head, want):
            return True
        # A citation spelled as a path (`archive/x.json:leaf`) is absolute by the time it
        # reaches here, while a referent is `name:leaf` - the loader drops the directory when
        # it builds that string, so basename is all a referent can answer. Requiring the
        # cited bytes to exist is the strongest directory check the referent's own
        # vocabulary allows: a moved or deleted receipt cannot match and cannot contradict.
        if ("/" in want and base == want.rsplit("/", 1)[-1]
                and (any((pathlib.Path(b) / want).is_file() for b in bases)
                     or pathlib.Path(want).is_file())):
            return True
    return False


# ---------------------------------------------------------------- verdicts

def _in_scope(referent, scope):
    """Does this candidate live in an artifact the claim's own sentence named?

    A referent is `name:path` for JSON and a receipt coordinate for a log; the scope token is
    whatever the author wrote, so the comparison is by file basename. This is what stops one
    gate's duration from answering another gate's note.
    """
    if not scope:
        return True
    head = referent.split(":")[0].rsplit("/", 1)[-1].rsplit("@", 1)[0]
    return any(head == str(t).split(":")[0].rsplit("/", 1)[-1].rsplit("@", 1)[0]
               for t in scope)


def scoped_values(ev, claim, key):
    """Figures the record's own sentence pins.

    A quoted code constant or receipt figure is entailed by the bytes it cites, not by any
    file that happens to share the key: the scope is the coordinate the author wrote.
    """
    if not key or not claim.get("scope"):
        return []
    want = str(key).lower()
    out = []
    for token in claim["scope"]:
        base = str(token).split(":")[0].rstrip("/").rsplit("/", 1)[-1]
        if not base:
            continue
        for name, entries in ev.scoped_kv.items():
            if name != base and not name.endswith(base):
                continue
            out.extend((v, f"{base}:{ref}") for k, v, ref in entries if k == want)
            if str(claim.get("metric") or "").lower() == "s":
                # The sentence cites the receipt, the receipt counts milliseconds and the
                # record quotes seconds - the same scale step unit() makes for the whole set.
                # The claim's key word may be prose (`clock 52.7 s`), which is why the unit in
                # the claim decides, not the word the author happened to put before it.
                out.extend((v / 1000.0, f"{base}:{ref} (ms/1000)")
                           for k, v, ref in entries if k in MS_TO_S_KEYS)
    return out


def scope_file(claim):
    """The artifact the record's own sentence cited, for the explanation line."""
    sc = claim.get("scope") or []
    return sc[0] if isinstance(sc, (list, tuple)) and sc else "the cited artifact"


def nearest(cands, value, limit=3):
    ranked = sorted(cands, key=lambda c: abs(c[0] - value))[:limit]
    return "; ".join(f"{fmt(c[0])} @ {c[1]}" for c in ranked)


def _scoped_text_pairs(claim, ev):
    """Keyed figures grouped by the receipt line the claim's own sentence cites.

    `scoped_kv` is filed per file, but a footer's pair is one fact only when both numbers sit on
    one line, so the `name:L73` coordinate is the group. This is what lets `72 checks, 0 fail`
    bind to the footer it quotes instead of borrowing a zero from the nearest JSON's `fail` key.
    """
    out = []
    for token in claim.get("scope") or []:
        base = str(token).split(":")[0].rstrip("/").rsplit("/", 1)[-1]
        if not base:
            continue
        for name, entries in ev.scoped_kv.items():
            if name != base and not name.endswith(base):
                continue
            grouped = {}
            for k, v, ref in entries:
                grouped.setdefault(ref.rsplit(" ", 1)[0], {})[k] = v
            out.extend(grouped.items())
    return out


def judge_pair(claim, ev):
    a, b = claim["a"], claim["b"]
    # A harness footer is text, not an object: `72 checks, 0 fail` lives on one cited receipt
    # line, and both figures there is the pair resolving where the sentence pointed. Judged only
    # against objects, the claim's zero is hunted under every artifact's `fail` key and the
    # record is called damaged for quoting its own receipt verbatim.
    for line, kv in _scoped_text_pairs(claim, ev):
        if any(k in TEXT_COUNT_NOUNS and agree(a, v, 0) for k, v in kv.items()) \
                and any(k == "fail" and agree(b, v, 0) for k, v in kv.items()):
            return "ENTAILED", f"{line} holds {fmt(a)} and {fmt(b)}"
    metric = str(claim.get("metric") or "").lower()
    named = ({metric} | {k for k, v in ev.by_leaf.items() if v and metric in k.split("_")}
             if claim.get("metric_named") else None)
    # A pair the sentence cites is adjudicated where it says it read it. Another run's receipt
    # carries a zero under `fail` and a sample count under `n` as a matter of course, and either
    # one makes this pair look contradicted when the cited bytes simply never filed that shape.
    scope = claim.get("scope") or []
    pool = [(p, o) for p, o in ev.objects if _in_scope(p, scope)] if scope else list(ev.objects)
    if scope and not pool:
        return "UNBOUND", (f"neither {fmt(a)} nor {fmt(b)} is in an artifact this sentence cites "
                           f"({", ".join(str(t) for t in scope[:3])})")
    # A fractional denominator is a measured quantity, not a tally: at zero decimals the sample
    # count 32 "agrees" with the mean 32.211, and a check count from another run's receipt calls
    # this pair contradicted. A fraction is matched at the digits the record wrote.
    dpair = int(claim.get("b_decimals") or 0)
    apair = int(claim.get("a_decimals") or 0)
    denom = [(v, referent) for path, obj in pool for k, v in obj.items()
             if k.lower() in DENOM_KEYS and agree(b, v, dpair) for referent in [f"{path}.{k}"]]
    if not denom:
        # No artifact carries that denominator. But when the record quotes its own metric
        # and numerator and something holds that number under exactly that key, the
        # evidence does contradict the pair - a denominator of zero, which is what the
        # record asserts, is never itself the proof of a match.
        if named:
            # Only an object that carries both halves can say this pair is wrong: a numerator
            # under the metric key, and a denominator whose own value disagrees. A file that
            # holds the numerator and no denominator at all is silence, not contradiction.
            quoted = [(f"{p}.{k}", d) for p, obj in pool for k, v in obj.items()
                      if k.lower() in named and agree(a, v, apair)
                      for d in [next((vv for kk, vv in obj.items()
                                      if kk.lower() in DENOM_KEYS and kk.lower() != k.lower()
                                      and isinstance(vv, (int, float))), None)]
                      if d is not None and not agree(b, d, dpair)]
            if quoted:
                return "CONTRADICTED", (f"{quoted[0][0]} = {fmt(a)} but its denominator is "
                                        f"{fmt(quoted[0][1])}, not {fmt(b)}")
        return "UNBOUND", f"no artifact object holds {fmt(b)} under a denominator key"
    for path, obj in pool:
        if named is not None and not any(k.lower() in named for k in obj):
            # A gate receipt records its own `fail: 0`; that zero is no artifact's
            # denominator for an unrelated `65/0` elsewhere in the record.
            continue
        if any(k.lower() in DENOM_KEYS and agree(b, v, dpair) for k, v in obj.items()) \
                and any(agree(a, v, apair) for v in obj.values()):
            return "ENTAILED", f"{path} holds {fmt(a)} and {fmt(b)}"
    if named and not any(k.lower() in named for _, obj in pool for k in obj):
        return "UNBOUND", (f"no artifact names {metric!r} with a denominator of {fmt(b)}; "
                           f"{fmt(b)} only under an unrelated key")
    if named is None:
        # the row named no quantity, so no denominator in the pool is the one it meant:
        # a tally under some other key that disagrees belongs to the run that wrote it
        return "UNBOUND", (f"no artifact object holds {fmt(a)} with {fmt(b)}; "
                           f"denominator {fmt(b)} at {nearest(denom, b)}")
    return "CONTRADICTED", (f"no artifact object holds {fmt(a)} with {fmt(b)}; "
                            f"denominator {fmt(b)} at {nearest(denom, b)}")


def judge(claim, ev, roots):
    """-> (verdict, referent_or_reason), with the two verdicts this tool must not overclaim."""
    verdict, why = _judge(claim, ev, roots)
    if verdict == "CONTRADICTED" and claim["metric"] == "seed":
        # A seed list is a set the record says a run used. A figure no artifact ran is UNBOUND
        # by this tool's own rule - absence from a set is not a value that disagrees.
        return "UNBOUND", f"no artifact ran this figure: {why}"
    return verdict, why


def _own_gate_readings(ev):
    """Every leaf the record's own gate artifact carries.

    A `knownkey` figure is one the record keyed on with no unit and no coordinate, so the leaf
    name the report files it under is not part of the claim - `mass 5521.08` may sit at
    `checks[7].value.bodies[3].m`. Only a report named for the same gate is consulted, and only
    figures with four significant digits qualify, so a short counter cannot land on a random
    leaf. Presence there is the record's own measurement; absence is still UNBOUND.
    """
    if not ev.record:
        return []
    m = re.search(r"G\d+[A-Z]*", ev.record.name)
    if not m:
        return []
    pat = re.compile(m.group(0) + r"(?![0-9A-Z])")
    return [(v, r) for leaf in ev.by_leaf.values() for v, r in leaf
            if pat.search(r.split(":")[0])]


def _sig_digits(value):
    return len(re.sub(r"^0+|0+$|[.]", "", f"{value:.10f}".rstrip("0").rstrip(".")))


CITED_RANGE_RE = re.compile(r"^(?P<path>[^@\s]+?):(?P<rng>[Ll]?\d+(?:-[Ll]?\d+)?"
                            r"(?:\s*,\s*[Ll]?\d+(?:-[Ll]?\d+)?)*)$")
_CITED_BYTES = {}


def _cited_file(name, roots):
    """(path, text, whole-file digest) for a cited path, or None. Cached: one read per cite."""
    key = (str(name), tuple(str(r) for r in roots))
    if key not in _CITED_BYTES:
        _CITED_BYTES[key] = _read_cited(name, roots)
    return _CITED_BYTES[key]


def _read_cited(name, roots):
    # the record's own roots first: a project quoting `run/x.json` means its own run/, and the
    # agent root is the fallback that carries archive/gates/ (the G6 case above)
    named = pathlib.Path(str(name)).expanduser()
    bases = ([named] if named.is_absolute()
             else [pathlib.Path(r) / named for r in roots]
             + [AGENT_ROOT / named, pathlib.Path.cwd() / named])
    for candidate in bases:
        if candidate.is_file():
            try:
                blob = candidate.read_bytes()
            except OSError:
                return None
            return (candidate, blob.decode("utf-8", "replace"),
                    hashlib.sha256(blob).hexdigest())
    return None


def _pin_covers(path, digest, pin, lo, hi):
    """Does this pin attest these bytes? A record pins the whole file or the cited slice."""
    want = str(pin).lower()
    if digest.lower().startswith(want):
        return True
    try:
        return fact_store.read_pin(path, lo, hi).lower().startswith(want)
    except (fact_store.AnchorError, OSError, ValueError):
        return False


def _num_equal(value, token, precision, scale=1.0):
    """Does a printed token state this figure? `scale` converts the claim into the line's unit."""
    try:
        seen = float(token)
    except ValueError:
        return False
    want = value * scale
    half = 0.5 * (10 ** -precision) * scale if precision else max(abs(want) * 1e-9, 1e-12)
    return abs(seen - want) <= half


# a figure printed on a cited line is this claim's measurement only if the line says so: a
# `200 ticks` is not the record's 200 ms, so a unit claim needs its unit printed after it
UNIT_TAIL = {"ms": r"ms\b", "s": r"(?:s|sec|secs|seconds|ms)\b", "mb": r"MB\b",
             "gb": r"GB\b", "fps": r"FPS\b", "percent": r"%", "c": r"(?:deg)?C\b"}


def _unit_ok(metric, tail):
    pat = UNIT_TAIL.get(str(metric or "").lower())
    return True if pat is None else bool(re.match(r"\s*" + pat, tail, re.IGNORECASE))


def cited_line_literal(claim, roots):
    """The claim's figure, printed on the line the record cites. None when no cite binds it.

    A citation that names lines is the record pointing at its own bytes. When the figure sits on
    those lines, no other artifact's same-named leaf can call it wrong: the comparison is against
    the bytes the record says it read, and a drifted figure is simply not there. Pins are verified
    before a line is trusted, so a replaced file cannot vouch for a number it no longer carries.
    A seconds claim may bind a millisecond field and the reverse, because a receipt declares
    duration in ms while prose reads it in s; a `key = value` claim never scales, since its
    contract is the key's own units. A unit claim binds only where the line prints that unit
    after the figure, so a shared number in another unit cannot answer it.
    """
    value, precision = claim["value"], int(claim.get("precision") or 0)
    forms = {f"{value:.{precision}f}" if precision else f"{value:g}", fmt(value)}
    if precision:
        forms.add(f"{value:.{max(precision - 1, 0)}f}")
    metric = str(claim.get("metric") or "")
    scale = 1000.0 if metric == "s" else 0.001 if metric == "ms" else None
    if claim.get("key"):
        scale = None
    for token in claim.get("scope") or []:
        tok = str(token).strip().lstrip("./")
        pin = None
        if "@" in tok:
            tok, _, pin = tok.partition("@")
        hit = CITED_RANGE_RE.match(tok)
        if not hit:
            continue
        found = _cited_file(hit.group("path"), roots)
        if found is None:
            continue
        path, text, digest = found
        lines = text.splitlines()
        for part in hit.group("rng").split(","):
            part = part.strip().lstrip("Ll")
            lo, _, hi = part.partition("-")
            try:
                a = int(lo)
                b = int(hi.lstrip("Ll")) if hi.strip().lstrip("Ll") else a
            except ValueError:
                continue
            if pin and not _pin_covers(path, digest, pin, a, b):
                continue
            for n in range(max(a, 1), min(b, len(lines)) + 1):
                for num in re.finditer(r"\d[\d,]*(?:\.\d+)?", lines[n - 1]):
                    word = num.group(0)
                    if claim.get("key") or _unit_ok(metric, lines[n - 1][num.end():num.end() + 9]):
                        bare = word.replace(",", "")
                        if bare in forms or _num_equal(value, bare, precision):
                            return f"{fmt(value)} on {path.name}:{n} (the cited line)"
                        if scale and _num_equal(value, bare, precision, scale):
                            return (f"{fmt(value)} on {path.name}:{n} as {word} (the cited "
                                    f"line, in {'ms' if scale > 1 else 's'})")
    return None


def _judge(claim, ev, roots):
    """CONTRADICTED requires a bound metric."""
    metric = claim["metric"]
    if "a" in claim:
        return judge_pair(claim, ev)

    # the strongest evidence a record can offer is its own figure, on the line it cites, in bytes
    # that still hash to the pin it gave; try that before a same-named leaf can contradict it
    cited = cited_line_literal(claim, roots)
    if cited is not None:
        return "ENTAILED", cited

    if metric == "lines":
        value, referent = line_count(claim["text"], roots)
        if value is None:
            return "UNBOUND", "no readable file named for the line count"
        if agree(claim["value"], value, claim["precision"]):
            return "ENTAILED", f"{referent} = {fmt(value)} lines"
        return "CONTRADICTED", f"{referent} = {fmt(value)} lines, record says {fmt(claim['value'])}"

    if metric == "keyvalue":
        key = claim["key"]
        # A dotted path is not unique across artifacts: two files with the same schema
        # (browser_report.json and browser_perf_probe.json) both carry perf.window_ms, and a
        # single-value map made the last one loaded the only referent, so the other's figures
        # reported CONTRADICTED against values their own artifact carries verbatim.
        cands = _path_suffix_cands(ev, key)
        scope = claim.get("scope") or []
        if scope:
            inside = [c for c in cands if _in_scope(c[1], scope)]
            # the sentence's own artifact wins; if none of its candidates live there, the
            # claim was not about that file after all and the whole set stays eligible
            cands = inside or cands
        if not cands:
            cands = ev.by_leaf.get(key, []) + ev.partner(key)
        elif claim.get("key"):
            # A raw lane full of the same word is not the whole truth: `ink 18.06 %` is the
            # measurement report-G4A filed as `inkFrac: 0.180611`, and the raw ink means in
            # between are other quantities. Partners join the candidates; they never replace them.
            cands = cands + ev.partner(key)
        scoped = scoped_values(ev, claim, key)
        if not cands:
            if not scoped:
                return "UNBOUND", f"no artifact key {key!r}"
            cands = scoped
        hit = next((c for c in scoped + cands
                    if agree(claim["value"], c[0], claim["precision"], claim.get("sci"))), None)
        if hit:
            return "ENTAILED", hit[1]
        near = [c for c in cands if same_quantity(claim["value"], c[0])]
        if not near:
            return "UNBOUND", (f"no artifact holds {key}={fmt(claim['value'])}; nearest "
                               f"{nearest(cands, claim['value'])} is another quantity")
        return "CONTRADICTED", f"{key}={fmt(claim['value'])} vs {nearest(near, claim['value'])}"

    if metric == "digest":
        # the strongest binding here: the record's pin and the file's own bytes, two independent
        # checks that must agree with each other, not merely with some other artifact's number.
        sha, size = claim["sha"], int(claim["value"])
        at = [(d, s, n) for d, s, n in ev.digests if d.startswith(sha)]
        if not at:
            same = [(d, s, n) for d, s, n in ev.digests if s == size]
            if same:
                return "CONTRADICTED", (f"{size} B is {same[0][2]} at {same[0][0]}, not {sha}")
            return "UNBOUND", f"no artifact read here digests {sha}"
        if any(s == size for _, s, _ in at):
            return "ENTAILED", f"{next(n for _, _, n in at)} digests {sha} at {size} B"
        return "CONTRADICTED", (f"{sha} is the digest of {at[0][2]}, at {fmt(at[0][1])} B "
                                f"not {size} B")

    cands = ev.unit(metric, claim.get("key"))
    if not cands and claim.get("key"):
        # the word before a unit is often prose ("a 1200 ms", "at 52.9 s"), not a field name;
        # the unit family is still the record's own measurement, so retry keyless
        cands = ev.unit(metric)
    if claim.get("key"):
        # A raw lane holding the record's word is not the last word. `ink 18.06 %` cites the
        # report that filed `inkFrac`; other artifacts file a raw `ink` under that same name,
        # so the cited file answers with nothing, the scope rule falls back to the whole set,
        # and the record is contradicted by a mean it never quoted. Partners join the
        # candidates and never replace them, exactly as in the keyvalue lane above.
        cands = cands + ev.partner(claim["key"])
    if not cands and metric == "knownkey" and claim.get("key"):
        # `duration_ms: 52866.721` names a field, not a unit family, and `knownkey` has no
        # unit family at all: the keyed lookup is the only place its figure can live.
        k = str(claim["key"]).lower()
        cands = (_path_suffix_cands(ev, k) or ev.by_leaf.get(k, []) or [])
    if claim.get("scope"):
        inside = [c for c in cands if _in_scope(c[1], claim["scope"])]
        # the artifact this sentence cites is where its figure was read; an unrelated leaf
        # holding the same number is a coincidence, not a second reading of the same quantity
        cands = inside or cands
    scoped = scoped_values(ev, claim, claim.get("key") or metric)
    if not cands:
        cands = scoped
    if not cands:
        return "UNBOUND", f"no artifact carries {metric!r}" + (
            f" under key {claim['key']!r}" if claim.get("key") else "")
    hit = next((c for c in scoped + cands
                if agree(claim["value"], c[0], claim["precision"], claim.get("sci"))), None)
    if hit:
        return "ENTAILED", hit[1]
    if claim.get("scope") and claim.get("kind") != "pair" and ev.record is not None:
        # The sentence cited one artifact, and a same-named leaf there disagrees. Before
        # calling that damage, look for the figure in the artifact this record's own gate
        # filed: `540 frames` in G4A's note is G4A's loop, and G0's `frame: 241` is a frame
        # index in a check - a different quantity that merely shares the word.
        own = re.search(r"G\d+[A-Z]*", ev.record.name)
        wide = ev.unit(metric, claim.get("key")) or []
        if own:
            fix = next((c for c in scoped + wide
                        if re.search(own.group(0) + r"(?![0-9A-Z])", c[1].split(":")[0])
                        and agree(claim["value"], c[0], claim["precision"], claim.get("sci"))),
                       None)
            if fix:
                return "ENTAILED", f"{fmt(claim['value'])} @ {fix[1]} (this gate's own artifact;"                     f" {scope_file(claim)} measures another quantity)"
    near = [c for c in cands if same_quantity(claim["value"], c[0])]
    if metric == "knownkey" and _sig_digits(claim["value"]) >= 4:
        # the record's own gate report holds this figure, whatever leaf it filed it under
        own = next((r for v, r in _own_gate_readings(ev)
                    if agree(claim["value"], v, claim["precision"], claim.get("sci"))), None)
        if own:
            return "ENTAILED", f"{fmt(claim['value'])} @ {own} (the record's own gate artifact)"
    if metric == "knownkey":
        # A key-word collision far from the stated figure is not the record's measurement, so it
        # cannot contradict it: the record named no unit and no coordinate, and a number
        # nothing here carries is UNBOUND, per the rule that unmatched digits are not damage.
        if not near:
            return "UNBOUND", (f"no artifact holds {fmt(claim['value'])}"
                               + (f" under {claim['key']!r}" if claim.get("key") else "")
                               + f"; nearest {nearest(cands, claim['value'])} is another key")
    # A shared unit word is not identity. With the whole project in the evidence set, another
    # gate's duration shares the word `s` with this gate's, and a different number there is a
    # different measurement, not damage: CONTRADICTED must stay "the record disagrees with the
    # artifact it says it read". Only a reading this record's own sentence names - the file it
    # cites, or the leaf it keys on - can call it wrong; anything else is an unbound figure.
    pool = near or cands
    # `G4A.md` and report-G4A.json are one measurement in two files; a leaf named `ink` in
    # report-G6.json is G6's ink. When the record's own gate artifact carries the key, only it
    # can call the note wrong - the neighbour's number is a different measurement, so the note
    # falls to UNBOUND and names where the nearest reading lives.
    stem_m = re.search(r"G\d+[A-Z]*", ev.record.name) if ev.record else None
    home = ([c for c in pool if re.search(stem_m.group(0) + r"(?![0-9A-Z])",
                                          c[1].split(":")[0])]
            if stem_m else [])
    pool = home or pool
    if (stem_m and claim.get("kind") == "key" and not claim.get("scope")
            and not any(re.search(stem_m.group(0) + r"(?![0-9A-Z])", ref.split(":")[0])
                        for _, ref in cands)):
        # `curves: 1.5` with no coordinate beside it, matched only in another gate's artifact,
        # is a perturbation this run was fed: G6's `f = 7` is no evidence about grayscott's f
        # in G7. A quoted input is a claim about its own gate's bytes, or it is not a claim.
        return None
    files = {c[1].split(":")[0] for c in pool}
    # the row's own coordinates, when it wrote them: a receipt it names is the measurement
    # it stands or falls on, and no sibling in the same directory got asked
    cited = (claim["scope"] if str(claim.get("kind") or "").startswith("cite_")
             else [t for t in (claim.get("scope") or [])
                   if PINNED_TAIL.search(str(t))
                   or re.search(r"\.[A-Za-z][A-Za-z0-9]{1,5}$",
                                str(t).split(":")[0].rsplit("@", 1)[0])])

    def falsifiable(ref):
        """Can this artifact call the record's figure wrong?

        A gate note's numbers are that gate's measurements. Another gate's report is a
        different run on different data, so it can leave the figure unbound but never
        contradict it; only the note's own artifact, or one the sentence cites, may.
        A closeout record names no gate, so the citation is the only scope it has: when the
        sentence or table row says which receipt it read, an artifact outside that list measured
        a different run, and calling the record wrong would blame it for another file's number.
        """
        # and a row that spelled its evidence as a path is answered by those bytes only
        here = not cited or _referent_matches(ref, cited, roots)
        if not stem_m:
            return here and (not claim.get("scope") or _in_scope(ref, claim["scope"]))
        if re.search(stem_m.group(0) + r"(?![0-9A-Z])", ref.split(":")[0]):
            return here
        # `_in_scope` is permissive when the sentence names nothing, which is exactly the
        # case where nothing here may be called the record's own measurement.
        return here and bool(claim.get("scope")) and _in_scope(ref, claim["scope"])

    named = [c for c in pool if _contradicts(claim, c[1], len(files) == 1, cited, roots)
             and falsifiable(c[1])]
    if not named:
        return "UNBOUND", (f"{fmt(claim['value'])} is in no artifact this record names; "
                           f"{nearest(near or cands, claim['value'])} is the same word elsewhere")
    # a frame rate and a frame time are the same measurement: if the artifact the row
    # cites carries the reciprocal in the other unit, it ran the claim and the row is
    # right, whatever its own fps field says
    other = RECIPROCAL.get(claim["metric"])
    if other and claim["value"]:
        pool2 = ([x for x in ev.unit(other) if _referent_matches(x[1], cited, roots)] if cited
                 else list(ev.unit(other)))
        twin = next((x for x in pool2
                     if agree(1000.0 / claim["value"], x[0], claim["precision"],
                              claim.get("sci"))), None)
        if twin:
            return "ENTAILED", f"1000/{fmt(claim['value'])} = {other} at {twin[1]}"
    return "CONTRADICTED", f"{fmt(claim['value'])} vs {nearest(named, claim['value'])}"


# Nouns whose artifact-side quantity is named by the noun itself: `9 nodes` disagrees with the
# plan's own node count, where a bare `52.9 s` only disagrees with whichever duration shares the
# second. `COUNTER_NOUNS` maps the ones that normalise; `nodes`/`checks`/`fail` arrive through
# the pair families and reach here as bare counters.
COUNTER_METRICS = set(COUNTER_NOUNS.values()) | {"nodes", "checks", "fail", "frames"}


def _names_envelope(claim, head):
    """Does the record's own wording name this envelope quantity?

    `duration_ms` is named by `duration_ms 52900` or by a `wall` token, never by a bare
    `100 ms`: the unit word is shared by every timing a receipt carries, so it is stripped from
    both sides before the comparison.
    """
    words = {t for t in re.split(r"[^a-z0-9]+", head.lower()) if t} - UNIT_TOKENS
    words |= {s for w in words for s in ENVELOPE_SYNONYMS.get(w, ())}
    said = set()
    for field in (claim.get("key"), claim.get("metric"), claim.get("want"), claim.get("excerpt")):
        if field:
            said |= {t for t in re.split(r"[^a-z0-9]+", str(field).lower()) if t}
    return bool(words & (said - UNIT_TOKENS))


def _contradicts(claim, referent, unambiguous, cited=None, bases=()):
    """Can this reading call the record wrong, or is it just the same word elsewhere?

    Three ways to have identity with a reading, in descending strength: the record's sentence
    cites the artifact it was read from; the record names a key and that key is a segment of the
    leaf path (`tone` in `perf.tone.ms_median`), so the leaf is the quantity it named; or the
    claim is a counter - `31 nodes`, `29 checks` - where the noun IS the quantity's name, but
    only while one artifact is in play: `33 checks` is this gate's tally and `34 checks` in the
    next report is the next gate's, so with no citation and no key the claim binds only when
    every reading of that noun comes from the same file. A bare unit word (`52.9 s`) is none of
    these: a dimension dozens of quantities share, so a different number under it elsewhere is a
    different measurement, and UNBOUND is the honest verdict.
    """
    # a row that spelled its evidence as a path, or named a file the pool actually holds,
    # is speaking about those bytes; a basename the directory sweep happened to share is not
    precise = bool(cited) and _referent_matches(referent, cited, bases)
    if claim.get("scope") and _in_scope(referent, claim["scope"]):
        # a gate stamps its own envelope - the wall it waited, the budget it was given,
        # its exit, its harness tally - onto the report; a row that cites some other file
        # and lands here only on a shared basename is quoting a different subject, and no
        # verdict belongs to that stamp
        tail = referent.rsplit(":", 1)[-1]
        # the line-range strip leaves a leading space, and `split(" ")` on that answers with
        # the empty string - the head vanishes and the envelope below is never recognised
        leaf = (LOG_LEAF_RANGE.sub(" ", tail, count=1).split() or [""])[0]
        head = leaf.rsplit(".")[-1].lower().split("[")[0]
        # A receipt's own envelope - the wall it waited, the tally it stamped - answers only a
        # note that names that envelope. A row may cite a receipt for its timings and still be
        # reading another column than `duration_ms`; the gate's 30 s is not the referent of a
        # 0.6 ms backlog, and the unit word they share is not a name.
        if head in GATE_ENVELOPE and not (precise and _names_envelope(claim, head)):
            return False
        return True
    key = claim.get("key")
    if key:
        leaf = referent.rsplit(":", 1)[-1].split(" ")[0]
        # a dotted citation names the whole field: `director.line_ms.mean` IS the leaf, not a
        # segment of it. Judging a claim keyed by the exact path the record cited as "the same word
        # elsewhere" let every dotted-cite drift report UNBOUND instead of CONTRADICTED.
        # A claim's key can arrive bare (`p95`, its unit suffix read off the prose) or as the dotted
        # path the record cited (`perf.p95_ms`). Both name the leaf; only a key that is a different
        # quantity is refused.
        last = leaf.rsplit(".", 1)[-1]
        if not (key == leaf or key in leaf.split(".") or leaf.endswith("." + key)
                or last == key or last.startswith(key + "_")):
            return False
        if not (precise or unambiguous):
            # a key binds a NAME, not a run: `perf.raf.mean` in this report and in the next are
            # two measurements of one quantity, and a row that cites neither is being judged by
            # a leg another file measured. Only the row's own artifact, or a pool drawn from a
            # single file, may call the figure wrong here.
            return False
        # the leaf path carries the units, and the key word is often prose (`wall 5.05 s`
        # against `wall_time_s`); a unit on the path that neither the key nor the claim's
        # own unit names is a different quantity quoted from the same file, which is
        # UNBOUND rather than a contradiction
        path_units = {t for t in re.split(r"[._\[]", leaf.lower()) if t in UNIT_TOKENS}
        key_units = {t for t in re.split(r"[._\-]", str(key).lower()) if t in UNIT_TOKENS}
        own = {str(claim.get("metric") or "").lower()}
        own |= set(re.findall(r"[a-z]+", (claim.get("excerpt") or "").lower()))
        return not (path_units - key_units - own)
    return claim["metric"] in COUNTER_METRICS and unambiguous


USAGE = __doc__.split("\nExit 0")[0].rstrip()


def section_slice(lines, want):
    """(hits, (lo, hi)) for --section: 1-based inclusive bounds of the `## ` section whose
    heading matches `want` - a section id ("28", "\u00a728") or a case-insensitive heading-text
    prefix - running to the next `## ` heading or EOF. `hits` holds every matching heading
    (0-based line) so an ambiguous prefix is refused by name instead of resolved by guessing."""
    want = want.lstrip("\u00a7# \t")
    heads = [i for i, ln in enumerate(lines)
             if ln.startswith("## ") and not ln.startswith("### ")]
    hits = []
    for i in heads:
        head = lines[i][3:].strip()
        m = re.match(r"^(\d+)\s*[.:]?\s+(.*)$", head)
        sid, rest = (m.group(1), m.group(2)) if m else (None, head)
        if sid == want if want.isdigit() else (
                head.lower().startswith(want.lower())
                or rest.lower().startswith(want.lower())):
            hits.append(i)
    if len(hits) != 1:
        return hits, None
    after = [j for j in heads if j > hits[0]]
    # the next heading is 1-based line j+1, so the section owns every line up to j (1-based)
    return hits, (hits[0] + 1, after[0] if after else len(lines))


# --- transcript-claim mode -------------------------------------------------------------------
# The record mode asks whether a written record agrees with the artifacts it cites. A session
# transcript is the other half of the exposure: it carries `Claim:` / `Inference:` / `Guess:`
# lines whose numbers were read off a tool result nobody archives, so a session can end holding a
# figure no artifact restates. kinds-bench solves the equivalent problem for agent runs by grading
# the agent's own final claim against the hidden grader's result and reporting a false-claim rate
# (kbench/runner.py:158-164, 296, 330) - it had the grader, we have receipts.
# So this mode needs no hidden answer: every fact in a tagged line must appear in a receipt the
# run wrote, or the line is reported as unbacked. It never decides whether the claim is TRUE; it
# decides whether anything on disk says it again.
CLAIM_TAG_RE = re.compile(
    r"^\s*(?:[-*+]\s*|\d+[.)]\s*)?(?:\*\*|__)?\s*"
    r"(CLAIM|INFERENCE|GUESS|HYPOTHESIS|SPECULATION|UNKNOWN)\s*(?:\*\*|__)?\s*[:\-\u2013]\s*"
    r"(\S.*)$", re.I)
SCORE_RE = re.compile(r"(\d[\d,]*)\s*/\s*(\d[\d,]*)")
# A claim's number usually wears its unit: `6.2s`, `2.4x`, `32GB`. The record mode's
# STANDALONE_RE refuses a digit glued to a letter because there the unit is a separate column;
# here the whole line is prose, so accept only a known unit and refuse what merely looks numeric -
# `13eedf55` is a sha, `v2.6` a version, and a fact that never was makes every claim unbacked.
UNIT = (r"us|ms|ns|secs?|sec|s|minutes?|mins?|min|hours?|hrs?|h|tok/s|toks?|tokens?|"
        r"MiB|GiB|KiB|MB|GB|TB|KB|%|pp|\u00b0C|px|rows|checks|passes|pass|fails?|frames|"
        r"lines|nodes|cores|procs|runs|sessions?|days?|x|\u00d7|Hz|MHz|GHz|W|V|A|C")
CLAIM_NUM_RE = re.compile(
    r"(?<![A-Za-z\d.,/:/-])(\d[\d,]*(?:\.\d+)?)[ \t]?(?:" + UNIT + r")?(?![\w.])")
# `spine_check.py`, `notes/x.md`, `/tmp/vd4/v.db`: a name is a fact the way a number is.
PATHISH_RE = re.compile(r"(?<![\w/~@-])(~?/[\w.~/-]+\.[A-Za-z][A-Za-z0-9]{1,5}"
                        r"|[\w][\w.~/-]*\.[A-Za-z][A-Za-z0-9]{1,5})(?![\w])")
RECEIPT_SUFFIXES = (".json", ".jsonl", ".log", ".txt", ".md", ".csv", ".out", ".sum", ".html")
RECEIPT_MAX_BYTES = 2 << 20       # per file
CORPUS_MAX_BYTES = 64 << 20       # total


def receipt_corpus(paths):
    """(files, corpus) for --receipts: each path a file, a directory (swept shallowly) or a glob.

    Refuses to read nothing silently: a mode that reports claims SUPPORTED over an empty corpus is
    worse than the mode that does not exist, so an unreadable or empty set is a usage error.
    """
    files, corpus = [], {}
    for spec in paths:
        s = str(spec)
        hits = sorted(pathlib.Path(s).parent.glob(s.split("/")[-1])) if any(c in s for c in "*?[") \
            else ([pathlib.Path(s)] if pathlib.Path(s).is_file()
                  else sorted(p for p in pathlib.Path(s).rglob("*")
                              if p.is_file() and p.suffix.lower() in RECEIPT_SUFFIXES)
                  if pathlib.Path(s).is_dir() else [])
        if not hits:
            return None, f"no receipts behind {s!r}"
        for p in hits:
            try:
                if p.stat().st_size > RECEIPT_MAX_BYTES:
                    continue
                text = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if len("".join(corpus.values())) + len(text) > CORPUS_MAX_BYTES:
                break
            files.append(p)
            corpus[str(p)] = text
    if not files:
        return None, f"no readable receipt behind {', '.join(map(str, paths))}"
    return (files, corpus), ""


def claim_facts(text):
    """The checkable facts one tagged line states: scores, numbers, file names.

    Dates, clock times and bare 0/1 are not facts - `0.1.59.1` and `2026-11-13` name a version or
    a timestamp, and treating them as measurements made every line about a run unbackable noise.
    """
    facts, seen = [], set()
    bare = DATEISH_RE.sub(" ", text)
    for m in SCORE_RE.finditer(bare):
        raw = f"{m.group(1)}/{m.group(2)}"
        facts.append(("score", raw, raw.replace(" ", "")))
    for m in CLAIM_NUM_RE.finditer(SCORE_RE.sub(" ", bare)):
        tok = m.group(1)
        v = float(tok.replace(",", ""))
        if v < 2 or (1900 <= v <= 2100 and float(int(v)) == v and m.group(0) == tok):
            continue          # a bare 0/1, or a year with no unit on it
        facts.append(("number", tok, tok.replace(",", "")))
    for m in PATHISH_RE.finditer(text):
        tok = m.group(0)
        if tok.lower().endswith((".py", ".md", ".json", ".log", ".jsonl", ".db", ".txt",
                                 ".sh", ".mjs", ".js", ".html", ".csv")):
            facts.append(("path", tok, tok))
    out = []
    for kind, shown, needle in facts:
        if (kind, needle) in seen:
            continue
        seen.add((kind, needle))
        out.append((kind, shown, needle))
    return out


def find_fact(kind, needle, corpus):
    """Where a fact appears again, or "". A number must appear as a whole token, not a substring
    of a longer figure: `89` inside `1890` is not the harness tally."""
    for path, text in corpus.items():
        if kind == "path":
            if needle in text or pathlib.Path(needle).expanduser().exists():
                return path
            continue
        if kind == "score":
            pat = re.compile(rf"(?<![\d.,]){re.escape(needle)}(?![\d])")
        else:
            pat = re.compile(r"(?<![\d.,])" + re.escape(needle) + r"(?![\d](?!\b)|[.,]\d)")
        if pat.search(text):
            return path
    return ""


def claims_mode(claims_path, receipt_specs, as_json=False, strict_uncheckable=False):
    """Every tagged line in a transcript, against the receipts the run wrote. Exit 3 (the same
    code a contradicted record gets) when a claim's facts appear in no receipt."""
    bundle, err = receipt_corpus(receipt_specs)
    if bundle is None:
        print(f"ERROR {err}", file=sys.stderr)
        return EXIT_USAGE
    files, corpus = bundle
    if not claims_path.is_file():
        print(f"ERROR --claims transcript not found: {claims_path}", file=sys.stderr)
        return EXIT_USAGE
    lines = strip_fences(claims_path.read_text(encoding="utf-8", errors="replace"))
    rows = []
    for num, line in enumerate(lines, 1):
        m = CLAIM_TAG_RE.match(line.rstrip())
        if not m:
            continue
        tag, body = m.group(1).lower(), m.group(2).strip()
        facts = claim_facts(body)
        hits = [(kind, shown, find_fact(kind, needle, corpus))
                for kind, shown, needle in facts]
        missing = [shown for kind, shown, where in hits if not where]
        if not hits:
            verdict = "UNCHECKABLE"
        elif not missing:
            verdict = "SUPPORTED"
        elif len(missing) == len(hits):
            verdict = "UNANCHORED"
        else:
            verdict = "PARTIAL"
        where = "; ".join(f"{shown}@{pathlib.Path(p).name}" for kind, shown, p in hits if p)
        rows.append({"line": num, "tag": tag, "excerpt": body[:110],
                     "facts": [shown for _, shown, _ in hits],
                     "missing": missing, "verdict": verdict, "where": where[:200]})
    tally = {v: sum(1 for r in rows if r["verdict"] == v)
             for v in ("SUPPORTED", "PARTIAL", "UNANCHORED", "UNCHECKABLE")}
    if as_json:
        print(json.dumps({"claims": rows, "tally": tally,
                          "receipts": [str(p) for p in files]}, indent=2))
    else:
        for r in rows:
            mark = "ok " if r["verdict"] == "SUPPORTED" else "  "
            print(f"{mark}claim L{r['line']} [{r['tag']}] {r['verdict']:11s} "
                  f"{r['excerpt'][:80]}")
            if r["missing"]:
                print(f"    facts no receipt restates: {', '.join(r['missing'][:8])}"
                      + (f" | restated: {r['where'][:120]}" if r["where"] else ""))
            elif r["verdict"] == "UNCHECKABLE":
                print("    names no number or file: nothing here re-checks it")
        mb = sum(len(t) for t in corpus.values()) / 1e6
        print("claims %d: %d supported, %d partial, %d unanchored, %d uncheckable "
              "(receipts: %d file(s), %.1f MB)"
              % (len(rows), tally["SUPPORTED"], tally["PARTIAL"], tally["UNANCHORED"],
                 tally["UNCHECKABLE"], len(files), mb))
    bad = tally["UNANCHORED"] + tally["PARTIAL"] + (
        tally["UNCHECKABLE"] if strict_uncheckable else 0)
    if bad:
        print(f"{bad} tagged line(s) whose facts no receipt restates - the number is in the "
              "transcript and nowhere else")
        return EXIT_CONTRADICTED
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        # spine_check --run-harness executes every spine-named script bare, and a script the
        # spine names must exit 0 there (EVENTS 2026-10-13, severity follows declaration).
        # A record named but unreadable is still a usage error - see the harness.
        print(USAGE)
        return 0
    ap = argparse.ArgumentParser(prog="entail_check.py",
                                 description="claim/evidence agreement for a project record")
    ap.add_argument("record", nargs="?", help="the record to audit (markdown)")
    ap.add_argument("--root", action="append", default=[],
                    help="directory to sweep for artifacts (repeatable); the record's own "
                         "directory stays a citation base either way")
    ap.add_argument("--evidence", action="append", default=[],
                    help="an explicit artifact file (repeatable)")
    ap.add_argument("--metrics", help="comma-separated metric filter (verdicts scoped to these)")
    ap.add_argument("--json", action="store_true", help="machine-readable verdict list")
    ap.add_argument("--show-units", action="store_true", dest="show_units",
                    help="list the units carrying digits an auditor cannot check")
    ap.add_argument("--strict", action="store_true", help="UNBOUND also fails")
    ap.add_argument("--min-claims", type=int, default=None, metavar="N", dest="min_claims",
                    help="the record must bind at least N claims: 300 numerals with 1 bound "
                         "claim is not entailed, it is unparsed (default 5 once --min-numerals "
                         "is reached; an explicit N applies at any size; 0 turns it off)")
    ap.add_argument("--min-coverage", type=float, default=0.5, metavar="F", dest="min_coverage",
                    help="fraction of a long record's numerals that must sit in a paragraph "
                         "naming a source (default 0.5; 0 waives the default floor)")
    ap.add_argument("--min-numerals", type=int, default=None, metavar="N", dest="min_numerals",
                    help="both floors apply at or above this many numerals (default 40)"
                         ": a short note with three numbers is not the same failure; on a slice"
                         " the default floors are waived unless this is passed")
    ap.add_argument("--claims", metavar="TRANSCRIPT",
                    help="audit tagged CLAIM/INFERENCE/GUESS lines in a session transcript "
                         "instead of a record: every number, score and file name such a line "
                         "states must appear in a --receipts artifact or the line is reported "
                         "as unbacked (exit %d, the code a contradicted record gets)"
                         % EXIT_CONTRADICTED)
    ap.add_argument("--receipts", action="append", default=[], metavar="PATH",
                    help="with --claims: a receipt file, directory or glob (repeatable)")
    ap.add_argument("--strict-uncheckable", action="store_true", dest="strict_uncheckable",
                    help="with --claims: a tagged line that names no number or file also fails")
    sl = ap.add_mutually_exclusive_group()
    sl.add_argument("--lines", metavar="N:M",
                    help="audit only claims on 1-based inclusive lines N:M of the record;"
                         " citations and receipts still resolve across the full record, and"
                         " the default whole-record floors are waived unless passed")
    sl.add_argument("--section", metavar="HEADING",
                    help="audit only the `## ` section whose heading matches a section id"
                         " (28 or the \u00a728 spelling) or a heading-text prefix, up to the"
                         " next `## ` or EOF")
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        return EXIT_USAGE if exc.code else 0
    if args.claims:
        if not args.receipts:
            print("ERROR --claims needs --receipts: unbacked claims are the thing being looked "
                  "for, so a mode with no artifacts to check against cannot answer",
                  file=sys.stderr)
            return EXIT_USAGE
        return claims_mode(pathlib.Path(args.claims), args.receipts, as_json=args.json,
                           strict_uncheckable=args.strict_uncheckable)
    if not args.record:
        print(USAGE)
        return EXIT_USAGE
    record = pathlib.Path(args.record)
    if not record.is_file():
        print(f"ERROR record not found: {record}", file=sys.stderr)
        return EXIT_USAGE
    roots = ([pathlib.Path(r).resolve() for r in args.root]
             or list(dict.fromkeys([record.resolve().parent, project_root(record)])))
    # A citation is the author pointing at bytes they read, and the base of that pointer is
    # where the record sits - the search roots are an addition, never a relocation.
    bases = tuple(dict.fromkeys([record.resolve().parent, project_root(record), *roots]))
    wanted = {m.strip().lower() for m in args.metrics.split(",")} if args.metrics else None

    record_text = record.read_text(encoding="utf-8", errors="replace")
    all_lines = record_text.splitlines()
    slice_lo = slice_hi = None
    if args.lines:
        m = re.fullmatch(r"(\d+)\s*:\s*(\d+)", args.lines)
        if not m:
            print(f"ERROR --lines wants N:M (1-based, inclusive), got {args.lines!r}",
                  file=sys.stderr)
            return EXIT_USAGE
        slice_lo, slice_hi = int(m.group(1)), int(m.group(2))
        if not 1 <= slice_lo <= slice_hi <= len(all_lines):
            print(f"ERROR --lines {args.lines} outside {record.name} (1..{len(all_lines)})",
                  file=sys.stderr)
            return EXIT_USAGE
    elif args.section:
        hits, bounds = section_slice(all_lines, args.section)
        if bounds is None:
            names = "; ".join(f"L{i + 1}: {all_lines[i].strip()[:60]}" for i in hits)
            print(f"ERROR --section {args.section!r} must match exactly one heading, "
                  f"matched {len(hits)}: {names or 'none'}", file=sys.stderr)
            return EXIT_USAGE
        slice_lo, slice_hi = bounds
    ev = discover(roots, [pathlib.Path(p) for p in args.evidence], record_text, bases)
    ev.record = record.resolve()
    # Resolve a cited coordinate against the record's directory, then the evidence roots and
    # --root, then the spine: a record at agent/notes/ names agent/archive/gates/ relatively,
    # and the first root that resolves wins so a project's own bytes beat the spine's.
    load_record_receipts(record_text, record.resolve(), ev,
                         tuple(dict.fromkeys([record.resolve().parent, *roots,
                                              pathlib.Path(__file__).resolve().parent.parent])))
    if not ev.files:
        print(f"ERROR no readable evidence under: {', '.join(map(str, roots))}", file=sys.stderr)
        return EXIT_NO_EVIDENCE

    kept = [p for p in ev.files if ARCHIVED_MARK in str(p)]
    if kept:
        ev.note(f"{len(ev.files)} artifact(s) read, {len(kept)} of them archived run "
                f"reports under {ARCHIVED_MARK[:-1]}: a figure matching none of them is "
                f"damage, not age")

    rows = []
    for claim in extract(strip_fences(record_text), ev):
        if wanted and claim["metric"] not in wanted:
            continue
        if slice_lo is not None and not slice_lo <= claim["line"] <= slice_hi:
            # judged inside the slice only; the line number still points into the full record
            continue
        verdict, referent = judge(claim, ev, bases)
        if verdict in ("CONTRADICTED", "UNBOUND") and claim["declared"]:
            # The paragraph disowns its own figures in its own words. That is a declaration,
            # not an oversight: counted apart, visible, and never damage against the record.
            verdict, referent = "DECLARED", "the record declares this figure transcript-only"
        rows.append({"metric": claim["metric"], "line": claim["line"],
                     "value": claim["value"] if "value" in claim
                     else f"{claim['a']:g}/{claim['b']:g}",
                     "excerpt": claim["excerpt"], "verdict": verdict, "referent": referent})

    tally = {v: sum(1 for r in rows if r["verdict"] == v)
             for v in ("ENTAILED", "CONTRADICTED", "UNBOUND", "DECLARED")}
    # strip_fences returns lines, not a string: a fenced dump is not a claim, and the record's
    # own dates, shas and receipt coordinates are not measurements either.
    audited_text = ("\n".join(all_lines[slice_lo - 1:slice_hi])
                    if slice_lo is not None else record_text)
    accounted, numerals, bound, unchecked = attribution(audited_text, rows)
    rate = accounted / numerals if numerals else 0.0
    if args.json:
        tally["NUMERALS"] = numerals
        tally["ATTRIBUTED"] = accounted
        tally["BOUND"] = bound
        print(json.dumps({"record": str(record), "evidence": ev.files, "notes": ev.errors,
                          "slice": [slice_lo, slice_hi] if slice_lo is not None else None,
                          "tally": tally, "claims": rows, "unattributed": unchecked},
                         indent=1))
    else:
        for row in rows:
            print(f"{row['verdict']:<12} {row['metric']:<9} line {row['line']:>4}  {row['excerpt']}"
                  f"\n             -> {row['referent']}")
        for note in ev.errors:
            print(f"note: {note}")
        scope = f" lines {slice_lo}-{slice_hi} of {len(all_lines)}" if slice_lo is not None else ""
        print(f"\nentail_check {record.name}{scope}: {len(rows)} claim(s) over {len(ev.files)} "
              f"artifact(s): {tally['ENTAILED']} ENTAILED, {tally['CONTRADICTED']} CONTRADICTED, "
              f"{tally['UNBOUND']} UNBOUND, {tally['DECLARED']} DECLARED")
        # a bare "0 CONTRADICTED, 0 UNBOUND" over 300 numerals is a pass that proved nothing,
        # so the denominator is printed whether or not a floor was asked for, and the stricter
        # bound-outright count travels with it so the softer attribution is never mistaken for it.
        print(f"coverage: {accounted}/{numerals} numerals in a paragraph that names its source"
              f" ({rate:.1%}); {bound} bound outright")
    if tally["CONTRADICTED"]:
        return EXIT_CONTRADICTED
    # A short note with three figures is not a record dodging its work, so the default floors
    # wait until the record is long enough for under-auditing to be a plausible motive; a floor
    # the caller asked for by name is honoured whatever the size, or asking for it would be a no-op
    # on exactly the small records a caller would be worried about.
    # a slice never claimed to audit the whole record, so the default floors (computed against
    # whole-record expectations) are waived unless the caller asks for them by name on the run
    min_numerals = args.min_numerals if args.min_numerals is not None else 40
    floors_waived = slice_lo is not None and args.min_claims is None and args.min_numerals is None
    claims_floor = args.min_claims if args.min_claims is not None else (
        0 if floors_waived else (5 if numerals >= min_numerals else 0))
    floor = None
    if tally["ENTAILED"] < claims_floor:
        floor = f"{claims_floor} bound claims"
    elif numerals >= min_numerals and not floors_waived and rate < args.min_coverage:
        floor = f"{args.min_coverage:.0%} of >= {min_numerals} numerals attributed"
    if floor:
        print(f"UNDERAUDITED {record.name}: {tally['ENTAILED']} bound claim(s), "
              f"{accounted}/{numerals} numerals ({rate:.1%}) attributed, floor is {floor}",
              file=sys.stderr)
    if floor or args.show_units:
        # the floor has to name the units to fix, or the author cites a line at random and the
        # coverage figure rises while the record stays exactly as unchecked as it was.
        for u in unchecked:
            print(f"  uncheckable L{u['lines'][0]}-L{u['lines'][1]} "
                  f"({u['numerals']} numeral(s)): {u['text']}", file=sys.stderr)
    if floor:
        return EXIT_CONTRADICTED
    if args.strict and tally["UNBOUND"]:
        return EXIT_CONTRADICTED
    return 0


if __name__ == "__main__":
    sys.exit(main())
