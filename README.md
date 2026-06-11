# autocompactor

Smarter, earlier, cheaper compaction for Claude Code. Two cooperating hooks:

```
UserPromptSubmit ─→ context_monitor.py ──┐  (watches occupancy + boundaries,
                                         │   recommends /compact, stages
                                         │   tailored instructions)
                                         ▼
PreCompact ──────→ precompact_analyzer.py   (backs up transcript, injects
                                             customInstructions into the
                                             compaction — manual OR auto)
                              │
                    transcript_lib.py        (shared JSONL parsing,
                                              phase detection, instruction builder)
                    stats.py                 (local telemetry -> events.jsonl)
                    analyze_corpus.py        (offline backtester + --events aggregator)
```

## Why this shape

Hooks cannot invoke `/compact` — `PreCompact` only fires once a compaction
is already happening. So the system splits into an **advisor** (monitor)
and an **enricher** (analyzer). The economics it exploits: compaction cost
scales with the context being summarized, and every token of bloat is
re-paid as input on every subsequent turn. Compacting at ~40–50% occupancy
at a task boundary is dramatically cheaper than autocompact firing near
the ceiling — and the summary is better because the instructions are
generated from what actually happened in the session (files edited,
TodoWrite state, recent errors, last task statement).

## Install

Clone anywhere and run the installer — hooks reference this checkout in
place (no files are copied):

```bash
git clone <repo-url> autocompactor && cd autocompactor
python3 install.py            # hooks + missing env defaults
python3 install.py --cron     # ... and the 03:30 nightly self-evaluation
python3 install.py --verify   # pytest + smoke + live transcript probe
```

Then run `/hooks` inside Claude Code to confirm registration.

Installer flags (all idempotent — safe to re-run any of them):

| Flag | Effect |
|---|---|
| *(none)* | Register both hooks in `~/.claude/settings.json`; fill in any **missing** `AUTOCOMPACTOR_*` env keys + `CLAUDE_CODE_AUTO_COMPACT_WINDOW` with the defaults below. Tuned values are never overwritten. |
| `--force-env` | Reset all managed env keys to the code defaults. |
| `--cron` | Also register the nightly `nightly_eval.py` cron job (marker `# autocompactor-nightly`, 03:30). |
| `--verify` | Run the pytest suite, the smoke test, and probe the newest real transcript through `context_monitor.py`. Nonzero exit on any failure. |
| `--status` | Doctor: hooks registered? env keys present (and which are tuned away from defaults)? cron registered? state dir writable? newest nightly report age? CLI version vs the last nightly-validated one? |
| `--remove` | Unregister hooks, delete `AUTOCOMPACTOR_*` env keys, remove the cron job. Leaves `CLAUDE_CODE_AUTO_COMPACT_WINDOW` (a native Claude Code setting) with a note. |

### Pi harness (optional)

The same advisor runs inside the [Pi coding agent]
(`@earendil-works/pi-coding-agent`) via a TypeScript extension that
shells out to the same Python core. Separate installer, same idempotency
rules:

```bash
python3 install_pi.py            # extension -> ~/.pi/agent/extensions/ (bridge path baked in)
python3 install_pi.py --status   # doctor: placeholder rewritten? bridge reachable? version drift?
python3 install_pi.py --remove   # delete only our shim + version pin
```

