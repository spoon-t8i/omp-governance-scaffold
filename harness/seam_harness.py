#!/usr/bin/env python3
# spine-gate: --gate
"""seam_harness.py - the gate for spine_check.py's promotion seam (--check-seam).

Why: a STATE.md row is the spine's only bridge from a project's receipts to a decision, and the
scaffold's Law-2 rule is that a promoted measurement must be entailed by the receipt index it
cites. A row that reads "z_crit 7.392" while receipts.json carries 7.391 is a number nobody
measured, and a token a project falsified must not walk back in as fact. Neither shape is
visible from the gate's own --self-test, which grades the live spine and cannot manufacture a
fabricated numeral on demand. So this harness builds throwaway spines under a temp dir, points
SPINE_DIR at them, and runs the real CLI: intact row -> rc 0; unentailed numeral -> rc 1;
resurrected falsified token -> rc 1; the same token scoped by a falsification clause -> rc 0;
an archived receipt path -> a NOTE, never a violation.

Portable by construction: every path is under tempfile.mkdtemp(), no personal home path, and the
live ~/.omp/agent spine is never opened.
"""
import argparse
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile

GATE = pathlib.Path(__file__).resolve().parent.parent / "src" / "gates" / "spine_check.py"
RESULTS = []


def check(name, cond, detail):
    RESULTS.append((name, bool(cond), detail))


def spine_case(root, state_body, permrec=None, journal=None, receipts=True):
    """A throwaway spine: AGENT holds STATE.md, its parent holds run/falsified.jsonl, and the
    cited project holds run/receipts.json."""
    agent = root / "agent"
    agent.mkdir(parents=True, exist_ok=True)
    (agent / "STATE.md").write_text(state_body, encoding="utf-8")
    proj = root / "proj"
    (proj / "run").mkdir(parents=True, exist_ok=True)
    if receipts:
        (proj / "run" / "receipts.json").write_text(json.dumps({"rows": {
            "z_crit": {"value": "7.392"}, "locks": {"value": "63"}}}), encoding="utf-8")
    if permrec is not None:
        (proj / "perm-record.md").write_text(permrec, encoding="utf-8")
    if journal is not None:
        run = root / "run"
        run.mkdir(parents=True, exist_ok=True)
        (run / "falsified.jsonl").write_text(journal, encoding="utf-8")
    return agent


def run_seam(agent):
    import os
    env = dict(os.environ, SPINE_DIR=str(agent))
    proc = subprocess.run([sys.executable, str(GATE), "--check-seam"], cwd=str(GATE.parent),
                          env=env, capture_output=True, text=True, timeout=120)
    return proc.returncode, proc.stdout + proc.stderr


