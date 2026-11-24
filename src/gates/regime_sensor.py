#!/usr/bin/env python3
"""S1 regime sensor (VS-10, measurement_governance.md section 1).

Derives the identity of this box - model | engine | gpu-set - from live observables
and publishes it as the only legal regime pin. Read-only, stateless, opinion-free:
it stores no judgement about "good regime", so it can be rerun-or-dead but never
wrong-while-passing. The hardcoded IQ3_XXS constant this replaces sat in
fact_store.py pinning a dead regime while the spine stayed green (FG-7); a value
nobody can type by hand dies the moment the box changes.

Observables (each named in the emitted JSON, derivation never falls back to a
literal):
  model   : the id the tokenizer/model server actually serves (:8080 /v1/models)
  engine  : "$STRATA_BUILD --version" - the binary's own report, per strata_update.sh
  gpu-set : "Nx<model>" from `nvidia-smi -L`, multiple models joined with '+'

Outputs:
  run/sensors/S1.pin   the identity line alone (LF-terminated); byte-stable across
                       pulls while the box is unchanged - CR-5 self-test is equality
                       of this line across three consecutive derivations
  run/sensors/S1.json  same line plus as_of, loadavg_1m, per-component detail
  archive/gates/regime_events.jsonl
                       REGIME_CHANGED(old, new, change-class) appended on diff;
                       the strata rebuild path (VS-13) is the intended consumer -
                       this sensor only emits, it never demotes anything

Exit: 0 ok | 2 a named observable could not be read (never a silent default).
"""
import argparse
import json
import os
import pathlib
import re
import subprocess
import sys
import time
import urllib.request

AGENT = pathlib.Path(os.environ.get("OMP_AGENT_ROOT") or pathlib.Path(__file__).resolve().parent.parent)
PIN_PATH = AGENT / "run" / "sensors" / "S1.pin"
JSON_PATH = AGENT / "run" / "sensors" / "S1.json"
EVENTS_PATH = AGENT / "archive" / "gates" / "regime_events.jsonl"
MODEL_URL = os.environ.get("OMP_MODEL_URL", "http://127.0.0.1:8080/v1/models")
def _default_strata_bin() -> str:
    root = pathlib.Path(os.environ.get("STRATA_DIR", "/mnt/data/strata-workspace/Strata"))
    for rel in ("terminal/engine/strata", "terminal/engine-cuda12/strata",
                "engine/strata", "engine-cuda12/strata"):
        cand = root / rel
        if cand.is_file():
            return str(cand)
    return str(root / "terminal/engine/strata")


STRATA_BIN = os.environ.get("STRATA_BUILD") or _default_strata_bin()


class ObservableError(RuntimeError):
    pass


def derive_model():
    try:
        with urllib.request.urlopen(MODEL_URL, timeout=10) as r:
            return json.load(r)["data"][0]["id"]
    except Exception as e:
        raise ObservableError(f"model server at {MODEL_URL}: {e}")


def derive_engine():
    try:
        out = subprocess.run([STRATA_BIN, "--version"], capture_output=True,
                             text=True, timeout=15).stdout
    except Exception as e:
        raise ObservableError(f"engine binary {STRATA_BIN}: {e}")
    m = re.search(r"(\S+)\s+v?([0-9][0-9a-z.\-]*)", out)
    if not m:
        raise ObservableError(f"engine version unparseable from {STRATA_BIN}: {out!r}")
    return f"{m.group(1)}-{m.group(2)}"


def derive_gpus():
    try:
        out = subprocess.run(["nvidia-smi", "-L"], capture_output=True,
                             text=True, timeout=15).stdout
    except Exception as e:
        raise ObservableError(f"nvidia-smi -L: {e}")
    models = []
    for line in out.splitlines():
        m = re.match(r"GPU \d+: (.+?) \(UUID", line.strip())
        if m:
            models.append("".join(m.group(1).split()[-2:]))
    if not models:
        raise ObservableError(f"no GPUs parsed from nvidia-smi -L: {out!r}")
    counts = {}
    for g in models:
        counts[g] = counts.get(g, 0) + 1
    return "+".join(f"{n}x{g}" for g, n in sorted(counts.items()))


