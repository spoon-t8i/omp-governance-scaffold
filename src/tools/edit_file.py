#!/usr/bin/env python3
"""edit_file.py - atomic, multi-hunk surgical edits.

Why this exists: the agent harness offers whole-file write, so a three-line change to
an 800-line file re-emits 32 KB and risks dropping lines nobody re-read. This tool takes
the smallest thing that says what changed - an old block and a new block per hunk -
validates it against the file on disk, and replaces the file atomically.

Modes
  edit_file.py TARGET --old BLOCK --new BLOCK [--lines N:M] [--dry-run]
  edit_file.py --spec spec.json            (or --spec - to read the JSON from stdin)
  edit_file.py --patch [--file PATH]       (unified diff on stdin, fuzz=0)

Spec shape
  {"file": "path", "dry_run": false,
   "hunks": [{"old": "...", "new": "...", "start_line": 10, "end_line": 14}]}

Semantics
  * Without a line range, `old` must match exactly once. Zero or many matches are a hard
    error and the candidate line numbers are printed, so the caller can disambiguate
    with start_line/end_line (or --lines N:M).
  * With a line range, a match wholly inside the stated range wins. If none is, the range
    widens by ±3 to absorb a stale line number, and that must still be unique - a range
    never silently picks between two candidates that sit inside it.
  * A failed match is diagnosed, not just refused: the first non-empty line of `old`
    supplies a search key, the nearest window of the real file is echoed back with L<n>:
    prefixes (at most 8 lines), and the other lines sharing the key are named. Re-emitting a
    block from memory is the common failure; the echo makes the next attempt a copy of disk.
  * An empty "new" deletes the matched block; a lone newline is one blank line.
  * Hunks are resolved against the ORIGINAL file, must not overlap, and are applied
    bottom-to-top, so an earlier hunk never shifts a later hunk's line numbers.
  * Every hunk of every file is validated before anything is written: a batch that fails
    halfway writes nothing.
  * The write is atomic: mkstemp in the target directory, mode and uid/gid copied from
    the original, fsync, os.replace, temp removed in `finally`.
  * Terminators are per line, not per file: a CRLF file stays CRLF, and inserted lines
    take the file's dominant style. The file's final terminator is a property of the
    file: no edit adds or removes a trailing newline, and an inserted line never merges
    with the last line. A patch's '\\ No newline at end of file' marker is therefore
    ignored - use write to change whether a file ends with a newline.
  * A symlink argument edits the file it points at; the link itself survives. A hard link
    to the target does not survive: the edit installs a new inode, as any editor that
    writes a replacement file does.

Exit codes
  0 ok (including --dry-run and a no-op hunk)   2 usage / bad spec
  3 no match                                    4 ambiguous match
  5 overlapping hunks                           6 patch context mismatch
  7 I/O

No arguments prints usage and exits 0: spine_check --run-harness smoke-runs every script
the spine names, and a tool that needs arguments must not turn that smoke run into a
false violation. Bad arguments still exit 2.
"""
import argparse
import difflib
import hashlib
import json
import os
import pathlib
import re
import stat
import sys
import tempfile

EXIT_USAGE, EXIT_NOMATCH, EXIT_AMBIG, EXIT_OVERLAP, EXIT_PATCH, EXIT_IO = 2, 3, 4, 5, 6, 7
TOL = 3                     # how far a stated line range may be off and still match
LIST_CAP = 12               # candidate line numbers printed before "and N more"
ECHO_CAP = 8                # real file lines echoed back when a hunk fails to match
ECHO_WIDTH = 160            # characters kept from each echoed line
ECHO_SCAN = 200             # a key hitting more lines than this is too blunt to locate
HUNK_KEYS = {"old", "new", "start_line", "end_line"}
SPEC_KEYS = {"file", "hunks", "dry_run"}
HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
RANGE_RE = re.compile(r"^(\d+):(\d+)$")

USAGE = __doc__.split("Exit codes")[0].rstrip()


