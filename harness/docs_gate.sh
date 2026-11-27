#!/usr/bin/env bash
# spine-gate: --gate
# docs_gate.sh - the generated pages and the llms-full.txt bundle can never rot.
#
# Why: docs/cfs-gate4.md and docs/omp-governance.md are the build artifact of
# src/tools/make_docs_pages.py over a receipts snapshot, index.html / projects.md /
# llms.txt carry regions that same generator rewrites, and docs/llms-full.txt is the
# build artifact of src/tools/bundle_docs.py over the pages. Every link rots silently:
# a numeral hand-edited into a generated page, a campaign re-run whose numbers never
# reached the page, a bundle still carrying an older body than the page it claims to
# contain. So this gate rebuilds into a scratch copy and refuses ANY difference -
# regenerating in place would overwrite the rot and certify nothing.
#
# Self-containment: the receipts document and the stale-pre-patch manifest the
# generator reads are vendored under harness/vendor/ (hashes pinned in
# harness/vendor/SHA256SUMS), so the gate runs with no ~/tmp at all. When the live
# campaign repo IS on disk the gate additionally asserts byte parity with it and
# proves it wrote nothing there - guppi_gate4 is read-only to this gate, always.
#
# Arms (causal exit contract, same as the guppi gates)
#   positive / --gate (rc=0)  : every check below passes and both mutant arms exit 1.
#   --mutant-stale-numeral (rc=1): perturb a generated numeral in a scratch copy; the
#       arm exits 1 only if the perturbation landed AND fresh-generation parity
#       rejects it AND the reference regeneration itself matches the committed page
#       (so the rejection is attributable to the perturbation, not to a broken
#       reference). A blind control exits 0, which the positive arm reads as failure.
#   --mutant-stale-bundle (rc=1) : append a line to theory.md - a bundled page the
#       generator never writes, so only the bundle check can see it - and require
#       bundle_docs.py --check to reject it with STALE.
#   --sync-vendor               : refresh the vendored snapshot from the live campaign
#       repo and re-pin SHA256SUMS. The only arm that writes under harness/, and the
#       legitimate repair path when the campaign re-runs.
# F9: the final line always carries "checks: N" on every arm.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 2
REPO="$(pwd)"
GEN=src/tools/make_docs_pages.py
BUND=src/tools/bundle_docs.py
DOCS=docs
VEND=harness/vendor/guppi_gate4/run
SUMS=harness/vendor/SHA256SUMS
LIVE=/home/snoop/tmp/guppi_gate4/run
N=0; PASS=0; FAIL=0
chk(){ N=$((N+1)); if [ "$2" = "$3" ]; then PASS=$((PASS+1)); printf 'PASS %s (%s)\n' "$1" "$3"; else FAIL=$((FAIL+1)); printf 'FAIL %s: want %s got %s\n' "$1" "$3" "$2"; fi; }
yes_(){ chk "$1" "$2" yes; }
rc(){ local r; "$@" >/dev/null 2>&1; r=$?; printf '%s' "$r"; }
sha(){ sha256sum "$1" 2>/dev/null | cut -d' ' -f1; }
yesrc(){ local r; "$@" >/dev/null 2>&1; r=$?; [ "$r" = 0 ] && printf yes || printf no; }
parses(){ yesrc python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "$1"; }
# tree_digest <dir>: content hash of every file, path-independent.
tree_digest(){ (cd "$1" && find . -type f | sort | xargs -d '\n' sha256sum | sha256sum) | cut -d' ' -f1; }
gen(){ python3 "$GEN" --receipts "$VEND/receipts.json" --docs "$1"; }

MODE=pos
case "${1:-}" in
  "")                     : ;;
  --gate)                 : ;;
  --mutant-stale-numeral) MODE=numeral ;;
  --mutant-stale-bundle)  MODE=bundle ;;
  --sync-vendor)          MODE=sync ;;
  *) echo "usage: $0 [--gate|--mutant-stale-numeral|--mutant-stale-bundle|--sync-vendor]"
     echo "passed: 0 failed: 1"; echo "checks: 1"; exit 2 ;;
esac

TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT

