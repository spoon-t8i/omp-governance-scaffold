#!/usr/bin/env python3
"""bundle_docs.py - regenerate docs/llms-full.txt from the docs pages on disk.

Why this exists: `llms-full.txt` is the single-file copy the agent-facing index points
at. Bodies appended to it by hand drift, and the drift is silent: the bundle keeps
asserting an older page body than the page it claims to contain. This rebuilds the whole
bundle from the pages and can run as a staleness gate.

Contract
  - every `docs/*.md` appears exactly once, in ORDER; a page missing from ORDER or a
    stale entry is a hard failure, never a silent skip;
  - section framing is byte-identical to the historical bundle: 80 `=` rules around a
    `FILE: docs/<name>` line, three blank lines after the header and between sections;
  - `--check` writes nothing and exits 1 when the bundle on disk differs from the
    rebuild, so a gate can catch an edited page with an un-regenerated bundle.

Modes
  bundle_docs.py [--docs DIR] [--check]
"""
import argparse
import pathlib
import sys

ORDER = ['architecture.md', 'compaction-sieve.md', 'attractors.md', 'workstation.md',
         'projects.md', 'theory.md', 'influences.md', 'omp-governance.md', 'cfs-gate4.md', 'chemgym-v2.md', 'cfs-g5.md']
SEP = '=' * 80
HEADER = '# OMP Governance Scaffold \u2014 Complete Documentation Bundle (llms-full.txt)\n\n\n'


def build(docs: pathlib.Path) -> str:
    present = sorted(p.name for p in docs.glob('*.md'))
    missing = [n for n in present if n not in ORDER]
    absent = [n for n in ORDER if n not in present]
    if missing or absent:
        sys.exit(f'bundle_docs: ORDER out of sync: unlisted={missing} missing={absent}')
    out = [HEADER]
    for name in ORDER:
        body = (docs / name).read_text().rstrip('\n')
        out.append(f'{SEP}\nFILE: docs/{name}\n{SEP}\n\n\n{body}\n\n\n')
    return ''.join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description='rebuild docs/llms-full.txt from docs/*.md')
    ap.add_argument('--docs', default=str(pathlib.Path(__file__).resolve().parents[2] / 'docs'))
    ap.add_argument('--check', action='store_true', help='fail instead of writing when stale')
    a = ap.parse_args()
    docs = pathlib.Path(a.docs)
    target = docs / 'llms-full.txt'
    new = build(docs)
    old = target.read_text() if target.exists() else ''
    if new == old:
        print(f'bundle_docs: current ({len(old)} bytes)')
        return 0
    if a.check:
        print(f'bundle_docs: STALE ({len(old)} bytes on disk, {len(new)} bytes rebuilt)')
        return 1
    target.write_text(new)
    print(f'bundle_docs: wrote {target} {len(new)} bytes')
    return 0


if __name__ == '__main__':
    sys.exit(main())
