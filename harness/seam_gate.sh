#!/usr/bin/env bash
# spine-gate: --gate
# seam_gate.sh - the promotion seam (spine_check.py --check-seam) grades what it claims to.
#
# Why: a spine row promoted from a project's run/receipts.json is the one place a number
# leaves a campaign and becomes operative law. --check-seam grades every measurement numeral
# in such a row against the receipt index it cites, and refuses a token the project
# falsified (run/falsified.jsonl, plus backtick spans on a falsification line of the
# project's perm-record). Both halves are silent by construction: a row whose numeral is
# absent from the receipt, or a falsified token re-asserted outside its falsification
# clause, must exit 1 - and a row that is honest must exit 0, including the two shapes that
# look like violations and are not (an archived receipt path, a receipt with no `rows`
# index). Nothing in the repo exercises that boundary, so this gate builds a throwaway spine
# under $TMPDIR and grades it.
#
# Portability: the whole fixture is built under mktemp -d and addressed through SPINE_DIR;
# no personal home path, no live campaign repo, no network.
#
# Arms (causal exit contract, same as docs_gate.sh)
#   positive / --gate (rc=0)     : every fixture check below passes and both mutant arms
#                                  exit 1 for the stated reason.
#   --mutant-unentailed (rc=1)   : a row whose numeral the receipt does not carry.
#   --mutant-resurrect (rc=1)    : a falsified token asserted outside any falsification clause.
# F9: the final line always carries "checks: N" on every arm.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 2
REPO="$(pwd)"
GATE=src/gates/spine_check.py
N=0; PASS=0; FAIL=0
chk(){ N=$((N+1)); if [ "$2" = "$3" ]; then PASS=$((PASS+1)); printf 'PASS %s (%s)\n' "$1" "$3"; else FAIL=$((FAIL+1)); printf 'FAIL %s: want %s got %s\n' "$1" "$3" "$2"; fi; }
yes_(){ chk "$1" "$2" yes; }
has(){ grep -qF -- "$1" "$OUT" && printf yes || printf no; }
rc(){ local r; "$@" >/dev/null 2>&1; r=$?; printf '%s' "$r"; }

MODE=pos
case "${1:-}" in
  "")                     : ;;
  --gate)                 : ;;
  --mutant-unentailed)    MODE=unentailed ;;
  --mutant-resurrect)     MODE=resurrect ;;
  *) echo "usage: $0 [--gate|--mutant-unentailed|--mutant-resurrect]"
     echo "passed: 0 failed: 1"; echo "checks: 1"; exit 2 ;;
esac

TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
SPINE="$TMP/spine"; PROJ="$TMP/proj"
mkdir -p "$SPINE" "$PROJ/run" "$TMP/run"
OUT="$TMP/out"

# The receipt index the rows cite: three values, one of them a count.
cat > "$PROJ/run/receipts.json" <<'JSON'
{"rows": {"z_crit": {"value": "7.392"}, "drift": {"value": "116.7043"},
          "locks": {"value": "63"}}}
JSON

# The entailment journal: z_crit 9.9999 is a measurement this project falsified.
printf '%s\n' '{"project": "proj", "claim": "{\"object\": \"z_crit 9.9999\"}"}' \
  > "$TMP/run/falsified.jsonl"

# The perm-record falsification: a second, independent harvest path for the same rule.
printf '%s\n' '- FALSIFIED: `drift 55.5` - the bytes do not carry it' > "$PROJ/perm-record.md"

# seam_run: write one STATE.md row and grade it; echoes rc, leaves the report in $OUT.
seam_run(){ printf '%s\n' "$1" > "$SPINE/STATE.md"
            SPINE_DIR="$SPINE" python3 "$GATE" --check-seam > "$OUT" 2>&1; printf '%s' "$?"; }

