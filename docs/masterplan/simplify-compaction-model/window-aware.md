# Window-size-aware compaction policy (design)

Date: 2026-06-16
Status: design (advisor-validated), not yet implemented
Advisor: `[Advisor] window-size-aware compaction policy design` — Paseo agent `040952be`, provider `claude/claude-opus-4-8`, thinking `xhigh`. Read-only.

## The problem (owner directive)

The policy must take different model window sizes into account, with three regimes:
1. **Small windows (64k/128k):** don't artificially limit or compress aggressively — compact only to remove old/stale data.
2. **Medium windows (256k/512k):** keep around ~150k for efficiency, but allow expansion when content is relevant.
3. **Large windows (1m):** keep unnecessary data compacted for efficiency, but allow much larger growth when justified.

Today the system is **ceiling-driven** (compact at X% of the wall, flat `SOFT`/`HARD` percentages with a crude `_WIDE` 2-point hack). The owner wants **target-driven with relevance-gated expansion**: an efficiency target that scales with window, compact to shed stale data, but let actively-relevant context grow toward the hard ceiling when it earns it.

## Advisor's thesis (two measured facts dominate)

1. **The post-compaction floor (~69k, n=494) is window-independent.** It's system prompt + tool schemas + injected summary; it does not shrink for a smaller window. Combined with `MIN_SAVINGS=30k`, the system physically cannot recommend below ~100k context **at any window size**.
   → **Consequence:** the small-window regime (64k/128k → "don't compact aggressively") is *already* enforced by the floor + min_savings guard, not by needing new logic. At W=64k the first-actionable context (100k) exceeds the window, so it can never recommend. The genuinely new value of this work lands at **≥256k**.

2. **`stale_output` is the wrong relevance gate.** Verified this session at `transcript_lib.py:390`: `stale_tool_chars` = bytes of tool_result text older than the last 30 turns — a **recency proxy, not a relevance measure**. It is measured *anti-predictive* (0.9x lift, below the 24% baseline) and goes **high precisely in the case the owner worries about**: a long autonomous burst where everything is still needed has most bytes >30 turns old. Gating expansion on it is backwards.

→ **Reframe:** "relevance-gated expansion" must be carried by the *mechanical* reclaim math (`est_reclaim ≥ min_savings`, already present and honest) plus the *predictive* boundary signals (`subagent_done` 3.8x, `burn_rate` 2.3x, `commit` 1.8x) — **never `stale_output`**.

## The design — absolute target & ceiling curves on the existing skeleton

Replace flat percentages with **absolute (token-valued) curves derived from window + floor + profile**. Keep the SOFT(signal-gated)/HARD(unconditional) skeleton; change only what SOFT/HARD *are*.

```
F  = post_floor          # ~69k, measured, window-INDEPENDENT  (existing POST_FLOOR)
MS = min_savings         # ~30k                               (existing MIN_SAVINGS)
a  = A[profile]          # {economy:130, balanced:188, lazy:266}  (REPLACES the 3 soft fractions)

def native_safe_line(W):                       # the HARD/ceiling line: beat native auto
    return max(min(0.675*W, W - 63_000) - 10_000, 0)   # ⚠ see caveat below

def ceiling(W):                                # HARD: unconditional safety
    return clamp(native_safe_line(W), F + 2*MS, 0.90*W)

def target(W):                                 # SOFT: boundary-gated line
    curve = F + a*sqrt(W - F)   if W > F else W
    return clamp(min(curve, ceiling(W) - MS), F + MS, 0.85*W)
```

Decision rule (drops into `policy.decide`, same cooldown/min_savings guards):

```
est_reclaim = context_tokens - F
reclaimable = est_reclaim >= MS                       # mechanical gate (exists)
boundary    = any(non-observe signal)                 # subagent_done / burn_rate / commit / todo_step

recommend = reclaimable AND (
      context_tokens >= ceiling(W)                     # HARD: unconditional safety
   OR (context_tokens >= target(W) AND boundary)       # SOFT: relevance-gated expansion
) AND not cooldown_suppressed
```

- **Below target:** never compact ("don't artificially limit"). For small windows target≈window, so automatic.
- **Target→ceiling:** compact only if reclaimable bulk AND a genuine breakpoint. No breakpoint ⇒ context grows ⇒ relevant work keeps its context. **That is the relevance-gated expansion** — via predictive breakpoints, not a staleness fraction.
- **At/above ceiling:** unconditional safety.

