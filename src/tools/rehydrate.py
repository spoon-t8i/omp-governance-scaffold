#!/usr/bin/env python3
"""OMP vNext rehydration — the executable form of "assume this session died".

Run at session start, or whenever state is suspect. It prints the recovery path in
the order that actually restores context: active objective, next actions, the
claims already disproved, durable records (validated to exist), recent ledger,
then each project's git history and harness files. Nothing here is remembered by
the model; everything here is read.

Usage: tools/rehydrate.py [--check] [--events N]
"""

import argparse
import json
import pathlib
import re
import subprocess
import sys

AGENT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(AGENT / "tools"))
import spine_check  # noqa: E402  (the recovery-path parser lives in the validator, once)
import pre_compact_sieve  # noqa: E402  (the falsification journal is read/written there, once)
import plan_check  # noqa: E402  (the plan frontier is read through the validator, once)

spine_check.AGENT = AGENT
STATE = AGENT / "STATE.md"
EVENTS = AGENT / "EVENTS.md"
ROOT_RE = re.compile(r"/(?:home|mnt)/[A-Za-z0-9_./-]+/")
JOURNAL = pathlib.Path.home() / ".omp" / "run" / "falsified.jsonl"

# One rule is permanent enough to re-seed: raw edit XML is chat prose, never an executed
# tool. The journal is a runtime file, so a cleaned ~/.omp/run silently restores the
# failure this claim exists to prevent; a restart re-seeds it instead of re-learning it.
SEED_SUBJECT = "edit-xml-not-a-tool"
SEED_CLAIM = json.dumps({
    "subject": SEED_SUBJECT, "predicate": "is",
    "object": ("raw <function=edit>/<function=edit_file> XML is chat prose, never an "
               "executed tool: it bypasses stats.db and edits MUST go through Bash + "
               "edit_file.py --spec"),
    "referent": "AGENTS.md", "class": "permanent",
    "why": "operator mandated the path twice after a raw XML block shipped as text",
    "evidence": "session 2026-11-03: instruction repeated verbatim; sieve harness locks "
                "the header rule",
    "gate": "harness/sieve_harness.py", "seed": "rehydrate.py"}, ensure_ascii=False)


def falsification_ledger(journal=JOURNAL):
    """Print the disproved claims a restart must carry; re-seed the permanent one if lost.

    The count is of distinct claims, not lines: the journal is append-only, so the same
    disproof recorded twice is one falsification, not two.
    """
    entries = pre_compact_sieve.read_journal(journal)
    subjects = []
    for rec in entries:
        raw = str(rec.get("claim", "")).strip()
        try:
            subjects.append(str(json.loads(raw).get("subject", "")).strip() or raw[:60])
        except json.JSONDecodeError:
            subjects.append(raw[:60])
    print("\n== falsifications ==")
    print(f"  {len(subjects)} active claim(s) in {journal}")
    for s in subjects[:5]:
        print(f"  - {s}")
    if len(subjects) > 5:
        print(f"  ... {len(subjects) - 5} more")
    if SEED_SUBJECT in subjects:
        return entries
    try:
        pre_compact_sieve.record(journal, SEED_CLAIM, cmd="tools/rehydrate.py", rc=0)
    except OSError as exc:
        print(f"  MISSING {SEED_SUBJECT}: cannot write {journal}: {exc.strerror or exc}")
        return entries
    print(f"  RESEEDED {SEED_SUBJECT}: the journal had lost the permanent rule")
    return pre_compact_sieve.read_journal(journal)


def section(body, name):
    if name not in body:
        return ""
    rest = body.split(name, 1)[1]
    nxt = rest.find("\n## ")
    return (rest if nxt < 0 else rest[:nxt]).strip()


def head_lines(body):
    return [l for l in body.splitlines() if l.startswith("**")]