ROW_OK="- [x] seam fixture: z_crit 7.392, drift 116.7043, 63 locks - receipt $PROJ/run/receipts.json"
ROW_BAD="- [x] seam fixture: z_crit 7.393, drift 116.7043, 63 locks - receipt $PROJ/run/receipts.json"
ROW_RES="- [x] seam fixture: z_crit 9.9999, drift 116.7043, 63 locks - receipt $PROJ/run/receipts.json"
ROW_SCOPED="- [x] seam fixture: z_crit 7.392, drift 116.7043, 63 locks; z_crit 9.9999 is FALSIFIED by the bytes - receipt $PROJ/run/receipts.json"
ROW_PERM="- [x] seam fixture: drift 55.5, z_crit 7.392, 63 locks - receipt $PROJ/run/receipts.json"
ROW_GONE="- [x] seam fixture: z_crit 7.392 - receipt $TMP/gone/run/receipts.json"

# ---------------------------------------------------------------- mutant arms
if [ "$MODE" = unentailed ]; then
  r="$(seam_run "$ROW_BAD")"
  [ "$r" = 1 ] && [ "$(has PROMOTION_SEAM_UNENTAILED)" = yes ] && exit 1
  exit 0
fi
if [ "$MODE" = resurrect ]; then
  r="$(seam_run "$ROW_RES")"
  [ "$r" = 1 ] && [ "$(has FALSIFIED_RESURRECTION)" = yes ] && exit 1
  exit 0
fi

# ---------------------------------------------------------------- positive arm
# 1 the honest row is entailed by the receipt index it cites.
chk 'intact receipt-bound row exits 0'        "$(seam_run "$ROW_OK")" 0
# 2-3 a numeral the index does not carry is a fabricated measurement.
chk 'unentailed numeral exits 1'              "$(seam_run "$ROW_BAD")" 1
yes_  'unentailed numeral names the row'      "$(has PROMOTION_SEAM_UNENTAILED)"
# 4-5 a journal-falsified token asserted plainly is a resurrection.
chk 'falsified token exits 1'                 "$(seam_run "$ROW_RES")" 1
yes_  'falsified token names the row'         "$(has FALSIFIED_RESURRECTION)"
# 6 the same token inside its own falsification clause is honest prose, not a claim.
chk 'falsified token in a falsification clause exits 0' "$(seam_run "$ROW_SCOPED")" 0
# 7 the perm-record is a second falsification source, not just the journal.
chk 'perm-record falsified token exits 1'     "$(seam_run "$ROW_PERM")" 1
# 8-9 an archived receipt path is skipped with a note, never guessed into a violation.
chk 'absent receipt path exits 0'             "$(seam_run "$ROW_GONE")" 0
yes_  'absent receipt path says it skipped'   "$(has 'absent on disk')"
# 10 a receipt with no `rows` index carries no claims to grade.
mv "$PROJ/run/receipts.json" "$TMP/keep.json"
printf '%s\n' '{"summary": "no rows map here"}' > "$PROJ/run/receipts.json"
chk 'receipt with no rows index exits 0'      "$(seam_run "$ROW_OK")" 0
mv "$TMP/keep.json" "$PROJ/run/receipts.json"
# 11 no citation, no seam: a row that cites nothing cannot be graded and must not fire.
chk 'row citing no receipt exits 0'           "$(seam_run '- [x] seam fixture: z_crit 9.9999 only')" 0

# 12-15 the gate CLI contract: --help works, an unknown flag is a usage error.
chk 'spine_check --help exits 0'              "$(rc python3 "$GATE" --help)" 0
chk 'spine_check unknown flag exits 2'        "$(rc python3 "$GATE" --check-seam --not-a-flag)" 2
chk 'surprise_check --help exits 0'           "$(rc python3 src/gates/surprise_check.py --help)" 0
chk 'surprise_check unknown flag exits 2'     "$(rc python3 src/gates/surprise_check.py --not-a-flag)" 2

# 16-17 both mutants fire for the stated reason, not by accident.
"$0" --mutant-unentailed > /dev/null 2>&1; chk 'mutant arm unentailed exits 1' "$?" 1
"$0" --mutant-resurrect  > /dev/null 2>&1; chk 'mutant arm resurrect exits 1'  "$?" 1
# 18 the unknown arm is a usage error.
"$0" --not-an-arm > /dev/null 2>&1; chk 'unknown arm exits 2' "$?" 2

printf 'passed: %s failed: %s\n' "$PASS" "$FAIL"
printf 'checks: %s\n' "$N"
[ "$FAIL" = 0 ] || exit 1
exit 0
