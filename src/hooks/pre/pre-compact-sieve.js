// pre-compact-sieve — feed the summarizer what the transcript is about to lose.
//
// Compaction takes the file text out of the session and leaves the intention behind. The
// next `edit_file.py` call then re-emits an `old` block from memory. tools/pre_compact_sieve.py
// prints the small set of facts that must be re-read or re-verified instead of recalled; this
// hook runs it against the session's repo, prints the same block to disk so it survives the
// summary, and puts it in front of the model twice: once for the summarizer (`session.compacting`
// `context`) and once as the first thing the model itself sees after the boundary.
//
// Since 2026-11-23 the sieve block may end with a transcript-derived guard block, run through
// OMP_SIEVE_SESSION_CWD: [ACTIVE-DIRECTIVE] (latest TASK verbatim) + [PRIOR-TASKS-SUPERSEDED],
// [MUTATION-POSTURE]/[LOCKED-PATHS] (read-only never flips to write-approved),
// [PINNED-ANCHORS]/[PINNED-FLAGS] (hyphen-exact --flags), [SPIN-LOOP-TRIPWIRE]. Absent
// transcript, absent block; a fired block rides into the armed event even on a clean repo.
//
// The second one is the whole point, and it is not free. Measured on 18.14.1 (2026-11-02): the
// returned `context` was persisted in the compaction entry's `preserveData` and the next model turn
// had none of it - asked to name the paths and hashes, it said no such block was in its context.
// Re-measured on 18.18.2 (2026-11-09, forced rollover, local model, three boundary events): the
// regime that fires is `experimental-context-rollover`, a deterministic boundary whose own record
// says "Do not assume this boundary is an LLM summary". There is no summarizer request, so nothing
// renders the returned `context`: it reached the record (`preserveData.sieve`, 776 chars) and no
// prompt. Only the `context` event reaches the model - armed at the boundary, spent 3 ms later, and
// the model, asked what was in its context, recited this block's section list. `session.compacting`
// still returns `context`, because a summary-based regime would render it; delivery is the event.
// Re-confirmed on 18.18.3 (2026-11-09, natural rollover under the patched binary, PID 2583840):
// armed at the boundary and spent 106 ms later, and the block arrived in the next turn's context
// (`boundary=rollover`, 1148 chars). `.3` touches no compaction or hook path, so the 18.18.2
// finding stands unmodified.
//
// Fail-open by construction: compaction must never be blocked, delayed past a hard timeout,
// or made worse by this hook. Every path out is either the sieve text or `undefined`, and a
// sieve that fails says so in ~/.omp/run/pre-compact-sieve.log rather than looking clean.
//
// Event contract (omp hooks): `session.compacting` may return
//   { context?: string[], prompt?: string, preserveData?: Record<string, unknown> }
// `context` entries are injected into the summarizer prompt as <additional-context>. This hook
// never returns `prompt`: the base summarizer prompt stays the runtime's. `context` (the event)
// receives the conversation messages of an outgoing request and may return `{messages}`; it fires on
// every request, so the injection is gated on the armed flag and spent the first time it is taken.

import { execFile } from "node:child_process";
import { existsSync, statSync, mkdirSync, appendFileSync, writeFileSync, readFileSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, join } from "node:path";

const HOME = homedir();
const SIEVE = join(HOME, ".omp", "agent", "tools", "pre_compact_sieve.py");
const LOG = join(HOME, ".omp", "run", "pre-compact-sieve.log");
const OUT_DIR = join(HOME, ".omp", "run", "sieve");
// Budget for the block the `context` event injects. 220 predates the lane floors and
// measurement showed what it bought: on the live spine the block at 220 shed [EVICTED_FILE]
// to one row of 41 -- three copies under ~/.omp/run/sieve on 2026-11-09, each carrying
// `evicted 21->1 ... to fit 220 tokens`. At 300 the evict lane keeps every row its cap allows
// and no lane empties, because a lane stops at its floor (lane_floors in the tool) instead of
// draining to zero. ~200 tokens on the one request whose prompt cache a rollover already cost.
// Measured on 18.18.2 and re-measured on 18.18.3 (2026-11-09, delivered receipt:
// `contracts 38->2 evicted 21->1 falsified 29->1 working 4->1 to fit 300 tokens`). Re-measure on
// upgrade rather than trusting the estimate. This 300 is the hook's: the tool's own default is 200,
// so a hand-run sieve without --max-tokens reports `to fit 200 tokens` for the same repo.
const MAX_TOKENS = Number(process.env.OMP_SIEVE_MAX_TOKENS || 300);
const TIMEOUT_MS = Number(process.env.OMP_SIEVE_TIMEOUT_MS || 12_000);