### balanced (a=188, F=70k) — computed and verified this session

| W | ceiling(W) | target(W) | target/W | SOFT→HARD band | first-actionable (F+MS) |
|---|---:|---:|---:|---:|---:|
| 64k | 57,600 | 54,400 | 85% | ~3k | 100k > window → **never** (physics blocks) |
| 128k | 115,200 | 100,000 | 78% | ~15k | 100k |
| 200k | 130,000 | 100,000 | 50% | 30k | 100k |
| 256k | 162,800 | 132,800 | 52% | 30k | 100k |
| 300k | 192,500 | 160,162 | 53% | ~32k | 100k |
| 512k | 335,600 | 194,988 | 38% | ~141k | 100k |
| 1m | 665,000 | 251,301 | 25% | ~414k | 100k |

**Validation:** the 200k balanced row reproduces the live hand-tuning (target 100k ≈ live SOFT floor; ceiling 130k ≈ live HARD 110k). The band only opens meaningfully at ≥256k — exactly where the medium/large regimes live. `target/W` falls 85%→25% because the irreducible working set F is window-independent: a small window is almost entirely floor; a large window is mostly reclaimable headroom.

## Composition: window sets shape, profile sets aggressiveness (KEEP BOTH)

Window = *shape*; profile = *offset along that shape*. They're orthogonal: the ">80% cached reads, compact often" directive is a **profile** statement (pull the whole curve down via `a`); window-awareness is **shape**. Replacing profile with "the window tier IS the aggressiveness" would throw away the cross-window economy/lazy lever the owner actively relies on. `a = {economy:130, balanced:188, lazy:266}`.

## Claude's blind spot → promote `native_ceiling` to the window

Claude can't see the live model window. Its one hard fact is `CLAUDE_CODE_AUTO_COMPACT_WINDOW` (`window_resolver.native_ceiling_from_settings`), which today only sets a *blocker flag*. **Promote it to the effective-window source for Claude when present:** `effective_window(claude) = native_ceiling`. The miss-attribution data confirms this: all 3 unwarned autos were `learned=512k` vs `native_ceiling=500k`, firing ~345k — the tier inference (512k) was wrong, 500k was right. For 64k/128k on Claude, add them to `AUTO_WINDOW_TIERS` only as the **fallback when `native_ceiling` is absent** (realistically a small-window model means a small `CLAUDE_CODE_AUTO_COMPACT_WINDOW` is set, so `native_ceiling` carries it).

## ⚠ Critical caveat — `native_safe_line` constants are STALE, must re-measure

The advisor's `native_safe_line(W) = min(0.675·W, W−63k)` uses the `W−63k` reserve from the HANDOFF, which was measured on **Claude Code 2.1.170**. **Last session I proved that stale**: on **2.1.178**, native auto fired at ~345k under a 500k ceiling → reserve ≈ **153k**, not 63k. So the `ceiling(W)` curve's `native_safe_line` is **not trustworthy on current Claude Code** and would place the safety ceiling too high (compaction would arrive too late again). 

**Two-part adoption:**
- **`target(W)` is reserve-independent** (depends only on F, a, W) → **safe to adopt now**.
- **`ceiling(W)` / `native_safe_line` needs the pending reserve re-measurement** (the "1-day reserve check" follow-up from the aggressive-config session). Until then, keep `ceiling(W)` conservative (the existing `HARD_PCT` against the effective window), or derive the reserve constant from the most recent nightly's `auto_pre_median` vs `ceiling`.

## What breaks (ranked)

1. **Small-window floor collision (fatal below ~110–130k).** F≈69k exceeds a 64k window and eats half a 128k window. Below `F + MS + headroom ≈ 110–130k` there is no reclaimable bulk, ever. → **Force `MODE→observe` below W_min and say so in `--status`** ("window 64k below operating minimum; advisory disabled"). Do not pretend "compact only stale data" works there.
2. **128–200k ceiling↔target inversion.** Compute `ceiling` first, pin `target = min(curve, ceiling − MS)`. Assert `ceiling ≥ target + MS` always, or the SOFT band inverts.
3. **No-boundary burst rides to ceiling, then HARD-compacts late.** "Expand when relevant" vs "compact early for economy" are in tension for a long single-topic burst. Acceptable; keep `burn_rate` (2.3x, fires ~8 turns pre-auto) + the PostToolUse watchdog as the advance-warning valve.
4. **Pi actuates on a wrong gate (real tokens burned).** Pi must NOT actuate on `stale_output`. For Pi SOFT-band actuation require `est_reclaim ≥ MS` **AND a strong signal** (subagent_done/commit). Keep Pi's target conservative until the curve is backtested.
5. **`native_safe_line(W)` unverified outside 200k/400k.** The 1m end (auto at 675k?) is speculative. Telemetry-confirm per window before trusting `ceiling(W)` at the extremes.

