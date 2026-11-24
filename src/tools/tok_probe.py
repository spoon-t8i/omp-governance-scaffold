#!/usr/bin/env python3
"""Re-derive the spine token and prefill-latency claims against the live server.

This is the referent for the cache claims in STATE.md and
notes/2026-10-11_boundary-map-state-vs-capability.md: run it and the numbers
in those files are checkable instead of remembered.

Modes
  (no flags)          snapshot: /metrics totals + recent requests (reused, hit_rate)
  --spine v3|vnext    build an injected-closure text from the spine files
  --files F1 F2 ...   explicit file list instead
  --once              one call per text (default is two: pass1, then pass2 repeat)

What it reports per call: prompt tokens, tokens reused from prefix cache,
prefill ms, wall seconds. pass1 vs pass2 on the same text is the reuse claim;
pass1 is genuinely cold only when that exact text has never been sent.
"""

import argparse
import json
import pathlib
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:8080"
AGENT = pathlib.Path(__file__).resolve().parent.parent
V3_ARCHIVE = AGENT / "archive" / "omp_v3_spine_2026-10-11"
VNEXT_FILES = ["AGENTS.md", "CORE.md", "STATE.md"]


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=30) as r:
        return json.loads(r.read().decode())


def post_chat(prompt, max_tokens=1):
    body = json.dumps({
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0,
        "stream": False,
    }).encode()
    req = urllib.request.Request(
        BASE + "/v1/chat/completions", data=body,
        headers={"Content-Type": "application/json"})
    t0 = time.monotonic()
    with urllib.request.urlopen(req, timeout=600) as r:
        out = json.loads(r.read().decode())
    return out, time.monotonic() - t0


def closure_files(kind):
    """Injected closure for a spine generation, following @file includes."""
    root = V3_ARCHIVE if kind == "v3" else AGENT
    seeds = ["AGENTS.md"] if kind == "v3" else VNEXT_FILES
    seen, order = set(), []

    def add(name):
        if name in seen:
            return
        seen.add(name)
        order.append(name)
        path = root / name
        if not path.exists():
            return
        for line in path.read_text(errors="replace").splitlines()[:12]:
            if line.startswith("@") and line[1:].strip().endswith(".md"):
                add(line[1:].strip())

    for name in seeds:
        add(name)
    return root, order


def snapshot():
    m = get("/metrics")
    t = m["totals"]
    print(f"model: {m['engine']['model']}  version {m['engine']['version']}")
    print(f"totals since {t['since']}: requests {t['requests']}  "
          f"prompt {t['prompt_tokens']}  reused {t['reused']}  "
          f"output {t['output_tokens']}")
    cc = m.get("conversation_cache", {})
    print(f"conversation_cache: enabled={cc.get('enabled')} slots={cc.get('slots')} "
          f"requests={cc.get('requests')} reused={cc.get('reused_tokens')}")
    print("last requests:")
    for r in m.get("requests", [])[:8]:  # newest-first
        print(f"  {r.get('prompt_tokens'):>6} tok  reused {str(r.get('reused')):>6}  "
              f"hit {str(r.get('hit_rate')):>5}  prefill {r.get('prompt_ms'):>7} ms  "
              f"{r.get('time')}")


def probe(texts, twice):
    for label, text in texts:
        calls = 2 if twice else 1
        for n in range(calls):
            before = get("/metrics")["totals"]
            out, wall = post_chat(text)
            m = get("/metrics")
            after = m["totals"]
            u = out.get("usage", {}) or {}
            prompt = u.get("prompt_tokens")
            reused_delta = (after.get("reused", 0) or 0) - (before.get("reused", 0) or 0)
            prompt_delta = (after.get("prompt_tokens", 0) or 0) - (before.get("prompt_tokens", 0) or 0)
            hit = (reused_delta / prompt_delta) if prompt_delta else 0.0
            # the server's own record for this call (usage has no prefill ms);
            # match on prompt size so an interleaved request is not mistaken for ours
            # /metrics "requests" is newest-first
            rec = next((r for r in m.get("requests", [])
                        if r.get("prompt_tokens") == prompt), {})
            # pass 1 is cold only if this exact text has not been sent before;
            # the live server may already hold it (the injected spine is sent every turn).
            kind = "pass2" if n else "pass1"
            print(f"{label:8s} {kind:4s}  prompt {prompt or prompt_delta:>6} tok  "
                  f"reused {reused_delta:>6}  reuse {hit:5.1%}  "
                  f"prefill {rec.get('prompt_ms', 0):>8.1f} ms  "
                  f"server_hit {str(rec.get('hit_rate')):>5}  wall {wall:6.2f} s")


def main():
    ap = argparse.ArgumentParser(description="OMP spine token/prefill probe")
    ap.add_argument("--spine", choices=["v3", "vnext"])
    ap.add_argument("--files", nargs="*")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--max-tokens", type=int, default=1)
    args = ap.parse_args()

    if not args.spine and not args.files:
        snapshot()
        return 0

    texts = []
    if args.spine:
        root, order = closure_files(args.spine)
        body = "\n".join((root / n).read_text(errors="replace") for n in order)
        print(f"{args.spine} closure: {', '.join(order)}  ({len(body)} chars)")
        texts.append((args.spine, body))
    if args.files:
        body = "\n".join(pathlib.Path(f).read_text(errors="replace") for f in args.files)
        texts.append(("files", body))

    probe(texts, twice=not args.once)
    return 0


if __name__ == "__main__":
    sys.exit(main())