// Files a frozen session must not re-guess in a multi-gate project: the plan, the frozen
// decisions, the frozen runner. Guarded by existence, so this is inert in any other repo.
const WATCH_IF_PRESENT = ["DAG.json", "MULTIGATE_PROMPT.md", "run/gates.mjs"];

function note(line) {
	try {
		mkdirSync(dirname(LOG), { recursive: true });
		appendFileSync(LOG, `${new Date().toISOString()} ${line}\n`, "utf8");
	} catch {
		/* logging must never be the reason a compaction fails */
	}
}

// Scope is the tool's decision, not this file's: --resolve-repo applies the one
// precedence (OMP_SIEVE_REPO > git cwd > operator pin > STATE.md Active > cwd) and
// writes the audit line itself. A hook-local copy of that order is exactly how the
// 2026-11-05 /tmp-scoped sieve happened: the hook trusted ctx.cwd, the session was
// never standing in its repo, and every source went missing from its own boundary.
function resolveSieveRepo(cwd) {
	return new Promise(resolve => {
		execFile(
			"python3",
			[SIEVE, "--resolve-repo", "--repo", cwd],
			{ encoding: "utf8", timeout: TIMEOUT_MS, maxBuffer: 1 << 16 },
			(err, stdout) => {
				if (err) {
					resolve({ error: String((err.killed || err.signal ? `timeout ${TIMEOUT_MS}ms` : err.message) || err) });
					return;
				}
				const line = (stdout || "").trim();
				const m = line.match(/^(\S+)\s+source=(\S+)\s+redirected=(\S+)$/);
				if (!m) {
					resolve({ error: `unparsable resolver line: ${line.slice(0, 120)}` });
					return;
				}
				let isDir = false;
				try {
					isDir = statSync(m[1]).isDirectory();
				} catch {
					isDir = false;
				}
				resolve({ repo: m[1], source: m[2], redirected: m[3], isDir });
			},
		);
	});
}

function runSieve(repo, source, sessionCwd) {
	const args = [SIEVE, "--repo", repo, "--max-tokens", String(MAX_TOKENS)];
	const env = { ...process.env };
	if (source) env.OMP_SIEVE_RESOLVED = source;
	// the sieve reads the session transcript itself; the hook only names the cwd whose
	// slug owns the JSONL, so session_telemetry's resolver stays the only slug rule
	if (sessionCwd) env.OMP_SIEVE_SESSION_CWD = sessionCwd;
	for (const rel of WATCH_IF_PRESENT) {
		if (existsSync(join(repo, rel))) args.push("--watch", rel);
	}
	return new Promise(resolve => {
		execFile(
			"python3",
			args,
			{ encoding: "utf8", timeout: TIMEOUT_MS, maxBuffer: 1 << 20, env },
			(err, stdout, stderr) => {
				if (err) {
					// execFile reports a timeout as killed+signal, not as an error code
					const why = err.killed || err.signal ? `timeout ${TIMEOUT_MS}ms` : String(err.message);
					resolve({ error: `${why} ${(stderr || "").trim().slice(0, 200)}` });
					return;
				}
				resolve({ text: (stdout || "").trim() });
			},
		);
	});
}

