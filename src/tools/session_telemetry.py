#!/usr/bin/env python3
"""L2B event writer - turn omp's own session transcript into surprise_check telemetry.

Why this file exists. The loop lane and the raw-XML lane are the two L2B detectors that read a
live signal, and both were permanently open gaps: nothing wrote `run/session-events.jsonl` and
nothing exported a turn slice, so the detectors calibrated on H-DEAD-25 (four relaunches of one
script) and H-DEAD-11/19 (raw XML re-emitted after a boundary) could only ever fire on synthetic
mutants. omp already persists every session as JSONL under `<agent>/sessions/<slug>/*.jsonl`;
this tool reads that transcript and writes the two artefacts L2B expects. Nothing is added to the
model loop: the writer is called at a boundary (`hooks/pre/pre-compact-sieve.js`) and before a
scan when the transcript has moved (`spine_check.py --surprise`).

What it writes.
1. `run/session-events.jsonl` - one event per line, the keys surprise_check reads (`t`, `kind`,
   `cmd`, `path`, `exit`). `kind` is command|patch|gate|read|other. Gate = a command naming a
   verification gate, because `d_looping` resets its counters on a gate PASS: classify ordinary
   work as a gate and legitimate re-gating reads as a loop, so the patterns come from
   `surprise_policy.json looping.gate_patterns` and a command with no match is never a gate: patterns are matched against the program and target a command RUNS (gate_identity), never against any word the line happens to contain, because `rm -rf /tmp/old_sieve.py` names a gate word while verifying nothing.
   `exit` comes from the paired tool result's error flag; when the transcript carries no result
   the value is null, which surprise_check treats as unverified rather than as a pass.
   The same slice is ALSO written per project to `run/telemetry/<session-slug>.jsonl`, and
   `run/telemetry/projects.json` records each stream's exact cwd. One box-wide file is not a
   project signal: it is overwritten by whichever session refreshed last, so a scan of repo X
   prices X's loop on another project's commands and blames X for them (H-DEAD-49).
2. `run/session-turn.txt` - the raw-XML lane's slice: assistant TEXT blocks only, fenced and
   inline code elided. The lane's rule is a substring search, and the live transcript contains
   every marker string in prose, in thinking, in tool arguments and in tool results (70 hits in
   the session that wrote this file, of which exactly one is assistant prose and that one is
   backticked documentation about the seam law). Scanning the whole transcript would fire a
   CRITICAL wake at every boundary, which is how a governance tool gets switched off. The slice
   header states what was elided so the narrowing is never hidden.
3. `run/session-telemetry.meta.json` - provenance: which transcript, how many entries, how many
   events, how many marker occurrences were elided. A claim about coverage binds to this file.

Fail-open contract: an unreadable transcript, an unwritable run dir or an unparsable line leaves
the previous artefacts in place and exits 0 with one line on stderr. L2B's gap rule then reports
"no telemetry, this lane did not run" instead of pretending the lane ran.

Run: python3 tools/session_telemetry.py [--cwd DIR] [--agent DIR] [--out DIR] [--quiet] [--json]
     python3 tools/session_telemetry.py --self-test
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import sys
import tempfile
from datetime import datetime, timezone

AGENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
POLICY = os.path.join(AGENT_DIR, "surprise_policy.json")
DEFAULT_GATE_PATTERNS = [
    r"harness/\S*harness\.py",
    r"_harness\.py",
    r"spine_check\.py",
    r"plan_check\.py",
    r"checkpoint\.sh",
    r"surprise_check\.py",
    r"gate_freeze\.py",
    r"--self-test",
    r"pytest",
]
PATCH_TOOLS = {"write", "edit", "multiedit", "multi_edit", "multi-edit", "apply_patch",
               "apply-patch", "patch", "edit_file", "notebookedit", "notebook_edit"}
READ_TOOLS = {"read", "grep", "glob", "ls", "list", "search", "websearch", "webfetch", "fetch"}
CALL_KEYS = ("toolCall", "toolUse", "tool_use", "tooluse")
PATH_KEYS = ("path", "file_path", "filePath", "notebook_path", "target", "filename")

# The raw-XML markers, composed so this source never contains the literal the lane searches for:
# a writer that embeds the marker is a writer that fires the detector wherever it is read.
OPEN = chr(60) + "tool_call" + chr(62)
FN = chr(60) + "function="
FNC = chr(60) + "/function" + chr(62)
TC = chr(60) + "tool_call/" if False else chr(60) + "/tool_call" + chr(62)
RAW_MARKERS = ("<|", "|>")
RAW_XML = (OPEN, FN, FNC, TC, chr(60) + "function_calls" + chr(62))
RAW_XML = RAW_XML + tuple(f"{a}{b}" for a in RAW_MARKERS for b in RAW_MARKERS)

FENCE = re.compile(r"^\s*(```|~~~)")
INLINE_CODE = re.compile(r"`[^`\n]*`")


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


ISO_FMT = "%Y-%m-%dT%H:%M:%SZ"


def _iso(val):
    """Event stamp, from either shape a transcript actually uses: epoch milliseconds or
    ISO-8601 text. An unparsable stamp used to fall back to `now`, which made every event
    in a dead session look seconds old, so the loop lane's staleness window could never
    expire it - the receipt lied about its own age. Unparsable now means empty, and the
    caller decides (per-file mtime), never the clock reading it."""
    if isinstance(val, (int, float)) and not isinstance(val, bool):
        try:
            return datetime.fromtimestamp(int(val) / 1000.0, timezone.utc).strftime(ISO_FMT)
        except (ValueError, OSError, OverflowError):
            return ""
    text = str(val if val is not None else "").strip()
    if not text:
        return ""
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        stamp = datetime.fromisoformat(text)
    except ValueError:
        return ""
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(timezone.utc).strftime(ISO_FMT)


def _text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(b.get("text", "") for b in content
                       if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _payload(entry):
    """A transcript entry as {role, content}: omp nests the message, older writers do not."""
    if not isinstance(entry, dict):
        return {}
    msg = entry.get("message")
    return msg if isinstance(msg, dict) else entry


def _blocks(payload):
    content = payload.get("content")
    return content if isinstance(content, list) else []


def slug_candidates(cwd):
    """The directory names omp might have filed this cwd under, best guess first.

    Measured on this box: `$HOME/tmp` -> `-tmp`, `$HOME/.omp/agent` ->
    `-.omp-agent`, `/mnt/data/strata/workspace/Strata` -> `--mnt-data-...-Strata--`. So the
    name is HOME-relative inside $HOME and absolute outside it, and an absolute path loses
    its trailing separator. One rule cannot be read off two samples, so every plausible
    variant is tried in order before the prefix fallback: guessing wrong is not harmless,
    it silently reads some other project's transcript and calls it this session's telemetry.
    """
    out = []

    def add(s):
        # a candidate of nothing but separators is a prefix of every session dir, so cwd=$HOME
        # would resolve to an unrelated project; a slug has to name something.
        if s and s.strip("-") and s not in out:
            out.append(s)

    home = os.path.expanduser("~")
    if cwd:
        if home and (cwd == home or cwd.startswith(home + os.sep)):
            add((cwd[len(home):] or "/").replace("/", "-"))
        add("-" + cwd.replace("/", "-"))
        add(cwd.replace("/", "-"))
        add(("-" + cwd.replace("/", "-") + "-").rstrip("-"))
        add(cwd.strip("/").replace("/", "-"))
    return out


def resolve_transcript(agent_dir, cwd, explicit=None):
    """Newest JSONL for this cwd's session slug; fall back to the newest anywhere, named."""
    if explicit:
        return (explicit, "explicit") if os.path.isfile(explicit) else (None, "explicit-missing")
    root = os.path.join(agent_dir, "sessions")
    def newest(dirpath):
        try:
            files = [os.path.join(dirpath, n) for n in os.listdir(dirpath) if n.endswith(".jsonl")]
        except OSError:
            return None
        files = [f for f in files if os.path.isfile(f)]
        return max(files, key=lambda f: os.path.getmtime(f)) if files else None
    for slug in slug_candidates(cwd):
        hit = newest(os.path.join(root, slug))
        if hit:
            return hit, "slug"
    if not os.path.isdir(root):
        return None, "absent"
    names = sorted(os.listdir(root))
    if cwd:
        for slug in slug_candidates(cwd):
            for name in names:  # a slug that is a prefix of a dir name still names this cwd
                if name.startswith(slug) and newest(os.path.join(root, name)):
                    return newest(os.path.join(root, name)), "slug-prefix"
    best = None
    for name in names:
        cand = newest(os.path.join(root, name))
        if cand and (best is None or os.path.getmtime(cand) > os.path.getmtime(best)):
            best = cand
    return (best, "newest-any") if best else (None, "absent")


