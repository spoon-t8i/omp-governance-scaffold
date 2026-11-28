#!/usr/bin/env python3
"""L2B surprise gate - the feedback half of the two-layer boundary.

L2A is feedforward: it fires at known deterministic discontinuities (session.compacting,
DAG node closure, a commit boundary) and pays its tokens there. L2B is feedback: it sleeps
during normal L1 execution - zero overhead when nothing is wrong - and wakes only when a
deterministic detector catches an assumption the box has already falsified. Each wake emits
one impedance-matched [L2B-SURPRISE-INTERRUPT: CLASS] block of at most
policy.response.budget_tokens tokens naming (1) the failed assumption and its receipt
coordinate, (2) one deterministic RCA-to-FIX step, (3) the H-DEAD log directive.

Detectors (MVP set; all deterministic, no model call):

  SURPRISE_LOOPING          HIGH      the same normalised command fingerprint >=3x, or the
                                      same file patched >=4x, with no verification gate
                                      between (H-DEAD-25, bg_19 calibration)
  SURPRISE_PROGRESS_STALL   HIGH      a stretch with no gate >= min_gate_free_events events
                                      whose events-per-distinct-target ratio reaches
                                      churn_ratio_threshold - the churn a per-identity bound
                                      cannot see because it spreads over files and launchers
                                      (calibrated 2026-11-19, sha 75463ff2c7f2)
  SURPRISE_REFERENT_BREAK   HIGH      a cited git SHA no live repo resolves (H-DEAD-21
                                      64e348e, H-DEAD-24 075615cb671d), a cited
                                      run/evidence|run/figs artifact absent from disk, or a
                                      gate script rewritten while a campaign runner of that
                                      repo holds the module in RAM (H-DEAD-32)
  SURPRISE_REFERENT_STALE      HIGH  the last green gate receipt no longer covers the tree:
         work landed inside the gated scope after the receipt, or the agent's own commands have
         gone red under it, and no re-certifying gate ran since.
  SURPRISE_POST_COMPACT_DRIFT     CRITICAL  raw <tool_call> / <function=edit> / <function=write>
                                      XML in a turn after a boundary (H-DEAD-11/19), or a
                                      closed DAG node whose gate script moved after its
                                      recorded pass (H-DEAD-32). The harness writes its own
                                      turn records, and a record that is nothing but a closed
                                      tool-call envelope is that serialization, not a leaked
                                      call, so it is skipped and counted in a note; a call
                                      truncated mid-envelope or wrapped in prose - the shape
                                      the corruption actually takes - still fires.
  SURPRISE_TEST_REGRESSION  CRITICAL  a gate command piped through a masking filter without
                                      pipefail, a gate_result that is red or carries no
                                      at_iso stamp (H-DEAD-31), or a gate score that dropped
                                      since its previous receipt

The wake ledger (wake-ledger.jsonl under log_dir) is the lane's memory of what it already woke
for: a finding whose scan scope, subject and cited line window overlap a wake reported inside
looping.max_event_age_s is counted in `repeat_suppressed` and not woken again, so a slid telemetry
window cannot cost the reader the same interrupt once per launch (--no-ledger re-wakes and neither
reads nor writes). The scan log keeps the raw findings, so an audit still sees the undeduped scan.

Inputs. Repos are scanned for the referent, staleness, regression and stale-receipt lanes; the stale lane reads both,
because it keys a green gate taken from the event stream on a referent that must still
exist in the tree. The loop lanes
read an event stream (--telemetry, JSONL objects: {"t","kind","cmd","path","exit"} with
`looping.telemetry_dir` streams one project's events per session slug, and a scan reads only the
streams whose indexed cwd is the repo it audits; a box-wide stream would price one project's loop
on another project's commands, so it is used only as a named fallback.
kind in command|patch|gate); with no stream the lanes report a coverage gap, never a pass.
Both loop findings carry the window their time claims were measured on: inter_arrival_p95_ms
and the longest gate-free stretch in events and seconds.
The raw-XML lane reads --turn-file (a transcript tail the harness exports); with no file the
lane reports a coverage gap. A gap is printed, not scored: L2B never claims a lane ran that
it could not read.

Usage:
  surprise_check.py [--repo DIR]... [--policy FILE] [--telemetry FILE] [--turn-file FILE]
                    [--json] [--quiet] [--max-blocks N] [--pin-baseline REPO] [--self-test]
Exit: 0 dormant (0 surprises), 1 surprises fired, 2 the gate could not run.
"""

import argparse
import glob as globmod
import hashlib
import copy
import json
import os
import re
import shutil
import shlex
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

TOOL_HOME = Path(__file__).resolve().parent
# The scaffold ships the policy beside the frozen spine, not beside the gate: a clone that
# never installs into a home directory still grades against the thresholds it was given.
DEFAULT_POLICY = TOOL_HOME.parent / "spine" / "surprise_policy.json"
SEVERITY_ORDER = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}

POLICY_DEFAULT = {
    "version": 1,
    "comment": "L2B feedback-governance thresholds. Every number here was calibrated against "
               "a recorded failure in EVENTS.md; changing one changes what wakes L2B.",
    "looping": {
        "command_repeat_threshold": 3,
        "file_rewrite_threshold": 4,
        "window_events": 400,
        "max_event_age_s": 7200,
        "telemetry": ["~/.omp/agent/run/session-events.jsonl",
                      "~/.omp/run/session-events.jsonl"],
        # One stream per project, written by session_telemetry beside the box-wide file. Empty or
        # missing dir = fall back to `telemetry`, and say so in a note.
        "telemetry_dir": "~/.omp/agent/run/telemetry",
        "gate_patterns": [
            "spine_check", "plan_check", "_harness", "harness/", "pytest", "py.test", "check_",
            "_test.py", "test_check", "gate_exec", "gates.mjs", "self-test", "self_test",
            "--self-test", "rehydrate", "sieve", "checkpoint.sh", "entail_check", "num_provenance",
            "fact_store", "impact_analysis", "ownership.py", "edit_file", "npm test", "cargo test",
            "make test", "run_tests", "verify",
        ],
        "interpreter_noise": {
            "drop": ["-W", "-B", "-O", "-OO", "-u", "-E", "-I", "-S", "-x", "-P", "-q", "-tt",
                     "-t", "-v", "-s", "--fast"],
            "value": ["-W", "-X", "-c", "-m", "-e", "--rootdir", "-k", "-n", "-p", "--timeout",
                      "-C", "-o"],
            "comment": "verbosity/warning flags whose value is not the target; -c and -m carry "
                       "the program, so their payload is the identity instead",
        },
        "comment": "H-DEAD-25: bg_19 re-launched calibrate_v2.py four times while the real "
                   "defect was one unguarded divide. Three relaunches with no gate between is "
                   "the point where relaunching stopped being exploration.",
    },
    "progress_stall": {
        "min_gate_free_events": 60,
        "churn_ratio_threshold": 3.0,
        "comment": "calibrated 2026-11-19 on the omp transcript corpus (manifest_sha256 75463ff2c7f2952188c8d2742627879919d506679b43ab5e5c2588002045de6c, 253 transcripts, 1299984656 bytes, 1661 labelled repeat increments): a gate-free stretch of >= 60 events whose events-per-distinct-target ratio reaches 3 fires on 5 churn intervals (rate 0.0338) and 7 intervals that ended in a passing gate (rate 0.0015). The grid's looser settings (N12/R2) reach 0.0163 on normal work, which is where a wake stops being a signal. ORDER_MIN holds at share_bound_first 0.9375, so when a per-identity bound crosses inside the same stretch the bound lane owns it and this lane keeps silent.",
    },
    "referent_stale": {
        "enabled": True,
        "min_in_scope_mutations": 1,
        "escalation_failures": 5,
        "comment": "k=1/F=5, calibrated on 295 transcripts / 7515 green-gate windows (7394 closed + 121 live, 39 labelled live): run/field-audit-2026-11-20/rs_calibration_receipt.json, replayed against the shipped lane in rs_shipped_lane_receipt.json. Shipped numbers on the 121 live windows: arm one fires 21, all 21 labelled (precision 1.0, recall 21/39); arms one+two fire 24, 21 labelled (precision 0.875, same recall) - arm two's 3-window increment is 0/3 labelled, so it is shipped for the structural blind spot (a green gate that names no file the mutation can point at: 127 of 7515 windows, 6 of them live) where arm one has no referent to key on, never for recall - recall is flat from F=5 to F=6. F=5 is past the base-rate knee: 3.2% of windows reach five distinct failing launchers, 6.8% reach three; F=1 would wake on every failed grep after a green gate, which is normal work. The calibration's own scope estimator is a substring test and counted 2 extra live windows (01a13762, 01a13f76) whose failing launches were greps and wc, not mutations of the gated file; the lane is path-anchored, so it correctly does not fire there - arm-one precision 1.0 is the shipped number, the estimator's 0.8846 is not. Arm two is also the guard the operator asked for: >=5 distinct failed launches under one uncertified receipt. The bg_19 incident (sessions/-tmp/2026-11-17T06-49-32-630Z_01a1e844-*.jsonl) shows 27 events touching calibrate_v2.py, 5 failing, 5 distinct launcher fingerprints - 4 of them `python3 -` heredocs separated only by rule I06 content hashing (2 distinct without it); that transcript is a LOOPING story (H-DEAD-25), not a stale-receipt window, and this lane does not fire on it.",
    },
    "referent": {
        "cue_grammar_only": True,
        "receipt_exempt": True,
        "json_commit_fields": True,
        "evidence_fields_only": True,
        "intent_fields": ["detail", "deliverable", "name", "title", "plan", "hypothesis",
                          "objective", "description", "next_action", "rationale", "deps"],
        "evidence_fields": ["evidence", "receipt", "receipts", "output", "gate_result",
                            "artifacts", "artifact", "output_path", "receipt_path"],
        "path_glob_exempt": True,
        "receipt_roots": ["~/.omp/agent"],
        "index_prune": [".git", "blobs", "cache", "node_modules", "__pycache__",
                        ".mypy_cache", ".ruff_cache", ".venv"],
        "index_max_files": 40000,
        "basename_elsewhere_is_drift": True,
        "runner_race_minutes": 10,
        "sha_min_len": 7,
        "sha_max_len": 40,
        "batch_git": True,
        "scan": ["STATE.md", "CORE.md", "DAG.json", "SUMMARY.md", "README.md", "FINDINGS.md",
                 "EXPERIMENTS.md", "HYPOTHESES.md", "perm-record.md"],
        "ref_repos": ["~/.omp/agent", "~/Projects/*", "~/*"],
        "ref_repo_max": 64,
        "comment": "A SHA is only a referent break when the grammar around it claims commit "
                   "identity: a bare hash beside sha256/digest/receipt is an artifact digest "
                   "and lives on disk, not in git. run/ is gitignored box-local liveness "
                   "evidence, so a cited run/ path whose basename exists somewhere is recorded "
                   "as path drift (a note, not a surprise); only a basename absent from the "
                   "whole repo means the artifact was never written here.",
    },
    "post_compact": {
        "raw_xml_patterns": ["<tool_call", "<invoke name=", "<function=", "<parameter="],
        "turn_file": "~/.omp/agent/run/session-turn.txt",
        "closed_statuses": ["done", "closed", "verified"],
        "honor_gate_freeze": True,
        "stale_required_from": "2026-11-15",
        "grace_seconds": 90,
        "gate_script_extensions": [".py", ".mjs", ".js", ".sh"],
        "comment": "H-DEAD-11/19: after a boundary the model re-emits an edit/write block from "
                   "evicted memory and the payload corrupts the call. H-DEAD-32: a node stamped "
                   "pass whose gate script has since moved is a pass for code that no longer "
                   "exists, so the node's gate command AND the scripts its evidence names are "
                   "both staleness referents.",
    },
    "test_regression": {
        "masking_filters": ["\\|\\s*head\\b", "\\|\\s*tail\\b", "\\|\\s*grep\\b",
                            "\\|\\s*sed\\s+-n", ">\\s*/dev/null"],
        "pipefail_ok": ["pipefail", "PIPESTATUS"],
        "at_iso_required_from": "2026-11-15",
        "at_iso_keys": ["at_iso", "ts", "stamp", "run_at", "when", "at"],
        "receipt_log": "run/harness_loop.log",
        "baseline_file": "run/surprise/baseline.json",
        "compare_last_two_receipts": True,
        "comment": "H-DEAD-31: a gate run as `... | head -20` returns head's exit status, so a "
                   "failing suite is recorded as a pass; `| tail -N` without pipefail does the "
                   "same to the suite's own exit code. A red or unstamped gate_result is a pass "
                   "claim with no time coordinate, which is unfalsifiable.",
    },
    "response": {
        "budget_tokens": 120,
        "max_blocks": 3,
        "tokenizer_url": "http://127.0.0.1:8080/v1/chat/completions",
        "tokenizer_timeout_s": 5,
        "token_divisor": 3.0,
        "failed_min_words": 4,
        "fix_min_words": 8,
        "comment": "120 tokens with gamma_down = 1.0: the block must be readable in one glance "
                   "and cost less than the next wasted launcher. Token counts come from the live "
                   "tokenizer when reachable (max_tokens=1, usage.prompt_tokens); the divisor is "
                   "the estimate used when it is not, and it is deliberately conservative.",
    },
    "l2b": {
        "telemetry_writer": "~/.omp/agent/tools/session_telemetry.py",
        "refresh_timeout_ms": 2000,
        "telemetry_cwd": None,
        # where a wake tells its reader to write the RCA; absolute, because the wake lands in a
        # session whose cwd is not this repo
        "events_log": "~/.omp/agent/EVENTS.md",
        "comment": "The loop lane reads an event stream it does not produce. This is the step "
                   "that keeps that stream current: an audit asks the writer for a fresh slice "
                   "of the transcript belonging to telemetry_cwd (default: the auditing "
                   "process's own cwd) before scanning. A writer that is missing, times out or "
                   "finds no transcript costs a gap note, never a false pass and never the "
                   "audit itself.",
    },
    "default_repos": ["~/.omp/agent", "~/Projects/chromatic_earthworm"],
    "log_dir": "~/.omp/run/surprise",
    "dead_tag": "H-DEAD-37",
    "comment_top": "Two layers, one boundary. L2A (spine_check/plan_check via the hook and the "
                   "commit boundary) is feedforward: it fires where a discontinuity is already "
                   "known. L2B (this policy) is feedback: it sleeps through normal execution and "
                   "wakes only on an anomaly. default_repos lists what L2B GUARDS; ref_repos "
                   "lists what it may RESOLVE a citation against - guarding every repo on the box "
                   "would make the DORMANT claim unprovable.",
}


def deep_merge(base, over):
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def validate(pol, base=None, path=""):
    """Structural audit of a policy override against the built-in defaults.

    A key the defaults do not have is a typo, and a typo in a policy is the vacuous-pass shape
    this tool exists to catch: the lane the operator meant to tune keeps the default and the
    scan still prints DORMANT. So an unknown key, a type change or a negative threshold is a
    hard error (exit 2), never a shrug. Defaults are the schema, which keeps the two in step.
    """
    base = POLICY_DEFAULT if base is None else base
    bad = []
    for k, v in pol.items():
        if k not in base:
            bad.append(f"unknown key {path}{k}")
            continue
        b = base[k]
        where = f"{path}{k}"
        if isinstance(b, dict):
            if not isinstance(v, dict):
                bad.append(f"{where} must be an object")
            else:
                bad += validate(v, b, where + ".")
        elif isinstance(b, bool):
            if not isinstance(v, bool):
                bad.append(f"{where} must be true or false")
        elif isinstance(b, (int, float)):
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                bad.append(f"{where} must be a number")
            elif b >= 0 and v < 0:
                bad.append(f"{where} must be >= 0")
        elif isinstance(b, str):
            if not isinstance(v, str):
                bad.append(f"{where} must be a string")
        elif isinstance(b, list):
            if not isinstance(v, list):
                bad.append(f"{where} must be a list")
        if where == "response.budget_tokens" and isinstance(v, (int, float)) and v < 40:
            bad.append("response.budget_tokens below 40 cannot hold class, severity, receipt "
                       "and the log directive, which is the minimum actionable wake")
    return bad


