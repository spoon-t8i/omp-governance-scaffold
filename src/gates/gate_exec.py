#!/usr/bin/env python3
"""gate_exec.py - run a gate so that nothing it spawned survives it.

Why this exists: both spine tools that run gates - plan_check.py (--run-gate, --prove-gate)
and spine_check.py (the sweep) - called `subprocess.run(cmd, ..., timeout=budget)`, which
kills only the `sh` it spawned. On a cut-off the harness -> pytest -> engine grandchildren
keep running; on a normal exit anything the gate daemonised keeps running. amber_ascii's
MOV1-MOV3 re-runs were cut off at 1800 s while the engine each one had started stayed at
307% CPU (rcpts 39cc854bae5f / eb0b9e7f8125 / 6d958957f292), so every re-run inherited the
load that failed the run before it, and each row recorded exit 124 next to the row for a
gate that genuinely timed out. kinds-bench got this right first: one process group per run,
`killpg` after every test, then a sweep for the survivors (kinds-bench kbench/sandbox.py
:129-171, :99-108). This module is that mechanism, spine-shaped, in one place.

The contract
  * every run gets its own process group (`start_new_session=True`), so the kill reaches
    the whole tree and not one shell;
  * a cut-off sends SIGTERM to the group, gives it KILL_GRACE_S, then SIGKILL - the grace
    is paid only on the timeout path, where the tree is alive and worth asking politely;
  * after a normal exit the group is swept with no grace, catching the daemon that
    outlived its parent - no rlimit covers that one;
  * RLIMIT_FSIZE is set in a preexec so a runaway writer dies at the cap (`OMP_GATE_FSIZE_MB`
    or `--max-fsize-mb`, default 4096, 0 turns it off) instead of filling the disk;
  * RLIMIT_NPROC is deliberately NOT set. It is a per-UID ceiling, so a gate that reached it
    would starve the session that launched it - and the agent itself runs as that uid.
    kinds-bench caps it per *session* with a counters thread (kbench/sandbox.py:99-108);
    that needs a sandbox, not an rlimit, and a sandbox is not what this boundary is.
  * the child's PATH can carry a prefix (`OMP_GATE_PATH_EXTRA`, colon-separated) so a gate
    can be pointed at a toolchain without editing the caller's environment, and any
    `KEY=VAL` pair a caller passes reaches the child verbatim;
  * `--user` drops the child to another uid/gid before exec. Opt-in only: it needs the
    privileges to do so, and a gate running as somebody else cannot read the spine it is
    meant to audit, which is the wrong kind of safe;
  * `classify` maps a run onto {pass, fail, timeout, launch-error, no-output} so a loop
    detector can tell "the gate said no twice" from "the gate never answered twice", and
    the exit code keeps the two apart too: 124 = cut off, 127 = never spawned.

Modes
  tools/gate_exec.py                    print the taxonomy and the live defaults, exit 0
  tools/gate_exec.py --self-test        prove the kill, the sweep, the file cap, the env
                                        and the taxonomy in a temp dir; exit 0 when all pass
  tools/gate_exec.py --timeout 30 --run CMD ARGS...
                                        run one command under the boundary and report
                                        `cwd=`, `kind=`, `exit=`; --isolated runs it in a
                                        fresh directory outside the spine tree, which is
                                        what a self-test of a spine tool needs: a gate whose
                                        paths are relative must not be able to rewrite the
                                        live ledger while proving it can be killed
"""
import argparse
import os
import pathlib
import pwd
import shutil
import signal
import subprocess
import sys
import tempfile
import time

try:
    import resource
except ImportError:  # non-Linux: no rlimits; the group isolation still applies
    resource = None

OUTCOME_KINDS = ("pass", "fail", "timeout", "launch-error", "no-output")
# `skip` is out-of-band: a gate whose required command is absent is neither a pass nor a
# failure, and the sweep already records such rows. kbench reports the same state (SKIP,
# kbench/validate.py:43-44) rather than calling it a pass.
LEDGER_KINDS = OUTCOME_KINDS + ("skip",)
GATE_TIMEOUT_S = 1800          # what plan_check passed as a literal
KILL_GRACE_S = 2.0             # SIGTERM -> SIGKILL, timeout path only
DEFAULT_FSIZE_MB = 4096        # receipts and evidence are MiB-scale; a writer gone wrong is GB
EXIT_TIMEOUT = 124             # timeout(1)'s convention, unchanged from the old code
EXIT_NO_LAUNCH = 127           # nothing spawned: distinct from 124 on purpose
PATH_EXTRA_ENV = "OMP_GATE_PATH_EXTRA"
AGENT_ROOT = pathlib.Path(__file__).resolve().parent.parent   # .../.omp/agent