# ---------------------------------------------------------------- positive arm
if [ "$MODE" = pos ]; then
  # 1-2 the vendored snapshot is real JSON, not a truncated copy.
  yes_ 'vendored receipts.json parses'    "$([ -s "$VEND/receipts.json" ] && parses "$VEND/receipts.json")"
  yes_ 'vendored manifest.json parses'    "$([ -s "$VEND/stale_pre_patch/manifest.json" ] && parses "$VEND/stale_pre_patch/manifest.json")"
  # 3 the snapshot matches its own pinned hashes: self-contained without ~/tmp.
  yes_ 'vendored snapshot matches SHA256SUMS' "$( (cd "$VEND" && yesrc sha256sum -c --status "$REPO/$SUMS") )"
  # 4 live parity, when the campaign repo is on disk.
  LIVE_BEFORE=''
  if [ -d "$LIVE" ]; then
    LIVE_BEFORE="$(sha "$LIVE/receipts.json") $(sha "$LIVE/stale_pre_patch/manifest.json")"
    live_now="$(sha "$LIVE/receipts.json") $(sha "$LIVE/stale_pre_patch/manifest.json")"
    vend="$(sha "$VEND/receipts.json") $(sha "$VEND/stale_pre_patch/manifest.json")"
    yes_ 'live receipts snapshot == vendored snapshot' "$([ "$live_now" = "$vend" ] && printf yes || printf no)"
  else
    printf 'INFO live campaign repo absent: vendored snapshot is authoritative\n'
  fi
  # 5 the gate must not regenerate in place: committed docs byte-stable throughout.
  DOCS_BEFORE="$(tree_digest "$DOCS")"
  # 6 fresh-generation parity: committed docs already equal a fresh build.
  cp -a "$DOCS" "$TMP/a"
  gen "$TMP/a" >/dev/null || printf 'generator crashed\n'
  yes_ 'committed docs == fresh generation (byte-for-byte)' \
       "$([ -z "$(diff -r "$DOCS" "$TMP/a")" ] && printf yes || printf no)"
  # 7 two-pass idempotence: a second build changes nothing.
  cp -a "$TMP/a" "$TMP/pass1"
  gen "$TMP/a" >/dev/null || printf 'generator crashed\n'
  yes_ 'second generation pass is byte-identical' \
       "$([ -z "$(diff -r "$TMP/pass1" "$TMP/a")" ] && printf yes || printf no)"
  # 8 the bundle over the freshly generated pages is current. (No separate
  # "rebuilt bundle == committed bundle" check: the copy's bundle IS the committed
  # bundle, so that comparison could only fail if the generator wrote the bundle,
  # which it never does - a tautology, not a control.)
  yes_ 'bundle_docs --check current on fresh copy' "$(yesrc python3 "$BUND" --docs "$TMP/a" --check)"
  # 9 leak guard, independent of the generator's own assertion.
  yes_ 'generated pages carry no placeholder or null leak' \
       "$(yesrc python3 - "$TMP/a/cfs-gate4.md" "$TMP/a/omp-governance.md" <<'PY'
import pathlib, re, sys
bad = []
for p in sys.argv[1:]:
    t = pathlib.Path(p).read_text()
    for pat in (r'MISSING', r'\bNone\b', r'\bnan\b', r'\{[a-z_]+\}'):
        if re.search(pat, t):
            bad.append(f'{pathlib.Path(p).name}:{pat}')
sys.exit(1 if bad else 0)
PY
)"
  # 10 committed docs untouched by this run.
  yes_ 'gate wrote nothing under docs/' "$([ "$(tree_digest "$DOCS")" = "$DOCS_BEFORE" ] && printf yes || printf no)"
  # 11-13 both mutant arms must bite, and obey F9.
  for m in --mutant-stale-numeral --mutant-stale-bundle; do
    out="$TMP/${m#--}.out"; r=0; bash "harness/docs_gate.sh" "$m" >"$out" 2>&1 || r=$?
    chk "mutant arm $m rejects (rc=1)" "$r" 1
    chk "mutant arm $m emits a checks: line" \
        "$(awk 'END{print}' "$out" | grep -cE '^checks: [0-9]+$')" 1
  done
  # 14 the live campaign repo is byte-untouched by this run.
  if [ -n "$LIVE_BEFORE" ]; then
    yes_ 'guppi_gate4 snapshot untouched' \
         "$([ "$(sha "$LIVE/receipts.json") $(sha "$LIVE/stale_pre_patch/manifest.json")" = "$LIVE_BEFORE" ] && printf yes || printf no)"
  fi