def load_policy(path):
    """Return (policy, provenance), or (None, reason) when the policy is unusable.

    A missing file means defaults, and says so. A file that cannot be parsed or that carries a
    key the defaults do not recognise is an error: silently running on defaults after the
    operator edited the thresholds would report a scan under a config nobody chose."""
    p = Path(os.path.expanduser(str(path))) if path else DEFAULT_POLICY
    if not p.exists():
        return copy.deepcopy(POLICY_DEFAULT), f"defaults (no policy at {p})"
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:  # a broken policy must not be mistaken for no policy
        return None, f"policy {p} unreadable: {type(exc).__name__}: {exc}"
    if not isinstance(raw, dict):
        return None, f"policy {p} must hold a JSON object"
    bad = validate(raw)
    if bad:
        return None, f"policy {p} rejected: {'; '.join(bad[:4])}"
    return deep_merge(POLICY_DEFAULT, raw), str(p)


def expand(spec):
    return Path(os.path.expanduser(str(spec)))


def utc_stamp(text):
    """Lenient ISO -> aware UTC. A date-only stamp is read as the END of that day: a node
    closed on 2026-10-17 must not look stale because a tool file moved at 09:00 that same
    day, and the spine's older nodes carry date-only stamps."""
    s = str(text or "").strip()
    if not s:
        return None
    s2 = s.replace("Z", "+00:00")
    for frac in (("%Y-%m-%dT%H:%M:%S.%f%z", True), ("%Y-%m-%dT%H:%M:%S%z", True),
                 ("%Y-%m-%dT%H:%M:%S", False), ("%Y-%m-%dT%H:%M", False),
                 ("%Y-%m-%d", False)):
        try:
            dt = datetime.strptime(s2, frac[0])
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if not frac[1] and len(s2) == 10:  # date-only: end of day
            dt = dt.replace(hour=23, minute=59, second=59)
        return dt.astimezone(timezone.utc)
    return None


def now_utc():
    return datetime.now(timezone.utc)


def mtime_utc(path):
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
    except OSError:
        return None


_NON_LAUNCH = ("cd", "export", "source", ".", "pushd", "popd", "set", "true", "false", "echo",
               "printf", "test", "[", "[[")
_LAUNCH_NOISE = ("sudo", "env", "time", "timeout", "nohup", "nice", "stdbuf",
               "ionice", "watch")
_SHELLS = ("bash", "sh", "zsh", "dash", "python3", "python")
_REDIR = re.compile(r"^(?:\d*(?:>&\d*|>>?|>\S*|<\S*)|&>>?|>>?|<)$")
_DURATION = re.compile(r"^\d+(?:\.\d+)?[smhd]?$")
_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=\S*$")


def _shlex(text):
    try:
        return shlex.split(text)
    except ValueError:
        return text.split()


def _strip_noise(toks, noise):
    """Drop env assignments, redirections, wrappers and interpreter verbosity flags (with the
    values they consume), so `timeout 300 python3 -W ignore x.py` and `python3 x.py` are one
    launcher. The wrappers carry their own arguments, which is why they eat their own flags."""
    out, i = [], 0
    drop, takes = set(noise.get("drop", [])), set(noise.get("value", []))
    while i < len(toks):
        t = toks[i]
        if not t:
            i += 1
            continue
        if _ASSIGN.match(t) or _REDIR.match(t):
            i += 2 if (t[-1] in "<>" and i + 1 < len(toks) and not _REDIR.match(toks[i + 1])) else 1
            continue
        if t in _LAUNCH_NOISE:
            i += 1
            while i < len(toks) and (toks[i].startswith("-") or _DURATION.match(toks[i])):
                i += 1
            continue
        if t in drop:
            i += 2 if (t in takes and i + 1 < len(toks)) else 1
            continue
        out.append(t)
        i += 1
    return out


def _norm_target(t):
    """Which file a launcher runs, in a form that survives the directory it was launched from.

    A path deeper than two components is named by the directory it lives in plus its name:
    `python3 harness/x.py` at the repo root and `python3 /home/dev/Projects/p/harness/x.py` are
    one launcher on one target, and counting them apart is how a loop breaker cries wolf on every
    relaunch that merely spells the path differently. Two projects that both keep a `tools/x.py`
    do share an identity; the loop window is one session's working set, so that collision costs a
    rarer false negative than path-form splitting costs false alarms.
    """
    v = t[2:] if t.startswith("./") else t
    v = v[1:] if v.startswith("~") else v
    parts = [x for x in v.rstrip("/").split("/") if x not in ("", ".")]
    return "/".join(parts[-2:]) if parts else v


def _segments(cmd):
    """Launchable segments of one command line, split at `&&`, `||`, `;`, `|` and newlines.

    The split must be quote-aware. Splitting on a raw newline first cuts a multi-line
    `python3 -c "..."` payload in half, leaving the launcher `python3 "` with no body: every
    inline probe in a session then shares one identity and three unrelated probes read as three
    launches of one experiment (H-DEAD-48). Quote state is what tells a separator from payload.
    """
    out, buf, q = [], [], ""
    s, i = str(cmd or ""), 0
    while i < len(s):
        c = s[i]
        if q:
            buf.append(c)
            if c == q:
                q = ""
            elif c == "\\" and q == '"' and i + 1 < len(s):
                buf.append(s[i + 1])
                i += 1
            i += 1
            continue
        if c in "\"'":
            q, i = c, i + 1
            buf.append(c)
            continue
        if c == "\\" and i + 1 < len(s):
            buf += [c, s[i + 1]]
            i += 2
            continue
        if s[i:i + 2] in ("&&", "||"):
            out.append("".join(buf)); buf = []; i += 2
            continue
        if c in ";|\n":
            out.append("".join(buf)); buf = []
            i += 1
            continue
        buf.append(c); i += 1
    out.append("".join(buf))
    return out


def fingerprint(cmd, lp):
    """Command identity: which launcher, on which target, with which meaning-bearing flags.

    Segments joined by `&&`, `;`, `|` or a newline are split and the first launchable one wins, so
    `cd repo && python3 harness/x.py` is the same experiment as `python3 harness/x.py` - the
    wrapper is not a new hypothesis. Verbosity noise (`-W ignore`, `-B`, `2>&1`, env assignments,
    `timeout 300`) is dropped because the spine writes the same gate both ways. Meaning-bearing
    flags survive, so `harness/x.py --self-test` is NOT a repeat of `harness/x.py`: a suite and
    its self-test are different activities. A shell or interpreter run with `-c` is identified by
    the payload it runs, not by `-c`.
    """
    noise = (lp or {}).get("interpreter_noise", {})
    for seg in _segments(cmd):
        toks = _shlex(seg.strip())
        if not toks or toks[0].lower().rsplit("/", 1)[-1] in _NON_LAUNCH:
            continue
        body = _strip_noise(toks, noise)
        if not body or body[0].lower().rsplit("/", 1)[-1] in _NON_LAUNCH:
            continue
        first = body[0]
        # -c after the wrappers are gone: `timeout 60 python3 -c "..."` is the same payload as
        # `python3 -c "..."`, and testing the raw first token instead missed every probe this
        # spine launches through a timeout, dropping the payload and identifying the launcher by
        # whatever the quote happened to start with.
        low = first.lower().rsplit("/", 1)[-1]
        if low in _SHELLS and "-c" in body:
            payload = re.sub(r"[\"'`]", "", " ".join(body[body.index("-c") + 1:]))
            payload = re.sub(r"\s+", " ", re.sub(r"[<>|]+", " ", payload)).strip()
            if payload:
                if len(payload) > 80:
                    # two long probes sharing their first 120 characters are not one experiment
                    return f"{low} -c {payload[:80]} #{sha12(payload)[:8]}"[:120]
                return f"{low} -c {payload}"[:120]
        if "/" in first:
            # a script run by path is its own target, not a new launcher: `./harness/x.py` and
            # `python3 harness/x.py` and the same file spelled absolutely are one experiment
            exe, target = "", _norm_target(first)
        else:
            exe, target = first.lower(), ""
        flags = []
        for t in body[1:]:
            if t.startswith("-"):
                if t not in flags:
                    flags.append(t)
            elif not target:
                target = _norm_target(t)
            elif t not in flags:
                flags.append(t)          # a positional (git subcommand, make target) is meaning
        if not target and not flags:
            continue
        ident = " ".join(([exe] if exe else []) + ([target] if target else [])
                         + sorted(flags)).strip()
        # A script fed in on stdin or by heredoc has no target token, so every such call shares
        # the bare launcher `python3 -`, and three unrelated probes read as three launches of one
        # experiment. The body IS the target here: salt the identity with its hash, so re-running
        # the same script is the repeat worth interrupting and a different script is a different
        # attempt. A body edited between attempts changes the fingerprint, which errs toward
        # silence - the false alarm is the expensive direction for a boundary interrupt.
        whole = str(cmd or "")
        if target in ("-", "/dev/stdin") or re.search(r"<<[-~]?\s*['\"]?[A-Za-z_]", whole):
            ident += " #" + hashlib.sha1(re.sub(r"\s+", " ", whole).encode("utf-8", "replace")).hexdigest()[:8]
        return ident[:120]
    return ""


def sha12(text):
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:12]


def finding(cls, sev, subject, failed, fix, receipt):
    return {"class": cls, "severity": sev, "subject": subject, "failed": failed,
            "fix": fix, "receipt": receipt}

# --------------------------------------------------------------------------- referent lane

SHA_CUE = re.compile(r"(commit|commits|rev|revision|sha|head=|main@|fork@|tree@|@)\s*[:=#`]?\s*$",
                     re.I)
RECEIPT_CUE = re.compile(r"(\.log|\.json|\.md|\.py|:L\d+|L\d+-L\d+|\brcpt\b|\breceipt\b|"
                         r"\bsha256\b|\bsha\b|\bdigest\b|\bhash\b)\s*[^\s`]{0,24}@$", re.I)
PRE_CUE = re.compile(r"(sha256|sha1|md5|digest|hash|rcpt|receipt|closure|\bsha)\s*[:=]?\s*[`'\"]?$",
                     re.I)
SHA_TOK = re.compile(r"(?<![0-9a-zA-Z@/.\-])([0-9a-f]{7,12}|[0-9a-f]{40})(?![0-9a-zA-Z])")
JSON_COMMIT = re.compile(r'"commit"\s*:\s*"([0-9a-f]{7,40})"')
EVID_PATH = re.compile(r"((?:run/(?:evidence|figs)|archive/gates)/[A-Za-z0-9_\-\./]*)")
EVID_CUE_LINE = re.compile(r"receipt|evidence|artifact|deliverable|gate_result|output|fig|plot|"
                           r"sha256", re.I)
JSON_FIELD = re.compile(r'"([A-Za-z0-9_]+)"\s*:\s*"')
SCRIPT_TOK = re.compile(r"([A-Za-z0-9_\-./]+\.(?:py|mjs|js|sh))")


def rp(root, p):
    """Repo-relative coordinate: a receipt must be cheap to read, and an absolute path costs
    ~8 tokens per occurrence inside a 120-token interrupt block."""
    try:
        return str(Path(p).resolve().relative_to(Path(root).resolve()))
    except (ValueError, OSError):
        return str(p)


def git_root(d):
    """Nearest ancestor holding .git. `~/.omp/agent` is inside the repo rooted at `~/.omp`, so
    treating only the directory itself as a repo makes every commit cited by the spine
    unresolvable and the detector cries wolf (measured: 5 agent SHAs, all real commits)."""
    d = Path(d)
    for cand in [d] + list(d.parents):
        if (cand / ".git").exists():
            return cand
    return None


def reference_repos(pol):
    """Every git repo L2B may resolve a cited SHA against. Resolution against the union is the
    honest rule: a spine may cite a commit in another live repo (amber_particles cites an
    amber_ascii commit), and a SHA that resolves nowhere is the defect the detector is named
    for. Guarded repos come first, so the cap can never price out the repo being audited."""
    out, seen = [], set()
    cap = int(pol["referent"].get("ref_repo_max", 64))
    specs = [str(expand(x)) for x in pol.get("default_repos", [])] + \
            [str(expand(x)) for x in pol["referent"].get("ref_repos", [])]
    for spec in specs:
        cands = sorted(globmod.glob(spec)) if any(ch in spec for ch in "*?[") else [spec]
        for c in cands:
            p = Path(c)
            root = git_root(p) if p.is_dir() else None
            if root and str(root) not in seen:
                seen.add(str(root))
                out.append(root)
    return out[:cap]


def resolve_shas(shas, repos, pol, cache):
    """Which cited SHAs resolve as a commit in at least one live repo. `git cat-file -e` per SHA
    per repo is len(shas) x len(repos) process launches; one --batch-check per repo is the same
    object query at len(repos) launches, and resolution is memoised across files because the
    same anchor is cited by several docs."""
    todo = set(shas) - cache
    if not todo:
        return set(shas) & cache
    resolved = set()
    if not pol["referent"].get("batch_git", True):
        for repo in repos:
            for s in sorted(todo):
                if subprocess.run(["git", "-C", str(repo), "cat-file", "-e", f"{s}^{{commit}}"],
                                  capture_output=True).returncode == 0:
                    resolved.add(s)
            todo -= resolved
            if not todo:
                break
    else:
        for repo in repos:
            if not todo:
                break
            batch = sorted(todo)
            try:
                proc = subprocess.run(
                    ["git", "-C", str(repo), "cat-file", "--batch-check=%(objectname) "
                     "%(objecttype)"],
                    input="".join(f"{s}^{{commit}}\n" for s in batch),
                    capture_output=True, text=True, timeout=30)
            except (OSError, subprocess.TimeoutExpired):
                continue
            for sha, line in zip(batch, proc.stdout.splitlines()):
                parts = line.split()
                if len(parts) > 1 and parts[1] == "commit":
                    resolved.add(sha)
            todo -= resolved
    cache |= resolved
    return resolved | (set(shas) & cache)


def sha_candidates(path, pol):
    """Cue-anchored only. A hex token beside `@`/`commit`/`head=`/`main@` asserts commit
    identity; a hex token beside sha256/digest/receipt, or inside `receipt: file:L1-L11@hex`,
    asserts an artifact digest and is not this lane's business."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return set()
    lo = int(pol["referent"]["sha_min_len"])
    hi = int(pol["referent"]["sha_max_len"])
    hits = set()
    if path.suffix == ".json" and pol["referent"].get("json_commit_fields", True):
        hits |= {s for s in JSON_COMMIT.findall(text) if lo <= len(s) <= hi}
    for m in SHA_TOK.finditer(text):
        s = m.group(1)
        if not lo <= len(s) <= hi:
            continue
        pre = text[max(0, m.start() - 30):m.start()]
        if pol["referent"].get("receipt_exempt", True) and RECEIPT_CUE.search(pre):
            continue
        if PRE_CUE.search(pre):
            continue
        if pol["referent"].get("cue_grammar_only", True) and not SHA_CUE.search(pre):
            continue
        hits.add(s)
    return hits


def _json_field_at(text, pos):
    """Name of the JSON string field containing offset pos (empty string when the text is not
    field-shaped there). Pre-registration fields state intent; only evidence fields assert
    that an artifact exists."""
    field = ""
    for m in JSON_FIELD.finditer(text[:pos]):
        field = m.group(1)
    return field


def evidence_candidates(path, pol):
    """(path, kind, field) triples where kind is 'assert' (an evidence/receipt field or a
    receipt-bearing prose line) or 'intent' (a pre-registration field). Globs and brace sets are
    exempt: a pattern is not a referent."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out = []
    is_json = path.suffix == ".json"
    for m in EVID_PATH.finditer(text):
        raw = m.group(1).rstrip(".,;:")
        nxt = text[m.end():m.end() + 1]
        if pol["referent"].get("path_glob_exempt", True) and (
                nxt in "*?[{[" or any(ch in raw for ch in "*?{")):
            continue
        if is_json:
            field = _json_field_at(text, m.start())
            if field in pol["referent"]["intent_fields"]:
                out.append((raw, "intent", field))
            elif field in pol["referent"]["evidence_fields"] or not field:
                out.append((raw, "assert", field or "root"))
        else:
            line = text[text.rfind("\n", 0, m.start()) + 1: text.find("\n", m.end())]
            if pol["referent"].get("evidence_fields_only", True) and not EVID_CUE_LINE.search(line):
                continue
            out.append((raw, "assert", "prose"))
    return out


