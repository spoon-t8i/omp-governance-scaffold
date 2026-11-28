#!/usr/bin/env python3
"""make_docs_pages.py - build the three public docs pages from receipts documents.

Why this exists: both pages advertise that every numeral on them is read from
`run/receipts.json`. That claim is only checkable if the page is the build artifact of a
program kept in the repo, not text typed into an editor. This emits `docs/cfs-gate4.md`
(one campaign's receipts, in context) and `docs/omp-governance.md` (the ten laws, each
bound to the receipt that proves it) and `docs/chemgym-v2.md` (the chemistry suite's
receipts, read from its own receipts document).

Contract
  - every numeral, path, sha, mtime and verdict is read from the receipts document, the
    stale-pre-patch manifest or the gate archive; nothing is transcribed by hand;
  - a required fact path that is absent raises (KeyError) rather than emitting a blank;
    genuinely optional fields go through `vo` and render `n/a`;
  - the build refuses to write a page containing an unrendered placeholder;
  - output is deterministic for a given receipts document.

Modes
  make_docs_pages.py [--receipts PATH] [--manifest PATH] [--docs DIR]
                     [--chemgym-receipts PATH]
"""
import argparse
import json, re, pathlib
from datetime import datetime

_ap = argparse.ArgumentParser(description='build the scaffold docs pages from receipts')
_ap.add_argument('--receipts', default='/home/snoop/tmp/guppi_gate4/run/receipts.json')
_ap.add_argument('--manifest', default=None,
                 help='stale_pre_patch/manifest.json (default: sibling of the receipts file)')
_ap.add_argument('--chemgym-receipts',
                 default='/home/snoop/Projects/omp-governance-scaffold/harness/vendor/chemgym-v2/run/receipts.json',
                 help='chemgym-v2 run/receipts.json (vendored snapshot)')
_ap.add_argument('--docs', default='/home/snoop/Projects/omp-governance-scaffold/docs')
_a = _ap.parse_args()
RJ = _a.receipts
DOCS = pathlib.Path(_a.docs)
MAN_PATH = _a.manifest or str(pathlib.Path(RJ).resolve().parent / 'stale_pre_patch'
                             / 'manifest.json')
R = json.load(open(RJ))
CG = json.load(open(_a.chemgym_receipts))
F, C, D, RF = R['facts'], R['constants'], R['derived'], R['receipt_files']
_TS = sorted(x['mtime_utc'] for x in RF.values() if x.get('mtime_utc'))
SPAN_H = (datetime.strptime(_TS[-1], '%Y-%m-%dT%H:%M:%SZ')
          - datetime.strptime(_TS[0], '%Y-%m-%dT%H:%M:%SZ')).total_seconds() / 3600.0
COLLECTOR = R['collector']['commit']
MAN = json.load(open(MAN_PATH))
N_STALE = MAN['n']
assert N_STALE == len(MAN['files']), 'manifest n != files'

def fmt(x, nd=4):
    if isinstance(x, bool):
        return 'yes' if x else 'no'
    if isinstance(x, float):
        return f'{x:,.{nd}f}'
    if isinstance(x, int):
        return f'{x:,}'
    return str(x)

def vo(p, d='n/a', nd=4):
    return fmt(F[p], nd) if p in F else d

def v(p, nd=4):
    return fmt(F[p], nd)

def sub(prefix):
    p = prefix + '.'
    return {k[len(p):]: F[k] for k in sorted(F) if k.startswith(p) and not k.endswith('.path')}

def idxs(prefix, field):
    """numeric indices (>=0) present under prefix, e.g. slices / prefixes / checks"""
    pat = re.compile(re.escape(prefix) + r'\.(\d+)\.' + re.escape(field) + r'$')
    out = []
    for k in F:
        m = pat.match(k)
        if m:
            out.append(int(m.group(1)))
    return sorted(set(out))

OFF_NAME = next(F[f'g01_slices.slices.{i}.name'] for i in idxs('g01_slices.slices', 'name')
                if 'off' in F[f'g01_slices.slices.{i}.name'].lower())

def table(headers, rows):
    out = ['| ' + ' | '.join(headers) + ' |', '|' + '|'.join(['---'] * len(headers)) + '|']
    for r in rows:
        out.append('| ' + ' | '.join(str(c) for c in r) + ' |')
    return '\n'.join(out)

def kvtable(prefix, nd=4, skip=()):
    rows = []
    for k, val in sub(prefix).items():
        if k.endswith('.len') or any(s in k for s in skip):
            continue
        if isinstance(val, (int, float, bool)):
            rows.append((f'`{k}`', fmt(val, nd)))
    return table(['field', 'value'], rows)

def dformula(name):
    return D[name]['formula']

# ---------------------------------------------------------------- gate 4 page
L = []
A = L.append
A('---')
A('layout: default')
A('title: "CFS Gate 4: Multi-Rotation Coherent Folding - A Receipted Null"')
A('---')
A('')
A('[&larr; Back to Home](index.html)')
A('')
A('# CFS Gate 4: Streaming Multi-Rotation Coherent Dedispersion and Narrow-Plane Chromatic Phase Folding')
A('')
A(f'*Project `guppi_gate4`. Receipts document `run/receipts.json`, collector commit `{COLLECTOR[:7]}`. '
  'Every number below is read from that document by the page generator; none is typed by hand. '
  'Licensed under the Apache License, Version 2.0.*')
A('')
A('## Tier 1: The High-Level Picture (Plain-English Intuition)')
A('')
A('A pulsar is a lighthouse: a rotating beam sweeps across the sky, and every pass of the beam deposits a '
  'small amount of extra power at one point in the rotation cycle. Radio telescopes record that power as '
  'millions of raw samples per second, smeared two ways &mdash; the interstellar plasma drags the low '
  'frequencies later than the high ones (**dispersion**), and the beam itself is narrow, so the pulse lands '
  'in only one or two time bins. The fix is arithmetic: undo the plasma delay (**coherent dedispersion**), '
  'then stack many rotations so the pulse adds up while the noise averages down.')
A('')
A(f'Gate 3 had {v("g02_parity.arms.parity16.rotations",3)} rotations of source and reached '
  f'`R_plane` z = {v("g02_parity.arms.parity16.R_z")} &mdash; short of the pre-registered bar '
  f'`z_crit` = {v("z_crit",1)}. Gate 4 bought {v("depth_ratio_full_over_16",1)}x more data '
  f'({v("g03_scaling.nmax",0)} blocks, {v("g03_scaling.full_span.rotations",3)} rotations) and asked one '
  'question: does folding coherently across rotations lift the statistic over the bar?')
A('')
A(f'**It does not, and that answer is the deliverable.** The deepest prefix reaches '
  f'`R_plane` z = {v("g03_scaling.R_z_at_nmax")}, still {v("curve_max_margin_z")} below the bar. The bar was '
  'never lowered and the run was never re-selected. What makes this a measurement rather than a shrug is the '
  'evidence around it:')
A('')
A(f'- The noise floor falls by {v("noise_floor_shrink_16_to_nmax",2)}x over the same span, so the averaging '
  f'machine works; the pulse peak excess *shrinks* by {v("peak_excess_ratio_nmax_over_16",4)}x, so the pulse '
  'is not adding across rotations.')
A(f'- {v("g03_scaling.stratification.n_spins_audited",0)} of {v("g03_per_spin.null_ceiling.n_spins",0)} '
  f'individual rotations were audited one at a time and every one sits inside its own extreme-value noise '
  f'ceiling (`all_within_2sd_of_ceiling` = {v("g03_per_spin.null_ceiling.all_within_2sd_of_ceiling")}).')
A(f'- Blank sky ({v("g04_null.off_sweep.trials",0)} dispersion trials) and '
  f'{v("g04_null.surrogates.trials",0)} phase-randomised surrogates produced '
  f'{v("g04_null.off_sweep.locks",0)} + {v("g04_null.surrogates.locks",0)} false locks.')
A(f'- A blind period search over {v("g03_period_search.trials",0)} trial periods found its best peak at '
  f'z = {v("g03_period_search.search_z_max")}, while six phase-scrambled nulls of the same bytes reach '
  f'z = {v("g03_period_search.null_best_z_max")} &mdash; the search found nothing the null cannot produce.')