class EditError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class Target:
    """One file's content as content lines plus each line's own terminator."""

    def __init__(self, path):
        if path.is_symlink():
            path = path.resolve()
        self.path = path
        try:
            raw = path.read_bytes()
        except IsADirectoryError:
            raise EditError(EXIT_IO, f"{path}: is a directory, not a file")
        except OSError as exc:
            raise EditError(EXIT_IO, f"{path}: cannot read ({exc.strerror})")
        self.sha_before = hashlib.sha256(raw).hexdigest()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise EditError(EXIT_IO, f"{path}: not valid UTF-8 at byte {exc.start}")
        crlf = text.count("\r\n")
        lf = text.count("\n") - crlf
        self.newline = "\r\n" if crlf and crlf >= lf else "\n"
        self.lines, self.terms = [], []
        parts = text.split("\n")
        for i, part in enumerate(parts):
            last = i == len(parts) - 1
            if last and part == "":
                break                       # the file ends with a newline
            if not last and part.endswith("\r"):
                self.lines.append(part[:-1])
                self.terms.append("\r\n")
            elif last:
                self.lines.append(part)
                self.terms.append("")       # last line, no terminator
            else:
                self.lines.append(part)
                self.terms.append("\n")

    def render(self, lines, terms):
        return "".join(l + t for l, t in zip(lines, terms))


def split_block(text, label):
    """A caller's block of text -> content lines, terminators stripped."""
    if not isinstance(text, str):
        raise EditError(EXIT_USAGE, f'{label}: "old"/"new" must be strings')
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if len(lines) > 1 and lines[-1] == "":
        lines.pop()             # a trailing newline is punctuation, not a blank line
    return lines


def candidates(lines, old):
    n = len(old)
    first = old[0]
    return [i for i in range(len(lines) - n + 1)
            if lines[i] == first and lines[i:i + n] == old]


def spans(cands, n):
    shown = ", ".join(f"{i + 1}-{i + n}" for i in cands[:LIST_CAP])
    if len(cands) > LIST_CAP:
        shown += f" (and {len(cands) - LIST_CAP} more)"
    return shown or "none"


def echo_key(old):
    """Search key for a failed block: the first token of its first non-empty line."""
    for line in old:
        toks = [t for t in re.split(r"\W+", line.strip()) if t]
        if toks:
            return toks[0] if len(toks[0]) >= 3 else max(toks, key=len)
    return ""


def echo_hits(lines, key):
    """Every line holding key, case-insensitively, in file order."""
    probe = key.casefold()
    return [i for i, line in enumerate(lines) if probe in line.casefold()]


def mismatch_echo(target, old):
    """The text the file really holds where a failed old block looks like it belongs.

    A caller re-emitting a block from memory - after a compaction, from a stale note - hits the
    smallest drift: a renamed local, a reflowed line, one edited character. "no match" alone
    makes the next attempt just as blind, so echo the nearest real lines with their line numbers
    and the caller edits truth instead of guessing twice.
    """
    key = echo_key(old)
    if not key:
        return ""
    hits = echo_hits(target.lines, key)
    if len(hits) > ECHO_SCAN:             # too blunt: widen to the head line's longest token
        head = next((l for l in old if l.strip()), "")
        words = [t for t in re.split(r"\W+", head) if len(t) >= 3]
        wider = max(words, key=len) if words else ""
        if wider and wider != key:
            key, hits = wider, echo_hits(target.lines, wider)
    if not hits:
        return ""

    def held(i):                          # how much of old this window already has right
        return sum(1 for k in range(len(old))
                   if i + k < len(target.lines) and target.lines[i + k] == old[k])

    best = min(hits, key=lambda i: (-held(i), i))
    width = min(max(len(old), 1), ECHO_CAP)
    rows = [f"L{i + 1}: {target.lines[i][:ECHO_WIDTH]}"
            for i in range(best, min(best + width, len(target.lines)))]
    also = [str(i + 1) for i in hits if i != best][:3]
    tail = f' ; "{key}" also at lines {", ".join(also)}' if also else ""
    return (f'\n  "{key}" lives at L{best + 1}; the file actually says:'
            + "\n" + "\n".join("    " + r for r in rows) + tail)