def fsize_bytes(override=None) -> int:
    """RLIMIT_FSIZE for a gate, from `override` else OMP_GATE_FSIZE_MB (0 = no cap)."""
    raw = override if override is not None else os.environ.get("OMP_GATE_FSIZE_MB", "")
    if raw is None or str(raw).strip() == "":
        mb = DEFAULT_FSIZE_MB
    else:
        try:
            mb = int(str(raw).strip())
        except ValueError:
            mb = DEFAULT_FSIZE_MB
    return mb * 1024 * 1024 if mb > 0 else 0


def child_env(extra=None):
    """The environment a gate gets: the caller's, plus the PATH prefix, plus KEY=VAL pairs."""
    env = dict(os.environ)
    prefix = os.environ.get(PATH_EXTRA_ENV, "").strip()
    if prefix:
        env["PATH"] = prefix.rstrip(":") + os.pathsep + env.get("PATH", "")
    for pair in (extra or ()):
        if "=" in pair:
            key, val = pair.split("=", 1)
            env[key] = val
    return env


def resolve_user(spec):
    """`--user NAME|UID` -> (uid, gid), gid from the user's group unless NAME:GID is given."""
    if not spec:
        return None, None
    name, _, group = str(spec).partition(":")
    try:
        rec = pwd.getpwnam(name) if not name.isdigit() else pwd.getpwuid(int(name))
    except KeyError:
        raise ValueError(f"no such user: {spec}") from None
    gid = None
    if group:
        gid = int(group) if group.isdigit() else pwd.getgrnam(group).gr_gid
    return rec.pw_uid, (gid if gid is not None else rec.pw_gid)


def preexec(fsize_mb=None, uid=None, gid=None):
    """The child's pre-exec: file cap, then the uid/gid drop, in that order.

    The cap is set first on purpose: dropping privileges cannot undo it, and a gate that
    could re-raise its own limit would make the cap a suggestion.
    """
    cap = fsize_bytes(fsize_mb)

    def _apply():
        if resource is not None and cap:
            resource.setrlimit(resource.RLIMIT_FSIZE, (cap, cap))
        if gid is not None:
            os.setgid(gid)
        if uid is not None:
            os.setuid(uid)
    return _apply


def group_pids(pgid: int):
    """Every live pid whose process group is `pgid` (Linux /proc; empty where unavailable)."""
    out = []
    try:
        entries = os.scandir("/proc")
    except OSError:
        return out
    with entries:
        for entry in entries:
            if entry.name.isdigit():
                try:
                    if os.getpgid(int(entry.name)) == pgid:
                        out.append(int(entry.name))
                except OSError:
                    pass
    return out


def kill_group(pgid: int, grace: float = KILL_GRACE_S):
    """SIGTERM the group, wait up to `grace` for it to empty, SIGKILL what is left.

    Returns the pids that were there when the kill started - the count a caller reports.
    Safe to call on a group that is already gone: a dead leader means os.getpgid raises,
    which group_pids swallows, so the sweep costs one /proc pass and no error.
    """
    victims = group_pids(pgid)
    if not victims:
        return []
    _sig(pgid, signal.SIGTERM)
    deadline = time.monotonic() + max(0.0, grace)
    while time.monotonic() < deadline and group_pids(pgid):
        time.sleep(0.02)
    if group_pids(pgid):
        _sig(pgid, signal.SIGKILL)
    return victims


def _sig(pgid: int, sig):
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def _text(value) -> str:
    if value is None:
        return ""
    return value.decode(errors="replace") if isinstance(value, bytes) else value


def isolated(label: str = "") -> pathlib.Path:
    """A fresh working directory outside the spine tree, for a gate that writes as it runs.

    A spine tool resolves its ledger from its own location or from the current directory;
    running such a tool with cwd inside ~/.omp lets a self-test overwrite the record it is
    supposed to be proving. This hands out a temp directory and refuses anything that
    resolves inside the agent tree, so "isolated" is checked, not intended.
    """
    tmp = pathlib.Path(tempfile.mkdtemp(prefix=f"gate_exec_{label or 'run'}_")).resolve()
    if tmp == AGENT_ROOT or AGENT_ROOT in tmp.parents:
        shutil.rmtree(tmp, ignore_errors=True)
        raise RuntimeError(f"isolated dir {tmp} resolves inside the spine tree {AGENT_ROOT}")
    return tmp