// Compaction-seam laws, emitted deterministically at every boundary. H-DEAD-11/H-DEAD-19:
// twice now the turn right after snap-compacted re-emitted an `edit`/`write` call from memory of
// a file the boundary had evicted, and the XML/dunder payload corrupted the call. The rules are
// static text, so they cost nothing and fail nothing; the two live probes (in-flight python3
// PIDs, open DAG nodes) degrade to omission, never to a blocked compaction.
const POST_COMPACT_LAWS = [
  "[POST-COMPACT-LAW-1]: NEVER call 'edit' or 'write' tools (H-DEAD-11/19: post-compaction " +
    "XML/dunder corruption). Mutate files ONLY via 'bash' + 'python3'.",
  "[POST-COMPACT-LAW-2]: NO EOS STALL: Do NOT end your first turn after compaction with " +
    "prose-only plan announcements. Pair your thought immediately with an executable 'bash' tool call.",
];
function pythonPids() {
  return new Promise(resolve => {
    execFile("pgrep", ["-af", "python3"], { encoding: "utf8", timeout: 2000, maxBuffer: 1 << 16 },
      (err, stdout) => {
        if (err) { resolve(null); return; }
        const all = (stdout || "").split("\n").map(s => s.trim()).filter(Boolean)
          .filter(s => !s.includes("pgrep") && !s.includes("pre-compact-sieve"));
        const rows = all.filter(s => !s.includes("pre_compact_sieve.py")).slice(0, 6);
        resolve(rows.length
          ? rows.join("\n") + (all.length > rows.length ? `\n(+${all.length - rows.length} more python3 procs)` : "")
          : null);
      });
  });
}
function openDagNodes(repo) {
  try {
    const f = join(repo, "DAG.json");
    if (!existsSync(f)) return null;
    const d = JSON.parse(readFileSync(f, "utf8"));
    const nodes = Array.isArray(d && d.nodes) ? d.nodes : [];
    const open = nodes.filter(n => n && !/^(done|closed)$/i.test(String(n.status || "")))
      .map(n => n.id);
    return open.length ? open.slice(0, 40).join(", ") : null;
  } catch {
    return null;
  }
}
// L2A -> L2B handoff, at the same boundary. Everything above is feedforward: the sieve, the seam
// laws and the open-node list fire because compaction happened, and none of them says whether
// this repo's own receipts are intact. surprise_check.py is the feedback half - it sleeps at zero
// token cost during normal work and wakes only when a receipt disagrees with the disk (a cited SHA
// no repo resolves, an evidence path that is gone, a gate stamped green on a tree the script has
// outgrown, a launcher repeated with no pass between). Silence costs nothing; a finding rides the
// same armed block as a <=120-token interrupt, so the first post-boundary turn repairs the receipt
// instead of re-gating from text the boundary just evicted.
// Bounded and fail-open on every path: the probe is priced by the char estimator (--token-url "")
// so a compaction never waits on a port, at most 2 blocks are injected (the rest stay in the log
// and in a full re-run), and a timeout, a missing tool or an unusable policy logs one line and
// injects nothing. A governance probe must never be able to stall a compaction.
const SURPRISE = join(HOME, ".omp", "agent", "tools", "surprise_check.py");
const SURPRISE_MS = Number(process.env.OMP_SURPRISE_TIMEOUT_MS ?? 8000);
function surpriseAudit(repo, sessionCwd) {
	return new Promise(resolve => {
		if (!existsSync(SURPRISE) || process.env.OMP_SURPRISE === "0") {
			resolve(null);
			return;
		}
		// The loop lane reads a stream the telemetry writer produces from the live transcript.
		// Ask for a refresh first, aimed at the session being compacted: compaction is precisely
		// the moment the transcript moves past what any earlier refresh saw, so without it the
		// boundary would audit a loop state frozen at the last scan. --telemetry-cwd is that
		// session's cwd, not the repo's: the transcript follows the session, the gates follow the
		// repo, and the writer takes both. OMP_SURPRISE_NO_REFRESH=1 scans the stream as it stands.
		const args = [SURPRISE, "--repo", repo, "--quiet", "--token-url", "", "--max-blocks", "2"];
		if (sessionCwd) args.push("--telemetry-cwd", sessionCwd);
		if (process.env.OMP_SURPRISE_NO_REFRESH === "1") args.push("--no-refresh-telemetry");
		execFile(
			"python3",
			args,
			{ encoding: "utf8", timeout: SURPRISE_MS, maxBuffer: 1 << 20 },
			(err, stdout) => {
				if (!err) {
					resolve(null); // exit 0: dormant, nothing to say
					return;
				}
				if (err.killed || err.signal) {
					note(`L2B skipped repo=${repo} timeout ${SURPRISE_MS}ms`);
					resolve(null);
					return;
				}
				if (err.code !== 1) {
					// exit 2 (or any signal) is the probe's own fault: log it, stay silent, fail open
					note(`L2B unusable repo=${repo} exit=${err.code} ${(String(err.stderr || "")).trim().slice(0, 160)}`);
					resolve(null);
					return;
				}
				const text = (stdout || "").trim();
				resolve(text.includes("[L2B-SURPRISE-INTERRUPT") ? text : null);
			},
		);
	});
}

async function lawBlock(repo, sessionCwd) {
  // The header rides inside the block, not only in the boundary ack: the copy that survives
  // into the new context window is the delivered one, and a law whose author is unstated is
  // advice.
  const lines = [SEAM_LAW_HEADER, ...POST_COMPACT_LAWS];
  try {
    const pids = await pythonPids();
    if (pids) lines.push(
      "[IN-FLIGHT-PYTHON-PIDS]: these python3 processes are running now:\n" + pids +
      "\nDo NOT patch imported .py modules on disk while a campaign PID is running in RAM.");
  } catch { /* probe is optional; the laws ride regardless */ }
  try {
    const open = openDagNodes(repo);
    if (open) lines.push("[OPEN-DAG-NODES]: non-done node ids in " + join(repo, "DAG.json") +
      ": " + open + " - these are the exact remaining gates; re-gate them, do not recall them.");
  } catch { /* same */ }
  try {
    const audit = await surpriseAudit(repo, sessionCwd);
    if (audit) lines.push(
      "[L2A->L2B HANDOFF]: the boundary's feedforward checks passed but this repo's own receipts " +
        "are broken. Fix the receipt named in each block BEFORE re-gating from recalled text; " +
        "python3 " + join(HOME, ".omp", "agent", "tools", "surprise_check.py") + " lists all of them.\n" +
        audit);
  } catch { /* same */ }
  return lines.join("\n");
}