def resolve_spec_hunk(target, hunk, label):
    """Locate one spec hunk in the original file; -> (start, end, new_lines, label)."""
    old = split_block(hunk["old"], f"{target.path}: {label}")
    if hunk["old"] == "":
        raise EditError(EXIT_USAGE,
                        f'{target.path}: {label}: "old" is empty; an empty block would match '
                        f'everywhere - name the block to replace, or use --patch to insert')
    new = [] if hunk["new"] == "" else split_block(hunk["new"], f"{target.path}: {label}")
    found = candidates(target.lines, old)
    start_line, end_line = hunk.get("start_line"), hunk.get("end_line")

    if start_line is None and end_line is None:
        if not found:
            raise EditError(EXIT_NOMATCH,
                            f"{target.path}: {label}: no match for the {len(old)}-line old block"
                            + mismatch_echo(target, old))
        if len(found) > 1:
            raise EditError(EXIT_AMBIG,
                            f"{target.path}: {label}: old block matches {len(found)} times at lines "
                            f"{spans(found, len(old))}; pass start_line/end_line to pick one")
        at = found[0]
    else:
        if start_line is None or end_line is None:
            raise EditError(EXIT_USAGE,
                            f"{target.path}: {label}: a line range needs both ends (start_line and "
                            f'end_line, or --lines N:M)')
        if start_line < 1 or end_line < start_line:
            raise EditError(EXIT_USAGE,
                            f"{target.path}: {label}: bad line range {start_line}-{end_line}")
        exact = [i for i in found if start_line - 1 <= i and i + len(old) <= end_line]
        inside = exact or [i for i in found
                           if i >= start_line - 1 - TOL and i + len(old) <= end_line + TOL]
        if len(inside) == 1:
            at = inside[0]
        elif not inside:
            raise EditError(EXIT_NOMATCH,
                            f"{target.path}: {label}: no match inside lines {start_line}-{end_line} "
                            f"(±{TOL}); the file has {len(found)} match(es) at lines "
                            f"{spans(found, len(old))}" + mismatch_echo(target, old))
        else:
            raise EditError(EXIT_AMBIG,
                            f"{target.path}: {label}: {len(inside)} matches inside lines "
                            f"{start_line}-{end_line} at lines {spans(inside, len(old))}; "
                            f"the range is too wide to pick one")
    return at, at + len(old), new, label


def check_overlap(resolved, path):
    ordered = sorted(resolved, key=lambda r: r[0])
    for a, b in zip(ordered, ordered[1:]):
        if b[0] < a[1]:
            raise EditError(EXIT_OVERLAP,
                            f"{path}: {a[3]} (lines {a[0] + 1}-{a[1]}) overlaps {b[3]} "
                            f"(lines {b[0] + 1}-{b[1]})")


def job(path_text, hunks):
    """Resolve a spec-style hunk list against a file -> (Target, resolved)."""
    target = Target(pathlib.Path(path_text).expanduser())
    resolved = [resolve_spec_hunk(target, h, f"hunk {n}") for n, h in enumerate(hunks, 1)]
    check_overlap(resolved, target.path)
    return target, resolved, len(hunks)


def strip_prefix(path):
    return path[2:] if path.startswith(("a/", "b/")) else path


def parse_patch(text, override):
    """Unified diff -> [(path, [(start, old_lines, new_lines, label)])], fuzz=0 strict."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("--- "):
            oldp = line[4:].strip()
            newp = None
            if i + 1 < len(lines) and lines[i + 1].startswith("+++ "):
                newp = lines[i + 1][4:].strip()
                i += 1
            if newp is None:
                raise EditError(EXIT_PATCH, f"a --- header has no matching +++ header: {line!r}")
            if oldp == "/dev/null" or newp == "/dev/null":
                raise EditError(EXIT_PATCH,
                                "new-file and delete-file diffs are not supported; use write")
            path = override or strip_prefix(newp)
            if not path:
                raise EditError(EXIT_PATCH, "the diff names no file; pass --file PATH")
            out.append([path, []])
            i += 1
            continue
        match = HUNK_RE.match(line)
        if match:
            if not out:
                if not override:
                    raise EditError(EXIT_PATCH,
                                    f"hunk header before any file header: {line!r}")
                out.append([override, []])
            old_start = int(match.group(1))
            old_count = 1 if match.group(2) is None else int(match.group(2))
            new_count = 1 if match.group(4) is None else int(match.group(4))
            i += 1
            old_lines, new_lines = [], []
            while len(old_lines) < old_count or len(new_lines) < new_count:
                if i >= len(lines):
                    raise EditError(EXIT_PATCH, f"hunk {line!r} is truncated")
                body = lines[i]
                i += 1
                if body.startswith("\\"):
                    continue                    # "\ No newline at end of file"
                if body.startswith("-"):
                    old_lines.append(body[1:])
                elif body.startswith("+"):
                    new_lines.append(body[1:])
                elif body.startswith(" "):
                    old_lines.append(body[1:])
                    new_lines.append(body[1:])
                elif body == "":
                    old_lines.append("")
                    new_lines.append("")
                else:
                    raise EditError(EXIT_PATCH,
                                    f"unexpected body line in hunk {line!r}: {body!r}")
            at = old_start if old_count == 0 else old_start - 1
            out[-1][1].append((at, old_lines, new_lines, f"@@ -{old_start},{old_count} @@"))
            continue
        i += 1
    if not out:
        raise EditError(EXIT_PATCH, "no unified-diff hunks found on stdin")
    if override and len(out) > 1:
        raise EditError(EXIT_USAGE, f"--file {override} cannot override a diff that names "
                                    f"{len(out)} files")
    return out


def resolve_patch_hunk(target, hunk):
    """Verify a patch hunk's context at its stated position. No fuzz, no offset search."""
    at, old_lines, new_lines, label = hunk
    if at < 0 or at > len(target.lines):
        raise EditError(EXIT_PATCH,
                        f"{target.path}: {label}: start line {at + 1} is past the end of a "
                        f"{len(target.lines)}-line file")
    if target.lines[at:at + len(old_lines)] == old_lines:
        return at, at + len(old_lines), new_lines, label
    where = next((k for k, want in enumerate(old_lines)
                  if at + k >= len(target.lines) or target.lines[at + k] != want), 0)
    got = target.lines[at + where] if at + where < len(target.lines) else "<end of file>"
    raise EditError(EXIT_PATCH,
                    f"{target.path}: {label}: context mismatch at line {at + where + 1}: "
                    f"expected {old_lines[where]!r}, found {got!r} (fuzz=0)"
                    + mismatch_echo(target, old_lines))