def resolve_history(agent_dir, cwd, explicit=None, max_files=4, max_bytes=48_000_000):
    """The cwd's recent transcripts, oldest first, newest always included.

    omp opens a new JSONL at every compaction boundary, so one file covers only the calls since
    that boundary. The loop lane needs the tail of the session, not the tail of the newest file:
    the boundary is exactly when a loop's evidence would otherwise disappear.
    """
    if explicit:
        ok = os.path.isfile(explicit)
        return ([explicit] if ok else []), ("explicit" if ok else "explicit-missing")
    newest, how = resolve_transcript(agent_dir, cwd)
    if not newest:
        return [], how
    root = os.path.dirname(newest)
    try:
        files = [os.path.join(root, n) for n in os.listdir(root) if n.endswith(".jsonl")]
    except OSError:
        return [newest], how
    files = sorted((f for f in files if os.path.isfile(f)), key=os.path.getmtime)
    picked, total = [], 0
    for f in reversed(files):
        size = os.path.getsize(f)
        if picked and total + size > max_bytes:
            break
        picked.append(f)
        total += size
        if len(picked) >= max(1, max_files):
            break
    return list(reversed(picked)), how


def load_gate_patterns(policy_path):
    try:
        with open(policy_path, encoding="utf-8") as h:
            pol = json.load(h)
        pats = ((pol.get("looping") or {}).get("gate_patterns")) or []
        pats = [p for p in pats if isinstance(p, str) and p]
    except Exception:
        pats = []
    if not pats:
        pats = list(DEFAULT_GATE_PATTERNS)
    out = []
    for p in pats:
        try:
            out.append(re.compile(p))
        except re.error:
            continue
    return out or [re.compile(p) for p in DEFAULT_GATE_PATTERNS]


DEFAULT_VALUE_FLAGS = frozenset({"-W", "-X", "-c", "-m", "-e", "--rootdir", "-k", "-n", "-p",
                                 "--timeout", "-C", "-o"})


def load_value_flags(policy_path):
    """Flags that eat the token after them (`-W ignore`, `-c PAYLOAD`), from the same policy table
    `surprise_check` fingerprints with. Reused rather than restated: two tables naming the noise
    would drift, and the identity of a command has to mean the same thing in both lanes."""
    try:
        with open(policy_path, encoding="utf-8") as h:
            pol = json.load(h)
        vals = (((pol.get("looping") or {}).get("interpreter_noise") or {}).get("value")) or []
        return frozenset(v for v in vals if isinstance(v, str) and v) or DEFAULT_VALUE_FLAGS
    except Exception:
        return DEFAULT_VALUE_FLAGS