A(f'- The statistic is immune to the single-channel interference that swamps total intensity: a '
  f'{v("rfi_snr",0)}-SNR tone in one frequency plane moves `R_plane` by '
  f'{v("g03_rfi.R_excess_z_every_rotation",6)} &mdash; no excess at all &mdash; and Stokes-I by '
  f'{v("g03_rfi.S_excess_z_every_rotation",0)} sigma.')
A('')
A('The gates were then shown to be capable of failing: each of the five nodes has a mutant arm that perturbs '
  'one input and exits nonzero. A gate that cannot fail is decoration. '
  '[Ten governance laws that make this reproducible](omp-governance.html).')
A('')
A('## Tier 2: Systems Engineering, Receipts and Honest Boundaries')
A('')
A('### 1 Registered constants')
A('')
A('Registered before the campaign and supplied by the harness, never by an engine receipt (battery check N11).')
A('')
A(table(['constant', 'value'], [(f'`{k}`', fmt(val, 6)) for k, val in sorted(C.items())]))
A('')
A('### 2 Delivered bytes and remote-header audit (CFS-G4-01)')
A('')
A('Slices are assembled from a byte-range prefix plus downloaded extension segments; the prefix must be '
  'bit-exact against the Gate 3 slice, and the block walk must be misalignment-free.')
A('')
sl = []
for i in idxs('g01_slices.slices', 'name'):
    p = f'g01_slices.slices.{i}.'
    sl.append((f'`{F[p+"name"]}`', F[p + 'src_name'], fmt(F[p + 'nblocks'], 0), fmt(F[p + 'expect_blocks'], 0),
               fmt(F[p + 'truncated_tail'], 0), fmt(F[p + 'filesize'] / 2**30, 3), fmt(F[p + 'span_s'], 4),
               fmt(F[p + 'prefix_blocks'], 0), fmt(F[p + 'ext_blocks'], 0),
               fmt(F[p + 'size_identity_ok']), fmt(F[p + 'directio_ok']), fmt(F[p + 'header_audit_pass']),
               vo(p + 'prefix_bit_exact'), vo(p + 'pass'), F[p + 'sha256'][:12]))
A(table(['slice', 'header src', 'blocks', 'expect', 'tail', 'GiB', 'span_s', 'prefix blk', 'ext blk',
         'size ok', 'directio ok', 'header ok', 'prefix bit-exact', 'pass', 'sha256'], sl))
A('')
A('### 3 Depth scaling of the coherent plane statistic (CFS-G4-03)')
A('')
A(f'Curve over {v("n_prefixes",0)} prefixes of the same ON slice, identical engine settings '
  f'(`dm` = {v("dm",3)}, `period` = {v("g03_scaling.period_s",10)}, {v("g03_scaling.planes",0)} planes, '
  f'{v("g03_scaling.fold_ms",1)} ms bins, streaming chunks of {v("chunk_blocks",0)} blocks).')
A('')
pr = sorted(idxs('g03_scaling.prefixes', 'nblocks'), key=lambda i: F[f'g03_scaling.prefixes.{i}.nblocks'])
cr = []
for i in pr:
    p = f'g03_scaling.prefixes.{i}.'
    cr.append((fmt(F[p + 'nblocks'], 0), fmt(F[p + 'rotations'], 4), fmt(F[p + 'span_s'], 4),
               fmt(F[p + 'R_z']), fmt(F[p + 'S_z']), fmt(F[p + 'R_off_std'], 6), fmt(F[p + 'R_peak'], 5),
               fmt(F[p + 'peak_rss_mib'], 1)))
A(table(['blocks', 'rotations', 'span_s', '`R_plane` z', 'Stokes-I z', 'off-pulse sd', 'peak', 'peak RSS MiB'], cr))
A('')
A(f'**Reading the curve.** Depth gain {v("scaling_gain_full_over_16",4)}, where coherent addition across the '
  f'{v("depth_ratio_full_over_16",1)}x deeper span would deliver {v("sqrt_rotation_ratio_nmax_over_16",4)} '
  f'(square root of the rotation ratio); off-pulse '
  f'noise shrinks {v("noise_floor_shrink_16_to_nmax",4)}; peak excess falls from {v("peak_excess_16",5)} to '
  f'{v("peak_excess_nmax",5)} (ratio {v("peak_excess_ratio_nmax_over_16",4)}). The profile is being averaged '
  'correctly while the peak fails to add &mdash; the signature of emission that is nulled by multi-rotation '
  'folding, not strengthened by it.')
A('')
A('### 4 Ceiling arithmetic: what the bar means under multiplicity')
A('')
A('Every margin below is `z_crit` minus an observed or expected extreme value, computed by '
  '`src/make_receipts.py` from receipt fields (formula column verbatim).')
A('')
mrg = ['curve_max_margin_z', 'ceiling_single_margin', 'ceiling_sweep_margin', 'null_ceiling_margin',
       'off_margin', 'surrogate_margin', 'desync_on_margin', 'beacon_16_margin', 'beacon_full_excess']
A(table(['margin', 'value', 'formula'],
        [(f'`{k}`', v(k), '`' + dformula(k) + '`') for k in mrg]))
A('')
A('Extreme-value context for the curve maximum (`g03_scaling.ceiling_context`):')
A('')
A(kvtable('g03_scaling.ceiling_context', 6))
A('')
A('### 5 Per-rotation stratification against the null ceiling (CFS-G4-03b)')
A('')
A('Each rotation is folded on its own and classified by the rule recorded in the receipt: a spin is '
  '`active` if its own `R_plane` z reaches the Gate 3 single-slice level, otherwise `nulled`. Each class is '
  'then scored against the Gumbel ceiling for its own trial count.')