def patch_jobs(sections):
    jobs = []
    for path_text, hunks in sections:
        target = Target(pathlib.Path(path_text).expanduser())
        resolved = [resolve_patch_hunk(target, h) for h in hunks]
        check_overlap(resolved, target.path)
        jobs.append((target, resolved, len(hunks)))
    return jobs


def compute(target, resolved):
    """Apply in memory; -> (rendered, sha_after, diff_lines, ins, del)."""
    lines, terms = list(target.lines), list(target.terms)
    for start, end, new, _label in sorted(resolved, key=lambda r: r[0], reverse=True):
        lines[start:end] = new
        terms[start:end] = [target.newline] * len(new)
    if lines:
        # terminators are positional, so a splice can strand the original last line's
        # "" in the middle of the file: every interior line needs a real terminator and
        # the last line needs the file's own (possibly "")
        final = target.terms[-1] if target.terms else target.newline
        for i in range(len(lines) - 1):
            if not terms[i]:
                terms[i] = target.newline
        terms[-1] = final
    rendered = target.render(lines, terms)
    after = hashlib.sha256(rendered.encode("utf-8")).hexdigest()
    diff = list(difflib.unified_diff(target.lines, lines, fromfile=str(target.path),
                                     tofile=str(target.path), n=3, lineterm=""))
    ins = sum(1 for l in diff if l.startswith("+") and not l.startswith("+++"))
    dele = sum(1 for l in diff if l.startswith("-") and not l.startswith("---"))
    return rendered, after, diff, ins, dele