fi

# ------------------------------------------------- mutant: stale generated numeral
if [ "$MODE" = numeral ]; then
  cp -a "$DOCS" "$TMP/m"; cp -a "$DOCS" "$TMP/r"
  gen "$TMP/r" >/dev/null
  before="$(sha "$TMP/m/cfs-gate4.md")"
  # Value-agnostic perturbation: bump the last digit of the first 4-decimal numeral
  # the generator emitted. Refuses to write a byte-identical file.
  python3 - "$TMP/m/cfs-gate4.md" <<'PY' || exit 2
import pathlib, re, sys
p = pathlib.Path(sys.argv[1]); t = p.read_text()
m = re.search(r'\d+\.\d{4}', t)
if not m:
    sys.exit('no generated numeral to perturb')
s = m.group(0)
p.write_text(t[:m.start()] + s[:-1] + str((int(s[-1]) + 1) % 10) + t[m.end():])
PY
  yes_ 'perturbation changed the page' \
       "$([ "$(sha "$TMP/m/cfs-gate4.md")" != "$before" ] && printf yes || printf no)"
  yes_ 'fresh-generation parity rejects the perturbed page' \
       "$([ -n "$(diff -q "$TMP/m/cfs-gate4.md" "$TMP/r/cfs-gate4.md" 2>&1)" ] && printf yes || printf no)"
  yes_ 'reference regeneration matches the committed page (control is sound)' \
       "$([ -z "$(diff -q "$TMP/r/cfs-gate4.md" "$DOCS/cfs-gate4.md")" ] && printf yes || printf no)"
fi

# ------------------------------------------------------ mutant: stale bundle body
if [ "$MODE" = bundle ]; then
  cp -a "$DOCS" "$TMP/m"
  before="$(sha "$TMP/m/theory.md")"
  printf '\nMutant arm: a hand-appended line the bundle must notice.\n' >> "$TMP/m/theory.md"
  yes_ 'perturbation changed a bundled page' \
       "$([ "$(sha "$TMP/m/theory.md")" != "$before" ] && printf yes || printf no)"
  out="$TMP/bundle.out"; r=0; python3 "$BUND" --docs "$TMP/m" --check >"$out" 2>&1 || r=$?
  yes_ 'bundle --check rejects the stale bundle with STALE' \
       "$([ "$r" = 1 ] && grep -q 'STALE' "$out" && printf yes || printf no)"
  yes_ 'bundle --check accepts the unperturbed copy (control is sound)' \
       "$(yesrc python3 "$BUND" --docs "$DOCS" --check)"
fi

# ------------------------------------------------------------- vendor maintenance
if [ "$MODE" = sync ]; then
  [ -d "$LIVE" ] || { echo "sync-vendor: no live campaign repo at $LIVE"; echo "passed: 0 failed: 1"; echo "checks: 1"; exit 1; }
  mkdir -p "$VEND/stale_pre_patch"
  cp "$LIVE/receipts.json" "$VEND/receipts.json"
  cp "$LIVE/stale_pre_patch/manifest.json" "$VEND/stale_pre_patch/manifest.json"
  ( cd "$VEND" && sha256sum receipts.json stale_pre_patch/manifest.json ) > "$SUMS"
  yes_ 'synced receipts.json parses'    "$(parses "$VEND/receipts.json")"
  yes_ 'synced manifest.json parses'    "$(parses "$VEND/stale_pre_patch/manifest.json")"
  yes_ 're-pinned SHA256SUMS verifies'  "$( (cd "$VEND" && yesrc sha256sum -c --status "$REPO/$SUMS") )"
fi

echo "passed: $PASS failed: $FAIL"
echo "checks: $N"
if [ "$MODE" = numeral ] || [ "$MODE" = bundle ]; then
  # A mutant arm exits 1 only when its control fired. A control that did not fire
  # exits 0: a blind control certifies a proof out of nothing, and the positive arm
  # reads rc=0 as "the negative arm did not fail" and refuses to pass.
  [ "$FAIL" -eq 0 ] && exit 1
  exit 0
fi
[ "$FAIL" -eq 0 ] || exit 1
exit 0