def run(cmd, cwd, timeout=None, env=None, uid=None, gid=None, fsize_mb=None):
    """Run one gate in its own process group, captured to files. Returns (proc, kind, leftovers).

    Never raises for a timeout or a failed spawn: both come back as a kind, because the
    caller must not have to repeat the kill to stop leaking the tree. `proc` always
    carries stdout/stderr/returncode, so fact_store.write_receipt can archive any row.
    A `str` cmd runs through the shell, exactly as the spine declares gates. `cwd=None`
    inherits the caller's directory; it is not the string "None", which used to reach Popen and
    come back as `launch-error` on a path that never existed - a rehearsal reporting a broken
    gate that was only a missing argument.

    Output goes to files, not pipes (kinds-bench kbench/sandbox.py:138-153): a pipe's write
    end is inherited by every child, so reading one waits for the *whole tree* to exit - a
    probe of this module hung 40 s on a leaked `sleep` that the sweep was supposed to kill.
    A file costs the same read and cannot hold the caller hostage to a daemon.
    """
    budget = GATE_TIMEOUT_S if timeout is None else timeout
    box = tempfile.TemporaryDirectory(prefix="gate_exec-")
    out_path = pathlib.Path(box.name) / "stdout"
    err_path = pathlib.Path(box.name) / "stderr"
    try:
        with open(out_path, "wb") as fo, open(err_path, "wb") as fe:
            try:
                p = subprocess.Popen(cmd, cwd=(None if cwd is None else str(cwd)),
                                     shell=isinstance(cmd, str),
                                     stdout=fo, stderr=fe, env=child_env(env),
                                     start_new_session=True,
                                     preexec_fn=preexec(fsize_mb, uid, gid))
            except OSError as exc:
                return (subprocess.CompletedProcess(cmd, EXIT_NO_LAUNCH, "",
                                                    f"gate could not spawn: {exc}"),
                        "launch-error", [])
            # start_new_session makes the child its own group leader, so its pid IS the pgid -
            # and it stays the pgid after the leader exits, which is how the sweep finds orphans.
            timed_out = False
            try:
                rc = p.wait(timeout=budget)
            except subprocess.TimeoutExpired:
                timed_out = True
                rc = EXIT_TIMEOUT
            # The group is swept on every path, not only the cut-off: a gate that exits and
            # leaves daemons behind is the leak that became the next sweep's CPU load.
            killed = kill_group(p.pid, grace=KILL_GRACE_S if timed_out else 0.0)
            if timed_out:
                try:
                    p.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
            _settle(p.pid)
        out = out_path.read_text(errors="replace") if out_path.is_file() else ""
        err = err_path.read_text(errors="replace") if err_path.is_file() else ""
    finally:
        box.cleanup()
    if timed_out:
        err = (err + f"\ngate_exec: cut off after {budget:.0f}s (partial output); the whole "
                     f"process group was killed ({len(killed)} process(es) in it)").lstrip("\n")
        return subprocess.CompletedProcess(cmd, EXIT_TIMEOUT, out, err), "timeout", killed
    return (subprocess.CompletedProcess(cmd, rc, out, err),
            classify(rc, out, err), killed)


def _settle(pgid: int, limit: float = 0.4):
    """Wait a moment for killed members to leave /proc, so their last bytes are on disk."""
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline and group_pids(pgid):
        time.sleep(0.02)


def classify(exit_code, stdout="", stderr="", kind=None) -> str:
    """One of OUTCOME_KINDS for a finished run.

    `kind` from run() wins where it is already decided (timeout, launch-error). Otherwise:
    exit 0 is a pass; a nonzero exit with nothing on either stream is `no-output` - the gate
    died without stating a reason, which is the crash/no-file bucket kbench splits into
    `crash`/`nofile` (kbench/runner.py classify); anything else is a verdict of `fail`.
    """
    if kind in ("timeout", "launch-error"):
        return kind
    try:
        code = int(exit_code)
    except (TypeError, ValueError):
        return "launch-error"
    if code == 0:
        return "pass"
    if not str(stdout or "").strip() and not str(stderr or "").strip():
        return "no-output"
    return "fail"