def write_atomic(target, rendered):
    directory = target.path.parent
    try:
        fd, tmp = tempfile.mkstemp(dir=str(directory), prefix=target.path.name + ".edit-",
                                   suffix=".tmp")
    except OSError as exc:
        raise EditError(EXIT_IO, f"{target.path}: cannot create a temp file in {directory} "
                                f"({exc.strerror})")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(rendered.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        info = target.path.stat()
        tmp_info = os.stat(tmp)
        os.chmod(tmp, stat.S_IMODE(info.st_mode))
        if (info.st_uid, info.st_gid) != (tmp_info.st_uid, tmp_info.st_gid):
            # chown clears setuid/setgid, so only pay it when the ids actually differ
            try:
                os.chown(tmp, info.st_uid, info.st_gid)
            except (PermissionError, AttributeError):
                pass
        os.replace(tmp, target.path)
        try:                    # the rename is durable only once the directory is synced
            dir_fd = os.open(str(directory), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass
    except OSError as exc:
        raise EditError(EXIT_IO, f"{target.path}: cannot replace ({exc.strerror})")
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def load_spec(spec_ref):
    try:
        text = sys.stdin.read() if spec_ref == "-" else pathlib.Path(spec_ref).read_text(
            encoding="utf-8")
    except OSError as exc:
        raise EditError(EXIT_IO, f"spec {spec_ref}: cannot read ({exc.strerror})")
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise EditError(EXIT_USAGE, f"spec {spec_ref}: not valid JSON ({exc})")
    if not isinstance(data, dict):
        raise EditError(EXIT_USAGE, f"spec {spec_ref}: must be a JSON object")
    unknown = sorted(set(data) - SPEC_KEYS)
    if unknown:
        raise EditError(EXIT_USAGE, f"spec {spec_ref}: unknown key(s): {', '.join(unknown)}")
    if not data.get("file"):
        raise EditError(EXIT_USAGE, f'spec {spec_ref}: needs "file"')
    if "dry_run" in data and not isinstance(data["dry_run"], bool):
        raise EditError(EXIT_USAGE, f'spec {spec_ref}: "dry_run" must be a boolean')
    hunks = data.get("hunks")
    if not isinstance(hunks, list) or not hunks:
        raise EditError(EXIT_USAGE, f'spec {spec_ref}: needs a non-empty "hunks" list')
    for n, h in enumerate(hunks, 1):
        if not isinstance(h, dict):
            raise EditError(EXIT_USAGE, f"spec {spec_ref}: hunk {n} must be an object")
        unknown = sorted(set(h) - HUNK_KEYS)
        if unknown:
            raise EditError(EXIT_USAGE,
                            f"spec {spec_ref}: hunk {n} unknown key(s): {', '.join(unknown)}")
        if "old" not in h or "new" not in h:
            raise EditError(EXIT_USAGE, f'spec {spec_ref}: hunk {n} needs "old" and "new"')
        for key in ("start_line", "end_line"):
            value = h.get(key)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
                raise EditError(EXIT_USAGE, f'spec {spec_ref}: hunk {n}: "{key}" must be an int')
    return data


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        print(USAGE)
        return 0
    ap = argparse.ArgumentParser(prog="edit_file.py", add_help=True,
                                 description="atomic multi-hunk surgical edit")
    ap.add_argument("target", nargs="?", help="file to edit (single-hunk mode)")
    ap.add_argument("--old", help="exact block to replace")
    ap.add_argument("--new", help="block to put in its place (\"\" deletes)")
    ap.add_argument("--lines", help="N:M line range that disambiguates --old")
    ap.add_argument("--spec", help='JSON spec file, or "-" for stdin')
    ap.add_argument("--patch", action="store_true", help="read a unified diff from stdin")
    ap.add_argument("--file", dest="patch_file", help="target path for a headerless diff, "
                                                      "or to override the path the diff names")
    ap.add_argument("--dry-run", action="store_true", help="print the diff, write nothing")
    args = ap.parse_args(argv)

    spec_dry = False
    try:
        if args.spec:
            if (args.target or args.old or args.new or args.lines or args.patch
                    or args.patch_file):
                raise EditError(EXIT_USAGE, "--spec takes no other edit arguments")
            spec = load_spec(args.spec)
            jobs = [job(spec["file"], spec["hunks"])]
            spec_dry = spec.get("dry_run", False)
        elif args.patch:
            if args.target or args.old or args.new:
                raise EditError(EXIT_USAGE, "--patch takes a diff on stdin, not --old/--new")
            jobs = patch_jobs(parse_patch(sys.stdin.read(), args.patch_file))
        else:
            if not args.target:
                raise EditError(EXIT_USAGE, "no target file; run with no arguments for usage")
            if args.old is None or args.new is None:
                raise EditError(EXIT_USAGE, "--old and --new must be given together")
            if args.lines is not None:
                match = RANGE_RE.match(args.lines)
                if not match:
                    raise EditError(EXIT_USAGE, f"--lines wants N:M, got {args.lines!r}")
                bounds = {"start_line": int(match.group(1)), "end_line": int(match.group(2))}
            else:
                bounds = {}
            jobs = [job(args.target, [dict(old=args.old, new=args.new, **bounds)])]
        dry = args.dry_run or spec_dry
        for target, resolved, count in jobs:
            rendered, after, diff, ins, dele = compute(target, resolved)
            unchanged = rendered == target.render(target.lines, target.terms)
            if not dry and not unchanged:
                # the edit lands before the echo: a truncated stdout (a caller piping into
                # `head`) must never be able to abort a validated edit
                write_atomic(target, rendered)
            if diff:
                print("\n".join(diff))
            else:
                print("(no textual change)")
            summary = (f"EDITED {target.path}: {count} hunk(s), +{ins}/-{dele} lines, "
                       f"sha256 {target.sha_before[:8]}..{after[:8]}")
            if dry:
                summary += " [dry-run: file unchanged]"
            elif unchanged:
                summary += " [no change]"
            print(summary)
    except EditError as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return exc.code
    except BrokenPipeError:
        # `edit_file.py ... | head -1` is a normal call; the file is already correct, so
        # silence the flush at shutdown instead of dumping a traceback on the caller
        try:
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        except OSError:
            pass
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