def _args_of(block):
    a = block.get("arguments")
    if a is None:
        a = block.get("input") or block.get("args")
    if isinstance(a, str):
        try:
            a = json.loads(a)
        except Exception:
            return {}
    return a if isinstance(a, dict) else {}


_SEG = re.compile(r"&&|;|\||\n")
_ASSIGN_R = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_REDIR_R = re.compile(r"^\d*[<>]{1,2}")
# Programs that run something else, so the thing they run is the identity of the command.
_RUNNERS = {"python", "python2", "python3", "pypy", "bash", "sh", "dash", "zsh", "node", "deno",
            "bun", "npm", "npx", "pnpm", "yarn", "cargo", "make", "perl", "ruby", "uv", "uvx",
            "poetry", "pytest", "py.test", "tox", "go", "dotnet", "timeout", "env", "time",
            "stdbuf", "nice", "nohup", "parallel"}
# Shell verbs that never verify anything, whatever they are pointed at.
_NOTHING = {"cd", "echo", "printf", "export", "set", "unset", "source", "true", "false", "test",
            "[", "rm", "mv", "cp", "mkdir", "rmdir", "touch", "chmod", "chown", "ln", "ls", "cat",
            "head", "tail", "wc", "grep", "rg", "sed", "awk", "jq", "sort", "uniq", "cut", "tr",
            "diff", "find", "du", "df", "stat", "pwd", "which", "sleep", "kill", "ps", "git"}


def gate_identity(cmd, value_flags=frozenset()):
    """What a command actually runs: the program and its target, per pipeline segment.

    Gate patterns are matched against this and nothing else. A whole-line match reads
    `rm -rf /tmp/old_sieve.py` and `jq . run/gate.log` as verification, because the words
    `sieve` and `gate` appear in them - and a command wrongly called a gate RESETS the loop
    counters in `d_looping`, so the false gate hides the loop the detector exists to find.
    A false loop spends one interrupt; a false gate spends the detection.
    """
    out = []
    for seg in _SEG.split(str(cmd or "")):
        try:
            toks = shlex.split(seg.strip())
        except ValueError:
            toks = seg.split()
        clean, i = [], 0
        while i < len(toks):
            t = toks[i]
            if _ASSIGN_R.match(t) or _REDIR_R.match(t):
                i += 2 if (i + 1 < len(toks) and not _REDIR_R.match(toks[i + 1])
                           and not toks[i + 1].startswith("-")) else 1
                continue
            clean.append(t)
            i += 1
        if not clean:
            continue
        head = clean[0].lower().rsplit("/", 1)[-1]
        if head in _NOTHING:
            continue
        out.append(head)
        if head in _RUNNERS or "/" in clean[0]:
            skip_val = False
            for t in clean[1:]:
                if t.startswith("-"):
                    # a flag that carries a value owns the token after it: `-W ignore` names no
                    # target, and treating `ignore` as the operand hides the real one behind it
                    skip_val = t in value_flags
                    continue
                if skip_val:
                    skip_val = False
                    continue
                low = t.lower()
                out += [low, low.rsplit("/", 1)[-1], f"{head} {low}"]
                break
    return out


def _path_of(args):
    for k in PATH_KEYS:
        v = args.get(k)
        if isinstance(v, str) and v:
            return v
    return ""


def extract(path, gate_res, value_flags=frozenset()):
    """Transcript(s) -> (events, assistant_text_blocks, counts). Chronological order kept.

    One file is not enough: a compaction boundary rolls the JSONL, so the post-boundary file
    holds only the calls made since the boundary and a launcher that looped before it vanishes
    from the loop lane at the exact moment the boundary made the model forget why it was
    looping. Passing a slug's recent files oldest-first keeps that history. Call ids are
    deduped, because a context block replayed into a newer file would count the same relaunch
    twice and manufacture a loop that never happened.
    """
    paths = [path] if isinstance(path, (str, bytes, os.PathLike)) else list(path)
    events, texts = [], []
    pending = {}
    seen = set()
    counts = {"entries": 0, "bad_lines": 0, "calls": 0, "echoes": 0, "results": 0, "gates": 0, "patches": 0,
              "transcripts": 0, "replays": 0}
    for one in paths:
        handle = open(one, encoding="utf-8", errors="replace")
        counts["transcripts"] += 1
        # an entry with no usable stamp is as old as the file that holds it, not as old as
        # this scan; mtime is the honest upper bound
        file_stamp = _iso(int(os.path.getmtime(one) * 1000)) or _now_iso()
        with handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                counts["entries"] += 1
                try:
                    entry = json.loads(line)
                except Exception:
                    counts["bad_lines"] += 1
                    continue
                if not isinstance(entry, dict):  # a JSON scalar line parses but carries no message
                    counts["bad_lines"] += 1
                    continue
                payload = _payload(entry)
                role = str(payload.get("role") or payload.get("type") or entry.get("type") or "")
                stamp = _iso(entry.get("timestamp") or payload.get("timestamp")) or file_stamp
                if role == "toolResult" or entry.get("toolCallId"):
                    call_id = entry.get("toolCallId") or payload.get("toolCallId") or ""
                    counts["results"] += 1
                    ev = pending.pop(call_id, None)
                    if ev is not None:
                        ev["exit"] = 1 if (entry.get("isError") or payload.get("isError")) else 0
                    continue
                if role != "assistant":
                    continue
                blocks = [b for b in _blocks(payload) if isinstance(b, dict)]
                # A parsed call and the adapter's text echo of that same call arrive in one
                # message. The echo is what the recorder would otherwise copy into the raw-XML
                # lane's scan surface, where it reads as a leaked call the session cannot act on.
                parsed_call = any(str(b.get("type") or "") in CALL_KEYS for b in blocks)
                for block in blocks:
                    btype = str(block.get("type") or "")
                    if btype in CALL_KEYS:
                        name = str(block.get("name") or "").lower().replace(" ", "_")
                        args = _args_of(block)
                        cmd = ""
                        for key in ("command", "cmd", "script"):
                            if isinstance(args.get(key), str):
                                cmd = args[key]
                                break
                        cid = str(block.get("id") or block.get("callId") or "")
                        if cid and (cid, name) in seen:  # a replayed context block is not a new call
                            counts["replays"] += 1
                            continue
                        seen.add((cid, name))
                        counts["calls"] += 1
                        kind, exit_val = "other", None
                        if name in PATCH_TOOLS:
                            kind = "patch"
                            counts["patches"] += 1
                        elif name in READ_TOOLS:
                            kind = "read"
                        elif cmd:
                            kind = "command"
                            ident = gate_identity(cmd, value_flags)
                            if any(rx.search(c) for c in ident for rx in gate_res):
                                kind = "gate"
                                counts["gates"] += 1
                        ev = {"t": stamp, "kind": kind, "cmd": cmd, "path": _path_of(args),
                              "exit": exit_val, "tool": name}
                        events.append(ev)
                        cid = block.get("id") or block.get("toolCallId")
                        if cid:
                            pending[cid] = ev
                    elif btype == "text":
                        body = block.get("text")
                        if isinstance(body, str) and body.strip():
                            if parsed_call and serialization_only(body):
                                counts["echoes"] += 1  # counted, never silently dropped
                                continue
                            texts.append((stamp, body))
    return events, texts, counts