def resolve_project(spec):
    """A project named by path or bare name, tried against the spine home, the account home
    and the working directory. Only a directory holding a DAG.json can resume a plan.
    """
    p = pathlib.Path(str(spec)).expanduser()
    cands = ([p] if p.is_absolute() else
             [AGENT / p, pathlib.Path.home() / p, pathlib.Path.cwd() / p])
    for cand in cands:
        if (cand / "DAG.json").is_file():
            return cand.resolve()
    return None


def active_project_root(state):
    """The first ACTIVE spine project that carries a plan.

    STATE.md names projects both ways - an absolute tree and a bare `Projects/x/` - so every
    field of every ACTIVE line is resolved before the line is passed over.
    """
    for line in section(state, "## Active").splitlines():
        for field in re.split(r"[\s,;|]+", line):
            if not field.strip():
                continue
            found = resolve_project(field.strip().rstrip("/"))
            if found:
                return found
    return None


def spine_price_note(out=print):
    """Say out loud which cap STATE.md actually runs into.

    AGENTS.md and CORE.md are injected beside STATE.md every turn, so a spine whose two doctrine
    files cost 1852 of a 2500 tok closure leaves STATE.md 648 tok - not the 780 its own heading
    claims. A resumed session that reads only a project digest compacts STATE.md to 700 tok,
    satisfies the number it was told, and still fails the spine; the digest has to name who took
    the room and which file has to shed. Price is a char estimate (`len // 4`, marked ~): the
    recovery path must work with no tokenizer reachable, and an unmarked estimate is how a guess
    starts being quoted as a measurement.
    """
    try:
        import spine_check
    except ImportError as exc:            # a partial checkout still digests its own project
        out(f"  spine   price not computed: spine_check unreadable ({type(exc).__name__})")
        return
    closure = spine_check.injected_closure()
    texts = {}
    for name in closure:
        path = AGENT / name
        texts[name] = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
    est = {n: len(t) // 4 for n, t in texts.items()}
    state = est.get("STATE.md")
    if state is None:
        return
    price = sum(est.values())
    cap, _ideal, others, _room, _m = spine_check.state_budget(est, closure, spine_check.MAX_TOKENS)
    if cap < spine_check.STATE_MAX_TOKENS:
        shed = others - (spine_check.MAX_TOKENS - spine_check.STATE_MAX_TOKENS)
        out(f"  spine   ~{price} of the {spine_check.MAX_TOKENS} tok injected closure; AGENTS.md + "
            f"CORE.md cost ~{others}, so STATE.md's EFFECTIVE cap is ~{cap} tok, not the nominal "
            f"{spine_check.STATE_MAX_TOKENS} it holds ~{state} tok "
            f"({'OVER by' if state > cap else 'headroom'}) ~{abs(cap - state)} tok")
        if shed > 0:
            out(f"          the ceiling is the spine's, not STATE.md's: shed ~{shed} tok from "
                f"CORE.md/AGENTS.md to restore the nominal cap - no STATE.md cut can raise it")
    elif state > spine_check.STATE_IDEAL_TOKENS:
        out(f"  spine   ~{price}/{spine_check.MAX_TOKENS} tok; STATE.md ~{state} tok sits above the "
            f"{spine_check.STATE_IDEAL_TOKENS} tok compaction ideal (effective cap {cap} tok)")


def dag_summary(root, out=print):
    """One screen for one project: what is next, what proves it, does the tree still match.

    `plan_check --frontier` answers what every plan on the box needs. This answers the question
    a resumed session actually asks - what do I do now, in this project - and says out loud when
    the same gate has already failed LOOP_REPEAT times, which is the dead branch a session
    otherwise carries forward as if it were the plan.
    """
    root = pathlib.Path(root)
    plan_path = root / "DAG.json"
    out(f"\n== current work: {root.name} ==")
    spine_price_note(out)
    if not plan_path.is_file():
        out(f"  no DAG.json under {root}: this project has no plan to resume")
        return 1
    try:
        problems, notes, summary, _plan, nodes = plan_check.validate(plan_path)
    except (ValueError, KeyError, json.JSONDecodeError) as exc:
        out(f"  UNREADABLE plan {plan_path}: {exc}")
        return 1
    head, _hrc = plan_check.git(root, "rev-parse", "--short", "HEAD")
    branch, _brc = plan_check.git(root, "rev-parse", "--abbrev-ref", "HEAD")
    when, _wrc = plan_check.git(root, "log", "-1", "--format=%cd", "--date=short")
    dirty_raw, drc = plan_check.git(root, "status", "--porcelain")
    tree = f"{branch}@{head} ({when})" if head else "no git tree"
    dirty = (f"{len([l for l in dirty_raw.splitlines() if l.strip()])} uncommitted file(s)"
             if drc == 0 else "working-tree state unknown (git unreadable)")
    out(f"  tree  {tree}, {dirty}")
    out(f"  plan  {summary['done']}/{summary['total']} closed, {len(summary['ready'])} ready, "
        f"{len(summary['blocked'])} blocked, {len(summary['failed'])} failed, "
        f"{len(problems)} violation(s)")
    if not any(summary[k] for k in ("ready", "running", "failed", "blocked")):
        out(f"  done  all {summary['total']} nodes closed: no gate to run; the next "
            f"action is a record, not a gate")
    for nid in summary["running"]:
        out(f"  IN FLIGHT {nid} {nodes[nid].get('name', '')}")
    for nid in summary["failed"]:
        res = nodes[nid].get("gate_result") or {}
        out(f"  FAILED  {nid} {nodes[nid].get('name', '')}: exit {res.get('exit')} "
            f"at {res.get('at', '?')}")
        out(f"          gate: {res.get('cmd') or nodes[nid].get('gate') or '(none declared)'}")
        out(f"          receipt: {res.get('receipt') or '(no receipt filed)'}")
    for nid in summary["ready"]:
        node = nodes[nid]
        out(f"  next    {nid} {node.get('name', '')}")
        gate = str(node.get("gate") or "").strip()
        out("          gate: " + (gate if gate else "(none declared: nothing to run)"))
        out(f"          run:  {sys.executable} {AGENT / 'tools' / 'plan_check.py'} "
            f"--run-gate {nid} --plan {plan_path}")
        failing = [a for a in (node.get("gate_attempts") or []) if isinstance(a, dict)
                   and str(a.get("exit")) not in ("0", "", "None")]
        if failing:
            last = failing[-1]
            tail = str(last.get("output") or "").strip()[:120]
            out(f"          tried: {len(failing)} failing attempt(s), last exit "
                f"{last.get('exit')} at {last.get('at_iso') or last.get('at')} "
                f"@ {last.get('commit', '?')} -> {tail}")
        if len(failing) >= plan_check.LOOP_REPEAT:
            out(f"          DEAD BRANCH: this gate failed {len(failing)} times; write the lesson "
                f"to the falsification journal and change the approach before running it again")
    if summary["blocked"]:
        out(f"  wait    {len(summary['blocked'])} blocked until the frontier closes: "
            f"{', '.join(summary['blocked'][:8])}")
    stale = [n for n in notes if n.startswith("STALE")]
    first = f", first: {stale[0].split(':')[0]}" if stale else ""
    out(f"  fresh   {len(stale)} closed node(s) whose evidence no longer matches HEAD{first}")
    if problems:
        out(f"  FIX     {problems[0]}")
    return 0


def main():
    ap = argparse.ArgumentParser(description="OMP vNext rehydration")
    ap.add_argument("--check", action="store_true", help="run tools/spine_check.py first")
    ap.add_argument("--events", type=int, default=4, help="ledger entries to print")
    ap.add_argument("--dag", nargs="?", const="", metavar="PROJECT",
                    help="one project's work digest: closed/ready/next gate, the run command, "
                         "failing attempts, tree drift. No argument takes the first ACTIVE "
                         "project on STATE.md.")
    args = ap.parse_args()

    if args.dag is not None:
        target = (resolve_project(args.dag) if args.dag else
                  active_project_root(STATE.read_text(encoding="utf-8") if STATE.exists() else ""))
        if target is None:
            print("no project digest: " + (f"{args.dag}: no DAG.json under it" if args.dag else
                  "STATE.md names no ACTIVE project holding a DAG.json; pass --dag PROJECT"))
            return 1
        return dag_summary(target)

    if args.check:
        proc = subprocess.run([sys.executable, str(AGENT / "tools" / "spine_check.py")],
                              capture_output=True, text=True)
        print(proc.stdout.strip())
        if proc.returncode != 0:
            print("\nvalidator reports violations — fix those before trusting anything below")

    if not STATE.exists():
        print(f"missing {STATE}", file=sys.stderr)
        return 1
    state = STATE.read_text(encoding="utf-8")

    print("== active state ==")
    for line in head_lines(state):
        print(f"  {line}")
    for name in ("## Active", "## Next"):
        body = section(state, name)
        if body:
            print(f"\n{name}")
            print("\n".join(f"  {l}" for l in body.splitlines()))

    # the one question a restart asks first: what is this project doing right now
    active = active_project_root(state)
    if active:
        dag_summary(active)
    else:
        print("\n== current work ==\n  no ACTIVE project on STATE.md holds a DAG.json; "
              "run tools/rehydrate.py --dag PROJECT")

    falsification_ledger()

    print("\n== durable records ==")
    # one parser with the validator: every path the Recovery section names, resolved against
    # the spine home then the project roots, globs included.
    for target in spine_check.recovery_targets(section(state, "## Recovery")):
        mark = "ok " if spine_check.recovery_resolves(target) else "MISSING"
        print(f"  [{mark}] {target}")

    print("\n== recent ledger ==")
    if EVENTS.exists():
        entries = [e for e in re.split(r"\n(?=- )", EVENTS.read_text(encoding="utf-8"))
                   if e.startswith("- ")]
        for entry in entries[-args.events:]:
            first, *rest = entry.strip().splitlines()
            print(f"  {first}")
            for line in rest:
                print(f"    {line.strip()}")
    else:
        print("  EVENTS.md missing")

    print("\n== projects ==")
    seen = set()
    for m in ROOT_RE.finditer(state):
        root = pathlib.Path(m.group(0).rstrip("/"))
        if root in seen or not root.is_dir():
            seen.add(root)
            continue
        seen.add(root)
        print(f"  {root}")
        try:
            log = subprocess.run(["git", "log", "--oneline", "-3"], cwd=str(root),
                                 capture_output=True, text=True)
            if log.returncode == 0:
                for line in log.stdout.strip().splitlines():
                    print(f"    {line}")
            else:
                print("    not a git repo")
        except OSError:
            print("    git unavailable")
        harnesses = list(root.glob("harness/*")) + list(root.glob("tools/*.py"))
        for h in harnesses[:6]:
            print(f"    harness/tool: {h}")

    # The frontier comes from plan_check's own project list, not from whatever paths
    # STATE.md happens to name: a restart sees what is ready even if the prose does not.
    print("\n== plan frontier ==")
    proc = subprocess.run([sys.executable, str(AGENT / "tools" / "plan_check.py"), "--frontier"],
                          capture_output=True, text=True)
    for line in (proc.stdout + proc.stderr).strip().splitlines():
        print(f"  {line.strip()}")
    if proc.returncode:
        print("  plan violations: run tools/plan_check.py before trusting a project")

    # Who decides, read from the artifacts rather than remembered: a restart should
    # know which axes already left the previous session's head.
    print("\n== decision authority ==")
    proc = subprocess.run([sys.executable, str(AGENT / "tools" / "ownership.py"), "--quiet"],
                          capture_output=True, text=True)
    for line in (proc.stdout + proc.stderr).strip().splitlines():
        if line.startswith(("== ownership", "AUTHORITY", "transfer:", "UNREADABLE")):
            print(f"  {line.strip()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