// Armed by session.compacting, spent by the first `context` request after the boundary. In-process
// state on purpose: the rollover and its continuation share this process, and a stale block is worse
// than no block, so a take is one-shot and time-bounded (OMP_SIEVE_ARM_TTL_MS, default 10 minutes).
const SEAM_LAW_HEADER = "POST-COMPACT SEAM LAWS - system-authored, apply to your very next turn; " +
	"they outrank your recollection of any file listed by them.";
function lawsOnlyArm(laws, repo) {
	armed = { text: laws, at: Date.now(), file: "" };
	note(`armed laws-only repo=${repo} chars=${laws.length}`);
}
let armed = null;
const ARM_TTL_MS = Number(process.env.OMP_SIEVE_ARM_TTL_MS ?? 600_000);

// The `context` event carries no discriminator: the emit site in the 18.18.2 binary builds exactly
// `{ type: "context", messages }` for every outgoing request, so the request that consumes the block
// is named from its own contents and logged, never guessed at. Measured on 18.18.2: the first request
// after arming arrives 3-4 ms later (79 ms in the 2026-11-08 audit) carrying the post-rollover
// history, and it IS the model's turn. Two earlier markers were dead on that account: `post` looked
// for a history entry typed `compaction`, which the deterministic rollover never writes (it leaves a
// <context-window-rollover> text block instead), and the `mark` test looked for SIEVE_HEADER, a
// string this hook only ever puts in the `session.compacting` return - never in the injected block -
// so it could not fire on a request that genuinely carried the block. Delivery is therefore
// unconditional; classification earns its keep by naming what the log sees - `boundary=` which
// marker the history held, `echo=` whether an injected block is already riding it (a provider retry,
// which must be served rather than starved).
const SIEVE_HEADER = "PRE-COMPACTION SIEVE";
const SIEVE_MARK = "System-authored compaction context";
const ROLLOVER_TAG = "<context-window-rollover>";
const SCAN_CAP_CHARS = 600_000;
function classifyRequest(messages) {
  const out = { n: 0, chars: 0, boundary: null, echo: false };
  try {
    const list = Array.isArray(messages) ? messages : [];
    out.n = list.length;
    for (const m of list) {
      if (m && !out.boundary && (m.type === "compaction" || m.type === "compaction_summary")) {
        out.boundary = m.type;
      }
      let s = "";
      try {
        s = typeof m === "string" ? m : JSON.stringify(m) || "";
      } catch {
        s = "";
      }
      out.chars += s.length;
      if (!out.boundary && s.indexOf(ROLLOVER_TAG) >= 0) out.boundary = "rollover";
      if (!out.echo && s.indexOf(SIEVE_MARK) >= 0) out.echo = true;
      if (out.chars > SCAN_CAP_CHARS) break;
    }
  } catch {
    /* unreadable request: classify as unknown, keep the delivery path */
  }
  return out;
}