The installer copies `pi/autocompactor.ts` with this checkout's
`pi_bridge.py` path rewritten in (a symlink can't carry the path), pins
the Pi package version observed at install time, and never touches
anything else under `~/.pi`. Restart Pi to load the extension. Pi-side
state and telemetry live under `~/.autocompactor/pi/` (the Claude state
dir is untouched).

## Tunables

Tuning lives in `config.json` at the repo root (bare key names, e.g.
`HARD_PCT`), read at runtime by both harnesses via `config_lib` — no env
plumbing or sync step needed. Per-harness sections (`"claude"`, `"pi"`)
override top-level keys; `<NAME>_WIDE` variants win on context windows
≥300k. Site-local values that must not be versioned (LLM endpoints,
model names) go in `config.local.json` (gitignored), merged over
`config.json`. The environment variables below remain available as
manual runtime overrides and always win over the config files;
`AUTOCOMPACTOR_CONFIG` points at an alternate config file (empty string
= no config files at all — used by the hermetic tests).

| Variable (env override)   | Default | Meaning                                  |
|---------------------------|---------|------------------------------------------|
| `AUTOCOMPACTOR_WINDOW`    | 200000  | Model context window in tokens. Set to 1000000 for 1M-context models. |
| `AUTOCOMPACTOR_SOFT_PCT`  | 0.40    | Recommend at this occupancy *if* a boundary signal is present. |
| `AUTOCOMPACTOR_HARD_PCT`  | 0.65    | Recommend unconditionally at this occupancy. |
| `AUTOCOMPACTOR_COOLDOWN`  | 25000   | Min tokens of growth between recommendations. |
| `AUTOCOMPACTOR_STALE_FRAC`| 0.50    | Stale-tool-output fraction that counts as a boundary signal. |
| `AUTOCOMPACTOR_POST_FLOOR`| 70000   | Estimated post-compaction context (measured ~69k median here). A compaction can only reclaim what sits above this. |
| `AUTOCOMPACTOR_MIN_SAVINGS` | 30000 | Min estimated reclaim (context − POST_FLOOR) to recommend; below it a compaction stalls 30-60s for almost nothing. |
| `AUTOCOMPACTOR_MAX_FULL_PARSE_MB` | 8 | Above this transcript size, parse only the active segment after the last compaction boundary (bounds worst-case hook latency). |
| `AUTOCOMPACTOR_OBSERVE_ONLY` | `error_resolved,tests_pass,idle_gap` | Signals logged to telemetry but never allowed to justify a recommendation (defaults measured anti-predictive on real corpora). Set empty to restore full gating. |
| `AUTOCOMPACTOR_ARTIFACT_BUDGET` | 1500 | Token budget for the post-compaction artifact digest. |
| `AUTOCOMPACTOR_LLM`       | unset   | `1` = PreCompact also runs a configured LLM over the transcript tail for a smarter must-preserve digest. Adds latency and its own token cost. |
| `AUTOCOMPACTOR_LLM_PROVIDER` | `claude` | Optional digest provider: `claude` (default), `openai`/`vllm` for OpenAI-compatible endpoints, or `command` when `AUTOCOMPACTOR_LLM_CMD` is set. |
| `AUTOCOMPACTOR_LLM_MODEL` | `haiku` | Model name for the optional digest. Site-local deployments can override this without changing public repo defaults. |
| `AUTOCOMPACTOR_LLM_BASE_URL` | unset | Base URL for `openai`/`vllm` provider, e.g. an OpenAI-compatible `/v1` endpoint. |
| `AUTOCOMPACTOR_LLM_CMD`   | unset   | Command template override for the optional digest. Supports `{model}` and `{prompt}` placeholders. |
| `AUTOCOMPACTOR_PI_MODE`   | `advise` | Pi only. `advise` = post a recommendation message; `actuate` = the extension calls `ctx.compact()` itself with the bridge-built instructions. The mode is delivered via the bridge's evaluate verdict from config.json (`pi.MODE`, shipped as `actuate`); this env var is a manual override. |
| `AUTOCOMPACTOR_PI_INTERCEPT` | unset | Pi only. `1` = cancel a native auto-compaction and re-trigger it enriched with our instructions. Default off; auto-disabled when `pi-custom-compactor` is configured. |
| `AUTOCOMPACTOR_PI_<NAME>` | —       | Pi-specific override for any tunable above (e.g. `AUTOCOMPACTOR_PI_SOFT_PCT`); falls back to the generic `AUTOCOMPACTOR_<NAME>`, then the default. |
| `AUTOCOMPACTOR_STATE_DIR` | unset   | Override the state root for BOTH harnesses (used by tests). Defaults: Claude `~/.claude/autocompactor`, Pi `~/.autocompactor/pi`. |

## Boundary signals detected

- `git commit` executed in the recent window
- all TodoWrite items completed / a plan step completed
- a subagent task just returned
- ≥50% of tool-result bytes in context are older than the recent window
- burn-rate projection (within ~8 turns of autocompact)
- topic shift in the incoming prompt

Observe-only (telemetry, never gating — measured anti-predictive on
real corpora; see `AUTOCOMPACTOR_OBSERVE_ONLY`): test-suite success
markers, debug-loop conclusion, long idle gap.

On Pi the todo-completion signal is dormant (Pi has no TodoWrite
analog), so boundary timing is carried by the commit, subagent-done,
stale-output, and burn-rate signals; the registry is shared, so the
signal simply never fires rather than being special-cased.

## Billing note

The recommendation path costs nothing — it's local Python parsing the
transcript JSONL (context occupancy is read from the `usage` block of the
last assistant message, the same technique ccusage uses). The compaction
itself is still a model call billed/quota'd normally; this system just
makes each one smaller, earlier, and better targeted. The optional
`AUTOCOMPACTOR_LLM=1` digest is the only feature that spends tokens.

## Caveats

- The transcript JSONL schema is not a stable public API. Pin your Claude
  Code version; re-run the included smoke test after upgrades.
- `transcript_path` has had reported bugs in some Claude Code versions
  (empty string on PreCompact). Both hooks degrade gracefully: backup and
  analysis are skipped, nothing breaks.
- Occupancy is an estimate; it doesn't account for the system prompt
  share that never appears in usage deltas, so treat thresholds as
  approximate and tune to taste with `/context` as ground truth.
- On subscription plans this saves *quota*, not dollars; on API billing
  it saves both.

## Smoke test

```bash
echo '{"session_id":"s1","transcript_path":"/path/to/real/session.jsonl","cwd":"'$PWD'","hook_event_name":"UserPromptSubmit","prompt":"x"}' \
  | python3 ~/.claude/hooks/context_monitor.py
```

Point it at a real transcript under `~/.claude/projects/` and confirm it
emits JSON (or exits silently below threshold).

### Full test matrix (Claude + Pi)

One sequence runs every gate in the repo — the Claude compat baseline
(unchanged from above) plus the Pi harness suites:

```bash
python3 -m pytest tests/ -q          # all harnesses' pytest cases
bash tests/smoke_test.sh             # Claude hook contract, isolated HOME
PI_SMOKE=1 bash tests/smoke_test_pi.sh  # Pi bridge contract, isolated HOME
node --test 'pi/test/*.test.mjs'     # Pi TS shim against stubbed pi/ctx
```

Per-harness expectations: the first two commands are the 100%
Claude-compatibility gate and must pass byte-identically with or without
the Pi files present; `smoke_test_pi.sh` is a no-op (exit 0) unless
`PI_SMOKE=1`, so plain CI runs are unaffected; the `node --test` suite
transpiles `pi/autocompactor.ts` with esbuild on the fly and needs no
Pi installation (note: pass the glob, not the bare directory — node 22
resolves a positional directory as a module and fails). Run the full matrix before any commit that touches
`pi_bridge.py`, `pi_session_lib.py`, `statedir.py`, or `pi/`.