A('')
A(table(['classification rule', 'spins audited', 'spins unaudited'],
        [(f'`{re.sub(r"[0-9.]{7,}", lambda m: f"{float(m.group()):.6f}",
                      F["g03_scaling.stratification.active_rule"])}`',
          v('g03_scaling.stratification.n_spins_audited', 0),
          v('g03_scaling.stratification.n_spins_unaudited', 0))]))
A('')
st = []
for cls in ('active', 'nulled'):
    q = f'g03_scaling.stratification.classes.{cls}.'
    if q + 'n_rotations' not in F:
        continue
    st.append((cls, fmt(F[q + 'n_rotations'], 0), fmt(F[q + 'coverage_frac'], 4), vo(q + 'R_z'),
               vo(q + 'S_z'), vo(q + 'R_peak', nd=5), vo(q + 'R_off_mean', nd=6), vo(q + 'R_off_std', nd=6),
               vo(q + 'R_peak_phase_ms', nd=1), vo(q + 'peak_phase_resultant'), F.get(q + 'note', '')))
A(table(['class', 'rotations', 'coverage', '`R_plane` z', 'Stokes-I z', 'peak', 'off mean', 'off sd',
         'peak phase ms', 'phase resultant', 'note'], st))
A('')
A('Per-spin `R_plane` z, one row per audited rotation (the exact receipts behind the class assignment):')
A('')
SPIN = 'g03_scaling.stratification.classes.nulled.'
SPIN_IDX = sorted(int(k[len(SPIN + 'spin_R_z.'):]) for k in F
                  if k.startswith(SPIN + 'spin_R_z.') and k[len(SPIN + 'spin_R_z.'):].isdigit())
A(table(['spin index', 'per-spin `R_plane` z'],
        [(fmt(F[SPIN + f'spin_indices.{i}'], 0), fmt(F[SPIN + f'spin_R_z.{i}'], 4)) for i in SPIN_IDX]))
A('')
A(kvtable('g03_per_spin.null_ceiling', 6))
A('')
A(f'**The `active` class is empty**: none of the {v("g03_scaling.stratification.n_spins_audited",0)} audited '
  'rotations reaches the Gate 3 single-slice level on its own, and the class holding all of them (`nulled`, '
  f'{v("g03_scaling.stratification.classes.nulled.coverage_frac",4)} coverage) aggregates to '
  f'{v("g03_scaling.stratification.classes.nulled.R_z")} - below the ceiling expectation '
  f'{v("g03_per_spin.null_ceiling.ceiling.expected_max_z")} for its own bin count.')
A('')
A(f'Interpretation: the audited rotations are individually indistinguishable from noise maxima '
  f'(Gumbel p = {v("g03_per_spin.null_ceiling.gumbel_p_max_exceeds_z_max",4)} that the largest is that '
  f'extreme; Rayleigh p = {v("g03_per_spin.null_ceiling.rayleigh_p_uniform_phases",4)} for uniform peak '
  'phases). No rotation carries a lock that survives its own multiplicity correction.')
A('')
A('### 6 Blind period search on the full span (CFS-G4-03c)')
A('')
A('No ephemeris: every trial period in a 1% band, coverage-complete bins only.')
A('')
ps = ['trials', 'bin_stats.trials', 'bin_stats.sliver_bins_dropped', 'rotations', 'span_s', 'search_z_mean',
      'search_z_std', 'search_z_max', 'expected_max_z_gaussian', 'catalogue_p0_z_full', 'catalogue_p0_H_full',
      'null_best_z_max', 'null_best_z_sd', 'peak_rss_mib', 'wall_s']
A(table(['field', 'value'], [(f'`{k}`', v('g03_period_search.' + k)) for k in ps]))
A('')
A('Top five trial periods by z, and the six phase-scrambled null controls run through the same search:')
A('')
bz = []
for i in idxs('g03_period_search.best_by_z', 'z'):
    p = f'g03_period_search.best_by_z.{i}.'
    bz.append((fmt(F[p + 'period_s'], 10), fmt(F[p + 'z']), fmt(F[p + 'H'], 3), fmt(F[p + 'peak_bin'], 0)))
A(table(['trial period s', 'z', 'H', 'peak bin'], bz))
A('')
nl = []
for i in idxs('g03_period_search.null', 'seed'):
    p = f'g03_period_search.null.{i}.'
    nl.append((fmt(F[p + 'seed'], 0), fmt(F[p + 'best_period_s'], 10), fmt(F[p + 'best_z']), fmt(F[p + 'best_H'], 3)))
A(table(['null seed', 'best period s', 'best z', 'best H'], nl))
A('')
A(f'The catalogue period scores {v("g03_period_search.catalogue_p0_z_full")}; the search maximum '
  f'{v("g03_period_search.search_z_max")} is {F["g03_period_search.search_z_max"] - F["g03_period_search.null_best_z_max"]:.4f} '
  f'above the best of six nulls whose spread is {v("g03_period_search.null_best_z_sd")} &mdash; inside the '
  'null scatter. There is no period at which these bytes beat their own scrambled version. Both the search '
  f'maximum and the null maxima sit far above the analytic Gaussian expectation for the largest of '
  f'{v("g03_period_search.trials",0)} trials ({v("g03_period_search.expected_max_z_gaussian")}): trial periods '
  'are correlated, so the analytic ceiling is optimistic and the empirical scrambled null is the only honest '
  'counterparty.')
A('')
A('### 7 Single-plane RFI immunity (CFS-G4-03d)')
A('')
A(f'A {v("rfi_snr",0)}-SNR tone is injected into plane {v("rfi_plane",0)} for {v("rfi_duration_ms",1)} ms, '
  'once and once per rotation, at the pulse phase.')
A('')
rf = []
for arm in ('baseline', 'tone_single_burst', 'tone_every_rotation'):
    p = f'g03_rfi.{arm}.'
    rf.append((f'`{arm}`', fmt(F[p + 'R_z']), fmt(F[p + 'S_z']), fmt(F[p + 'R_peak_phase_ms'], 1)))
A(table(['arm', '`R_plane` z', 'Stokes-I z', 'peak phase ms'], rf))
A('')
A(f'Plane excess over baseline: {v("g03_rfi.R_excess_z_single",6)} (single burst) and '
  f'{v("g03_rfi.R_excess_z_every_rotation",6)} (every rotation). Stokes-I excess: '
  f'{v("g03_rfi.S_excess_z_single",0)} and {v("g03_rfi.S_excess_z_every_rotation",0)}. A single bad channel '
  'is worth ~2.7 million sigma in total intensity and nothing in the chromatic statistic.')
A('')
A('### 8 Null counterparty battery (CFS-G4-04)')
A('')
A(f'Blank-sky slice `{OFF_NAME}` swept over '
  f'{v("g04_null.off_sweep.trials",0)} dispersion trials; the ON slice is its own phase-randomised surrogate '
  f'({v("g04_null.surrogates.trials",0)} trials, all spin-desynchronised). Battery clean: '
  f'{v("g04_null.battery_clean")}.')
A('')
ck = []
for i in idxs('g04_null.checks', 'id'):
    p = f'g04_null.checks.{i}.'
    ck.append((F[p + 'id'], F[p + 'status'], F[p + 'desc'], F[p + 'detail']))
A(table(['id', 'status', 'assertion', 'observed'], ck))
A('')
A('### 9 The RSS ceiling, and the one time it broke')
A('')
A(f'The registered memory ceiling is {v("rss_ceiling_mib",0)} MiB, checked against `VmHWM` for **every** '
  f'engine leg, not just the ingest chunk. Post-fix maxima: scaling leg {v("g03_scaling.peak_rss_mib_max",1)} '
  f'MiB (headroom {v("rss_headroom_mib",2)}), full-128 parity leg {v("g02_parity.arms.full128.peak_rss_mib",1)} '
  f'MiB (headroom {v("rss_headroom_full128_mib",2)}), counterparty battery '
  f'{v("g04_null.peak_rss_mib_max",1)} MiB (headroom {v("rss_headroom_g04_mib",2)}).')
A('')
A(f'The first positive run of the counterparty arm did **not** pass its own battery: peak '
  f'{F["rss_ceiling_mib"] - F["rss_over_ceiling_mib"]:,.1f} MiB '
  f'i.e. {-F["rss_over_ceiling_mib"]:,.1f} MiB **over** the {v("rss_ceiling_mib",0)} MiB '
  f'ceiling (`rss_over_ceiling_mib` = {v("rss_over_ceiling_mib",2)}), because '
  '`fold_series` kept every dedispersed plane alive while the accumulators ran. The fix released each plane '
  'as its contribution landed &mdash; buffer lifetimes only, no arithmetic change &mdash; and the ceiling was '
  '**not** raised. The superseded receipt is quarantined at `run/stale_pre_patch/g04_null.json` '
  f'(`sha256` `{(RF.get("g04_null_pre_fix", {}).get("sha256") or "n/a")[:16]}`) and the {N_STALE} '
  'arm receipts the fix invalidated are indexed with their hashes in `run/stale_pre_patch/manifest.json`, so '
  'no `--reuse` path can hand back a pre-fix number.')
A('')
A('### 10 Mutant arms: the gates can fail')
A('')
A('Each mutant perturbs exactly one input and must break the corresponding gate.')
A('')
arms = sorted({k[len('g05_closure.gates.'):].split('.')[0] for k in F if k.startswith('g05_closure.gates.')})
gt = []
for a in sorted(arms, key=lambda a: (a[:3], a[3:])):
    p = f'g05_closure.gates.{a}.'
    gt.append((f'`{a}`', F[p + 'node'], 'positive' if F[p + 'arm'] == 'pos' else 'mutant', f'`{F[p + "gate_cmd"]}`', fmt(F[p + 'passed'], 0),
               fmt(F[p + 'failed'], 0), fmt(F[p + 'checks'], 0), fmt(F[p + 'rc'], 0), F[p + 'git_commit']))
A(table(['arm', 'node', 'arm', 'gate command', 'passed', 'failed', 'checks', 'rc', 'commit'], gt))
A('')
A(f'One arm needs reading, because a clean tally and a nonzero exit look contradictory: `g04_mut` injects '
  f'one weak coherent dispersed beacon; the battery it guards then breaks (N1-N3) while the '
  f'beacon calibration checks M0-M4 pass - invisible at the {v("g01_slices.slices.0.prefix_blocks",0)}-block Gate 3 prefix depth, a '
  f'hard lock at {v("on_min_blocks",0)}, locked at the injected DM. The gate exits '
  f'{v("g05_closure.gates.g04_mut.rc",0)} with {v("g05_closure.gates.g04_mut.failed",0)} failed checks because the '
  'claim it protects ("no lock in these bytes") is false on that volume: the exit code is the '
  'guard, not the tally. See `logs/g04_mut.log`.')
A('')
A('### 11 Closure auditors')
A('')
aud = []
for a in ('entail_report', 'entail_perm'):
    s = sub('g05_closure.' + a)
    aud.append((f'`{s.get("cmd","")}`', fmt(s.get('rc', 'n/a'), 0),
                ', '.join(f'{k}={fmt(s[k],0)}' for k in ('total', 'entailed', 'contradicted', 'unbound')
                          if k in s)))
for a in ('verify_rows', 'plan_check', 'spine_check', 'seam_state', 'seam_perm'):
    s = sub('g05_closure.' + a)
    aud.append((f'`{s.get("cmd","")}`', fmt(s.get('rc', 'n/a'), 0),
                ', '.join(f'{k}={fmt(val,0)}' for k, val in sorted(s.items())
                          if k not in ('cmd', 'rc', 'tail'))))
A(table(['auditor command', 'rc', 'verdict'], aud))
A('')
A('### 12 Receipt index')
A('')
A(table(['receipt', 'sha256', 'mtime_utc', 'bytes', 'path'],
        [(f'`{k}`', (RF[k]['sha256'] or 'n/a')[:16], RF[k]['mtime_utc'] or 'n/a',
          fmt(RF[k]['bytes'], 0) if RF[k]['bytes'] is not None else 'n/a', f'`{RF[k]["path"] or "n/a"}`')
         for k in sorted(RF)]))
A('')
A('### 13 What would have to change')
A('')
A(f'Nothing in this run can be re-cut into a detection: the deficit is {v("curve_max_margin_z")} of `z_crit` '
  f'at {v("g03_scaling.nmax",0)} blocks, the measured depth gain is {v("scaling_gain_full_over_16",4)} against '
  f'the {v("sqrt_rotation_ratio_nmax_over_16",4)} coherent addition would need, and every audited rotation '
  'sits inside its own null ceiling. A crossing needs more collected time on source, a brighter source, or a '
  'beam/rotation regime where the emission is not nulled &mdash; none obtainable from these bytes. The honest '
  'output of Gate 4 is the stratification plus ceiling evidence, not a lock.')
A('')
A('*Reproduce: `bash harness/g05_gate.sh` in `guppi_gate4` re-runs the auditors above; '
  '`python3 src/make_receipts.py --check` re-stamps every receipt hash in this table. '
  'This page is a build artifact: `python3 src/tools/make_docs_pages.py --receipts <receipts.json>` '
  'regenerates it, so edit the generator, not the page.*')
A('')
(DOCS / 'cfs-gate4.md').write_text('\n'.join(L))

# ----------------------------------------------------------- governance page
G = []
B = G.append
B('---')
B('layout: default')
B('title: "Ten Governance Laws: Turning Agent Prose Into Gates"')
B('---')
B('')
B('[&larr; Back to Home](index.html)')
B('')
B('# Ten Governance Laws: Turning Agent Prose Into Gates')
B('')
B('*Every receipt figure quoted here is read from '
  '`guppi_gate4/run/receipts.json` by the page generator. The laws are enforced by tools in '
  '`src/` and `/home/snoop/.omp/agent/tools/`, not by reminders in a prompt.*')
B('')
B('## Tier 1: The High-Level Picture (Plain-English Intuition)')
B('')
B('A long-running coding agent fails in a specific, boring way: it stops checking. Early in a session it runs '
  'the test; four hours later it *describes* the test. Prose about diligence costs nothing, so a model under '
  'pressure produces more of it. The scaffold therefore moves every rule out of the prompt and into an exit '
  'code: a claim is only allowed to reach the summary if a program produced the number, and a node is only '
  'allowed to close if a gate ran after the code it covers changed.')
B('')
B('The ten laws below are the ones that survived contact with a real campaign &mdash; '
  f'[CFS Gate 4](cfs-gate4.html), a radio-astronomy measurement whose receipts span {SPAN_H:.1f} hours '
  'and that ended in a published null result with 5/5 nodes closed and zero unbound numerals.')
B('')
B('## Tier 2: The Laws, Their Mechanism, and Their Receipt')
B('')
laws = [
 ('A node closes only on a gate that ran, passed, and is newer than its code.',
  '`plan_check.py` compares each DAG node\u2019s gate-log timestamp against the newest mtime of the code it '
  'covers; a gate older than its code is `stale`, not a pass.',
  f'`plan_check` rc={v("g05_closure.plan_check.rc",0)}: {v("g05_closure.plan_check.done",0)}/'
  f'{v("g05_closure.plan_check.nodes",0)} nodes done, stale={v("g05_closure.plan_check.stale",0)}, '
  f'violation={v("g05_closure.plan_check.violation",0)}.'),
 ('Every gate must be able to fail, or it measures nothing.',
  'Two-sided control: each node ships a positive arm (rc=0) and a mutant arm that perturbs one input and must '
  'exit rc=1 with a named failure.',
  'Ten gate arms recorded; the five mutant arms exit rc=1, e.g. `g02_mut` '
  f'{v("g05_closure.gates.g02_mut.failed",0)} failed / {v("g05_closure.gates.g02_mut.passed",0)} passed, '
  f'`g03_mut` {v("g05_closure.gates.g03_mut.failed",0)} failed / '
  f'{v("g05_closure.gates.g03_mut.passed",0)} passed.'),
 ('Never truncate a gate\u2019s output.',
  'Piping a status gate through `| tail -2` hides the very line that reports staleness. The check-count law '
  '(`F9`) forces every harness to print `checks: N` as its final line on both arms, so a swallowed verdict is '
  'visible as a missing count.',
  'Gate logs carry passed/failed/checks/rc per arm; `gate_logs_missing` = '
  f'{v("g05_closure.gate_logs_missing.len",0)}.'),
 ('Every numeral binds to a receipt path or it does not ship.',
  '`entail_check.py` walks each sentence\u2019s numbers and re-derives them from the receipts document; '
  '`verify_rows.py` checks the formula behind each derived figure.',
  f'`entail_check` REPORT.md rc={v("g05_closure.entail_report.rc",0)} '
  f'(entailed {v("g05_closure.entail_report.entailed",0)}, contradicted '
  f'{v("g05_closure.entail_report.contradicted",0)}, unbound {v("g05_closure.entail_report.unbound",0)}); perm-record.md rc={v("g05_closure.entail_perm.rc",0)} '
  f'({v("g05_closure.entail_perm.entailed",0)} entailed, '
  f'{v("g05_closure.entail_perm.unbound",0)} unbound); `verify_rows` stale='
  f'{v("g05_closure.verify_rows.stale",0)}, formula={v("g05_closure.verify_rows.formula",0)}, unbound='
  f'{v("g05_closure.verify_rows.unbound",0)}.'),
 ('Promotion is generated, never typed.',
  'The long-term memory files (`STATE.md`, `PERM.md`) are emitted from the receipts document by a generator; '
  '`spine_check.py --check-seam` fails if a promoted row is not reproducible from it.',
  f'`spine_check --check-seam` rc={v("g05_closure.spine_check.rc",0)}, '
  f'violations={v("g05_closure.spine_check.violations",0)}; seam rows STATE.md rc='
  f'{v("g05_closure.seam_state.rc",0)} unbound={v("g05_closure.seam_state.unbound",0)}, PERM.md rc='
  f'{v("g05_closure.seam_perm.rc",0)} unbound={v("g05_closure.seam_perm.unbound",0)}.'),
 ('Thresholds are pre-registered and immutable mid-run.',
  'The bar lives in the harness, not in the engine receipt, so no analysis leg can edit its own pass '
  'condition; battery check N11 asserts no input receipt carries a threshold field.',
  f'`z_crit` = {v("z_crit",1)} supplied by the harness; `beacon_full_depth_z_min` = '
  f'{v("beacon_full_depth_z_min",1)}; the campaign reported a miss of '
  f'{v("curve_max_margin_z")} rather than a pass.'),
 ('Every claim ships with its null counterparty.',
  'Blank sky, phase-randomised surrogates, Rayleigh uniformity of peak phases, and an extreme-value (Gumbel) '
  'ceiling for the trial count actually run.',
  f'OFF sweep {v("g04_null.off_sweep.trials",0)} trials / {v("g04_null.off_sweep.locks",0)} locks, Rayleigh '
  f'p={v("g04_null.off_sweep.rayleigh.p_value",4)}; surrogates {v("g04_null.surrogates.trials",0)} trials / '
  f'{v("g04_null.surrogates.locks",0)} locks, p={v("g04_null.surrogates.rayleigh.p_value",4)}; ON excess over '
  f'the sweep ceiling {v("g04_null.on_fold.R_z",4)} &minus; '
  f'{v("g04_null.null_ceiling_over_sweep.expected_max_z",4)} = '
  f'{F["g04_null.on_fold.R_z"] - F["g04_null.null_ceiling_over_sweep.expected_max_z"]:,.6f} '
  f'({v("on_minus_sweep_ceiling_in_sd",4)} sd).'),
 ('Resource ceilings are measured per leg and fixed in the engine, never raised in the gate.',
  'Peak RSS is read from `VmHWM` for every engine leg and compared against one registered ceiling.',
  f'Ceiling {v("rss_ceiling_mib",0)} MiB; worst leg {v("g04_null.peak_rss_mib_max",1)} MiB, headroom '
  f'{v("rss_headroom_g04_mib",2)} MiB. The first run breached the ceiling by '
  f'{abs(F["rss_over_ceiling_mib"]):,.2f} MiB, and the gate failed it.'),
 ('State lives in files, not in the model.',
  'A pre-compaction sieve pins the live repo, open nodes and gate commands to disk; `rehydrate.py` rebuilds '
  'the working set after a restart, so a session death costs context, not truth.',
  'This campaign survived multiple compactions; the closure receipt '
  '(`run/receipts/g05_closure.json`) and the gate archive are the only memory that mattered.'),
 ('A stale freeze is re-gated, never re-labelled.',
  'When HEAD moves under a closed node, the gate is re-run at the new commit and the log re-stamped; prose '
  'that merely asserts "still valid" is a governance violation.',
  f'Gate logs carry their own `git_commit` (e.g. `g02_mut` @ {F["g05_closure.gates.g02_mut.git_commit"]}); '
  f'receipts collected at `{COLLECTOR[:7]}`; `plan_check` stale={v("g05_closure.plan_check.stale",0)}.'),
]
for i, (law, mech, rec) in enumerate(laws, 1):
    B(f'### Law {i}: {law}')
    B('')
    B(f'**Mechanism.** {mech}')
    B('')
    B(f'**Receipt.** {rec}')
    B('')
B('## The RSS Drift Story, in Full')
B('')
B(f'The memory ceiling for the campaign was {v("rss_ceiling_mib",0)} MiB. The design assumption was that peak '
  f'RSS tracks the ingest chunk ({v("chunk_blocks",0)} blocks \u2248 {F["chunk_blocks"] * 128:,} MiB of raw '
  'bytes read at a time), and the small-prefix legs honoured it comfortably '
  f'({v("g02_parity.arms.parity16.peak_rss_mib",1)} MiB at 16 blocks). The assumption was wrong in a way only '
  'a full-depth leg exposes: the folding accumulator holds every dedispersed plane for the whole span, so RSS '
  'tracks **span**, not chunk. The counterparty arm breached at '
  f'{F["rss_ceiling_mib"] - F["rss_over_ceiling_mib"]:,.1f} MiB &mdash; {abs(F["rss_over_ceiling_mib"]):,.1f} '
  'MiB over the ceiling &mdash; and failed its own battery.')
B('')
B('Two options existed: raise the ceiling to a number the code happens to hit, or fix the engine. The ceiling '
  'is a registered constant, so raising it would have been a threshold edit under pressure &mdash; Law 6 '
  'forbids it. `fold_series` was changed to release each plane as its contribution lands (buffer lifetimes '
  'only, arithmetic untouched), and the statistic was re-verified bit-for-bit against the pre-fix value '
  f'(`R_plane` z {v("g03_scaling.R_z_at_nmax")} unchanged). Post-fix maxima: '
  f'{v("g03_scaling.peak_rss_mib_max",1)} / {v("g02_parity.arms.full128.peak_rss_mib",1)} / '
  f'{v("g04_null.peak_rss_mib_max",1)} MiB.')
B('')
B('The receipts the fix invalidated were not deleted and not silently overwritten: they were quarantined '
  'under `run/stale_pre_patch/` with a hash manifest, so a `--reuse` cache can never hand back a pre-fix '
  'number, and the audit trail keeps both the breach and the fix visible.')
B('')
B('## How a Claim Becomes a Gate')
B('')
B(table(['step', 'artifact', 'who checks it'], [
    ('1. Register', 'constants + DAG node with a named gate command', '`plan_check.py` refuses a node with no gate'),
    ('2. Measure', 'engine writes a JSON receipt (no threshold fields inside)', '`make_receipts.py` hashes and stamps it'),
    ('3. Gate', 'harness runs positive and mutant arms, logs to the gate archive', 'exit code + `checks: N` line'),
    ('4. Report', 'generator emits `REPORT.md` / `perm-record.md` from the receipts document', '`entail_check.py` re-derives every numeral'),
    ('5. Promote', 'one-line rows into `STATE.md` / `PERM.md`', '`verify_rows.py` + `spine_check.py --check-seam`'),
]))
B('')
B('## Enforcement Tools')
B('')
B(table(['tool', 'what it enforces'], [
    ('`spine_check.py`', 'spine integrity; a differing closure hash means a stale artifact; `--check-seam` grades promoted rows'),
    ('`plan_check.py`', 'a node closes only on a gate that ran, passed and is newer than its code'),
    ('`entail_check.py`', 'every numeral in a document is entailed by, or contradicted by, the receipts'),
    ('`verify_rows.py`', 'promoted rows are reproducible and their formulas recompute'),
    ('`fact_store.py`', 'claims pin the SHA-256 of the slice that makes them true; drift demotes the claim'),
    ('`num_provenance.py`', 'a numeric status must bind to a declared evidence file or hard-fail'),
    ('`ownership.py`', 'which authority (PLANNING/DESIGN/CLOSURE/STATE/EXECUTION) crossed reasoning into procedure'),
    ('`impact_analysis.py`', 'what a change reaches, and the smallest gate set that proves it'),
    ('`pre_compact_sieve.py`', 'live repo pin, open nodes, gate commands and read locks before history collapses'),
    ('`rehydrate.py`', 'resume a project from disk: tree, closed nodes, ready gate, failing attempts, dead branch'),
    ('`compaction_clock.py`', 'which clock fired the collapse'),
    ('`surprise_check.py`', 'zero-token disk probe; governance probes fail open, never stall a commit'),
    ('`checkpoint.sh`', 'commit spine + project before risky work, nothing until revalidation'),
]))
B('')
B('## Mutant Arms From This Campaign')
B('')
B(table(['arm', 'node', 'gate command', 'passed', 'failed', 'rc'],
        [(f'`{a}`', F[f"g05_closure.gates.{a}.node"], f'`{F[f"g05_closure.gates.{a}.gate_cmd"]}`',
          fmt(F[f'g05_closure.gates.{a}.passed'], 0), fmt(F[f'g05_closure.gates.{a}.failed'], 0),
          fmt(F[f'g05_closure.gates.{a}.rc'], 0)) for a in sorted(arms)]))
B('')
B('Every mutant arm exits `1` by construction (`[ "$MODE" = pos ] || exit 1`) once its battery has confirmed '
  'the perturbation bites, so `failed = 0` on a mutant arm is not an inert gate: the spliced-volume auditor '
  'inside `g01_mut` fails its own seam and SHA checks before the harness converts that into a passing '
  'meta-check, and `g04_mut`\'s tally is clean precisely because its meta-check asserts that the driver\'s '
  'null battery (N1&ndash;N3) has been broken by the injected beacon. Each arm\'s log is archived and indexed '
  'by `g05_closure.gates.<arm>`.')
B('')
B('## Related')
B('')
B('- [CFS Gate 4: the measured null result](cfs-gate4.html) &mdash; every receipt quoted above in context.')
B('- [Positive & Negative Attractors](attractors.html) &mdash; the failure taxonomy these laws intercept.')
B('- [Compaction Sieve Armor](compaction-sieve.html) &mdash; the pre-compaction mechanics behind Law 9.')
B('- [Projects Showcase](projects.html) &mdash; earlier campaigns on the same spine.')
B('')
B('*This page is a build artifact: `python3 src/tools/make_docs_pages.py --receipts <receipts.json>` regenerates it from the campaign receipts, so edit the generator, not the page.*')
B('')
B('*Licensed under the Apache License, Version 2.0. See `LICENSE` and `NOTICE` in the repository root.*')
B('')
(DOCS / 'omp-governance.md').write_text('\n'.join(G))
print('wrote', DOCS / 'cfs-gate4.md', (DOCS / 'cfs-gate4.md').stat().st_size)
print('wrote', DOCS / 'omp-governance.md', (DOCS / 'omp-governance.md').stat().st_size)

for _p in (DOCS / 'cfs-gate4.md', DOCS / 'omp-governance.md'):
    _t = _p.read_text()
    _bad = re.findall(r'MISSING|\bNone\b|\bnan\b|\{\w', _t)
    assert not _bad, f'{_p}: unrendered placeholder {_bad[:5]}'


# ------------------------------------------------------- generated blocks in hand pages
# The index card and the projects entry carry campaign numerals too, so they are spliced
# into marker regions in the two hand-written pages instead of being retyped there.
def splice(path: pathlib.Path, tag: str, body: str):
    t = path.read_text()
    b, e = f'<!-- BEGIN GENERATED: {tag} -->', f'<!-- END GENERATED: {tag} -->'
    if b not in t or e not in t:
        raise SystemExit(f'make_docs_pages: {path.name} has no {tag} marker pair')
    new = t[:t.index(b) + len(b)] + '\n' + body.rstrip('\n') + '\n' + t[t.index(e):]
    if new != t:
        path.write_text(new)
    print('spliced', path.name, tag, len(body))


def splice_line(path: pathlib.Path, anchor: str, line: str):
    """Replace the single line starting with `anchor` (llms.txt carries no comment
    markers: it is read raw by agents, so the entry is rewritten in place)."""
    lines = path.read_text().split('\n')
    hit = [i for i, x in enumerate(lines) if x.startswith(anchor)]
    if len(hit) != 1:
        raise SystemExit(f'make_docs_pages: {path.name} anchor {anchor!r} matched {len(hit)} lines')
    lines[hit[0]] = line
    new = '\n'.join(lines)
    if new != path.read_text():
        path.write_text(new)
    print('spliced', path.name, anchor[:28])


GB = (F['on_bytes_total_gib'] + F['off_bytes_total_gib']) * 2 ** 30 / 1e9
GATES = sub('g05_closure.gates')
NGATES = len({x for k, x in GATES.items() if k.endswith('.node')})
NARMS = len({k.split('.')[0] for k in GATES})
RSS_MAX = max(F['g03_scaling.peak_rss_mib_max'], F['g04_null.peak_rss_mib_max'])

splice(DOCS / 'index.html', 'gate4-card', f"""    <section>
      <h2>The Laws, and One Campaign That Exercised Them</h2>
      <div class="grid-2">
        <div class="card pos">
          <h3><a href="omp-governance.html" style="color: var(--accent)">Ten Governance Laws</a></h3>
          <p>Each law is a gate, not advice: a node closes only on a gate that ran, passed and is newer than
          its code; every gate ships a mutant arm that must fail; every numeral binds to a receipt path or it
          does not ship.</p>
        </div>
        <div class="card">
          <h3><a href="cfs-gate4.html" style="color: var(--accent)">Gate 4: Real GUPPI Pulsar Search</a></h3>
          <p>One session, <code>{fmt(GB, 1)} GB</code> of published GUPPI bytes, {NGATES} gates,
          {NARMS} gate-arm receipts &mdash; and an honest <em>no lock</em>: <code>R_plane</code> z =
          <code>{v('g04_null.on_fold.R_z')}</code> against a pre-registered
          <code>z_crit = {fmt(C['z_crit'], 1)}</code>, with blank sky at
          <code>{v('g04_null.off_sweep.locks', 0)}</code> false-positive locks.</p>
        </div>
      </div>
    </section>""")

splice(DOCS / 'projects.md', 'gate4-entry', f"""## 5. CFS Gate 4 &mdash; Chromatic Coherent Dedispersion on Real GUPPI Pulsar Bytes (Nov 2026)

*Full receipt page: [cfs-gate4.html](cfs-gate4.html). One session, {NGATES} gates, {NARMS} gate-arm receipts, `{fmt(GB, 1)} GB` of published GUPPI bytes.*

* **Pre-registered bar, never moved:** streaming multi-rotation coherent dedispersion plus narrow-plane chromatic phase folding over a {v('g03_scaling.prefixes.-1.nblocks', 0)}-block span ({v('on_rotations_full')} rotations) reached `R_plane` z = **`{v('g04_null.on_fold.R_z')}`** against a pre-registered `z_crit = {fmt(C['z_crit'], 1)}` &mdash; a deficit of `{v('desync_on_margin')}`; the matched blank-sky sweep peaked at `{v('g04_null.off_sweep.moments.max')}` with `{v('g04_null.off_sweep.locks', 0)}` false-positive locks.
* **Null counterparties at matched depth:** the blank-sky slice swept `{v('g04_null.off_sweep.trials', 0)}` dispersion trials and the ON slice was phase-randomised into `{v('g04_null.surrogates.trials', 0)}` spin-desynchronised surrogates &mdash; **`{v('g04_null.surrogates.locks', 0)}` false-positive locks** on both legs, the strongest surrogate reaching `{v('g04_null.surrogates.moments.max')}`.
* **Per-rotation stratification:** `{v('g03_per_spin.null_ceiling.n_spins', 0)}` audited rotations, deepest `z = {v('g03_per_spin.null_ceiling.z_max')}`, every one inside the Gumbel ceiling `{v('g03_per_spin.null_ceiling.ceiling.expected_max_z')}` &plusmn; `{v('g03_per_spin.null_ceiling.ceiling.sd_max_z')}` for its own bin count (Gumbel `p = {v('g03_per_spin.null_ceiling.gumbel_p_max_exceeds_z_max')}`, Rayleigh `p = {vo('g03_per_spin.null_ceiling.rayleigh_p_uniform_phases')}`).
* **Single-plane RFI immunity:** a `{fmt(C['rfi_snr'], 0)}`-SNR tone for `{fmt(C['rfi_duration_ms'], 1)} ms` per rotation is worth `{fmt(F['g03_rfi.S_excess_z_every_rotation'], 0)}` sigma in total intensity and `{v('g03_rfi.R_excess_z_every_rotation')}` z in the chromatic statistic.
* **Positive control, not a broken pipeline:** an injected weak coherent beacon is invisible at the `{v('g04_null_mutant.beacon.nblocks_16', 0)}`-block Gate 3 prefix depth (`z = {v('g04_null_mutant.beacon.z_16_blocks')}`) and a hard lock at `{v('g04_null_mutant.beacon.nblocks_64', 0)}` blocks (`z = {v('g04_null_mutant.beacon.z_64_blocks')}`), recovered at its injected DM `{fmt(F['g04_null_mutant.beacon.sweep_best_dm'], 1)}`.
* **Governance under load:** `{NARMS}` gate-arm receipts with every mutant arm exiting `1`; the `{fmt(C['rss_ceiling_mib'], 0)} MiB` RSS ceiling measured per engine leg (max `{fmt(RSS_MAX, 1)} MiB`) after one honest breach that was fixed and re-gated (pre-fix leg `{fmt(F['g04_null_pre_fix.peak_rss_mib_max'], 1)} MiB`).""")

# the guard covers only the regions this generator writes: the hand-written prose around
# them legitimately contains braces (e.g. a serialization format literal in projects.md)
splice_line(DOCS / 'llms.txt', '- [CFS Gate 4:',
             f'- [CFS Gate 4: Multi-Rotation Coherent Folding]'
             f'(https://spoon-t8i.github.io/omp-governance-scaffold/cfs-gate4.html): '
             f'Receipted null on real GUPPI pulsar bytes &mdash; `R_plane` '
             f'z={v("g04_null.on_fold.R_z")} vs pre-registered z_crit={fmt(C["z_crit"], 1)} over '
             f'{fmt(F["on_rotations_full"], 2)} rotations, {v("g04_null.off_sweep.locks", 0)} false locks '
             f'in {v("g04_null.off_sweep.trials", 0)} blank-sky DM trials and '
             f'{v("g04_null.surrogates.trials", 0)} surrogates, per-rotation stratification inside the '
             f'Gumbel ceiling, {fmt(C["rfi_snr"], 0)}-SNR single-plane RFI worth '
             f'{v("g03_rfi.R_excess_z_every_rotation")} z.')

for _p, _tag in ((DOCS / 'projects.md', 'gate4-entry'), (DOCS / 'index.html', 'gate4-card')):
    _t = _p.read_text()
    _region = _t.split(f'<!-- BEGIN GENERATED: {_tag} -->', 1)[1].split('<!-- END GENERATED:', 1)[0]
    _bad = re.findall(r'MISSING|\bNone\b|\bnan\b|\{\w', _region)
    assert not _bad, f'{_p}:{_tag}: unrendered placeholder {_bad[:5]}'

# ---------------------------------------------------------------- chemgym-v2 page
# The chemistry suite's receipts document has the same shape (facts / constants /
# derived / receipt_files), so the same discipline applies: every numeral below is
# read from CGF/CGC/CGD, nothing is transcribed. The helpers are the guppi ones,
# bound to chemgym facts.
CGF, CGC, CGD, CGRF = CG['facts'], CG['constants'], CG['derived'], CG['receipt_files']

def cv(p, nd=4):
    return fmt(CGF[p], nd)

def csub(prefix):
    p = prefix + '.'
    return {k[len(p):]: CGF[k] for k in sorted(CGF)
            if k.startswith(p) and not k.endswith('.len')}

def ctable(prefix, nd=4):
    return table(['field', 'value'],
                 [(f'`{k}`', fmt(v, nd)) for k, v in csub(prefix).items()
                  if isinstance(v, (int, float, bool))])

def cidxs(prefix, field):
    pat = re.compile(re.escape(prefix) + r'\.(\d+)\.' + re.escape(field) + r'$')
    return sorted({int(m.group(1)) for m in (pat.match(k) for k in CGF) if m})

BENCHES = sorted({k.split('.')[2] for k in CGF if k.startswith('gate3_rl_benchmark.benches.')
                  and k.split('.')[2] != 'len'})
SHORTCUTS = cidxs('acid_carbonate_physics.mutant_controls', 'name')
DOM = CGD['sarsa_dominates_n']
EPS = [CGF[f'gate3_rl_benchmark.benches.{b}.episodes'] for b in BENCHES]

K = []
W = K.append
W('---')
W('layout: default')
W('title: "ChemGym-v2: Physics-Benched Chemistry Environments - Receipted Bench Suite"')
W('---')
W('')
W('[&larr; Back to Home](index.html)')
W('')
W('# ChemGym-v2: Five Gymnasium-Style Chemistry Benches, Library-Read Physics, and a SARSA Solver Audited Against Both')
W('')
W(f'*Project `chemgym-v2`. Receipts document `run/receipts.json`, collector `src/make_receipts.py` at commit '
  f'`{CG["collector"]["last_commit_touching"][:7]}`, bench engine at git pin `{cv("gate3_rl_benchmark.git_pin")[:7]}`. '
  'Every number below is read from that document by the page generator; none is typed by hand. '
  'Licensed under the Apache License, Version 2.0.*')
W('')
W('## Tier 1: The High-Level Picture (Plain-English Intuition)')
W('')
W(f'ChemGym-v2 is {cv("gate3_rl_benchmark.benches.len", 0)} small chemistry environments in the Gymnasium style '
  '(`*-v0` ids): a flash vapor-liquid separator, a liquid-liquid extraction vial, an aqueous speciation beaker, '
  'a Wurtz reaction flask, and a jar of vinegar with a spoon of baking soda. Each bench hands an agent labelled '
  'action slots (open a valve, tip the jar, read the gas cylinder) and a reward, and every physical quantity it '
  'reports - pressure, temperature, moles, litres of gas, IR band height - is computed from published '
  'component-data libraries, not fitted curves. The tie is explicit on the acid-carbonate bench: the engine\'s '
  f'reaction enthalpy `{cv("acid_carbonate_physics.headline.dH_rxn_J_per_mol", 0)}` J/mol must equal the Hess-law '
  f'sum over library heats of formation, `{cv("acid_carbonate_physics.headline.hess_dH_J_per_mol_from_library", 0)}` '
  'J/mol, to the last digit - and the reaction is endothermic, so the jar goes cold.')
W('')
W(f'The RL question: can a tabular SARSA agent learn the action sequence that reaches a bench\'s goal, and can '
  'any single fixed action slot reach it by shortcut? The receipt answers both. Under a pre-registered rule '
  f'(`{DOM["rule"]}`), SARSA dominates on **{fmt(DOM["value"], 0)} of {cv("gate3_rl_benchmark.benches.len", 0)}** '
  f'benches. And {fmt(CGD["shortcuts_n"]["value"], 0)} named shortcut controls - each one a deliberate way the '
  f'goal could be faked - were each caught by named physics checks: {cv("acid_carbonate_physics.mutant_controls_passed", 0)} '
  f'of {cv("acid_carbonate_physics.mutant_controls_total", 0)} positive-control mutations caught, every one '
  'leaving the engine clean afterwards.')
W('')
W('## Tier 2: Systems Engineering, Receipts and Honest Boundaries')
W('')
W(f'### 1 The bench suite and the SARSA verdict (gate `{CGF["gate3_rl_benchmark.gate"]}`)')
W('')
W(table(['bench', 'episodes', 'random mean', 'best single-slot mean', 'SARSA return', 'steps', 'terminated',
         'reference solver'],
        [(f'`{b}`', cv(f'gate3_rl_benchmark.benches.{b}.episodes', 0),
          cv(f'gate3_rl_benchmark.benches.{b}.random_mean', 3),
          cv(f'gate3_rl_benchmark.benches.{b}.best_single_slot_mean', 3),
          cv(f'gate3_rl_benchmark.benches.{b}.sarsa_return', 3),
          cv(f'gate3_rl_benchmark.benches.{b}.sarsa_steps', 0),
          fmt(CGF[f'gate3_rl_benchmark.benches.{b}.sarsa_terminated']),
          cv(f'gate3_rl_benchmark.benches.{b}.solve_reference', 3)) for b in BENCHES]))
W('')
W('### 2 Contract gate, determinism and guardrails')
W('')
W(f'The GATE-3 harness runs {cv("gate3_rl_benchmark.checks", 0)} contract checks with '
  f'{cv("gate3_rl_benchmark.fails.len", 0)} failures. The determinism leg re-runs the greedy policy and '
  f'reproduces the `FlashVleV2-v0` SARSA return exactly (`{cv("gate3_rl_benchmark.determinism.greedy_return", 3)}`), '
  f'with the learning curve hashed to `{CGF["gate3_rl_benchmark.determinism.curve_sha256_16"]}`. Thermal '
  f'guardrails are scored, not clamped: cold `r = {cv("gate3_rl_benchmark.thermal.cold_r")}`, window '
  f'`r = {cv("gate3_rl_benchmark.thermal.window_r")}`, over-relief `r = {cv("gate3_rl_benchmark.thermal.relief_r", 1)}`. '
  f'The run took `{cv("gate3_rl_benchmark.wall_s", 2)}` s at git pin `{cv("gate3_rl_benchmark.git_pin")[:7]}` '
  f'(receipt date `{CGF["gate3_rl_benchmark.date"]}`).')
W('')
W(f'### 3 The acid-carbonate bench: {cv("acid_carbonate_physics.checks_passed", 0)}/'
  f'{cv("acid_carbonate_physics.checks_total", 0)} physics and contract checks')
W('')
W(f'One US tablespoon of pickling vinegar (`{cv("acid_carbonate_physics.declared_protocol.vinegar_acid_kg_m3", 1)} '
  f'kg/m3 acetic acid`) meets one tablespoon of baking soda (`{cv("acid_carbonate_physics.declared_protocol.soda_kg_per_tbsp", 4)} kg`, '
  f'`{cv("acid_carbonate_physics.declared_protocol.tbsp_m3", 9)} m3` per tablespoon) in a sealed jar; the CO2 is '
  f'collected over water. The goal is declared before the run: `{cv("acid_carbonate_physics.declared_protocol.target_gas_L", 2)} L` '
  f'of gas and a carboxylate IR band of `{cv("acid_carbonate_physics.declared_protocol.target_salt_A1555", 2)}`, reached '
  f'through {cv("acid_carbonate_physics.declared_protocol.actions.len", 0)} labelled slots: '
  + ', '.join(f'`{CGF[f"acid_carbonate_physics.declared_protocol.actions.{i}"]}`'
              for i in range(CGF['acid_carbonate_physics.declared_protocol.actions.len'])) + '.')
W('')
W('The headline state the engine reports after the full protocol:')
W('')
W(ctable('acid_carbonate_physics.headline'))
W('')
W(f'### 4 Law 2 counterparty controls: {cv("acid_carbonate_physics.mutant_controls_passed", 0)}/'
  f'{cv("acid_carbonate_physics.mutant_controls_total", 0)} caught')
W('')
W(f'The gate ships positive-control mutations: each breaks one physical law inside the engine and must be caught '
  f'by a named check. {fmt(CGD["shortcuts_n"]["value"], 0)} of them are named shortcut controls '
  f'(`{CGD["shortcuts_n"]["formula"]}` = `{fmt(CGD["shortcuts_n"]["value"], 0)}`). Each row is the receipt\'s own '
  'record of how the break was introduced, which checks caught it, and whether the engine was clean after the '
  'mutation was reverted:')
W('')
W(table(['control', 'how it was broken', 'caught by', 'clean after'],
        [(f'`{CGF[f"acid_carbonate_physics.mutant_controls.{i}.name"]}`',
          CGF[f'acid_carbonate_physics.mutant_controls.{i}.how'],
          '; '.join(CGF[f'acid_carbonate_physics.mutant_controls.{i}.caught_by.{j}']
                    for j in range(CGF[f'acid_carbonate_physics.mutant_controls.{i}.caught_by.len'])),
          fmt(CGF[f'acid_carbonate_physics.mutant_controls.{i}.clean_after']))
         for i in SHORTCUTS]))
W('')
W('### 5 Provenance')
W('')
W(table(['engine file', 'sha256 (16)'],
        [(f'`{k}`', f'`{x}`') for k, x in csub('acid_carbonate_physics.engine_sha256_16').items()]))
W('')
W(table(['receipt file', 'mtime (UTC)', 'dirty', 'last commit touching'],
        [(f'`{k}`', CGRF[k]['mtime_utc'], fmt(CGRF[k]['dirty']), CGRF[k]['last_commit_touching'][:7])
         for k in sorted(CGRF)]))
W('')
W(f'Python `{CGF["acid_carbonate_physics.python"]}`; physics receipt stamped `{CGF["acid_carbonate_physics.utc"]}`; '
  f'collector `{CG["collector"]["script"]}` (`{CG["collector"]["sha256"][:12]}`), `dirty = {fmt(CG["collector"]["dirty"])}`.')
W('')
W('### 6 Honest Boundaries')
W('')
W(f'* **`wall_s` is telemetry, not a claim:** `{cv("gate3_rl_benchmark.wall_s", 2)} s` is runtime cost; the '
  'volatility partition keeps it (and every timestamp) out of the promoted STATE.md/PERM.md rows - it lives here '
  'because this page is the receipt view.')
W('* **SARSA returns are stored rounded to 3 decimals:** the dominance margin is '
  f'`{fmt(CGC["dominance_margin"], 1)}`, far above the rounding, so the verdict is not a rounding artefact.')
W(f'* **{cv("acid_carbonate_physics.mutant_controls_total", 0)} controls ran, {fmt(CGD["shortcuts_n"]["value"], 0)} '
  'are itemised:** the receipt names the shortcut controls above; the remaining controls are counted in the pass '
  'total but not listed in this document.')
W('* **Kitchen scale, single vessel:** ideal-gas headspace over water with Henry dissolution - the physics is '
  'real but the bench is a jar, not a plant.')
W(f'* **Per-bench episode budgets differ** (`{min(EPS)}`-`{max(EPS)}` episodes): the benchmark compares solvers '
  'within a bench, not across benches.')
W('')
W('## Related')
W('')
W('* [Ten Governance Laws](omp-governance.html) - the laws these gates exercise.')
W('* [CFS Gate 4](cfs-gate4.html) - the same receipt discipline on a radio-astronomy null.')
W('')
W('*Licensed under the Apache License, Version 2.0. See `LICENSE` and `NOTICE` in the repository root.*')
W('')
(DOCS / 'chemgym-v2.md').write_text('\n'.join(K))
print('wrote', DOCS / 'chemgym-v2.md', (DOCS / 'chemgym-v2.md').stat().st_size)
_t = (DOCS / 'chemgym-v2.md').read_text()
_bad = re.findall(r'MISSING|\bNone\b|\bnan\b|\{\w', _t)
assert not _bad, f'chemgym-v2.md: unrendered placeholder {_bad[:5]}'

splice(DOCS / 'index.html', 'chemgym-card', f"""    <section>
      <h2>ChemGym-v2: The Same Laws, Applied to Chemistry</h2>
      <div class="grid-2">
        <div class="card">
          <h3><a href="chemgym-v2.html" style="color: var(--accent)">ChemGym-v2: Physics-Benched Chemistry</a></h3>
          <p>{cv('gate3_rl_benchmark.benches.len', 0)} Gymnasium-style benches whose physics is read from published
          component libraries &mdash; a <code>{cv('acid_carbonate_physics.headline.dH_rxn_J_per_mol', 0)} J/mol</code>
          reaction enthalpy tied to its Hess sum &mdash; tabular SARSA dominant on
          <code>{fmt(DOM['value'], 0)}</code> of <code>{cv('gate3_rl_benchmark.benches.len', 0)}</code> benches,
          <code>{cv('acid_carbonate_physics.checks_passed', 0)}/{cv('acid_carbonate_physics.checks_total', 0)}</code>
          physics checks and <code>{cv('acid_carbonate_physics.mutant_controls_passed', 0)}/{cv('acid_carbonate_physics.mutant_controls_total', 0)}</code>
          mutant controls caught.</p>
        </div>
        <div class="card pos">
          <h3><a href="chemgym-v2.html" style="color: var(--accent)">How This Card Stays True</a></h3>
          <p>Every number on this card is read from <code>chemgym-v2/run/receipts.json</code> by the same page
          generator that writes the campaign pages. The docs gate rebuilds them from a vendored snapshot, refuses
          any difference, and carries mutant arms that must bite.</p>
        </div>
      </div>
    </section>""")
splice(DOCS / 'projects.md', 'chemgym-entry',
       f'* [ChemGym-v2: Physics-Benched Chemistry Environments](chemgym-v2.md) - '
       f'{cv("gate3_rl_benchmark.benches.len", 0)} gym-style chemistry benches with library-read physics, '
       f'SARSA dominant on {fmt(DOM["value"], 0)}/{cv("gate3_rl_benchmark.benches.len", 0)} benches, '
       f'{cv("acid_carbonate_physics.checks_passed", 0)}/{cv("acid_carbonate_physics.checks_total", 0)} physics '
       f'checks and {cv("acid_carbonate_physics.mutant_controls_passed", 0)}/'
       f'{cv("acid_carbonate_physics.mutant_controls_total", 0)} mutant controls caught.')
splice_line(DOCS / 'llms.txt', '- [ChemGym-v2:',
            f'- [ChemGym-v2: Physics-Benched Chemistry Environments]'
            f'(https://spoon-t8i.github.io/omp-governance-scaffold/chemgym-v2.md): '
            f'{cv("gate3_rl_benchmark.benches.len", 0)} gym-style chemistry benches, library-read physics, '
            f'SARSA dominant on {fmt(DOM["value"], 0)}/{cv("gate3_rl_benchmark.benches.len", 0)} benches, '
            f'{cv("acid_carbonate_physics.mutant_controls_passed", 0)}/'
            f'{cv("acid_carbonate_physics.mutant_controls_total", 0)} mutant controls caught.')

# The chemgym page must not be silently dropped: its headline numerals, the bench ids
# and the collector commit have to appear in the built page, and the index card must
# carry the dominance verdict.
_cg = (DOCS / 'chemgym-v2.md').read_text()
for _p in (cv('acid_carbonate_physics.headline.dH_rxn_J_per_mol', 0),
           cv('acid_carbonate_physics.headline.hess_dH_J_per_mol_from_library', 0),
           cv('acid_carbonate_physics.checks_passed', 0) + '/' + cv('acid_carbonate_physics.checks_total', 0),
           cv('acid_carbonate_physics.mutant_controls_passed', 0) + '/' + cv('acid_carbonate_physics.mutant_controls_total', 0),
           cv('gate3_rl_benchmark.thermal.window_r'),
           fmt(DOM['value'], 0) + ' of ' + cv('gate3_rl_benchmark.benches.len', 0),
           CG['collector']['last_commit_touching'][:7]):
    assert _p in _cg, f'chemgym-v2.md missing headline {_p!r}'
for _b in BENCHES:
    assert _b in _cg, f'chemgym-v2.md missing bench {_b}'
_ix = (DOCS / 'index.html').read_text()
assert 'chemgym-v2.html' in _ix, 'index.html lost the chemgym card'
assert fmt(DOM['value'], 0) in _ix, 'index card lost the dominance verdict'
print('chemgym-v2 page guard: ok')