def derive():
    model, engine, gpus = derive_model(), derive_engine(), derive_gpus()
    return "|".join([model, engine, gpus]), {"model": model, "engine": engine,
                                             "gpu_set": gpus}


def fields_of(line):
    parts = line.split("|")
    return dict(zip(("model", "engine", "gpu_set"), parts + ["?"] * (3 - len(parts))))


def changed_fields(old, new):
    of, nf = fields_of(old), fields_of(new)
    return sorted(k for k in of if of[k] != nf[k])


def main():
    ap = argparse.ArgumentParser(description="S1 regime sensor: derive the box identity line")
    ap.add_argument("--print", dest="print_only", action="store_true",
                    help="print the derived line, write nothing")
    ap.add_argument("--self-test", action="store_true",
                    help="CR-5: three derivations byte-equal, and equal to the published pin")
    ap.add_argument("--check", action="store_true",
                    help="compare derived line to the published pin; write nothing "
                         "(VS-13: rc 0 match, rc 1 REGIME_CHANGED or no pin)")
    ap.add_argument("--publish", action="store_true",
                    help="write S1.pin + S1.json, append the REGIME_CHANGED event on diff "
                         "(this is also the no-flag default)")
    args = ap.parse_args()

    if args.print_only:
        print(derive()[0])
        return 0

    if args.check:
        try:
            line, _ = derive()
        except ObservableError as e:
            print(f"S1 FAIL: {e}", file=sys.stderr)
            return 2
        old_pin = (PIN_PATH.read_text(encoding="utf-8").strip()
                   if PIN_PATH.exists() else None)
        if old_pin is None:
            print(f"REGIME_CHANGED none -> {line} (no pin published)")
            return 1
        if old_pin == line:
            print(f"no change ({line})")
            return 0
        print(f"REGIME_CHANGED {old_pin} -> {line} "
              f"[{','.join(changed_fields(old_pin, line)) or 'format'}]")
        return 1

    if args.self_test:
        lines = [derive()[0].encode() for _ in range(3)]
        equal = lines[0] == lines[1] == lines[2]
        pinned = PIN_PATH.read_bytes() if PIN_PATH.exists() else None
        matches_pin = pinned is None or pinned == lines[0] + b"\n"
        print(json.dumps({"cr5_three_pulls_byte_equal": equal,
                          "three_pulls_distinct_bytes": len(set(lines)),
                          "matches_published_pin": matches_pin, "line": lines[0].decode()}))
        return 0 if equal and matches_pin else 2

    try:
        line, detail = derive()
    except ObservableError as e:
        print(f"S1 FAIL: {e}", file=sys.stderr)
        return 2

    load = open("/proc/loadavg").read().split()
    old = PIN_PATH.read_text(encoding="utf-8").strip() if PIN_PATH.exists() else None
    PIN_PATH.parent.mkdir(parents=True, exist_ok=True)
    PIN_PATH.write_text(line + "\n", encoding="utf-8")
    JSON_PATH.write_text(json.dumps({
        "sensor": "S1 regime", "line": line, "components": detail,
        "as_of": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "loadavg_1m": float(load[0]),
        "observables": {"model_url": MODEL_URL, "engine_bin": STRATA_BIN,
                        "gpu_probe": "nvidia-smi -L"},
    }, indent=1) + "\n", encoding="utf-8")

    if old and old != line:
        changed = changed_fields(old, line)
        event = {"event": "REGIME_CHANGED", "old": old, "new": line,
                 "change_class": ",".join(changed) or "format",
                 "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                 "consumer": "VS-13 rebuild hook (run: fact_store --live-pin '<new>' --check-regime)"}
        EVENTS_PATH.parent.mkdir(parents=True, exist_ok=True)
        with EVENTS_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event) + "\n")
        print("REGIME_CHANGED " + json.dumps(event))
    print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