def repo_basename_index(repo, pol):
    """Basenames that exist anywhere under the repo. run/ is included (a box-local artifact that
    exists is not a referent break) and structural noise is pruned: the tool home holds a
    26k-file blob store, and walking it to answer one path question is not a check."""
    prune = set(pol["referent"].get("index_prune", []))
    cap = int(pol["referent"].get("index_max_files", 40000))
    idx = {}
    n = 0
    for dp, dn, fn in os.walk(repo):
        dn[:] = [d for d in dn if d not in prune]
        for f in fn:
            n += 1
            if n > cap:
                return idx
            idx.setdefault(f, os.path.join(dp, f))
    return idx


def active_repo_runners(repo):
    """python3 PIDs whose command line belongs to THIS repo - a campaign runner that holds an
    imported module in RAM. Every python3 on the box is not a campaign runner: the ambient MCP
    servers, the sieve and this tool are excluded, or the lane would fire forever."""
    try:
        proc = subprocess.run(["pgrep", "-af", "python3"], capture_output=True, text=True,
                              timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return []
    me = os.getpid()
    out = []
    for line in (proc.stdout or "").splitlines():
        parts = line.strip().split(" ", 1)
        if len(parts) != 2 or not parts[0].isdigit():
            continue
        pid, cmd = int(parts[0]), parts[1]
        if pid == me or "surprise_check.py" in cmd or "pre_compact_sieve.py" in cmd:
            continue
        if str(repo) in cmd:
            out.append((pid, cmd[:140]))
    return out


def gate_scripts(repo, nodes, pol):
    """Script files a node's own gate claims to have exercised: the scripts in its gate command
    plus the scripts its evidence text names. `gate` is a COMMAND string in both DAG shapes, so
    the referent is extracted, not assumed."""
    exts = tuple(pol["post_compact"].get("gate_script_extensions", [".py"]))
    out = {}
    for n in nodes:
        scripts = []
        blob = " ".join([str(n.get("gate") or ""),
                         str((n.get("gate_result") or {}).get("cmd") or ""),
                         " ".join(map(str, n.get("gate_args") or [])),
                         str(n.get("evidence") or "")])
        for tok in SCRIPT_TOK.findall(blob):
            if not tok.endswith(exts):
                continue
            p = (repo / tok) if not Path(tok).is_absolute() else Path(tok)
            if p.exists():
                scripts.append(p)
        if scripts:
            out[str(n.get("id"))] = sorted(set(scripts), key=str)
    return out


def d_referent(repo, pol, ctx):
    findings, notes = [], []
    refs = ctx["ref_repos"]
    idx = None
    roots = [repo] + [expand(r) for r in pol["referent"].get("receipt_roots", [])]
    for rel in pol["referent"]["scan"]:
        p = repo / rel
        if not p.exists():
            continue
        cands = sha_candidates(p, pol)
        if cands:
            bad = sorted(cands - resolve_shas(cands, refs, pol, ctx["resolved"]))
            if bad:
                findings.append(finding(
                    "SURPRISE_REFERENT_BREAK", "HIGH", f"{rel}: {len(bad)} cited SHA(s)",
                    f"{' '.join(bad[:4])} is cited as commit identity and no live repo resolves "
                    f"it - the citation cannot be checked, so it cannot be true or false",
                    f"git -C {refs[0] if refs else '.'} log --oneline | grep -m1 <sha>; correct "
                    "the citation to the real commit or replace it with the artifact receipt "
                    "that does exist",
                    f"{rel} (git cat-file --batch-check over {len(refs)} repos)"))
        drift, homed = [], []
        for raw, _kind, field in [x for x in evidence_candidates(p, pol) if x[1] == "assert"]:
            if (repo / raw).exists():
                continue
            base = raw.rstrip("/").rsplit("/", 1)[-1]
            outside = next((r for r in roots[1:] if (r / raw).exists()), None)
            if outside is not None:
                homed.append(f"{raw} -> {outside}")
                continue
            if idx is None:
                idx = repo_basename_index(repo, pol)
            if base in idx:
                drift.append(f"{raw} -> {os.path.relpath(idx[base], repo)}")
                continue
            findings.append(finding(
                "SURPRISE_REFERENT_BREAK", "HIGH", f"{rel}: {base}",
                f"the {field} field names {raw} as an artifact that exists; it is absent from "
                f"the repo and its basename is nowhere in the tree - the evidence for that claim "
                f"is not on this box",
                f"re-run the gate so it writes {raw}, or rewrite the field to name what the run "
                "really produced (ls run/evidence run/figs archive/gates)",
                f"{rel}:{field}"))
        if drift:
            notes.append(f"{rel}: {len(drift)} evidence path(s) resolve by basename at another "
                         f"prefix ({drift[0]}) - path drift; box-local run/ is gitignored, so "
                         "not a referent break")
        if homed:
            notes.append(f"{rel}: {len(homed)} receipt path(s) resolve against the tool home, not "
                         f"the repo ({rp(repo, homed[0].split(' -> ')[0])} is written by the gate "
                         "sweep relative to ~/.omp/agent)")
    minutes = float(pol["referent"].get("runner_race_minutes", 10))
    runners = active_repo_runners(repo)
    if runners and minutes > 0:
        limit = now_utc().timestamp() - minutes * 60
        for nid, scripts in gate_scripts(repo, ctx["nodes"].get(str(repo), []), pol).items():
            for script in scripts:
                mt = mtime_utc(script)
                if mt and mt.timestamp() > limit:
                    pid, cmd = runners[0]
                    findings.append(finding(
                        "SURPRISE_REFERENT_BREAK", "HIGH", f"{script.name} (node {nid})",
                        f"{script.name} was rewritten at "
                        f"{mt.isoformat(timespec='seconds')} while campaign PID {pid} still "
                        f"holds the old module in RAM - a gate result recorded after that edit "
                        f"measures code the runner is not executing",
                        f"kill {pid}, then re-gate node {nid}: a gate run against a stale module "
                        "is not evidence (H-DEAD-32)",
                        f"{rp(repo, script)} mtime {mt.isoformat(timespec='seconds')} PID {pid}"))
                    break
    return findings, notes
def read_events(paths, limit, max_age_s=None):
    """Events from the given JSONL lanes, oldest first.

    max_age_s drops events whose stamp is older than the window: a stream left behind by a
    session that ended hours ago would otherwise report a loop that is no longer running.
    Unstamped events are kept - the fingerprint needs them, and an absent stamp is the fixture's
    business, not a freshness claim. Returns (events, dropped_as_stale).
    """
    events, stale = [], 0
    cutoff = None if not max_age_s else now_utc() - timedelta(seconds=float(max_age_s))
    for spec in paths:
        p = expand(spec)
        if not p.is_file():
            continue
        try:
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        base = max(0, len(lines) - int(limit))
        for off, raw in enumerate(lines[base:]):
            ln_no, ln = base + off + 1, raw.strip()
            if not ln:
                continue
            try:
                obj = json.loads(ln)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                obj["_src"], obj["_ln"] = str(p), ln_no
                if cutoff is not None:
                    ts = utc_stamp(str(obj.get("t") or ""))
                    if ts is not None and ts < cutoff:
                        stale += 1
                        continue
                events.append(obj)
    return events[-limit:], stale


def _coord(ev):
    """Where this event sits in its telemetry lane, so the wake can cite lines a reader can open.

    Lanes that do not carry coordinates (a fixture, a foreign telemetry file) yield None bounds
    and the receipt falls back to the lane name plus the event stamp.
    """
    return {"src": ev.get("_src"), "a": ev.get("_ln"), "b": ev.get("_ln"), "t": ev.get("t")}


def _bump(c, ev):
    if ev.get("_ln") is not None:
        c["b"] = ev["_ln"]
        c["src"] = ev.get("_src") or c["src"]
    if ev.get("t"):
        c["t"] = ev["t"]


def _cite(source, c):
    """`session-events.jsonl:L711-L714 t=...` - a path a reader can open, at lines they can read.

    Never a bare hash beside the filename: a hex token next to a receipt reads as the file's
    digest (the policy's own rule at `referent.identity_note`), and a hash of the launcher string
    is not the digest of anything on disk, so the wake's own evidence could not be checked.
    """
    nm = Path(str(c.get("src"))).name if c.get("src") else source
    rng = f":L{c['a']}-L{c['b']}" if c.get("a") and c.get("b") else ""
    return f"{nm}{rng} t={c.get('t') or 'unstamped'}"


def d_looping(events, pol, source):
    lp = pol["looping"]
    ct = int(lp["command_repeat_threshold"])
    ft = int(lp["file_rewrite_threshold"])
    clock_note = _clock_note(clock_stats(events))
    cmd_hits, file_hits = {}, {}
    pending_cmds, pending_files = {}, {}
    for ev in events:
        kind = str(ev.get("kind") or "").lower()
        if kind == "gate":
            # a gate run - pass or fail - is verification activity: the loop breaker is
            # "gates were being run", and a failing gate that gets re-run is a different defect
            pending_cmds, pending_files = {}, {}
            continue
        if kind == "command":
            fp = fingerprint(ev.get("cmd") or ev.get("command") or "", lp)
            if not fp:
                continue
            rec = pending_cmds.setdefault(fp, [0, _coord(ev)])
            rec[0] += 1
            _bump(rec[1], ev)
            if rec[0] == ct and fp not in cmd_hits:
                cmd_hits[fp] = (rec, ev)
            pending_files = {}
        elif kind == "patch":
            tgt = str(ev.get("path") or ev.get("file") or "")
            if not tgt:
                continue
            rec = pending_files.setdefault(tgt, [0, _coord(ev)])
            rec[0] += 1
            _bump(rec[1], ev)
            if rec[0] == ft and tgt not in file_hits:
                file_hits[tgt] = (rec, ev)
            pending_cmds = {}
    out = []
    for fp, (rec, ev) in sorted(cmd_hits.items(), key=lambda kv: -kv[1][0][0]):
        n, c = rec[0], rec[1]
        out.append(finding(
            "SURPRISE_LOOPING", "HIGH", f"`{fp}`",
            f"the same launcher ran {n}x with no verification gate between - repeating a launch "
            f"is not a hypothesis, and the recorded class of failure is that the defect was "
            f"elsewhere (H-DEAD-25)" + clock_note,
            "stop relaunching: print the failing value at the fault site and read it, then "
            "state the one hypothesis the next launch tests before launching",
            _cite(source, c)))
    for tgt, (rec, ev) in sorted(file_hits.items(), key=lambda kv: -kv[1][0][0]):
        n, c = rec[0], rec[1]
        out.append(finding(
            "SURPRISE_LOOPING", "HIGH", f"{Path(str(tgt)).name}",
            f"{tgt} was patched {n}x with no gate run in between - blind editing; the calibration "
            "(bg_19: four relaunches, one unguarded divide) says four edits without a gate is "
            "where guessing starts costing more than measuring" + clock_note,
            f"run the gate for {Path(str(tgt)).name} now and read the failure before the next "
            "edit; then edit against the observed value, not the expected one",
            _cite(source, c)))
    return out

def clock_stats(events):
    """The window the loop lanes' time claims were measured on, so a wake can be checked.

    "You have been churning" is a claim about a clock; inter_arrival_p95_ms says how fast the
    stream arrives and max_gate_gap says how long the longest stretch ran with no verification
    in it. These lanes' stamps are second-granular, so the ms field's low digits are zeros and
    the granularity is reported beside the number instead of being hidden by the unit.
    """
    ts = [utc_stamp(ev.get("t")) for ev in events]
    kinds = [str(ev.get("kind") or "").lower() for ev in events]
    gaps = [(b - a).total_seconds() for a, b in zip(ts, ts[1:])
            if a is not None and b is not None and b >= a]
    p95 = None
    if gaps:
        srt = sorted(gaps)
        p95 = round(srt[max(0, (95 * len(srt) - 1) // 100)] * 1000)   # nearest rank
    best_n, best_s, n, t0, tlast = 0, None, 0, None, None

    def span():
        return (n, None if n < 2 or t0 is None or tlast is None
                else round((tlast - t0).total_seconds(), 1))

    for k, t in zip(kinds, ts):
        if k == "gate":
            if n > best_n:
                best_n, best_s = span()
            n, t0, tlast = 0, None, None
            continue
        n += 1
        if t is not None:
            t0 = t0 or t
            tlast = t
    if n > best_n:
        best_n, best_s = span()
    return {"inter_arrival_p95_ms": p95,
            "stamp_granularity": ("s" if all(abs(g - round(g)) < 1e-9 for g in gaps) else "ms")
            if gaps else "unknown",
            "max_gate_gap_events": best_n, "max_gate_gap_s": best_s,
            "gates_in_window": kinds.count("gate")}


def _clock_note(cs):
    return (f" [lane clock: inter_arrival_p95_ms={cs['inter_arrival_p95_ms']} "
            f"max_gate_gap_events={cs['max_gate_gap_events']} "
            f"max_gate_gap_s={cs['max_gate_gap_s']} gates_in_window={cs['gates_in_window']} "
            f"stamp_granularity={cs['stamp_granularity']}]")


def d_progress_stall(events, pol, source):
    """The second detector of the bound pair: a gate-free stretch that is wide but shallow.

    SURPRISE_LOOPING fires on one identity repeated and is blind to churn spread over several
    files and launchers, where no counter reaches a bound and no gate ever runs. That shape is
    events per distinct target inside one gate-to-gate stretch - the same unit the calibration
    swept. ORDER_MIN is enforced here, not just measured: once a per-identity bound has crossed
    inside the stretch the bound lane owns it, and this one reports the suppression instead of
    firing a second wake on one loop. Returns (findings, suppressed_count).
    """
    sp, lp = pol["progress_stall"], pol["looping"]
    n_min = int(sp["min_gate_free_events"])
    r_min = float(sp["churn_ratio_threshold"])
    ct, ft = int(lp["command_repeat_threshold"]), int(lp["file_rewrite_threshold"])
    note = _clock_note(clock_stats(events))
    out, suppressed, run = [], 0, None

    for idx, ev in enumerate(events):
        kind = str(ev.get("kind") or "").lower()
        if kind == "gate":
            run = None
            continue
        if kind not in ("command", "patch", "read"):
            continue
        if run is None:
            run = {"n": 0, "paths": [], "fps": [], "cmd_n": {}, "path_n": {}, "bound": False,
                   "fired": False, "src": ev.get("_src"), "a": ev.get("_ln"), "t": ev.get("t")}
        run["n"] += 1
        if kind == "patch":
            tgt = str(ev.get("path") or ev.get("file") or "")
            if tgt:
                if tgt not in run["paths"]:
                    run["paths"].append(tgt)
                run["path_n"][tgt] = run["path_n"].get(tgt, 0) + 1
                run["bound"] = run["bound"] or run["path_n"][tgt] >= ft
        elif kind == "command":
            fp = fingerprint(ev.get("cmd") or ev.get("command") or "", lp)
            if fp:
                if fp not in run["fps"]:
                    run["fps"].append(fp)
                run["cmd_n"][fp] = run["cmd_n"].get(fp, 0) + 1
                run["bound"] = run["bound"] or run["cmd_n"][fp] >= ct
        prog = max(1, len(run["paths"]) + len(run["fps"]))
        if run["fired"] or run["n"] < n_min or run["n"] < r_min * prog:
            continue
        run["fired"] = True
        if run["bound"]:
            suppressed += 1        # the bound lane owns this stretch; wake once, not twice
            continue
        out.append(finding(
            "SURPRISE_PROGRESS_STALL", "HIGH", f"{run['n']} events with no gate",
            f"{run['n']} events since the last gate touched {len(run['paths'])} file(s) and "
            f"{len(run['fps'])} launcher(s): {run['n'] / prog:.1f} events per distinct target, "
            f"the shape of work that moves without producing a receipt, and no single identity "
            f"reached the repeat bound so the bound lane cannot see it{note}",
            "run the gate for what you are editing right now and read the failure, then name the "
            "one hypothesis the next launch tests; spreading the same guess over more files is "
            "not a new experiment",
            _cite(source, {"src": run["src"], "a": run["a"], "b": ev.get("_ln"),
                           "t": ev.get("t") or run["t"]})))
    return out, suppressed


# ------------------------------------------------------------------- stale-receipt lane
# H-DEAD-53: a green receipt that stopped describing the tree. The referent lane asks whether a
# cited referent exists; the gate-freeze tier asks whether a gate ran. Neither asks whether the
# green *result* still covers what the work has since changed, and the bound pair cannot see it
# either: the window is not a repeated identity and it is not churn - it is work that keeps moving
# under a receipt quoted as though it were still true.

GATE_TARGET_RE = re.compile(
    r"[\w.@/+-]*\.(?:py|pyi|sh|bash|mjs|cjs|js|jsx|ts|tsx|json|sql|rb|go|c|cc|cpp|h|toml|yaml|yml)\b")
REDIR_RE = re.compile(r">>?\s*([\w./@+-]+)")
# A directory scope is written with a trailing slash (`pytest -q tests/`). File tokens are stripped
# before directory tokens are read, because the file regex also matches the `harness/` prefix of
# `harness/x.py`: a gate over one file does not vouch for the directory that file sits in.
DIR_TARGET_RE = re.compile(r"(?<![\w./@+-])(?:[\w.@+-]+/)+")
DIR_QUOTE_RE = re.compile(r"'[^']*'|\"[^\"]*\"")
# A command that changes the tree rather than reading it. A bare `>` is a write; a stderr merge
# (`2>&1`, `2>/dev/null`) is a pipe.
MUTATION_RES = [
    re.compile(r"\bsed\s+-\w*i"), re.compile(r"\bperl\s+-\w*i"), re.compile(r"\btee\b"),
    re.compile(r"\bpatch\b"), re.compile(r"\bapply_patch\b"), re.compile(r"\bedit_file\.py\b"),
    re.compile(r"\bgit\s+(commit|checkout|switch|restore|reset|apply|stash|merge|rebase|clean"
               r"|rm|mv|am|cherry-pick)\b"),
    re.compile(r"\b(cp|mv|rm|touch|mkdir|rmdir|chmod|chown|ln|truncate|rsync|unzip)\b"),
    re.compile(r"\btar\s+[a-zA-Z]*[xzj][a-zA-Z]*f"), re.compile(r"\bdd\b[^|]*\bof="),
    re.compile(r"write_text\(|write_bytes\(|open\([^()]*,\s*['\"][wax]"),
    re.compile(r"os\.replace\(|os\.rename\(|shutil\.(move|copy|rmtree)"),
    re.compile(r"(?<![12])>\s*[^\s&|]"),
]


def _exit(ev):
    try:
        return int(ev.get("exit") or 0)
    except (TypeError, ValueError):
        return 0


def gate_scope(cmd):
    """What a green gate receipt can vouch for: the files its command names (`python3
    harness/x.py`, `python3 -m mod`) and the directories it names with a trailing slash (`pytest -q
    tests/`). A gate naming neither (`make`, bare `pytest`) vouches for no file the mutation
    evidence can point at, so arm one cannot key on it - arm two is what covers that gate."""
    cmd = str(cmd or "")
    files, dirs = set(), set()
    for tok in GATE_TARGET_RE.findall(cmd):
        base = os.path.basename(tok)
        files.add(base.lower())
        files.add(os.path.splitext(base)[0].lower())
    rest = GATE_TARGET_RE.sub(" ", DIR_QUOTE_RE.sub(" ", cmd))
    for tok in DIR_TARGET_RE.findall(rest):
        d = tok.strip("/").lower()
        if d:
            dirs.add(d)
            dirs.add(d.split("/")[-1])
    for m in re.finditer(r"(?:^|\s)(?:-m|--module)\s+([\w.]+)", cmd):
        files.add(m.group(1).split(".")[-1].lower())
    return {"files": files, "dirs": dirs}


def ev_paths(ev):
    """Every path a call touches: the declared path argument, else path-like tokens in the command
    (`sed -i .. f.py`, `python3 probe.py`, `cat > t.py <<EOF`)."""
    out = [str(ev["path"])] if ev.get("path") else []
    cmd = str(ev.get("cmd") or ev.get("command") or "")
    return out + GATE_TARGET_RE.findall(cmd) + REDIR_RE.findall(cmd)


def in_scope(tok, scope):
    tok = str(tok).lower()
    base = os.path.basename(tok)
    if base in scope["files"] or os.path.splitext(base)[0] in scope["files"]:
        return True
    d = os.path.dirname(tok).strip("/")
    return bool(d) and any(x and ("/" + x + "/") in ("/" + d + "/") for x in scope["dirs"])


def owned_referents(findings):
    """Basenames the referent lane has already woken on: the ORDER_MIN key for this lane."""
    out = set()
    for f in findings:
        if f["class"] != "SURPRISE_REFERENT_BREAK":
            continue
        for tok in re.split(r"[\s,;()]+", str(f["subject"])):
            tok = tok.strip(":").lower()
            if "." in tok:
                out.add(os.path.basename(tok))
    return out


def d_referent_stale(events, pol, source, owned=frozenset()):
    """The stale-receipt lane (H-DEAD-53), scored on the window the last green gate opened.

    Two arms, either one is a wake:
      one  a mutation landed inside that gate's own scope after the receipt, so the receipt now
           describes a referent that no longer exists;
      two  >= escalation_failures distinct failing launchers ran in that window with no green gate
           since: the agent's own commands disproved the receipt, which is the case a gate naming
           no file (`make`, bare `pytest`) leaves arm one blind to.
    Only the live window is scored: a window closed by a later green gate has already healed, and
    a re-gate is exactly the fix this finding asks for. Distinct fingerprints, not repeats - a
    repeated identical failure is the bound lane's property (ORDER_MIN, H-DEAD-44). If the referent
    lane already woke on the same basename this receipt covers, that lane owns the referent and
    this one reports the suppression instead of a second wake. Returns (findings, suppressed).
    """
    rs = pol["referent_stale"]
    if not rs.get("enabled", True):
        return [], 0
    gi = max((i for i, e in enumerate(events)
              if str(e.get("kind") or "").lower() == "gate" and _exit(e) == 0), default=None)
    if gi is None:
        return [], 0
    g = events[gi]
    tail = events[gi + 1:]
    if not tail:
        return [], 0
    gcmd = str(g.get("cmd") or g.get("command") or "").strip()
    scope = gate_scope(gcmd)
    named = sorted(x for x in scope["files"] if "." in x) or sorted(scope["dirs"])
    hits = [e for e in tail
            if str(e.get("kind") or "").lower() == "patch"
            or any(rx.search(str(e.get("cmd") or "")) for rx in MUTATION_RES)]
    touched = sorted({os.path.basename(p) for e in hits for p in ev_paths(e)
                      if in_scope(p, scope)})
    arm1 = len(touched) >= int(rs["min_in_scope_mutations"])
    fails, seen = [], set()
    for e in tail:
        if str(e.get("kind") or "").lower() not in ("command", "gate", "patch") or _exit(e) == 0:
            continue
        fp = fingerprint(str(e.get("cmd") or e.get("command") or ""), pol["looping"])
        fp = fp or "%s:%s" % (e.get("kind"), e.get("_ln"))
        if fp not in seen:
            seen.add(fp)
            fails.append(e)
    red_gate = [e for e in tail if str(e.get("kind") or "").lower() == "gate"]
    arm2 = len(fails) >= int(rs["escalation_failures"])
    if not (arm1 or arm2):
        return [], 0
    if named and owned and ({x.lower() for x in named} & set(owned)):
        return [], 1
    why = []
    if arm1:
        why.append("%d mutation(s) landed inside the gated scope (%s)"
                   % (len(touched), ", ".join(touched[:3])))
    if arm2:
        why.append("%d distinct failing launcher(s) after it, none re-certified green" % len(fails))
    if red_gate:
        why.append("a re-gate ran and stayed red (%s)"
                   % str(red_gate[0].get("cmd") or "")[:48].strip() or "red re-gate")
    subject = (named[0] if named else (gcmd.split()[-1] if gcmd else "gate"))[:48]
    return [finding(
        "SURPRISE_REFERENT_STALE", "HIGH", f"{subject}: green receipt behind the work",
        f"the last green gate ({gcmd[:56]}) at line {g.get('_ln')} is followed by {len(tail)} "
        f"event(s): " + "; ".join(why) + " - the receipt is the only green state left on this "
        "referent, and quoting it now is a claim the tree disproves"
        + _clock_note(clock_stats(events)),
        "re-run the gate and read its output before citing the old receipt: " + (gcmd or "the gate"),
        _cite(source, {"src": g.get("_src"), "a": g.get("_ln"), "b": tail[-1].get("_ln"),
                       "t": tail[-1].get("t") or g.get("t")}))], 0


# --------------------------------------------------------------------- post-compaction lane

def load_nodes(repo):
    f = repo / "DAG.json"
    if not f.is_file():
        return []
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None  # None = unreadable, which the caller reports instead of calling clean
    nodes = data.get("nodes") if isinstance(data, dict) else data
    return nodes if isinstance(nodes, list) else []


def gate_stamp(node, pol):
    keys = pol["test_regression"]["at_iso_keys"]
    gr = node.get("gate_result") or {}
    for src in (gr, node):
        if not isinstance(src, dict):
            continue
        for k in keys:
            if src.get(k):
                dt = utc_stamp(src[k])
                if dt:
                    return dt, k
    return None, None


# A turn file mixes two authors. The harness serializes a tool call as a record whose body is
# nothing but the envelope; a model leaking a call into prose is a different thing, and the lane
# must read only the second. A line counts as serialization only when it is nothing but a
# structural tag AND the envelope it opens closes later with nothing but structural lines inside.
# The adapter's parser consumes the closing ">" of a parameter tag when it lifts the argument out
# of the stream, so the recorder writes that one line without it - still structural, still inside
# an envelope that closes clean. A prose line inside the envelope, or an envelope that never
# closes, stays prose. A whole record that is a complete prose-free envelope is indistinguishable
# from the harness's own write and is skipped; the skip is never silent, it is counted in a note.
XML_TAG_LINE = re.compile(
    r"^\s*</?(?:tool_call|invoke\b|function\b|parameter\b|arg\b|result\b)[^\n]*>\s*$")
# A structural line whose closing ">" the parser ate: the tag name, then no ">" anywhere in it.
XML_UNCLOSED_TAG_LINE = re.compile(
    r"^\s*</?(?:tool_call|invoke\b|function\b|parameter\b|arg\b|result\b)[^>\n]*$")
ENVELOPE_CLOSE = (("tool_call", "</tool_call>"), ("invoke", "</invoke>"),
                  ("function", "</function>"), ("parameter", "</parameter>"),
                  ("arg", "</arg>"))


def serialized_xml_lines(lines):
    """1-based line numbers that are harness turn-record serialization, not prose."""
    ignore = set()
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or not XML_TAG_LINE.match(stripped):
            continue
        head = stripped.lstrip("< /")
        for key, closer in ENVELOPE_CLOSE:
            if not head.startswith(key):
                continue
            if stripped.startswith("</"):
                ignore.add(i + 1)  # a bare close tag belongs to the envelope too
                break
            for j in range(i + 1, len(lines)):
                inner = lines[j].strip()
                if inner == closer:
                    ignore.update(range(i + 1, j + 2))
                    break
                if not (XML_TAG_LINE.match(inner) or XML_UNCLOSED_TAG_LINE.match(inner)):
                    break  # prose inside the envelope: leaked, not serialized
            break
    return ignore


def d_post_compact(repo, pol, ctx, turn_file):
    findings, notes = [], []
    pats = pol["post_compact"]["raw_xml_patterns"]
    tf = turn_file or pol["post_compact"].get("turn_file")
    if tf:
        p = expand(tf)
        if p.is_file():
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                notes.append(f"raw-XML lane: {p} unreadable ({exc.__name__}) - gap, not a pass")
                text = ""
            hits = []
            tl = text.splitlines()
            ignore = serialized_xml_lines(tl)
            suppressed = 0
            for pat in pats:
                seen = [n for n, ln in enumerate(tl, 1) if pat in ln]
                kept = [n for n in seen if n not in ignore]
                suppressed += len(seen) - len(kept)
                if kept:
                    hits.append((pat, len(kept), kept[0]))
            if suppressed:
                notes.append(
                    f"raw-XML lane: {suppressed} structural line(s) of {p.name} ignored as "
                    "harness serialization of a tool call, not leaked prose - an envelope with "
                    "a prose line inside, or one that never closes, still fires")
            if hits:
                pat, n, line = hits[0]
                findings.append(finding(
                    "SURPRISE_POST_COMPACT_DRIFT", "CRITICAL", f"{p.name}:{line}",
                    f"raw tool-call XML ({pat}) reached a turn {n}x after a compaction boundary - "
                    f"the seam law forbids edit/write calls because the block is recalled from "
                    f"evicted memory and the payload corrupts the call (H-DEAD-11/19)",
                    "do NOT retry that call; re-read the target file so the bytes come from disk, "
                    "then mutate it through a bash + python3 heredoc - never edit/write",
                    f"{rp(git_root(p) or p.parent, p)}:{line} pattern={pat}"))
        elif str(tf) != "None":
            notes.append(f"raw-XML lane: no turn file at {p} - gap, not a pass")
    else:
        notes.append("raw-XML lane: no --turn-file given - gap, not a pass")

    statuses = [s.lower() for s in pol["post_compact"]["closed_statuses"]]
    grace = float(pol["post_compact"].get("grace_seconds", 0))
    cutoff = utc_stamp(str(pol["post_compact"].get("stale_required_from") or ""))
    pre_contract = []
    scripts = gate_scripts(repo, ctx["nodes"].get(str(repo), []), pol)
    for node in ctx["nodes"].get(str(repo), []):
        if str(node.get("status") or "").lower() not in statuses:
            continue
        if pol["post_compact"].get("honor_gate_freeze", True) and node.get("gate_freeze"):
            continue  # a frozen node claims no liveness, so it cannot be stale
        stamp, key = gate_stamp(node, pol)
        if not stamp:
            continue
        if cutoff and stamp < cutoff:
            # Contract precedence (plan_check.py): a contract binds only what closes after it.
            # The re-gating rule was recorded on stale_required_from; back-applying it to every
            # October closure makes 19 legacy nodes "stale" and drowns the signal it exists to
            # catch, so the lane claims nothing for them and says so in a note.
            pre_contract.append(str(node.get("id")))
            continue
        mine = None
        for script in scripts.get(str(node.get("id")), []):
            mt = mtime_utc(script)
            if mt and (mine is None or mt > mine[0]):
                mine = (mt, script)
        if mine and mine[0].timestamp() > stamp.timestamp() + grace:
            findings.append(finding(
                "SURPRISE_POST_COMPACT_DRIFT", "CRITICAL", f"node {node.get('id')} ({mine[1].name})",
                f"node {node.get('id')} is {node.get('status')} with gate pass {key}="
                f"{stamp.isoformat(timespec='seconds')} but {mine[1].name} moved at "
                f"{mine[0].isoformat(timespec='seconds')} - the recorded pass is for code that no "
                f"longer exists (H-DEAD-32)",
                f"re-gate the node: `cd {repo} && {node.get('gate')}` and record the new "
                "gate_result; an un-re-gated node is STALE, not done",
                f"DAG.json#{node.get('id')} vs {rp(repo, mine[1])}"))
    if pre_contract:
        notes.append(
            f"DAG.json: {len(pre_contract)} closed node(s) gated before the re-gating contract "
            f"({pol['post_compact']['stale_required_from']}) - not claimed stale; first: "
            f"{', '.join(pre_contract[:3])}")
    return findings, notes


# ------------------------------------------------------------------------ regression lane

def receipts(repo, pol):
    """(gate, passes, fails) rows in file order from the gate receipt log spine_check writes."""
    log = repo / pol["test_regression"]["receipt_log"]
    rows = []
    if not log.is_file():
        return rows, log
    try:
        lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return rows, log
    for ln in lines:
        m = re.search(r"gate=(\S+)", ln)
        s = re.search(r"score=(\d+)/(\d+)", ln)
        if m and s:
            rows.append((m.group(1), int(s.group(1)), int(s.group(2))))
    return rows, log


def d_regression(repo, pol, nodes):
    findings, notes = [], []
    tr = pol["test_regression"]
    masks = [re.compile(p) for p in tr["masking_filters"]]
    ok = [t.lower() for t in tr["pipefail_ok"]]
    cutoff = utc_stamp(tr["at_iso_required_from"])
    try:  # the DAG's own liveness is the only date an unstamped row can be judged against
        dag_live = datetime.fromtimestamp((repo / "DAG.json").stat().st_mtime, timezone.utc)
    except OSError:
        dag_live = None
    seen = set()
    for node in nodes:
        gr = node.get("gate_result") or {}
        cmds = [str(node.get("gate") or ""), str(gr.get("cmd") or "")]
        if isinstance(node.get("gate_args"), list):
            cmds.append(" ".join(map(str, node["gate_args"])))
        for cmd in cmds:
            if not cmd.strip():
                continue
            low = cmd.lower()
            for rx in masks:
                if rx.search(cmd) and not any(t in low for t in ok):
                    key = (str(node.get("id")), rx.pattern)
                    if key in seen:
                        continue
                    seen.add(key)
                    findings.append(finding(
                        "SURPRISE_TEST_REGRESSION", "CRITICAL", f"node {node.get('id')}",
                        f"the gate `{cmd[:90]}` pipes through a masking filter "
                        f"({rx.pattern}) with no pipefail, so the exit code recorded is the "
                        f"filter's - a suite that printed 26 passes and then failed still exits 0 "
                        f"(H-DEAD-31)",
                        f"re-run without the mask, or with `set -o pipefail` / "
                        f"`PYTHONUNBUFFERED=1 python3 ... ; exit ${{PIPESTATUS[0]}}`, then "
                        "re-record the node's gate_result",
                        f"{repo / 'DAG.json'}#{node.get('id')} gate"))
        # A stamp is what orders a pass against the code it ran, and the contract binds rows
        # written at or after it: a stamp taken before the cutoff predates the rule, and a DAG
        # nobody has touched since the cutoff cannot be claiming a new pass. A red receipt is
        # different - it contradicts the disk whatever the calendar says, so age never excuses it.
        stamp, key = gate_stamp(node, pol)
        if cutoff and stamp and stamp >= cutoff and key != "at_iso":
            findings.append(finding(
                "SURPRISE_TEST_REGRESSION", "CRITICAL", f"node {node.get('id')}",
                f"a node stamped after {tr['at_iso_required_from']} carries only {key}="
                f"{stamp.date()} - a pass claim with a day-resolution stamp cannot be "
                f"ordered against the code it ran, so it proves nothing (H-DEAD-31)",
                f"re-record gate_result with `at_iso` = `date -u +%Y-%m-%dT%H:%M:%SZ` taken "
                "when the gate actually ran",
                f"DAG.json#{node.get('id')} gate_result"))
        rc = None
        for k in ("exit", "exit_code", "rc", "returncode"):
            if isinstance(gr.get(k), int):
                rc = gr[k]
                break
        if rc not in (None, 0):
            findings.append(finding(
                "SURPRISE_TEST_REGRESSION", "CRITICAL", f"node {node.get('id')}",
                f"gate_result records exit {rc} for `{str(node.get('gate'))[:80]}` while the "
                f"node is {node.get('status')} - a red receipt under a closed node is a pass "
                f"claim the box already contradicts",
                f"re-run the gate; either it passes (record the fresh receipt) or it fails "
                "(open the node and write the RCA)",
                f"DAG.json#{node.get('id')} gate_result.exit={rc}"))
        elif gr and stamp is None and cutoff and dag_live and dag_live >= cutoff:
            findings.append(finding(
                "SURPRISE_TEST_REGRESSION", "CRITICAL", f"node {node.get('id')}",
                f"gate_result claims a pass with no stamp any of {', '.join(tr['at_iso_keys'])} "
                "can parse, in a DAG.json last written after the re-gating contract began - an "
                "unstamped pass is the vacuous-pass shape H-DEAD-31 recorded: the node reads "
                "closed and nothing on disk says when the gate ran",
                "re-run the gate and re-record the receipt with `at_iso` = "
                "`date -u +%Y-%m-%dT%H:%M:%SZ` taken at the run, not the day it was typed",
                f"DAG.json#{node.get('id')} gate_result (no stamp)"))
    rows, log = receipts(repo, pol)
    if tr.get("compare_last_two_receipts", True) and rows:
        per = {}
        for gate, p, f in rows:
            per.setdefault(gate, []).append((p, f))
        for gate, hist in sorted(per.items()):
            if len(hist) < 2:
                continue
            (p0, f0), (p1, f1) = hist[-2], hist[-1]
            if f1 > f0 or (p1 < p0 and f1 == f0):
                findings.append(finding(
                    "SURPRISE_TEST_REGRESSION", "CRITICAL", gate,
                    f"{Path(gate).name} scored {p1}/{f1} on its latest receipt against {p0}/{f0} "
                    f"on the one before it - the suite got worse and nothing said so",
                    f"run the gate at full verbosity and diff the failing check names against the "
                    f"last passing run: `cd {repo} && python3 -W ignore {gate}`",
                    f"{rp(repo, log)}"))
    pin = repo / tr["baseline_file"]
    if pin.is_file():
        try:
            base = json.loads(pin.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            base = {}
        last = {}
        for gate, p, f in rows:
            last[gate] = (p, f)
        for gate, want in sorted(base.items() if isinstance(base, dict) else []):
            got = last.get(gate)
            if not got:
                continue
            w = want if isinstance(want, (list, tuple)) else None
            if w and (got[1] > w[1] or (got[0] < w[0] and got[1] == w[1])):
                findings.append(finding(
                    "SURPRISE_TEST_REGRESSION", "CRITICAL", gate,
                    f"{Path(gate).name} scored {got[0]}/{got[1]} against the pinned baseline "
                    f"{w[0]}/{w[1]} - a regression under the score this repo promised",
                    f"run `cd {repo} && python3 -W ignore {gate}` and restore the checks, or "
                    f"re-pin deliberately with --pin-baseline and say why in "
                    f"{Path(repo) / 'EVENTS.md'}",
                    f"{rp(repo, pin)}"))
    return findings, notes

# ------------------------------------------------------------------ response (impedance match)

def log_path(pol):
    """Where an RCA belongs: an absolute path. A wake is injected into a session whose cwd is
    somewhere else, so a bare `EVENTS.md` is a pointer the reader cannot follow, and a finding
    whose receipt cannot be opened is a finding that cannot be checked."""
    ev = (pol.get("l2b") or {}).get("events_log")
    return str(expand(ev)) if ev else str(Path.cwd() / "EVENTS.md")


_TOK_CACHE = {}


def measure_tokens(text, pol):
    """Absolute count from the live tokenizer: the chat request is priced with max_tokens=1 and
    usage.prompt_tokens is the prompt alone, the same method spine_check uses to price the
    injected spine. Returns None when the server is unreachable; the caller then falls back to
    the conservative character estimate and says the number is an estimate."""
    url = pol["response"].get("tokenizer_url")
    if not url:
        return None
    if text in _TOK_CACHE:
        return _TOK_CACHE[text]
    body = json.dumps({"messages": [{"role": "user", "content": text}],
                       "max_tokens": 1, "temperature": 0}).encode()
    try:
        req = urllib.request.Request(url, data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=float(pol["response"]["tokenizer_timeout_s"])) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
        n = (data.get("usage") or {}).get("prompt_tokens")
        n = int(n) if isinstance(n, (int, float)) else None
    except Exception:
        n = None
    if n is not None:
        # a timeout is never cached: one slow probe while another process hammers the same
        # server must not price every later wake in this process off a failed reading
        _TOK_CACHE[text] = n
    return n


_TOK_BASE = {}


def template_base(pol):
    """What the chat wrapper costs with an empty user turn. The server prices the template plus
    the BOS token, so a raw prompt_tokens reading overstates a short block by ~50 tokens (52
    measured here on 2026-11-18). An interrupt's own price is the marginal count; the absolute
    reading is printed beside it so the subtraction never has to be trusted on its own.
    Returns None when the tokenizer is unreachable; a caller that has no baseline
    has no marginal count either and must price the block as an estimate."""
    # a None is a missing reading, not a result: retry (bounded) so a single timeout does not
    # leave this process pricing every block without a baseline for its whole life
    if _TOK_BASE.get("v") is None and int(_TOK_BASE.get("tries", 0)) < 3:
        _TOK_BASE["tries"] = int(_TOK_BASE.get("tries", 0)) + 1
        _TOK_BASE["v"] = measure_tokens("", pol)
    return _TOK_BASE.get("v")


# The severity each class is calibrated to (H-DEAD-21/24/31/32 severity ladder). A mutant that
# wakes the right class at the wrong severity is a mis-tuned lane, not a pass.
EXPECTED_SEVERITY = {
    "SURPRISE_LOOPING": "HIGH",
    "SURPRISE_REFERENT_BREAK": "HIGH",
    "SURPRISE_REFERENT_STALE": "HIGH",
    "SURPRISE_POST_COMPACT_DRIFT": "CRITICAL",
    "SURPRISE_TEST_REGRESSION": "CRITICAL",
}


def render_block(f, pol):
    """One interrupt block, priced by the live tokenizer and fitted to the budget.

    Price: the server charges for the chat template as well, so `tokens` is the marginal cost the
    block adds to a prompt and `absolute` is the raw reading (see template_base). The block is cut
    to 95% of budget so the published wake has headroom, and it is trimmed in stages ordered by
    how much the reader loses: absolute path prefixes first, then the subject tail (the receipt
    usually repeats it), then WHOLE WORDS off the two prose lines down to a floor, and only as a
    last resort mid-word. The failed line sheds from its TAIL (the fact is in its head) and the fix
    line sheds from its HEAD (the imperative sits at its end): a fix trimmed to "re-read the target
    file, then mu..." has lost the verb, and a wake without its verb is a warning, not an
    instruction.
    Never the class, the severity, the receipt coordinate or the log directive - without those the
    wake is not actionable. Each pass cuts the characters the measured excess is worth at this
    text's own density, so the fit converges rather than oscillating. Returns
    (text, marginal_tokens, absolute_tokens, approx)."""
    budget = int(pol["response"]["budget_tokens"])
    target = int(budget * float(pol["response"].get("budget_fraction", 0.95)))
    div = float(pol["response"].get("token_divisor", 3.0))
    floors = {"failed": int(pol["response"].get("failed_min_words", 4)),
              "fix": int(pol["response"].get("fix_min_words", 9))}
    cls, sev = f["class"], f["severity"]
    subject, receipt = f["subject"], f["receipt"]
    failed, fix = f["failed"], f["fix"]
    log_line = f"log: {pol['dead_tag']} RCA -> " + log_path(pol)

    def build():
        return (f"[L2B-SURPRISE-INTERRUPT: {cls}] sev={sev} subject={subject}\n"
                f"failed: {failed}\nfix: {fix}\nreceipt: {receipt}\n{log_line}")

    def price(text):
        absn = measure_tokens(text, pol)
        base = template_base(pol) if absn is not None else None
        if absn is None or base is None:
            # The block's own price is the MARGINAL count: what the server charges for the block
            # minus what the same request costs with an empty turn (~52 tokens of chat template).
            # Without the baseline the marginal is unknown, and pricing off the absolute alone
            # charges the block for a template that is not its cost: measured on the worst-case
            # block that reads 124 tokens over a 120 budget and trims failed:/fix: to stubs to pay
            # for it. So the estimate is declared, and the absolute is still reported beside it.
            return int(len(text) / div) + 1, absn, True
        return absn - base, absn, False

    def ellipsis(v, keep):
        return v[:max(1, keep - 1)].rstrip() + "\u2026" if keep < len(v) else v

    def trim_prose(cut):
        """Cut `cut` characters out of the two prose fields, split by their current sizes."""
        nonlocal failed, fix
        total = len(failed) + len(fix)
        if total <= 2:
            return False
        room = max(2, total - cut)
        nf = min(len(failed), max(1, int(room * (len(failed) / total)) - 1))
        nx = min(len(fix), max(1, room - nf - 1))
        if nf >= len(failed) and nx >= len(fix):
            return False
        failed, fix = ellipsis(failed, nf), ellipsis(fix, nx)
        return len(failed) + len(fix) < total

    def word_trim(cut):
        """Shed whole words off the two prose lines, split by their current sizes, never below the
        field's floor. Returns True only if something actually shrank."""
        nonlocal failed, fix
        total = len(failed) + len(fix)
        if total <= 2:
            return False
        room = max(2, total - cut)
        share = {"failed": min(len(failed), max(1, int(room * (len(failed) / total)) - 1)),
                 "fix": 0}
        share["fix"] = min(len(fix), max(1, room - share["failed"] - 1))
        changed = False
        for name in ("failed", "fix"):
            v = failed if name == "failed" else fix
            ws = v.split()
            n, acc = len(ws), 0
            for k, w in enumerate(ws):
                acc += len(w) + (1 if k else 0)
                if acc > share[name] - 1:
                    n = k
                    break
            n = max(min(n, len(ws)), floors[name])
            if n >= len(ws):
                cand = v
            elif name == "fix":
                cand = "\u2026 " + " ".join(ws[-n:])       # keep the imperative, drop the preamble
            else:
                cand = " ".join(ws[:n]) + "\u2026"
            if len(cand) < len(v):
                if name == "failed":
                    failed = cand
                else:
                    fix = cand
                changed = True
        return changed

    def shed_subject():
        nonlocal subject
        if len(subject) <= 14:
            return False
        keep = max(14, len(subject) // 2)
        head, sep, tail = subject.rpartition(":")      # keep the line/node coordinate
        if sep and len(subject) > keep:
            room = max(1, keep - len(sep) - len(tail))
            cand = "\u2026" + head[-room:] + sep + tail
            edge = cand.find(" ")   # shed to a word edge: "…n3 tools/x.py" reads as a defect
            if 0 < edge < 14:
                cand = "\u2026" + cand[edge + 1:]
        else:
            cand = "\u2026" + subject[-keep:]
        # when the coordinate IS the payload (a full SHA, a long basename) cutting its head cannot
        # shrink it, and a stage that returns True without progress stalls the ladder above budget
        if len(cand) > len(subject) // 2 + 2:
            cand = "\u2026" + subject[-keep:]
        if len(cand) >= len(subject):
            return False
        subject = cand
        return True

    def shorten_receipt():
        nonlocal receipt, subject
        home = str(Path.home())
        for field in ("receipt", "subject"):
            v = receipt if field == "receipt" else subject
            if v.startswith(home + "/"):
                v = "~" + v[len(home):]
            elif v.startswith("/") and len(v) > 16:
                v = "\u2026/" + "/".join(v.rstrip("/").split("/")[-2:])
            else:
                continue
            if field == "receipt":
                receipt = v
            else:
                subject = v
            return True
        return False

    text = build()
    for _ in range(14):
        marg, absn, approx = price(text)
        if marg <= target:
            return text, marg, absn, approx
        # cost per character at this text's own density, +15% so the next pass undershoots
        cpt = max(div, (len(text) / marg if marg > 0 else div)) * 1.15
        cut = int((marg - target) * cpt) + 1
        if (shorten_receipt() or shed_subject() or word_trim(cut) or trim_prose(cut)):
            text = build()
            continue
        break                      # nothing left to give: report the true size, never hide it
    marg, absn, approx = price(text)
    return text, marg, absn, approx


def dormant_line(count_notes=0):
    return "[L2B: DORMANT - 0 surprises]"


# ------------------------------------------------------------------------------ orchestration

def discover_repos(pol):
    out = []
    for spec in pol.get("default_repos", ["~/.omp/agent"]):
        p = str(expand(spec))
        cands = globmod.glob(p) if any(ch in p for ch in "*?[") else [p]
        for c in cands:
            d = Path(c)
            if (d / "DAG.json").exists() or (d / ".git").exists():
                out.append(d)
    return out


def refresh_telemetry(pol, cwd, timeout_ms):
    """Ask the telemetry writer for a fresh slice of this session's transcript.

    Best-effort by design: L2B must not lose the loop lane because a helper timed out, and it
    must not report a clean loop lane off a stream written during someone else's session. Either
    way the lane says so - a timeout returns "writer-timeout", which the gap text already covers.
    """
    script = pol.get("l2b", {}).get("telemetry_writer")
    if not script:
        return "disabled"
    p = expand(script)
    if not p.is_file():
        return "writer-missing"
    try:
        subprocess.run([sys.executable, "-W", "ignore", str(p), "--quiet", "--cwd", cwd],
                       timeout=max(0.2, float(timeout_ms) / 1000.0), check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        return "writer-timeout"
    return "refreshed"


def project_streams(pol, repos):
    """The per-project event streams for the loop and stall lanes, [] when there are none.

    `run/session-events.jsonl` is one box-wide file holding the slice of whichever session last
    refreshed it, so a scan of repo X can wake on project Y's repeated command, price the
    inter-arrival bound on Y's cadence, and attribute both to X (H-DEAD-49). session_telemetry
    therefore keeps one stream per session slug and indexes the cwd each came from; a repo matches
    a stream when that cwd is the repo, or contains it, or sits inside it (a session opened in a
    workspace audits the repos under it, and vice versa).
    """
    d = pol["looping"].get("telemetry_dir")
    if not d:
        return []
    index = expand(d) / "projects.json"
    try:
        rows = json.loads(index.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(rows, dict):
        return []
    out = []
    for repo in repos:
        r = Path(str(repo))
        for row in rows.values():
            if not isinstance(row, dict):
                continue
            cwd, stream = Path(str(row.get("cwd", ""))), str(row.get("stream", ""))
            if not cwd.is_absolute() or not stream:
                continue
            if cwd == r or cwd in r.parents or r in cwd.parents:
                p = expand(stream)
                if p.is_file() and str(p) not in out:
                    out.append(str(p))
    return out


def resolve_telemetry(pol, repos, opts):
    """Which event streams this scan reads, plus the note naming the choice it had to fall back to.

    Precedence: --telemetry (the operator naming the evidence), then this repo's own project
    streams, then the policy's box-wide list. The last case is reported, never silent: reading
    another project's loop and calling it this repo's is the misattribution this resolves.
    """
    if opts.telemetry:
        return [opts.telemetry], None
    mine = project_streams(pol, repos)
    if mine:
        return mine, None
    shared = [str(s) for s in pol["looping"].get("telemetry", [])]
    if shared and pol["looping"].get("telemetry_dir"):
        return shared, ("loop lane: no per-project stream indexed for "
                        f"{', '.join(str(r) for r in repos)} - read the box-wide stream, whose "
                        "events may belong to another project")
    return shared, None


def scan(repos, pol, opts):
    """Run every lane over every repo. Returns (findings, notes, gaps)."""
    raw = {str(r): load_nodes(r) for r in repos}
    nodes = {k: (v or []) for k, v in raw.items()}
    findings, notes, gaps = [], [], []
    ctx = {"ref_repos": reference_repos(pol), "nodes": nodes, "resolved": set()}
    if not ctx["ref_repos"]:
        gaps.append("referent lane: no git repo found for SHA resolution - gap, not a pass")
    for spec, v in raw.items():
        if v is None:
            gaps.append(f"{spec}: DAG.json unreadable - the node lanes did not run here")
    tel, tel_note = resolve_telemetry(pol, repos, opts)
    if tel_note:
        notes.append(tel_note)
    # The window applies to every source, fixture included: a freshness rule you can switch off
    # by naming a file is not a rule, and the stale case would have nothing to be tested against.
    max_age = float(pol["looping"].get("max_event_age_s", 0)) or None
    events, stale = (read_events(tel, int(pol["looping"]["window_events"]), max_age)
                     if tel else ([], 0))
    live = [str(expand(s)) for s in tel if expand(s).is_file()]
    # A box-wide stream is one file for the whole box, so on that fallback an event may belong to
    # another project entirely: score only events whose cwd is a repo this scan is about (a session
    # opened deeper inside one counts). Nothing left is a named gap, never a wake and never a pass.
    # A pin named by --telemetry is the operator's evidence and is never filtered.
    if events and not opts.telemetry and not project_streams(pol, repos):
        want = [str(expand(r)) for r in repos]

        def _foreign(e):
            # Unknown provenance is not evidence of another project: an event with no cwd is kept,
            # because dropping it would silence the lane on every stream predating the cwd stamp.
            c = str(e.get("cwd") or "")
            return bool(c) and not any(c == w or c.startswith(w + "/") for w in want)

        same = [e for e in events if not _foreign(e)]
        if len(same) != len(events):
            if same:
                notes.append(f"loop lane: {len(events) - len(same)} of {len(events)} event(s) in "
                             f"the box-wide stream carry another cwd and were not scored against "
                             f"{' '.join(want)[:60]}")
            else:
                notes.append("loop lane: the box-wide stream holds no event whose cwd is a repo "
                             "this scan is about, so no wake is raised from a neighbour's loop - "
                             "the lane did not run here: gap, not a pass")
            events = same
    if events:
        srcname = ",".join(Path(x).name for x in live)
        findings += d_looping(events, pol, srcname)
        stall, bound_owned = d_progress_stall(events, pol, srcname)
        findings += stall
        if bound_owned:
            notes.append(f"stall lane: {bound_owned} gate-free stretch(es) met the churn bar but "
                         "a per-identity repeat bound crossed inside the same stretch - the bound "
                         "lane owns them, so this lane kept silent (ORDER_MIN)")
    elif stale:
        notes.append(f"loop lane: {stale} telemetry event(s) older than "
                     f"{int(max_age or 0)}s ignored - no live session was visible, a gap and not "
                     "a pass on the loop lane")
    else:
        gaps.append(f"loop lane: no telemetry events in {', '.join(map(str, tel))} - gap, "
                    "not a pass")
    for repo in repos:
        f, n = d_referent(repo, pol, ctx)
        findings += f
        notes += n
        f, n = d_post_compact(repo, pol, ctx, opts.turn_file)
        findings += f
        notes += n
        f, n = d_regression(repo, pol, nodes[str(repo)])
        findings += f
        notes += n
    if events:
        stale_f, stale_sup = d_referent_stale(events, pol, srcname,
                                               owned=owned_referents(findings))
        findings += stale_f
        if stale_sup:
            notes.append("stale-receipt lane: %d stale window(s) suppressed by ORDER_MIN - the "
                         "referent lane already owns that referent, so this lane kept silent"
                         % stale_sup)
    findings.sort(key=lambda x: (SEVERITY_ORDER.get(x["severity"], 9), x["class"], x["subject"]))
    dedupe = lambda xs: list(dict.fromkeys(xs))
    return findings, dedupe(notes), dedupe(gaps)


# ------------------------------------------------------------------ repeat-wake ledger (H-DEAD-49)
# One loop must cost one wake. The loop lane reads a sliding window, so the same dead command is
# re-read a few events later under a receipt range that has slid along; without a memory of what
# was already reported the lane re-wakes on every launch until the stream turns over. But the
# ledger lives in `log_dir`, which every project on the box shares, so a row only ever suppresses
# a wake about the same scope, subject and cited window - never a look-alike node id, a scratch
# fixture, or a different experiment whose lines do not overlap. Fail open throughout: a ledger
# that cannot be read or written costs the next scan its memory, never the wake itself.
LEDGER_NAME = "wake-ledger.jsonl"
SPAN_IN_RECEIPT = re.compile(r":L(\d+)-L(\d+)")


def ledger_path(pol):
    return expand(pol["log_dir"]) / LEDGER_NAME


def scan_scope(repos, telemetry):
    """What a wake is about: the roots it was scanned over and the stream it read."""
    return "|".join(sorted(str(r) for r in repos)) + "#" + ",".join(sorted(telemetry))


def read_ledger(pol):
    """Wakes already reported, newest last. An unreadable ledger is an empty history."""
    try:
        rows = []
        for line in ledger_path(pol).read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
        return rows
    except OSError:
        return []


def append_ledger(kept, pol, scope, limit=240):
    """Record what was actually woken, so the next scan of the same window stays quiet."""
    try:
        pth = ledger_path(pol)
        pth.parent.mkdir(parents=True, exist_ok=True)
        stamp = now_utc().strftime("%Y-%m-%dT%H:%M:%SZ")
        with pth.open("a", encoding="utf-8") as fh:
            for f in kept:
                fh.write(json.dumps({"at": stamp, "epoch": time.time(), "scope": scope,
                                     "class": f.get("class"), "subject": f.get("subject"),
                                     "receipt": f.get("receipt") or ""}) + "\n")
        lines = pth.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]
        pth.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return pth
    except OSError:
        return None


def _age_s(epoch, stamp):
    if isinstance(epoch, (int, float)):
        return time.time() - float(epoch)
    try:
        then = datetime.strptime(str(stamp)[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None
    return (now_utc() - then).total_seconds()


def suppress_repeats(findings, prior, age_s, scope=None):
    """One loop, one wake, one scope.

    A finding is a repeat only when scope, class and subject all match a row still inside `age_s`
    and the line range it cites overlaps a range already reported: a slid window keeps overlapping,
    a different experiment does not, and the ledger holds no verdict on a range it never saw. A row
    older than `age_s` stops suppressing, so a loop that resumes after the window turns over is
    fresh news. Returns (kept, repeats).
    """
    reported = []
    for e in prior:
        age = _age_s(e.get("epoch"), e.get("at"))
        if age is None or age > age_s:
            continue
        if scope is not None and e.get("scope") != scope:
            continue
        reported.append(((e.get("class"), e.get("subject")),
                         SPAN_IN_RECEIPT.search(str(e.get("receipt") or ""))))
    kept, n = [], 0
    for f in findings:
        key = (f.get("class"), f.get("subject"))
        m = SPAN_IN_RECEIPT.search(str(f.get("receipt") or ""))
        hit = False
        for rkey, rs in reported:
            if rkey != key:
                continue
            if m and rs:
                if int(m.group(1)) <= int(rs.group(2)) and int(rs.group(1)) <= int(m.group(2)):
                    hit = True
                    break
            elif not m and not rs:
                hit = True
                break
        if hit:
            n += 1
        else:
            kept.append(f)
    return kept, n


def emit(findings, notes, gaps, pol, opts, repeats=0):
    budget = int(pol["response"]["budget_tokens"])
    maxb = int(opts.max_blocks or pol["response"]["max_blocks"])
    results = []
    if opts.json:
        print(json.dumps({"surprises": len(findings), "repeat_suppressed": repeats,
                          "budget_tokens": budget,
                          "findings": findings, "notes": notes, "gaps": gaps}, indent=1))
        return results
    if not findings:
        if not opts.quiet:
            print(dormant_line())
            for g in gaps:
                print(f"[L2B-GAP] {g}")
            for n in notes:
                print(f"[L2B-NOTE] {n}")
        return results
    shown = findings[:maxb]
    for f in shown:
        text, tok, absolute, approx = render_block(f, pol)
        results.append((f, text, tok, approx))
        print(text)
        mark = "~" if approx else ""
        price = (f"marginal={mark}{tok}/{budget}" if absolute is None
                 else f"marginal={tok}/{budget} absolute={absolute}")
        print(f"[L2B-TOK] {f['class']} {price} "
              f"({'char estimate' if approx else 'tokenizer'}) "
              f"{'OVER-BUDGET' if tok > budget else 'in budget'}")
    if len(findings) > len(shown):
        print(f"[L2B: {len(findings) - len(shown)} more surprise(s) suppressed by "
              f"max_blocks={maxb}; --json lists all]")
    for g in gaps:
        print(f"[L2B-GAP] {g}")
    for n in notes:
        print(f"[L2B-NOTE] {n}")
    return results


def write_log(findings, pol, tag="scan"):
    try:
        d = expand(pol["log_dir"])
        d.mkdir(parents=True, exist_ok=True)
        stamp = now_utc().strftime("%Y%m%dT%H%M%SZ")
        f = d / f"{stamp}-{tag}.json"
        f.write_text(json.dumps({"at": stamp, "surprises": len(findings),
                                 "findings": findings}, indent=1), encoding="utf-8")
        return f
    except OSError:
        return None


def pin_baseline(repo, pol):
    rows, log = receipts(Path(repo), pol)
    last = {}
    for gate, p, f in rows:
        last[gate] = [p, f]
    out = expand(repo) / pol["test_regression"]["baseline_file"]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(dict(sorted(last.items())), indent=1, sort_keys=True) + "\n",
                   encoding="utf-8")
    return out, len(last)

# ------------------------------------------------------------------------------- self-test

def selftest(live_pol, opts):
    """Two-sided: the clean spine must stay DORMANT, and every mutant must wake the exact class
    inside the impedance budget. Mutants run in a temp tree against a private policy so a probe
    can never write to the spine it is guarding."""
    base = Path(tempfile.mkdtemp(prefix="l2b-selftest-"))

    # Fixtures are stamped relative to now, never to a calendar date: the loop lane honours
    # looping.max_event_age_s, so a literal stamp would age out of the window one morning and
    # the mutants would stop firing for a reason that has nothing to do with the detector.
    fresh = now_utc().strftime("%Y-%m-%dT%H:%M:%SZ")
    stale = (now_utc() - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    budget = int(live_pol["response"]["budget_tokens"])
    tok_ok = measure_tokens("probe", live_pol) is not None

    def polx(ref_repo):
        p = json.loads(json.dumps(POLICY_DEFAULT))
        p["response"]["tokenizer_url"] = live_pol["response"].get("tokenizer_url")
        p["referent"]["ref_repos"] = [str(ref_repo)]
        return p

    def gitinit(d):
        """A real repo, not a directory named .git: a fake one makes every SHA query fail for
        the wrong reason, and a mutant that fires for the wrong reason proves nothing."""
        d.mkdir(parents=True, exist_ok=True)
        r = subprocess.run(["git", "init", "-q", str(d)], capture_output=True, text=True)
        return r.returncode == 0

    def mk(name):
        r = base / name
        r.mkdir(parents=True)
        return r

    # a real one-commit repo so SHA resolution is a live query, never a vacuous pass
    gitref = base / "refs"
    if not gitinit(gitref):
        print("[L2B-SELFTEST-BLOCKED] git init failed - SHA lane cannot be proven")
        return 2
    subprocess.run(["git", "-C", str(gitref), "-c", "user.email=a@b", "-c", "user.name=t",
                    "commit", "-q", "--allow-empty", "-m", "real anchor"], cwd=str(gitref))
    ok = subprocess.run(["git", "-C", str(gitref), "rev-parse", "HEAD"], capture_output=True,
                        text=True).stdout.strip()
    if not ok:
        print("[L2B-SELFTEST-BLOCKED] anchor commit failed - SHA lane cannot be proven")
        return 2

    quiet_turn = base / "quiet-turn.txt"
    quiet_turn.write_text("[assistant] nothing anomalous here\n", encoding="utf-8")

    def run(repo, p, telemetry=None, turn=None):
        # A case that names no turn file must not fall through to the policy default, which points
        # at the live session's own transcript: the raw-XML lane would then grade whoever happens
        # to be running the test, and every mutant would carry an unrelated CRITICAL.
        ns = argparse.Namespace(repo=[str(repo)], policy=None, telemetry=telemetry,
                                turn_file=turn or str(quiet_turn), json=True, quiet=True,
                                max_blocks=None, self_test=False, token_url=None,
                                pin_baseline=None)
        return scan([repo], p, ns)

    def fired(findings, cls, sev=None):
        return [f for f in findings if f["class"] == cls and (sev is None or f["severity"] == sev)]

    cases = []

    def case(cid, cls, repo, p, telemetry=None, turn=None, want=1, probe="", note=""):
        """want >= 1: the class must fire that many times, at the expected severity, inside the
        budget. want == 0: the class must NOT fire at all - the negative controls are what stop a
        detector from being a machine that always screams, and a `>= 0` test would pass them.
        note: a substring the lane's own notes must carry - a suppression that is never stated is
        indistinguishable from a lane that never read the file."""
        fnd, notes, _gaps = run(repo, p, telemetry=telemetry, turn=turn)
        hits = fired(fnd, cls)
        tok = None
        if hits:
            _txt, tok, _abs, _approx = render_block(hits[0], p)
        sev = hits[0]["severity"] if hits else None
        ok = (len(hits) == 0) if want == 0 else (
            len(hits) >= want and sev == EXPECTED_SEVERITY[cls]
            and tok is not None and tok <= budget)
        ok = ok and ((not note) or any(note in n for n in notes))
        cases.append({
            "id": cid, "class": cls, "ok": ok, "want": want, "got": len(hits),
            "sev": sev, "tokens": tok, "fits": (tok is not None and tok <= budget),
            "probe": probe,
            "extra": sorted({f["class"] for f in fnd if f["class"] != cls}),
        })

    # S01 loop lane: identical launcher x3, no gate between
    r = mk("s01")
    tel = base / "s01.jsonl"
    tel.write_text("".join(json.dumps({"t": fresh, "kind": "command",
                                       "cmd": "python3 tools/run_campaign.py --seed 7"}) + "\n"
                           for i in range(1, 4)), encoding="utf-8")
    case("S01", "SURPRISE_LOOPING", r, polx(gitref), telemetry=str(tel), probe="x3 same cmd")

    # S02 loop lane: same file patched x4, no gate run
    r = mk("s02")
    tel = base / "s02.jsonl"
    tel.write_text("".join(json.dumps({"t": fresh, "kind": "patch",
                                       "path": "tools/env.py"}) + "\n" for i in range(1, 5)),
                   encoding="utf-8")
    case("S02", "SURPRISE_LOOPING", r, polx(gitref), telemetry=str(tel), probe="x4 same file")

    # S03 referent lane: a cited commit no repo resolves
    r = mk("s03")
    (r / "README.md").write_text("Fixed the sampler in commit deadbeef after the phase-3 run.\n",
                                 encoding="utf-8")
    case("S03", "SURPRISE_REFERENT_BREAK", r, polx(gitref), probe="cites deadbeef")

    # S04 referent lane: an evidence receipt naming an artifact that is not on disk
    r = mk("s04")
    (r / "DAG.json").write_text(json.dumps({"nodes": [{"id": "N1", "status": "done",
        "gate": "python3 tools/gate.py",
        "gate_result": {"exit": 0, "at_iso": "2026-11-10T00:00:00Z",
                        "output": "run/evidence/ghost_phase3.csv rows=40"}}]}), encoding="utf-8")
    case("S04", "SURPRISE_REFERENT_BREAK", r, polx(gitref), probe="cites missing run/evidence file")

    # S05 post-compaction lane: raw tool XML in a turn
    r = mk("s05")
    turn = base / "s05.txt"
    turn.write_text("working\n<function=edit name=\"edit_file.py\">\n  <parameter=\"path\">x\n",
                    encoding="utf-8")
    case("S05", "SURPRISE_POST_COMPACT_DRIFT", r, polx(gitref), turn=str(turn),
         probe="<function=edit> in turn")

    # S06 post-compaction lane: gate script moved after the node's recorded pass
    r = mk("s06")
    (r / "tools").mkdir()
    (r / "tools" / "gate.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
    (r / "DAG.json").write_text(json.dumps({"nodes": [{"id": "N6", "status": "done",
        "gate": "python3 tools/gate.py",
        "gate_result": {"exit": 0, "at_iso": "2026-11-16T00:00:00Z", "output": "PASS 9/9"}}]}),
        encoding="utf-8")
    case("S06", "SURPRISE_POST_COMPACT_DRIFT", r, polx(gitref), probe="gate script newer than pass")

    # S07 regression lane: a gate masked by head, pipefail absent
    r = mk("s07")
    (r / "DAG.json").write_text(json.dumps({"nodes": [{"id": "N7", "status": "done",
        "gate": "python3 harness/ceis_harness.py | head -20",
        "gate_result": {"exit": 0, "at_iso": "2026-11-17T00:00:00Z", "output": "PASS 26/26"}}]}),
        encoding="utf-8")
    case("S07", "SURPRISE_TEST_REGRESSION", r, polx(gitref), probe="| head -20 gate")

    # S08 regression lane: a red receipt under a closed node AND an unstamped post-cutoff pass
    r = mk("s08")
    (r / "DAG.json").write_text(json.dumps({"nodes": [
        {"id": "N8a", "status": "done", "gate": "python3 tools/gate.py",
         "gate_result": {"exit": 1, "at_iso": "2026-11-17T00:00:00Z", "output": "FAIL 3/26"}},
        {"id": "N8b", "status": "done", "gate": "python3 tools/gate.py",
         "gate_result": {"exit": 0, "at": "2026-11-17", "output": "PASS 1/1"}}]}), encoding="utf-8")
    case("S08", "SURPRISE_TEST_REGRESSION", r, polx(gitref), want=2,
         probe="red receipt + no at_iso")

    # N01-N04: the same lanes must stay shut on honest state - a detector that always screams is
    # as useless as one that never wakes, and these are the only thing proving the thresholds bit
    r = mk("n01")
    tel = base / "n01.jsonl"
    tel.write_text("".join(json.dumps({"t": fresh, "kind": "command",
                                       "cmd": "python3 tools/run_campaign.py --seed 7"}) + "\n"
                           for i in (1, 2)), encoding="utf-8")
    case("N01", "SURPRISE_LOOPING", r, polx(gitref), telemetry=str(tel), want=0,
         probe="x2 same cmd, threshold 3")

    r = mk("n02")
    tel = base / "n02.jsonl"
    tel.write_text("".join(json.dumps({"t": fresh, "kind": "command",
                                       "cmd": f"python3 harness/x.py --self-test{i}"}) + "\n"
                           for i in (1, 2, 3)), encoding="utf-8")
    case("N02", "SURPRISE_LOOPING", r, polx(gitref), telemetry=str(tel), want=0,
         probe="three distinct invocations")

    r = mk("n03")
    (r / "DAG.json").write_text(json.dumps({"nodes": [{"id": "N3", "status": "done",
        "gate": "set -o pipefail; python3 harness/x.py | tail -20",
        "gate_result": {"exit": 0, "at_iso": "2026-11-17T00:00:00Z", "output": "PASS 9/9"}}]}),
        encoding="utf-8")
    case("N03", "SURPRISE_TEST_REGRESSION", r, polx(gitref), want=0,
         probe="pipefail present, tail allowed")

    r = mk("n04")
    (r / "tools").mkdir()
    (r / "tools" / "gate.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
    st = (mtime_utc(r / "tools" / "gate.py") + timedelta(seconds=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    (r / "DAG.json").write_text(json.dumps({"nodes": [{"id": "N4", "status": "done",
        "gate": "python3 tools/gate.py",
        "gate_result": {"exit": 0, "at_iso": st, "output": "PASS 9/9"}}]}), encoding="utf-8")
    case("N04", "SURPRISE_POST_COMPACT_DRIFT", r, polx(gitref), want=0,
         probe="gate script stamped at the pass")

    # L01/L02: the freshness window on the live lane. The loop lane reads a stream another
    # process writes, so two things must both hold: a stream that is current must wake it, and a
    # stream left behind by a session that stopped days ago must not - otherwise a dead session
    # reports a loop nobody is running, and a 120-token interrupt is spent on a ghost.
    r = mk("l01")
    tel = base / "l01.jsonl"
    tel.write_text("".join(json.dumps({"t": fresh, "kind": "command",
                                       "cmd": "python3 tools/run_campaign.py --seed 7"}) + "\n"
                           for _ in range(3)), encoding="utf-8")
    case("L01", "SURPRISE_LOOPING", r, polx(gitref), telemetry=str(tel), want=1,
         probe="three identical launches, stamped now")

    r = mk("l02")
    tel = base / "l02.jsonl"
    tel.write_text("".join(json.dumps({"t": stale, "kind": "command",
                                       "cmd": "python3 tools/run_campaign.py --seed 7"}) + "\n"
                           for _ in range(4)), encoding="utf-8")
    case("L02", "SURPRISE_LOOPING", r, polx(gitref), telemetry=str(tel), want=0,
         probe="four identical launches, stamped three days ago")


    def rspol(**over):
        """The shipped policy plus the escalation floor this lane is scored under. The cases read
        their fixture size from the same policy the lane reads, so moving a calibrated threshold
        retriggers the cases instead of silently leaving a case that no longer probes anything."""
        p = json.loads(json.dumps(POLICY_DEFAULT))
        p["response"]["tokenizer_url"] = live_pol["response"].get("tokenizer_url")
        p["referent"]["ref_repos"] = [str(gitref)]
        p["referent_stale"].update(over)
        return p

    # S09 stale-receipt lane: the gate went green, then the gated file was edited under it
    r = mk("s09")
    tel = base / "s09.jsonl"
    tel.write_text("\n".join([json.dumps({"t": fresh, "kind": "gate", "exit": 0,
                                           "cmd": "python3 harness/x.py --json"}),
                               json.dumps({"t": fresh, "kind": "patch", "path": "harness/x.py"}),
                               json.dumps({"t": fresh, "kind": "command", "exit": 0,
                                           "cmd": "python3 harness/y.py"})]) + "\n",
                   encoding="utf-8")
    case("S09", "SURPRISE_REFERENT_STALE", r, rspol(), telemetry=str(tel),
         probe="mutation inside the gated scope")

    # S10 stale-receipt lane arm two: the gated file is untouched, but the work has gone red under
    # the receipt and no gate re-certified it - the `make`-style gate whose scope arm one cannot key
    r = mk("s10")
    nfail = int(rspol()["referent_stale"]["escalation_failures"])
    ev = [json.dumps({"t": fresh, "kind": "gate", "exit": 0, "cmd": "make check"})]
    ev += [json.dumps({"t": fresh, "kind": "command", "exit": 1,
                       "cmd": "python3 tools/probe%d.py" % i}) for i in range(nfail)]
    tel = base / "s10.jsonl"
    tel.write_text("\n".join(ev) + "\n", encoding="utf-8")
    case("S10", "SURPRISE_REFERENT_STALE", r, rspol(), telemetry=str(tel),
         probe="%d distinct failing launchers under one green receipt" % nfail)

    # N05 stale-receipt negative control: the gate went green over a file the later work never
    # touched, so the receipt still covers what it claims
    r = mk("n05")
    tel = base / "n05.jsonl"
    tel.write_text("\n".join([json.dumps({"t": fresh, "kind": "gate", "exit": 0,
                                           "cmd": "python3 tools/gate.py"}),
                               json.dumps({"t": fresh, "kind": "patch", "path": "notes/other.md"}),
                               json.dumps({"t": fresh, "kind": "command", "exit": 0,
                                           "cmd": "python3 notes/build.py"})]) + "\n",
                   encoding="utf-8")
    case("N05", "SURPRISE_REFERENT_STALE", r, rspol(), telemetry=str(tel), want=0,
         probe="mutations outside the gated scope, nothing red")

    # N06 stale-receipt negative control: one failing launcher short of the escalation floor, with
    # no in-scope mutation - a single red command is a normal experiment, not a stale receipt
    r = mk("n06")
    tel = base / "n06.jsonl"
    tel.write_text("\n".join([json.dumps({"t": fresh, "kind": "gate", "exit": 0,
                                           "cmd": "python3 tools/gate.py"})]
                              + [json.dumps({"t": fresh, "kind": "command", "exit": 1,
                                             "cmd": "python3 tools/probe.py --case %d" % i})
                                 for i in range(max(1, nfail - 1))]) + "\n", encoding="utf-8")
    case("N06", "SURPRISE_REFERENT_STALE", r, rspol(), telemetry=str(tel), want=0,
         probe="below the escalation floor")

    # N07 stale-receipt negative control: the mutation predates the green gate, so the receipt
    # already covers the tree - the healed window is the fix, not the finding
    r = mk("n07")
    tel = base / "n07.jsonl"
    tel.write_text("\n".join([json.dumps({"t": fresh, "kind": "patch", "path": "harness/x.py"}),
                               json.dumps({"t": fresh, "kind": "gate", "exit": 0,
                                           "cmd": "python3 harness/x.py --json"}),
                               json.dumps({"t": fresh, "kind": "command", "exit": 0,
                                           "cmd": "python3 harness/y.py"})]) + "\n",
                   encoding="utf-8")
    case("N07", "SURPRISE_REFERENT_STALE", r, rspol(), telemetry=str(tel), want=0,
         probe="healed by a later green gate")

    # S11 ORDER_MIN: the referent lane already woke on this basename, so the stale lane keeps
    # silent. Both claims point at one broken referent, and the referent lane is the earlier one.
    r = mk("s11")
    (r / "DAG.json").write_text(json.dumps({"nodes": [{"id": "N1", "status": "done",
        "gate": "python3 harness/x.py --json",
        "gate_result": {"exit": 0, "at_iso": "2026-11-10T00:00:00Z",
                        "output": "run/evidence/x.py rows=40"}}]}), encoding="utf-8")
    tel = base / "s11.jsonl"
    tel.write_text("\n".join([json.dumps({"t": fresh, "kind": "gate", "exit": 0,
                                           "cmd": "python3 harness/x.py --json"}),
                               json.dumps({"t": fresh, "kind": "patch", "path": "harness/x.py"}),
                               json.dumps({"t": fresh, "kind": "command", "exit": 0,
                                           "cmd": "python3 harness/y.py"})]) + "\n",
                   encoding="utf-8")
    case("S11", "SURPRISE_REFERENT_STALE", r, rspol(), telemetry=str(tel), want=0,
         probe="ORDER_MIN: referent lane owns x.py")

    # N08 the mirror of S11: the referent lane woke on an unrelated basename, so ownership does not
    # transfer and the stale lane keeps its wake
    r = mk("n08")
    (r / "DAG.json").write_text(json.dumps({"nodes": [{"id": "N1", "status": "done",
        "gate": "python3 harness/x.py --json",
        "gate_result": {"exit": 0, "at_iso": "2026-11-10T00:00:00Z",
                        "output": "run/evidence/ghost_phase3.csv rows=40"}}]}), encoding="utf-8")
    tel = base / "n08.jsonl"
    tel.write_text("\n".join([json.dumps({"t": fresh, "kind": "gate", "exit": 0,
                                           "cmd": "python3 harness/x.py --json"}),
                               json.dumps({"t": fresh, "kind": "patch", "path": "harness/x.py"}),
                               json.dumps({"t": fresh, "kind": "command", "exit": 0,
                                           "cmd": "python3 harness/y.py"})]) + "\n",
                   encoding="utf-8")
    case("N08", "SURPRISE_REFERENT_STALE", r, rspol(), telemetry=str(tel), want=1,
         probe="ORDER_MIN does not transfer across basenames")

    # S12 / N09, the two-sided pair on the raw-XML lane's own blind spot. The harness serializes
    # the calls it records into the very turn file this lane scans, so a bare substring scan reads
    # the recorder quoting a call back as the model leaking one - a CRITICAL no session can act
    # on. N09: a record that is nothing but a closed tool-call envelope stays DORMANT, and the
    # skip is counted in a note, never silent. S12: an envelope wrapped in sentences - the shape
    # H-DEAD-11/19 corruption actually takes - still fires CRITICAL.
    r = mk("s12")
    turn = base / "s12-serialized.txt"
    turn.write_text("# turn 2026-11-28T14:51:32Z\n"
                    "<tool_call>\n<function=read>\n"
                    '<parameter=path="src/gates/spine_check.py:1-2:raw">\n</parameter>\n'
                    "</function>\n</tool_call>\n"
                    "# turn 2026-11-28T15:17:26Z\n"
                    "whole-file grading of the spine is wrong scope\n", encoding="utf-8")
    case("N09", "SURPRISE_POST_COMPACT_DRIFT", r, polx(gitref), turn=str(turn), want=0,
         note="harness serialization",
         probe="serialized turn record is the recorder, not a leaked call")
    # N10: the recorder's real line shape. The adapter's parser eats the closing ">" of the
    # parameter tag, so the recorded line is <parameter=path="x:raw with no ">"; requiring a
    # complete tag read that structural line as prose and fired a CRITICAL at every boundary.
    turn = base / "s12-serialized-unclosed.txt"
    turn.write_text("# turn 2026-11-28T14:51:32Z\n"
                    "<tool_call>\n<function=read>\n"
                    '<parameter=path="src/gates/spine_check.py:1-2:raw\n</parameter>\n'
                    "</function>\n"
                    "</tool_call>\n"
                    "# turn 2026-11-28T15:17:26Z\n"
                    "whole-file grading of the spine is wrong scope\n", encoding="utf-8")
    case("N10", "SURPRISE_POST_COMPACT_DRIFT", r, polx(gitref), turn=str(turn), want=0,
         note="harness serialization",
         probe="a serialized parameter line whose closing > the parser ate is still the recorder")
    turn = base / "s12-leaked.txt"
    turn.write_text("# turn 2026-11-28T15:17:26Z\n"
                    "I will apply it now <tool_call><function=edit>\n"
                    "and then re-run the gate\n", encoding="utf-8")
    case("S12", "SURPRISE_POST_COMPACT_DRIFT", r, polx(gitref), turn=str(turn),
         probe="tool_call envelope leaked into prose")
    # S13 bounds the relaxation: the recorder never writes prose, so a prose line inside an
    # envelope that closes cleanly is a leaked call, not serialization.
    turn = base / "s13-prose-inside.txt"
    turn.write_text("# turn 2026-11-28T15:17:26Z\n"
                    "<tool_call>\n<function=edit>\n"
                    "and then re-run the gate\n"
                    "</function>\n</tool_call>\n", encoding="utf-8")
    case("S13", "SURPRISE_POST_COMPACT_DRIFT", r, polx(gitref), turn=str(turn),
         probe="prose inside a cleanly closed envelope is a leaked call")

    # clean side: a spine that did nothing wrong must stay dormant on every lane. The session
    # lanes are global by nature - the loop lane sees whatever session is writing telemetry, which
    # right now is the one running this test - so they are pinned to a quiet fixture.
    quiet_tel = base / "quiet-events.jsonl"
    quiet_tel.write_text(json.dumps({"t": fresh, "kind": "gate", "cmd":
                                     "python3 harness/quiet_harness.py --self-test"}) + "\n",
                                    encoding="utf-8")

    def scan_clean(d):
        fnd, notes, gaps = scan([d], live_pol, argparse.Namespace(
            repo=[str(d)], policy=None, telemetry=str(quiet_tel), turn_file=str(quiet_turn),
            json=True, quiet=True, max_blocks=None, self_test=False, token_url=None,
            pin_baseline=None))
        return {"repo": d.name, "surprises": len(fnd),
                "findings": [f"{f['class']}:{f['subject']}" for f in fnd][:6],
                "gaps": gaps, "notes": notes[:4]}

    # The verdict rests on a spine this test builds. The repos named in the policy are whatever the
    # box happens to hold: a clone with no agent home would grade nothing, and drift in somebody's
    # STATE.md is a wake for them, not a broken detector. They are still scanned, and anything they
    # wake is printed as an advisory - seen, never counted.
    clean = base / "clean-spine"
    (clean / "tools").mkdir(parents=True)
    (clean / "tools" / "gate.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
    stamped = (mtime_utc(clean / "tools" / "gate.py")
               + timedelta(seconds=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    (clean / "STATE.md").write_text("# STATE\n\n## Active\n- [x] demo gate green\n",
                                    encoding="utf-8")
    (clean / "DAG.json").write_text(json.dumps({"nodes": [{"id": "G1", "status": "done",
        "gate": "python3 tools/gate.py",
        "gate_result": {"exit": 0, "at_iso": stamped, "output": "PASS 3/3"}}]}),
        encoding="utf-8")
    cleans = [scan_clean(clean)]
    advisories = []
    for spec in live_pol["default_repos"]:
        for c in (globmod.glob(str(expand(spec))) if any(ch in spec for ch in "*?[")
                  else [str(expand(spec))]):
            d = Path(c)
            if not (d / "DAG.json").is_file() or d == clean:
                continue
            advisories.append(scan_clean(d))

    # C-lane: the tool's own fail-closed surface. A policy that cannot be read, or a key nobody
    # implemented, must never be reported as a clean scan - that is H-DEAD-31's vacuous pass.
    cfg = []

    def check(cid, cond, probe):
        cfg.append({"id": cid, "ok": bool(cond), "probe": probe})

    check("C01", any("must be >= 0" in x for x in
                     validate({"looping": {"command_repeat_threshold": -2}})),
          "negative threshold rejected")
    check("C02", any("unknown key" in x for x in validate({"refferent": {}})),
          "typo lane name rejected")
    check("C03", any("budget_tokens" in x for x in
                     validate({"response": {"budget_tokens": 10}})),
          "unusable budget rejected")
    brk = base / "broken.json"
    brk.write_text("not json", encoding="utf-8")
    pnil, why = load_policy(brk)
    check("C04", pnil is None and "unreadable" in why, "unparseable policy -> hard error, not defaults")
    pdef, wdef = load_policy(base / "absent.json")
    check("C05", pdef is not None and wdef.startswith("defaults")
          and pdef is not POLICY_DEFAULT and pdef["looping"] is not POLICY_DEFAULT["looping"],
          "missing policy -> deep-copied defaults, spine defaults not mutable")
    check("C06", validate(json.loads(DEFAULT_POLICY.read_text(encoding="utf-8"))) == [],
          "shipped policy validates against the shipped schema")
    rc = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--policy", str(brk),
                         "--repo", str(gitref)], capture_output=True, text=True)
    check("C07", rc.returncode == 2 and "[L2B-ERROR]" in rc.stderr,
          "bad policy exits 2 (never 0 = no anomaly)")
    rc = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--repo",
                         str(base / "no-such-repo"), "--token-url", ""],
                        capture_output=True, text=True)
    check("C08", rc.returncode == 2 and "not a directory" in rc.stderr,
          "a missing audit target is a usage error, not a clean scan")

    # I-lane: command identity, the loop lane's whole discrimination
    lpn = POLICY_DEFAULT["looping"]
    noise_group = ["python3 -W ignore harness/ceis_harness.py",
                   "cd /home/dev/Projects/ceis && python3 harness/ceis_harness.py 2>&1 | tail -20",
                   "timeout 900 python3 -B harness/ceis_harness.py",
                   "OMP_TELEMETRY=1 python3 harness/ceis_harness.py > run/log.txt"]
    idents = {fingerprint(c, lpn) for c in noise_group}
    check("I01", len(idents) == 1 and "" not in idents,
          f"wrapper/verbosity/redirection noise collapses to one launcher ({sorted(idents)})")
    check("I02", fingerprint("python3 harness/x.py", lpn)
          != fingerprint("python3 harness/x.py --self-test", lpn),
          "a suite and its self-test are not the same experiment")
    check("I03", fingerprint("bash -c " + '"' + "python3 g.py | tail" + '"', lpn)
          == fingerprint("bash -c 'python3 g.py|tail'", lpn),
          "a shell -c payload is identified by what it runs")
    check("I04", fingerprint("python3 harness/a/x.py", lpn) != fingerprint("python3 harness/b/x.py", lpn),
          "two different gates with the same basename stay two experiments")
    check("I05", fingerprint("git -C /r1 log --oneline -5", lpn)
          != fingerprint("git -C /r2 log --oneline -3", lpn),
          "the same verb on two repos is not a loop")
    h1 = "python3 - <<'PY'\nimport io\nprint(1)\nPY"
    h2 = "python3 - <<'PY'\nimport json\nprint(2)\nPY"
    check("I06", fingerprint(h1, lpn) != fingerprint(h2, lpn),
          "two different heredoc probes are two experiments, not three launches of one script")
    check("I07", fingerprint(h1, lpn) == fingerprint(h1.replace("print(1)", "print(1)   "), lpn)
          and fingerprint(h1, lpn).startswith("python3 - #"),
          "the same heredoc re-run, restyled, is still the repeat a loop is made of")

    print(f"[L2B-SELFTEST] tree={base} budget={budget} "
          f"tokenizer={'live' if tok_ok else 'char-estimate fallback'}")
    npass = 0
    for c in cases:
        npass += c["ok"]
        print(f"  {c['id']} {c['class']:<26} {c['sev'] or '-':<9} "
              f"{'PASS' if c['ok'] else 'FAIL':<5} tok={c['tokens']} "
              f"({c['got']}/{c['want']} fired, {c['probe']})"
              + (f" extra={','.join(c['extra'])}" if c["extra"] else ""))
    cpass = 0
    for c in cleans:
        ok = c["surprises"] == 0
        cpass += ok
        print(f"  CLEAN {c['repo']:<26} {'PASS' if ok else 'FAIL':<5} "
              f"{c['surprises']} surprises; gaps={len(c['gaps'])} notes={len(c['notes'])}")
        for f in c["findings"]:
            print(f"    unexpected: {f}")
        for n in c["notes"]:
            print(f"    note: {n}")
        for g in c["gaps"]:
            print(f"    gap: {g}")
    for c in advisories:
        if c["surprises"]:
            print(f"  ADVISORY {c['repo']:<20} {c['surprises']} surprise(s) in a repo outside "
                  f"this test, not graded: {'; '.join(c['findings'])}")
    kpass = sum(1 for c in cfg if c["ok"])
    for c in cfg:
        print(f"  {c['id']} {'config/identity'::<18} {'PASS' if c['ok'] else 'FAIL':<5} {c['probe']}")
    good = npass == len(cases) and cpass == len(cleans) and cleans and kpass == len(cfg) and cfg
    ncase = len([c for c in cases if c["want"] > 0])
    nneg = len(cases) - ncase
    print(f"{'PASS' if good else 'FAIL'} {npass}/{len(cases)} mutants "
          f"({ncase} wake, {nneg} must-stay-dormant)  {cpass}/{len(cleans)} clean repos  "
          f"{kpass}/{len(cfg)} config/identity  "
          f"({'measured tokens' if tok_ok else 'estimated tokens'})")
    shutil.rmtree(base, ignore_errors=True)
    return 0 if good else 1


# ------------------------------------------------------------------------------------- main

def main(argv=None):
    ap = argparse.ArgumentParser(prog="surprise_check.py", formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog=__doc__.split("Usage:")[-1])
    ap.add_argument("--repo", action="append", default=[],
                    help="project directory to audit (repeatable); default policy default_repos")
    ap.add_argument("--policy", help="policy JSON (default the shipped spine policy, "
                        "src/spine/surprise_policy.json)")
    ap.add_argument("--telemetry", help="override the event stream for the loop lane")
    ap.add_argument("--turn-file", help="transcript tail to scan for raw tool-call XML")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--quiet", action="store_true", help="print nothing when dormant")
    ap.add_argument("--max-blocks", type=int, help="override max_blocks")
    ap.add_argument("--token-url", help="override the tokenizer URL (\"\" disables it)")
    ap.add_argument("--pin-baseline", metavar="REPO",
                    help="pin the current per-gate score of REPO as the regression floor")
    ap.add_argument("--no-ledger", action="store_true",
                    help="report these findings even if the same scope, subject and cited "
                         "window was already woken inside looping.max_event_age_s: the wake "
                         "ledger keeps one loop to one wake (H-DEAD-49), this reads and "
                         "writes nothing")
    ap.add_argument("--self-test", action="store_true", help="run the S01-S08 mutants")
    ap.add_argument("--no-refresh-telemetry", action="store_true",
                    help="scan the event stream as it stands, without asking the telemetry "
                         "writer to refresh it from the live transcript first")
    ap.add_argument("--telemetry-cwd",
                    help="cwd whose session transcript refreshes the event stream "
                         "(default: this process's cwd, which is the session being audited)")
    opts = ap.parse_args(argv)

    pol, note = load_policy(opts.policy)
    if pol is None:
        print(f"[L2B-ERROR] {note}", file=sys.stderr)
        return 2
    print(f"[L2B-POLICY] {note}", file=sys.stderr)
    if opts.token_url is not None:
        pol["response"]["tokenizer_url"] = opts.token_url or None
    if opts.self_test:
        return selftest(pol, opts)
    if opts.pin_baseline:
        out, n = pin_baseline(opts.pin_baseline, pol)
        print(f"[L2B] pinned {n} gate baseline(s) -> {out.relative_to(expand(opts.pin_baseline))}")
        return 0
    if not opts.no_refresh_telemetry and not opts.telemetry:
        refresh_telemetry(pol,
                          opts.telemetry_cwd or os.getcwd(),
                          pol["l2b"].get("refresh_timeout_ms", 2000))
    if opts.repo:
        missing = [str(r) for r in opts.repo if not expand(r).is_dir()]
        if missing:
            print(f"[L2B-ERROR] --repo is not a directory: {', '.join(missing)} - an audit "
                  "target that does not exist is not a clean scan", file=sys.stderr)
            return 2
        repos = [expand(r) for r in opts.repo]
    else:
        repos = [r for r in discover_repos(pol) if r.is_dir()]
    if not repos:
        print("[L2B-ERROR] no repo to audit", file=sys.stderr)
        return 2
    findings, notes, gaps = scan(repos, pol, opts)
    # The ledger is a property of the verdict, not of the renderer: --json and the interrupt view
    # must report the same wake and remember it the same way (the ledger is disk state, never
    # stdout). The scan log keeps the RAW findings, so an audit still sees the undeduped scan.
    tel_for_scope, _tel_note = resolve_telemetry(pol, repos, opts)
    scope = scan_scope(repos, tel_for_scope)
    raw, repeats = findings, 0
    if findings and not opts.no_ledger:
        findings, repeats = suppress_repeats(
            findings, read_ledger(pol), int(pol["looping"]["max_event_age_s"]), scope)
    emit(findings, notes, gaps, pol, opts, repeats)
    if repeats and not opts.quiet and not opts.json:
        print(f"[L2B: {repeats} repeat wake(s) for this scope already reported, not re-woken "
              f"(history: {ledger_path(pol).name}; --no-ledger re-wakes them)]")
    if raw:
        f = write_log(raw, pol, tag="-".join(sorted({x["class"][9:] for x in raw})))
        if findings and not opts.no_ledger:
            append_ledger(findings, pol, scope)
        if f and not opts.json and not opts.quiet:
            print(f"[L2B-LOG] {f}")
    return 1 if findings else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(2)