def kind_for(rc, stdout="", stderr="") -> str:
    """Ledger-side taxonomy, for a recorder whose `rc` is also the strings skip/timeout/error."""
    if rc == "skip":
        return "skip"
    if rc == "timeout":
        return "timeout"
    if rc == "error":
        return "launch-error"
    return classify(rc, stdout, stderr)


def _self_test() -> int:
    """Prove the mechanism on this box: kill, sweep, file cap, env, launch error, taxonomy."""
    tmp = isolated("selftest")
    fails, total = [], 0

    def check(name, ok, detail=""):
        nonlocal total
        total += 1
        if not ok:
            fails.append(f"{name}: {detail}")

    def alive(pid):
        return pathlib.Path(f"/proc/{pid}").exists()

    try:
        # 1. a cut-off kills the grandchild, not just the shell that spawned it
        victim = tmp / "gp"
        proc, kind, left = run(["sh", "-c",
                                f'sleep 40 & echo $! > {victim}; wait'], tmp, timeout=1)
        gp = int(victim.read_text().strip()) if victim.is_file() else -1
        time.sleep(0.4)
        check("timeout kind", kind == "timeout", kind)
        check("timeout exit", proc.returncode == EXIT_TIMEOUT, proc.returncode)
        check("grandchild killed", gp > 0 and not alive(gp), f"pid {gp} still alive")
        check("timeout reported leftovers", len(left) >= 1, left)

        # 2. a clean exit still gets swept: the daemon that outlives its parent
        proc, kind, left = run(["sh", "-c",
                                f'sleep 40 & echo $! > {victim}; echo done'], tmp)
        dp = int(victim.read_text().strip()) if victim.is_file() else -1
        time.sleep(0.4)
        check("clean exit passes", kind == "pass" and proc.returncode == 0,
              f"{kind} exit {proc.returncode} out={proc.stdout!r}")
        check("orphan swept", len(left) >= 1 and dp > 0 and not alive(dp),
              f"leftovers={left} pid {dp} alive={alive(dp) if dp > 0 else 'n/a'}")

        # 3. RLIMIT_FSIZE actually bites (the cap is read in the child, so it applies)
        proc, kind, _ = run(["python3", "-c",
                             "open('big','w').write('x' * (32 * 1024 * 1024))"], tmp,
                            fsize_mb=1)
        cap = 1024 * 1024
        written = (tmp / "big").stat().st_size if (tmp / "big").exists() else -1
        check("fsize cap bites", proc.returncode != 0 and 0 <= written <= cap,
              f"exit {proc.returncode} kind {kind} wrote {written} bytes")

        # 4. a gate that cannot start is not a timeout
        proc, kind, _ = run([str(tmp / "no-such-gate")], tmp)
        check("launch-error kind", kind == "launch-error", kind)
        check("launch-error exit distinct", proc.returncode == EXIT_NO_LAUNCH,
              proc.returncode)

        # 5. the taxonomy separates a verdict from silence
        check("classify pass", classify(0, "", "") == "pass")
        check("classify fail", classify(1, "2 failures", "") == "fail")
        check("classify no-output", classify(1, "", "") == "no-output")
        check("classify timeout wins", classify(124, "x", "", kind="timeout") == "timeout")
        check("kinds are the five",
              set(OUTCOME_KINDS) == {"pass", "fail", "timeout", "launch-error", "no-output"},
              str(OUTCOME_KINDS))

        # 6. the env the caller asks for is the env the child sees, PATH prefix included
        (tmp / "bin").mkdir(exist_ok=True)
        shim = tmp / "bin" / "spine_shim_probe"
        shim.write_text('#!/bin/sh\necho shim\n', encoding="utf-8")
        shim.chmod(0o755)
        os.environ[PATH_EXTRA_ENV] = str(tmp / "bin")
        try:
            proc, kind, _ = run(["sh", "-c",
                                 'echo "$GATE_PROBE_MARKER"; spine_shim_probe'], tmp,
                                env=("GATE_PROBE_MARKER=marked",))
        finally:
            del os.environ[PATH_EXTRA_ENV]
        check("caller env reaches the child", "marked" in proc.stdout, proc.stdout[:80])
        check("OMP_GATE_PATH_EXTRA resolves a tool", "shim" in proc.stdout,
              f"kind {kind} out {proc.stdout[:80]!r} err {proc.stderr[:80]!r}")

        # 7. isolation is checked, not intended: the spine tree is never handed out
        check("isolated dir is outside the spine tree",
              AGENT_ROOT not in tmp.parents and tmp != AGENT_ROOT, str(tmp))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"gate_exec self-test: {total} checks, {len(fails)} failures")
    for f in fails:
        print(f"  FAIL {f}")
    return 1 if fails else 0