## Parameter budget → ZERO new public knobs (surface shrinks)

- `target/ceiling` derive entirely from: resolved `window`, measured `F` (existing `POST_FLOOR`), `MS` (existing `MIN_SAVINGS`), `a(profile)` (replaces the 3 `soft` fractions already in `_PROFILES`).
- `hard` fraction is **replaced by** the derived `ceiling(W)` — can retire (or stay as a clamp cap).
- `MAX_CONTEXT_TOKENS` composes: `effective_ceiling = min(ceiling(W), MAX_CONTEXT_TOKENS)`.
- **Retire `HARD_PCT_WIDE` / the whole `_WIDE` mechanism** (`config_lib.py:78-95`). It is a degenerate one-breakpoint approximation of *exactly this curve*; a real `target(W)` makes it obsolete. Leave inert as a deprecated override during migration, then delete.

Net: public knobs stay at 3, the internal profile table stays at 3 entries (shape changes), `_WIDE` goes away. Strict improvement on the "don't re-explode parameters" constraint.

## Framing note

Keep "target + ceiling"; rename "relevance gate" to **"reclaimability gate (mechanical) + predictive-boundary gate."** The only reliable relevance proxy is the reclaim math; the heuristic one (`stale_output`) is measured-bad. **Headline: the small-window regime is mostly already implemented by the window-independent floor + min_savings guard; the genuinely new value lands at ≥256k.**

## Implementation status (2026-06-16)

**target(W) — DONE (policy.py), LIVE on the Claude main path.**
- `policy.target_tokens(W, profile, F, MS, hard_pct)` implemented + clamped to the current HARD line as interim ceiling (keeps SOFT < HARD; proper `ceiling(W)` deferred until the reserve is measured). `_A = {economy:130, balanced:188, lazy:266}`.
- `resolve_policy_config` derives `soft = target_tokens/effective_limit` UNLESS a deprecated `SOFT_PCT` override is set. `context_monitor._run()` now reads SOFT from this (after window resolution), and `nightly_eval` derives its backtester `--soft` from the curve too — so the offline backtest stays consistent with the live path. Top-level `SOFT_PCT` retired from `config.json` (the curve governs; `pi.SOFT_PCT=0.50` stays pinned — Pi is actuate, kept conservative per advisor trap #4).
- Verified regimes (LIVE, economy profile): 200k->soft 50% (100k, floored by physics), 512k->31% (156k), 1m->20% (195k) — small windows not starved, large windows target low occupancy.
- 7 new policy tests + 4 window_resolver tests; rich fixture bumped to ~170k context so recommend tests fire naturally under the curve; min_savings test floor bumped to match. pi_bridge left on its pinned flat soft (actuate = conservative; full Pi rewire is follow-up).

**native_ceiling — DONE (window_resolver.py), LIVE now.**
- Promoted as a **cap** (`effective = min(resolved, native_ceiling)`, Claude-only) rather than a full replacement. Rationale: the advisor's "native_ceiling IS the window" assumed `WINDOW` was a loose 1m default; for this owner's deliberate aggressive `WINDOW` (200k < ceiling 300k), a full replacement would *loosen* it and move the hard line later (the unmeasured-reserve regression). The cap honors "native_ceiling is the enforced wall" (caps over-inference 512k→500k; handles small models 128k binds) **without ever loosening**.
- Live effect for the current config: **dormant** (200k < 300k, so the cap doesn't bind) — exactly the safe, no-regression outcome. It activates when `WINDOW`/inference exceeds the ceiling (big-window models, small enforced ceilings).
- 4 new window_resolver tests pin: caps over-inference, caps small models, never loosens a tighter window, doesn't affect Pi.

**ceiling(W) / native_safe_line — DEFERRED.** Needs the pending native-auto reserve re-measurement (the `W−63k` constant is stale on CC 2.1.178; measured ~153k under a 500k ceiling, unknown under 300k). Until then the current flat `HARD_PCT` is the interim ceiling inside `target_tokens`'s clamp.