# The adapter echoes a parsed call back as assistant text beside the toolCall block it was
# parsed from, and its parser eats the closing ">" of the parameter tag. That echo is the
# recorder quoting a call, not the model leaking one, so it must not reach the lane's scan
# surface. Only the echo is dropped: a text block with prose around the call, or a call with no
# parsed twin in the same message, is a real leak and stays in the slice.
SERIAL_TAG_CLOSED = re.compile(
    r"^\s*</?(?:tool_call|invoke\b|function\b|parameter\b|arg\b|result\b)[^\n]*>\s*$")
SERIAL_TAG_UNCLOSED = re.compile(
    r"^\s*</?(?:tool_call|invoke\b|function\b|parameter\b|arg\b|result\b)[^>\n]*$")
SERIAL_CLOSE = ("</tool_call>", "</invoke>")


def serialization_only(text):
    """True when a text block is nothing but one closed tool-call envelope of tag-only lines."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines or not any(ln in SERIAL_CLOSE for ln in lines):
        return False
    return all(SERIAL_TAG_CLOSED.match(ln) or SERIAL_TAG_UNCLOSED.match(ln) for ln in lines)


def turn_slice(texts, limit=24000):
    """Assistant prose only, fenced and inline code elided; the raw-XML lane's scan surface."""
    kept, elided = [], 0
    for stamp, body in texts:
        out, in_fence = [], False
        for ln in body.split("\n"):
            if FENCE.match(ln):
                in_fence = not in_fence
                continue
            if in_fence:
                elided += 1
                continue
            stripped, n = INLINE_CODE.subn("", ln)
            elided += n
            out.append(stripped)
        body = "\n".join(out)
        if body.strip():
            kept.append(f"# turn {stamp}\n{body}")
    head = ("# L2B raw-XML scan slice - assistant text blocks only, generated by "
            "tools/session_telemetry.py\n"
            "# Elided before writing: fenced code blocks, inline code spans, thinking blocks, "
            "tool arguments, tool results and user turns.\n"
            "# A marker below is therefore model prose, not a quoted example or a tool echo.\n")
    body = "\n".join(kept)
    if len(body) > limit:
        body = body[-limit:]
    return head + body, elided


def scan_markers(body):
    hits = {}
    for label, pat in (("xml", OPEN), ("fn", FN), ("fnClose", FNC), ("tcClose", TC)):
        if pat in body:
            hits[label] = body.count(pat)
    for pat in RAW_XML:
        if pat not in (OPEN, FN, FNC, TC) and pat in body:
            hits["amp"] = hits.get("amp", 0) + body.count(pat)
    return hits


def _epoch(v):
    """An event stamp as seconds, whatever form the transcript carried; None when unparseable.

    A transcript stamps some calls with epoch numbers and some with ISO strings, and one carries
    prose where a time should be. A freshness comparison has to put all three on one scale or it
    will silently compare nothing.
    """
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        pass
    text = str(v).strip().replace("Z", "+0000")
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
    return None