#: flags --run swallows: REMAINDER takes the rest of the line, so a budget written after
#: --run never reaches the parser and the gate silently gets the 1800 s default.
_OWN_FLAGS = ("--timeout", "--cwd", "--env", "--user", "--max-fsize-mb", "--isolated",
              "--self-test")


def _run_mode(args) -> int:
    """`--run`: one command under the boundary, reported. The exit code is the gate's."""
    tokens = args.cmd if isinstance(args.cmd, list) else [args.cmd]
    # A gate is declared as a shell string everywhere in the spine; the CLI must run what it
    # is handed the same way, not exec the whole line as one argv[0].
    cmd = " ".join(tokens)
    swallowed = [t.split("=")[0] for t in tokens if t.split("=")[0] in _OWN_FLAGS]
    if swallowed:
        # Ask for 4 s, silently get 1800 s: the lost-budget failure this module exists to
        # stop, arriving through the CLI instead of the DAG. Refuse instead of guessing.
        print(f"gate_exec: {' '.join(swallowed)} after --run belongs to gate_exec, not the "
              f"gate; REMAINDER takes the rest of the line, so put every gate_exec flag "
              f"before --run", file=sys.stderr)
        return 2
    cwd = isolated("cli") if args.isolated else pathlib.Path(args.cwd or os.getcwd()).resolve()
    uid, gid = resolve_user(args.user)
    timeout = args.timeout if args.timeout is not None else GATE_TIMEOUT_S
    print(f"cwd={cwd} timeout={timeout:.0f}s fsize="
          f"{fsize_bytes(args.max_fsize_mb) // (1024 * 1024)}MiB "
          f"user={args.user or os.environ.get('USER', str(os.getuid()))}")
    proc, kind, leftovers = run(cmd, cwd, timeout=timeout, env=tuple(args.env or ()),
                                uid=uid, gid=gid, fsize_mb=args.max_fsize_mb)
    sys.stdout.write(_text(proc.stdout))
    sys.stderr.write(_text(proc.stderr))
    print(f"kind={kind} exit={proc.returncode} swept={len(leftovers)}")
    if args.isolated:
        # the caller's directory is its own business; an isolated one is ours to remove
        shutil.rmtree(cwd, ignore_errors=True)
    return 0 if kind == "pass" else (124 if kind == "timeout" else 1)


def main() -> int:
    ap = argparse.ArgumentParser(description="OMP vNext gate execution boundary")
    ap.add_argument("--self-test", action="store_true",
                    help="prove the kill, the sweep, the file cap and the taxonomy here")
    ap.add_argument("--run", nargs=argparse.REMAINDER, dest="cmd", metavar="CMD",
                    help="run one command under the boundary and report cwd/kind/exit; "
                         "this takes the rest of the line, so put every other flag first")
    ap.add_argument("--cwd", default="", help="directory to run in (default: current)")
    ap.add_argument("--isolated", action="store_true",
                    help="run in a fresh directory outside the spine tree, then remove it")
    ap.add_argument("--timeout", type=float, default=None,
                    help=f"wall-clock budget in seconds (default {GATE_TIMEOUT_S})")
    ap.add_argument("--env", action="append", metavar="KEY=VAL",
                    help="extra environment for the gate (repeatable)")
    ap.add_argument("--max-fsize-mb", default=None,
                    help=f"RLIMIT_FSIZE for the gate, 0 for none (default {DEFAULT_FSIZE_MB})")
    ap.add_argument("--user", default="",
                    help="run the gate as NAME or UID (drop privileges before exec)")
    args = ap.parse_args()
    if args.self_test:
        return _self_test()
    if args.cmd:
        return _run_mode(args)
    print(f"gate_exec: kinds {', '.join(OUTCOME_KINDS)} (+ skip)")
    print(f"  timeout {GATE_TIMEOUT_S}s grace {KILL_GRACE_S}s fsize "
          f"{fsize_bytes() // (1024 * 1024)} MiB nproc cap none (per-uid, would starve "
          f"the launching session)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