export default function preCompactSieve(pi) {
	// Discovery proof: one line per session that actually loaded this factory through
	// <agentDir>/hooks/pre ambient discovery. Without it a hook that was never wired looks
	// exactly like a hook that fired and had nothing to say.
	note(`loaded pid=${process.pid} cwd=${process.cwd()}`);

	// Delivery: the armed sieve rides the first request after the boundary, bracketed so the model
	// can see it is system-authored rather than something the user typed.
	// Costs that one request its prompt cache, which a rollover has already lost. Fail-open like
	// every other path here.
	pi.on("context", event => {
		try {
			if (!armed) return undefined;
			const age = Date.now() - armed.at;
			if (age > ARM_TTL_MS) {
				note(`dropped: armed ${age}ms ago, past ${ARM_TTL_MS}ms`);
				armed = null;
				return undefined;
			}
			const cls = classifyRequest(event && event.messages);
			const block = armed.text;
			const copy = armed.file || "";
			armed = null;
			const messages = (event && event.messages) || [];
			note(`delivered: sieve injected into the next request, chars=${block.length} age=${age}ms n=${cls.n} reqChars=${cls.chars} boundary=${cls.boundary || "none"} echo=${cls.echo ? 1 : 0}`);
			return {
				messages: messages.concat([
					{
						role: "user",
						content: [
							{
								type: "text",
								// The on-disk path and the re-run command ride inside the block: the budget
								// sheds lanes to their floors, so a `[BUDGET_SPILL] ... ->N` line has to name
								// where the rest lives or the shed reads as a deletion.
								text:
									"<pre-compact-sieve>\n" +
									block +
									(copy ? "\nThe shed above is a budget, not the whole finding: read the" +
										" on-disk copy at " + copy + ", or re-run tools/pre_compact_sieve.py" +
										" at the --max-tokens figure its [BUDGET_SPILL] line names.\n" : "") +
									"\nSystem-authored compaction context: this list was measured at the boundary by" +
									" tools/pre_compact_sieve.py, not summarized from the transcript. Trust it over" +
									" your own recollection of the file contents.\n</pre-compact-sieve>",
							},
						],
					},
				]),
			};
		} catch (err) {
			note(`skipped context injection: ${(err && err.message) || err}`);
			return undefined;
		}
	});
	pi.on("session.compacting", async (_event, ctx) => {
		let text = "";
		let repo = "";
		try {
			const cwd = (ctx && ctx.cwd) || process.cwd();
			if (!existsSync(SIEVE)) {
				note(`skipped: no sieve at ${SIEVE}`);
				return undefined;
			}
			// fail open even here: if the resolver itself is sick, sieve the old way
			// (session cwd) and say so - an honest cwd-scoped block beats no block.
			const res = await resolveSieveRepo(cwd);
			let source = null;
			if (res.error || !res.isDir) {
				note(`repo-resolution-failed cwd=${cwd}: ${(res.error || `not a directory: ${res.repo}`).slice(0, 160)} - falling back to cwd`);
				repo = cwd;
			} else {
				repo = res.repo;
				source = res.source;
				if (res.redirected !== "none") {
					note(`redirected cwd=${cwd} -> repo=${repo} source=${res.source}`);
				}
			}
			let laws = "";
			try { laws = await lawBlock(repo, cwd); } catch (err) { note(`laws failed: ${(err && err.message) || err}`); }
			const ran = await runSieve(repo, source, cwd);
			if (ran.error) {
				note(`sieve-failed repo=${repo} source=${source || "cwd-fallback"}: ${ran.error} - arming laws only`);
				if (laws) { lawsOnlyArm(laws, repo); }
				return laws ? { context: [laws] } : undefined;
			}
			text = ran.text;
			// a clean repo is not a clean directive: a fired guard block must ride into
			// the armed context even when the working set itself held nothing to re-read
			if (!text || (text.includes("[NOTHING_TO_SIEVE]") && !text.includes("[ACTIVE-DIRECTIVE]"))) {
				// the sources were read and came back clean: the sieve lane has nothing to re-read,
				// but the seam laws still apply to the next turn, so they arm alone.
				note(`sieve-clean repo=${repo}: arming laws only`);
				if (laws) { lawsOnlyArm(laws, repo); }
				return laws ? { context: [laws] } : undefined;
			}
			if (laws) text = `${text}

${laws}`;
		} catch (err) {
			note(`skipped: ${(err && err.message) || err}`);
			return undefined;
		}

		const stamp = new Date().toISOString().replace(/[:.]/g, "");
		const file = join(OUT_DIR, `${stamp}-${(repo.split("/").filter(Boolean).pop() || "repo")}.md`);
		try {
			mkdirSync(OUT_DIR, { recursive: true });
			writeFileSync(file, `${text}\n`, "utf8");
			armed = { text, at: Date.now(), file };
			note(`armed repo=${repo} chars=${text.length} file=${file}`);
			return {
				context: [
					SIEVE_HEADER + " — machine-generated by tools/pre_compact_sieve.py from the repo, " +
						"git, memory.db and the falsified-claim journal, not from this transcript. Carry every " +
						"path, sha256, exit code and number below into the summary verbatim; name the on-disk " +
						"copy so the next session can re-run the sieve or read it. Do not paraphrase a path or " +
						"a hash, and never write an edit_file.py `old` block for a file listed under " +
						"[EVICTED_FILE] until it has been read again.",
					`on-disk copy: ${file}`,
					text,
				],
				preserveData: { sieve: text, repo, copy: file },
			};
		} catch (err) {
			note(`injected without on-disk copy repo=${repo}: ${(err && err.message) || err}`);
			return { context: [`pre-compaction sieve (on-disk copy failed): ${text}`] };
		}
	});

}