def _iso_utc(sec):
    """Seconds since the epoch as the ISO form the lane carries (the module's _iso takes ms)."""
    return datetime.fromtimestamp(float(sec), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _lane_newest(path):
    """The newest stamp already on the lane, or None when there is no lane to compare against.

    Only the tail is read: the lane is append-only in practice and the newest event is what the
    comparison needs. Lanes whose events carry no stamp yield None, so a fixture can never block
    a real refresh.
    """
    try:
        with open(path, "rb") as h:
            h.seek(0, os.SEEK_END)
            h.seek(max(0, h.tell() - 4_000_000))
            tail = h.read().decode("utf-8", "replace")
    except OSError:
        return None
    best = None
    for raw in tail.splitlines():
        try:
            obj = json.loads(raw)
        except ValueError:
            continue
        if isinstance(obj, dict):
            sec = _epoch(obj.get("t"))
            if sec is not None and (best is None or sec > best):
                best = sec
    return best


def _write_if_changed(path, data):
    """Stable bytes keep the artefact mtime stable; surprise_check keys staleness off it."""
    try:
        if os.path.isfile(path):
            with open(path, "rb") as h:
                if h.read() == data:
                    return False
    except OSError:
        pass
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", prefix=".session-telemetry.")
    try:
        with os.fdopen(fd, "wb") as h:
            h.write(data)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return True


def session_cwd(out_dir, session_path, requester, how):
    """The directory a session's events actually came from, or "" when that is not knowable.

    The requester's cwd is authoritative only when the transcript was found BY that cwd; a
    newest-any refresh pulls in whatever session is running, and stamping the requester's directory
    onto it would forge the provenance the loop lane relies on to tell its own launches from a
    neighbour's (H-DEAD-49). Otherwise the answer is what projects.json already recorded for this
    session slug. Unknown stays unknown: an unstamped event is merely unplaceable, a wrongly stamped
    one is a lie that silences or wakes the wrong repo.
    """
    if requester and how != "newest-any":
        return os.path.abspath(os.path.expanduser(requester))
    slug = os.path.basename(os.path.dirname(os.path.abspath(session_path or "")))
    try:
        with open(os.path.join(out_dir, "telemetry", "projects.json"), encoding="utf-8") as h:
            return str((json.load(h).get(slug) or {}).get("cwd") or "")
    except (OSError, ValueError):
        return ""


def write_project_streams(out_dir, session_path, cwd, payload, slice_newest):
    """Write this session's slice as its own project stream, and index it by the cwd it came from.

    The slug is the session directory's own name, never a reconstruction from cwd: omp writes
    `--mnt-...-Strata--` for a cwd with a trailing slash, and a rebuilt slug would split one
    project across two streams. projects.json keeps the exact cwd so a reader can match a repo to a
    stream without guessing at slug encoding. Fail-open: an unwritable lane leaves the global
    artefacts (and the scan) intact.

    Returns (stream_path, wrote). A lane only ever moves forward, like the global one: an older
    slice of the same session must not rewind a window a newer slice already filled.
    """
    slug = os.path.basename(os.path.dirname(os.path.abspath(session_path or "")))
    if not slug or slug in (".", "/"):
        return None, False
    stream = os.path.join(out_dir, "telemetry", slug + ".jsonl")
    try:
        own = _lane_newest(stream)
        if own is not None and slice_newest is not None and own > slice_newest:
            return stream, False
        wrote = _write_if_changed(stream, payload)
        index = os.path.join(out_dir, "telemetry", "projects.json")
        rows = {}
        try:
            with open(index, encoding="utf-8") as h:
                loaded = json.load(h)
            if isinstance(loaded, dict):
                rows = loaded
        except (OSError, ValueError):
            pass
        rows[slug] = {"cwd": os.path.abspath(os.path.expanduser(cwd or "")),
                      "stream": stream, "updated": _now_iso()}
        os.makedirs(os.path.dirname(index), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(index), prefix=".projects.")
        with os.fdopen(fd, "w", encoding="utf-8") as h:
            json.dump(rows, h, indent=1, sort_keys=True)
            h.write("\n")
        os.replace(tmp, index)
    except OSError:
        return stream, False
    return stream, wrote


def run(agent_dir=AGENT_DIR, cwd=None, out_dir=None, session=None, limit=24000, policy=POLICY,
        max_files=4, max_bytes=48_000_000):
    agent_dir = os.path.abspath(os.path.expanduser(agent_dir))
    out_dir = out_dir or os.path.join(agent_dir, "run")
    cwd = cwd or os.getcwd()
    path, how = resolve_transcript(agent_dir, cwd, session)
    history, _ = resolve_history(agent_dir, cwd, session, max_files=max_files,
                                 max_bytes=max_bytes)
    meta = {"written_at": _now_iso(), "cwd": cwd, "session": path, "resolution": how,
            "history_files": len(history),
            "history_bytes": sum(os.path.getsize(f) for f in history),
            "events": os.path.join(out_dir, "session-events.jsonl"),
            "turn": os.path.join(out_dir, "session-turn.txt")}
    if not path:
        meta["status"] = "no-transcript"
        return meta
    pol_path = policy if os.path.isfile(policy) else ""
    gate_res = load_gate_patterns(pol_path)
    events, texts, counts = extract(history or [path], gate_res, load_value_flags(pol_path))
    body, elided = turn_slice(texts, limit)
    # Every event names the working directory of the session that produced it: a box-wide lane is
    # one file for the whole box, and a reader cannot drop an event it cannot place (H-DEAD-49).
    # The stamp is the transcript's own cwd, never the requester's: a newest-any fallback pulled in
    # a neighbour's session, and stamping the caller's directory onto it would forge provenance for
    # events that came from elsewhere - worse than the unknown it replaces.
    where = session_cwd(out_dir, path, cwd, how)
    payload = "".join(json.dumps(dict(ev, cwd=where) if where and not ev.get("cwd") else ev,
                                 sort_keys=True) + "\n" for ev in events).encode("utf-8")
    meta.update(counts)
    meta["event_cwd"] = where or "unknown"
    meta["marker_hits_in_slice"] = scan_markers(body)
    meta["elided_spans"] = elided
    meta["slice_chars"] = len(body)
    meta["sha256"] = hashlib.sha256(payload).hexdigest()[:16]
    # A refresh resolves the session by the CWD it was launched from. Launched from the tool home
    # that is this box's own dead session, and the write would replace a live lane with days-old
    # events: the loop lane then reports "no live session" while a session is running, which is a
    # blindfold that reads as a clean spine. The lane only ever moves forward in time.
    stamps = [x for x in (_epoch(ev.get("t")) for ev in events) if x is not None]
    slice_newest = max(stamps) if stamps else None
    lane_newest = _lane_newest(meta["events"])
    if lane_newest is not None and slice_newest is not None and lane_newest > slice_newest:
        meta["status"] = "kept-stale-source"
        meta["lane_newest"] = _iso_utc(lane_newest)
        meta["slice_newest"] = _iso_utc(slice_newest)
        meta["stale_source_session"] = path
        with open(os.path.join(out_dir, "session-telemetry.meta.json"), "w",
                  encoding="utf-8") as h:
            json.dump(meta, h, indent=2, sort_keys=True)
            h.write("\n")
        return meta
    wrote_events = _write_if_changed(meta["events"], payload)
    wrote_turn = _write_if_changed(meta["turn"], (body + "\n").encode("utf-8"))
    project_stream, wrote_project = write_project_streams(out_dir, path, cwd, payload,
                                                         slice_newest)
    meta["project_stream"] = project_stream
    meta["status"] = "written" if (wrote_events or wrote_turn or wrote_project) else "unchanged"
    with open(os.path.join(out_dir, "session-telemetry.meta.json"), "w", encoding="utf-8") as h:
        json.dump(meta, h, indent=2, sort_keys=True)
        h.write("\n")
    return meta


# ---------------------------------------------------------------- self-test ----
def _fixture(path, entries):
    with open(path, "w", encoding="utf-8") as h:
        for e in entries:
            h.write(json.dumps(e) + "\n")


def _call(cid, name, args, ts=1):
    return {"type": "message", "timestamp": ts,
            "message": {"role": "assistant", "content": [
                {"type": "toolCall", "id": cid, "name": name, "arguments": args}]}}


def _result(cid, err=False, ts=2):
    return {"type": "message", "timestamp": ts, "role": "toolResult", "toolCallId": cid,
            "toolName": "bash", "isError": err, "content": [{"type": "text", "text": "out"}]}


def _assistant(text, ts=3):
    return {"type": "message", "timestamp": ts,
            "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


def self_test():
    tmp = tempfile.mkdtemp(prefix="session-telemetry-st.")
    agent = os.path.join(tmp, "agent")
    cwd = os.path.join(tmp, "proj")
    slug = cwd.replace("/", "-")
    sdir = os.path.join(agent, "sessions", slug)
    os.makedirs(sdir)
    os.makedirs(cwd, exist_ok=True)
    out = os.path.join(agent, "run")
    tx = os.path.join(sdir, "s1.jsonl")
    _fixture(tx, [
        _call("c1", "bash", {"command": "python3 harness/x_harness.py"}, ts=1000),
        _result("c1", err=True, ts=1001),
        _call("c2", "bash", {"command": "ls -la"}, ts=1002),
        _result("c2", err=False, ts=1003),
        _call("c3", "write", {"path": "/tmp/v2.py", "content": "x"}, ts=1004),
        _result("c3", err=False, ts=1005),
        _call("c4", "read", {"path": "/tmp/v2.py"}, ts=1006),
        _result("c4", err=False, ts=1007),
        _call("c5", "bash", {"command": "rm -rf /tmp/x_harness.py"}, ts=1008),
        _result("c5", err=False, ts=1009),
        _call("c6", "bash", {"command": "python3 -W ignore tools/y_harness.py --self-test"},
              ts="2026-01-02T03:04:05.678Z"),
        _result("c6", err=False, ts=1011),
        _call("c7", "bash", {"command": "bash -c 'grep self-test notes.md'"}, ts="not-a-time"),
        _assistant("I will re-run it.\n\n```\n" + FN + "edit\n```\n"
                   + "the backticked `" + FN + "edit>` form is documentation\n"
                   + "and a stray unclosed " + FN + "edit in prose is a real event\n", ts=1007),
        {"type": "compaction", "timestamp": 1008, "method": "manual", "firstKeptEntryId": "x"},
        "not json at all",
    ])
    checks, fails = 0, []
    def ck(name, cond, detail=""):
        nonlocal checks
        checks += 1
        print(("  PASS " if cond else "  FAIL ") + name + ("" if cond or not detail else ": " + detail))
        if not cond:
            fails.append(name)
    # pin the transcript's own mtime so a fallback stamp is a fixed date, not "whenever the
    # test happened to run" - otherwise the fallback and the scanning clock are the same value
    # and the case cannot tell them apart
    os.utime(tx, (1577836800, 1577836800))  # 2020-01-01T00:00:00Z
    meta = run(agent_dir=agent, cwd=cwd, out_dir=out, policy=os.path.join(tmp, "absent.json"))
    ev = [json.loads(l) for l in open(meta["events"], encoding="utf-8") if l.strip()]
    kinds = [e["kind"] for e in ev]
    ck("T01 the session for this cwd's slug is the one read",
       meta["status"] == "written" and meta["resolution"] == "slug"
       and meta["session"] == tx, json.dumps(meta)[:200])
    ck("T02 a gate command is classified as a gate and carries its non-zero exit",
       kinds[0] == "gate" and ev[0]["exit"] == 1, json.dumps(ev[:1]))
    ck("T03 an ordinary command is not a gate", "command" in kinds, json.dumps(kinds))
    ck("T04 a patch call becomes a patch event with its target path",
       any(e["kind"] == "patch" and e["path"] == "/tmp/v2.py" for e in ev), json.dumps(ev[2:4]))
    ck("T05 a read is recorded as a read, never as a patch",
       any(e["kind"] == "read" for e in ev), json.dumps(kinds))
    body = open(meta["turn"], encoding="utf-8").read()
    ck("T06 a fenced example and a closed inline span are elided, so documentation cannot wake L2B",
       body.count(FN) == 1 and "documentation" in body and meta["elided_spans"] >= 2,
       f"fn_hits={body.count(FN)} elided={meta['elided_spans']} tail={body[-160:]!r}")
    ck("T07 a marker in model prose survives the elision so the lane can still see it",
       FN + "edit in prose" in body and meta["marker_hits_in_slice"].get("fn") == 1,
       f"fn={meta['marker_hits_in_slice']}")
    ck("T08 the slice header states what was narrowed",
       body.startswith("# L2B raw-XML scan slice") and "Elided before writing" in body, body[:120])
    ck("T09 a second run with the same transcript rewrites nothing",
       run(agent_dir=agent, cwd=cwd, out_dir=out, policy="")["status"] == "unchanged", "")
    ck("T10 unparsable transcript lines are counted, not fatal",
       meta["bad_lines"] == 1 and meta["entries"] == 16,
       f"entries={meta['entries']} bad_lines={meta['bad_lines']} events={len(ev)}")
    cmd_by_path = {e["cmd"]: e["kind"] for e in ev}
    ck("T13 deleting a file whose name contains a gate word is not a gate - a false gate would "
       "reset the loop counters and hide the loop",
       cmd_by_path.get("rm -rf /tmp/x_harness.py") == "command", json.dumps(cmd_by_path)[:220])
    empty = os.path.join(tmp, "elsewhere")
    os.makedirs(empty, exist_ok=True)
    meta2 = run(agent_dir=agent, cwd=empty, out_dir=out, policy="")
    ck("T11 a cwd with no transcript of its own falls back to the newest session",
       meta2["resolution"] == "newest-any" and meta2["status"] == "unchanged",
       json.dumps(meta2)[:200])
    ck("T11b a newest-any refresh stamps the SESSION's directory, never the requester's: forging "
       "provenance from whoever ran the refresh would let a neighbour's loop wake this repo",
       meta2["resolution"] == "newest-any"
       and meta2["event_cwd"] == os.path.abspath(cwd)
       and all(json.loads(l).get("cwd") == os.path.abspath(cwd)
               for l in open(os.path.join(out, "session-events.jsonl"),
                             encoding="utf-8").read().splitlines()),
       json.dumps(meta2)[:200])
    ck("T11c a session the index has never seen stays unplaceable rather than being stamped to the "
       "refreshing directory",
       session_cwd(os.path.join(tmp, "no-index"), os.path.join(agent, "sessions", "zz", "s.jsonl"),
                   empty, "newest-any") == "",
       session_cwd(os.path.join(tmp, "no-index"), "x", empty, "newest-any"))
    ck("T12 an agent dir with no sessions at all reports the gap instead of inventing events",
       run(agent_dir=os.path.join(tmp, "empty-agent"), cwd=cwd, out_dir=out,
           policy="")["status"] == "no-transcript", "")
    # the gate vocabulary and the flag-noise table both come from the policy, and a flag that
    # carries a value must not be read as the target: `-W ignore tools/y_harness.py` runs a gate
    pol = os.path.join(tmp, "policy.json")
    with open(pol, "w", encoding="utf-8") as h:
        json.dump({"looping": {"gate_patterns": ["_harness", "self-test"],
                               "interpreter_noise": {"value": ["-W", "-c", "-m"]}}}, h)
    meta3 = run(agent_dir=agent, cwd=cwd, out_dir=os.path.join(tmp, "run2"), policy=pol)
    ev3 = [json.loads(l) for l in open(meta3["events"], encoding="utf-8") if l.strip()]
    k3 = {e["cmd"]: e["kind"] for e in ev3}
    ck("T15 the policy's gate vocabulary applies, and a value-taking flag does not mask the target",
       k3.get("python3 -W ignore tools/y_harness.py --self-test") == "gate"
       and k3.get("rm -rf /tmp/x_harness.py") == "command", json.dumps(k3)[:260])
    ck("T14 a gate word carried inside a shell -c payload is not a gate",
       k3.get("bash -c 'grep self-test notes.md'") == "command", json.dumps(k3)[:260])
    ev6 = [e for e in ev if e.get("cmd", "").startswith("python3 -W ignore")]
    ck("T17 an ISO transcript stamp becomes the event stamp, so a dead session's events age out "
       "of the loop window instead of looking seconds old forever",
       len(ev6) == 1 and ev6[0]["t"] == "2026-01-02T03:04:05Z", json.dumps(ev6[:1])[:160])
    ev7 = [e for e in ev if e.get("cmd", "").startswith("bash -c 'grep")]
    ck("T17b an unparsable stamp falls back to the transcript's own mtime, never the scanning clock",
       len(ev7) == 1 and ev7[0]["t"] == "2020-01-01T00:00:00Z", json.dumps(ev7[:1])[:160])
    ck("T16 loading the policy yields its value flags", sorted(load_value_flags(pol)) == ["-W", "-c", "-m"],
       repr(sorted(load_value_flags(pol))))
    lane_path = meta["events"]
    fresh = json.dumps({"kind": "command", "cmd": "ls", "t": "2027-01-01T00:00:00Z"}) + "\n"
    with open(lane_path, "w", encoding="utf-8") as h:
        h.write(fresh)
    turn_before = open(meta["turn"], "rb").read()
    m_keep = run(agent_dir=agent, cwd=cwd, out_dir=out, policy="")
    ck("T26 a refresh resolving an older session keeps the fresher lane and says so, instead of "
       "erasing live events and leaving the loop lane blind",
       m_keep["status"] == "kept-stale-source" and m_keep["lane_newest"] == "2027-01-01T00:00:00Z"
       and open(lane_path, encoding="utf-8").read() == fresh
       and open(meta["turn"], "rb").read() == turn_before, json.dumps(m_keep)[:220])
    with open(lane_path, "w", encoding="utf-8") as h:
        h.write(json.dumps({"kind": "command", "cmd": "ls", "t": "2020-06-01T00:00:00Z"}) + "\n")
    m_new = run(agent_dir=agent, cwd=cwd, out_dir=out, policy="")
    ck("T27 a refresh carrying newer events still overwrites the lane",
       m_new["status"] == "written"
       and "2026-01-02T03:04:05Z" in open(lane_path, encoding="utf-8").read(),
       json.dumps(m_new)[:200])
    # H-DEAD-49: the box-wide lane is ONE file for the whole box, so a scan of another repo reads
    # these very events as its own loop. Every slice must also land in its project's own stream.
    proj = meta["project_stream"]
    idx_path = os.path.join(out, "telemetry", "projects.json")
    idx = json.load(open(idx_path, encoding="utf-8")) if os.path.isfile(idx_path) else {}
    ck("T28 the slice is ALSO written to this session's project stream, indexed by the cwd it came "
       "from, so a scan of another repo cannot read these events as its own loop",
       proj == os.path.join(out, "telemetry", slug + ".jsonl")
       and open(proj, encoding="utf-8").read() == open(meta["events"], encoding="utf-8").read()
       and idx.get(slug, {}).get("cwd") == os.path.abspath(cwd)
       and all(json.loads(l).get("cwd") == os.path.abspath(cwd)
               for l in open(proj, encoding="utf-8").read().splitlines()),
       json.dumps({"p": proj, "i": idx}, sort_keys=True)[:220])
    cwd2 = os.path.join(tmp, "other-proj")
    slug2 = cwd2.replace("/", "-")
    os.makedirs(os.path.join(agent, "sessions", slug2))
    os.makedirs(cwd2, exist_ok=True)
    tx2 = os.path.join(agent, "sessions", slug2, "s2.jsonl")
    # ISO stamps, not the fixture's epoch-1000 defaults: a slice stamped 1970 is older than the
    # live lane and would take the kept-stale-source exit, testing the wrong branch.
    _fixture(tx2, [_call("c1", "bash", {"command": "python3 harness/zz_neighbour.py"},
                         ts="2027-01-02T03:04:05Z"),
                   _result("c1", ts="2027-01-02T03:04:06Z")])
    m2 = run(agent_dir=agent, cwd=cwd2, out_dir=out, policy="")
    idx2 = json.load(open(idx_path, encoding="utf-8"))
    ck("T29 a second project's refresh leaves the first project's stream intact, even though the "
       "box-wide lane it also wrote IS last-writer-wins - that is why per-project streams exist",
       m2["project_stream"] == os.path.join(out, "telemetry", slug2 + ".jsonl")
       and "zz_neighbour" in open(m2["project_stream"], encoding="utf-8").read()
       and "zz_neighbour" not in open(proj, encoding="utf-8").read()
       and "zz_neighbour" in open(lane_path, encoding="utf-8").read()
       and set(idx2) >= {slug, slug2} and idx2[slug]["cwd"] == os.path.abspath(cwd)
       and idx2[slug2]["cwd"] == os.path.abspath(cwd2),
       json.dumps({"b": m2.get("project_stream"), "own": proj,
                   "neighbour_in_own": "zz_neighbour" in open(proj, encoding="utf-8").read(),
                   "i": sorted(idx2)})[:260])

    print(f"\n{checks - len(fails)}/{checks} " + ("PASS" if not fails else "FAIL: " + ", ".join(fails)))
    return 0 if not fails else 1


def main():
    ap = argparse.ArgumentParser(prog="session_telemetry.py", description=__doc__.splitlines()[0])
    ap.add_argument("--cwd", help="session slug to read (default: the current directory)")
    ap.add_argument("--agent", default=AGENT_DIR, help="agent dir holding sessions/ (default: this one)")
    ap.add_argument("--out", help="where to write (default: <agent>/run)")
    ap.add_argument("--session", help="explicit transcript path, bypassing slug resolution")
    ap.add_argument("--policy", default=POLICY, help="policy supplying looping.gate_patterns")
    ap.add_argument("--limit", type=int, default=24000, help="slice tail cap in characters")
    ap.add_argument("--json", action="store_true", help="print the provenance record")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--history-files", type=int, default=4,
                    help="recent transcripts of this cwd read into the event stream (default 4)")
    ap.add_argument("--history-bytes", type=int, default=48_000_000,
                    help="cap on those transcripts; the newest is always read (default 48 MB)")
    opts = ap.parse_args()
    if opts.self_test:
        return self_test()
    try:
        meta = run(agent_dir=opts.agent, cwd=opts.cwd, out_dir=opts.out, session=opts.session,
                   limit=opts.limit, policy=opts.policy, max_files=opts.history_files,
                   max_bytes=opts.history_bytes)
    except Exception as exc:  # fail open: a boundary must never stall behind telemetry
        print(f"session_telemetry: skipped ({exc})", file=sys.stderr)
        return 0
    if opts.json:
        print(json.dumps(meta, indent=2, sort_keys=True))
    elif not opts.quiet:
        print(f"{meta['status']} {meta.get('resolution')} session={meta.get('session')} "
              f"events={meta.get('calls', 0)} gates={meta.get('gates', 0)} "
              f"patches={meta.get('patches', 0)} elided={meta.get('elided_spans', 0)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