def main():
    ap = argparse.ArgumentParser(description="promotion-seam gate (spine_check.py --check-seam)")
    ap.add_argument("--gate", action="store_true", help="declared gate mode (no extra flags)")
    args = ap.parse_args()

    base = pathlib.Path(tempfile.mkdtemp(prefix="seam-harness-"))
    try:
        root = base / "a"
        root.mkdir()
        cite = str(root / "proj" / "run" / "receipts.json")

        # 1. intact: every measurement numeral is a value the cited index carries.
        agent = spine_case(root, f"# STATE\n\n## Active\n"
                                 f"- [x] demo@1a84d0f z_crit 7.392 of 63 locks; receipt {cite}\n")
        rc, out = run_seam(agent)
        check("S01 intact row exits 0", rc == 0, f"rc={rc} {out.strip()[:160]}")
        check("S02 intact row reports the judged rows",
              "seam: 1 row(s) bound" in out and "0 violation(s)" in out, out.strip()[:160])

        # 2. the numeral the receipts never carried: a measurement nobody measured.
        agent = spine_case(root, f"# STATE\n\n## Active\n"
                                 f"- [x] demo@1a84d0f z_crit 7.393 of 63 locks; receipt {cite}\n")
        rc, out = run_seam(agent)
        check("S03 unentailed numeral exits 1", rc == 1, f"rc={rc}")
        check("S04 it names the numeral and the index",
              "PROMOTION_SEAM_UNENTAILED" in out and "7.393" in out and "receipts.json" in out,
              out.strip()[:160])

        # 3. a token the project falsified, asserted as fact.
        falsified = "- \u00a79: the claim `z_crit 9.999` is FALSIFIED by the bytes.\n"
        agent = spine_case(root, f"# STATE\n\n## Active\n"
                                 f"- [x] demo@1a84d0f z_crit 9.999 of 63 locks; receipt {cite}\n",
                           permrec=falsified)
        rc, out = run_seam(agent)
        check("S05 resurrected token exits 1", rc == 1, f"rc={rc}")
        check("S06 it names the token and the project",
              "FALSIFIED_RESURRECTION" in out and "9.999" in out and "proj" in out,
              out.strip()[:160])

        # 4. the same token, honestly scoped: a falsification clause is not a resurrection.
        agent = spine_case(root, f"# STATE\n\n## Active\n- [x] demo@1a84d0f z_crit 9.999 is "
                                 f"FALSIFIED by the bytes; receipt {cite}\n", permrec=falsified)
        rc, out = run_seam(agent)
        check("S07 a scoped falsification stays clean", rc == 0, f"rc={rc} {out.strip()[:160]}")

        # 5. an archived receipt path is a NOTE about an unverifiable row, never a verdict.
        agent = spine_case(root, "# STATE\n\n## Active\n- [x] demo@1a84d0f z_crit 7.392 of 63 "
                                 "locks; receipt /nonexistent-archived/proj/run/receipts.json\n",
                           receipts=False)
        rc, out = run_seam(agent)
        check("S08 an absent receipt is skipped, not guessed",
              rc == 0 and "seam skipped" in out, f"rc={rc} {out.strip()[:160]}")

        # 6. the entailment journal is a second falsified-token source, scoped to its project.
        journal = json.dumps({"project": "proj",
                              "claim": json.dumps({"object": "R_plane 4.7862"})}) + "\n"
        agent = spine_case(root, f"# STATE\n\n## Active\n"
                                 f"- [x] demo@1a84d0f R_plane 4.7862 of 63 locks; receipt {cite}\n",
                           journal=journal)
        rc, out = run_seam(agent)
        check("S09 a journal-falsified token exits 1",
              rc == 1 and "FALSIFIED_RESURRECTION" in out and "4.7862" in out,
              f"rc={rc} {out.strip()[:160]}")
        # An identifier the project falsified, with every numeral in the row entailed: the only
        # thing wrong here is the resurrected name, so the id lane is graded on its own.
        idjournal = json.dumps({"project": "proj",
                                "claim": json.dumps({"object": "q042-3"})}) + "\n"
        agent = spine_case(root, f"# STATE\n\n## Active\n- [x] demo@1a84d0f q042-3 z_crit 7.392 "
                                 f"of 63 locks; receipt {cite}\n", journal=idjournal)
        rc, out = run_seam(agent)
        check("S11 a falsified id alone exits 1",
              rc == 1 and "FALSIFIED_RESURRECTION" in out and "q042-3" in out
              and "PROMOTION_SEAM_UNENTAILED" not in out, f"rc={rc} {out.strip()[:160]}")
        agent = spine_case(root, f"# STATE\n\n## Active\n"
                                 f"- [x] demo@1a84d0f z_crit 7.392 of 63 locks; receipt {cite}\n",
                           journal=journal)
        rc, out = run_seam(agent)
        check("S12 an untouched row is not guilty by journal", rc == 0,
              f"rc={rc} {out.strip()[:160]}")
    finally:
        shutil.rmtree(base, ignore_errors=True)

    bad = [r for r in RESULTS if not r[1]]
    for name, ok, detail in RESULTS:
        print(f"  {name:<44} {'PASS' if ok else 'FAIL':<5} {detail}")
    print(f"{'PASS' if not bad else 'FAIL'} {len(RESULTS) - len(bad)}/{len(RESULTS)} "
          f"promotion-seam checks ({GATE.name} --check-seam on fixture spines)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
