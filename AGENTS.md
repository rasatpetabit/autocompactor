# ras/autocompactor


<!-- agentic-dispatch:central-pointer v2 -->
## Central agent policy

Cross-repo AskUserQuestion/ask_user_question (AUQ), Serena, Hindsight,
context-mode, and subagent/model-dispatch policy is centralized in the
agent-dispatch repo. Read it via `agent-dispatch where` (repo root) or
`agent-dispatch digest` (live routing policy). Do not duplicate or override
that policy here.

## Project substance

Autocompactor is a **Pi context compactor**: it provides earlier,
instruction-tailored compaction for Pi coding-agent sessions — boundary-aware
timing for when to compact, phase-aware structured instructions for how to
summarize, mechanical artifact extraction for content that should not be
entrusted to a summarizer, and local telemetry. The core is harness-agnostic by
design; Pi is the sole adapter that ships. Read `HANDOFF.md` for the decision
record, including the Claude-adapter-removal rationale, the pi-custom-compactor
evaluation, and prioritized open items.

## Operating notes (harness-agnostic)

- Before changing behavior, run `python3 -m pytest tests/ -q` and
  `PI_SMOKE=1 bash tests/smoke_test_pi.sh` when safe. Current baseline is 100
  pytest cases.
- Owner directive: `>80%` of spend is cached reads. Compact often and keep
  context low.
- Every-turn cheapness relies on the min-savings guard: no recommendation when
  `context - POST_FLOOR(70k) < MIN_SAVINGS(30k)`, quiet below about `100k`.
- For transcripts larger than `MAX_FULL_PARSE_MB(8)`, use tail-only parsing
  from the last verified `compact_boundary`; `peak_ctx` is carried in session
  state for the window clamp.
- Open refinements: improve `topic_shift` precision with prompt replay at
  sample points; keep watching `stale_output`, which was below baseline at
  `0.90`.
- Signal gating (`OBSERVE_ONLY`) is the Pi conservative set: because Pi
  actuates (`ctx.compact()`), it must retain `subagent_done`/`commit` as strong
  gates (design trap #4). `error_resolved`/`tests_pass`/`idle_gap` are
  observe-only (anti-predictive on real corpora) — `active_signals()` still
  reports them for telemetry, but they never justify a recommendation.

## Conventions

- Transcript JSONL schema is not a public API. Pin the producer version and
  re-run smoke tests after upgrades.
- Telemetry and artifacts are local-only and content-free by design:
  counts/ratios/paths only, never transcript text.
- Hooks must never raise into the hook path. Degrade silently and log
  best-effort.
- `transcript_lib.active_signals()` is the single signal registry consumed by
  the Pi bridge; it is the one source of truth for which signals fire.

## Architecture

All implementation modules live in the `src/autocompactor/` package. The thin
shims in `src/*.py` are the entrypoints (the Pi bridge, cron, installer) — they
put `src/` on `sys.path` and call the matching `autocompactor.<module>.main()`.
`config.json` and `config.local.json` stay at the checkout root as
user-facing config. The core is harness-agnostic by design even though Pi is
the only adapter that ships.

| file | role |
|---|---|
| `src/autocompactor/transcript_lib.py` | signal registry, phase detection, instruction builder (shared brain) |
| `src/autocompactor/config_lib.py` | unified config reader: `config.json`(+local) + `AUTOCOMPACTOR_*` env overrides (single namespace) |
| `src/autocompactor/artifacts.py` | mechanical extraction -> disk -> budgeted digest |
| `src/autocompactor/llm_digest.py` | optional cheap-model "must-survive" digest (harness-agnostic; consumed by pi_bridge) |
| `src/autocompactor/stats.py` | telemetry appender |
| `src/autocompactor/statedir.py` | state root (`~/.autocompactor/pi`) |
| `src/autocompactor/window_resolver.py` | effective-window resolution (Pi runtime window: `contextWindow − reserveTokens`) |
| `src/autocompactor/nightly_eval.py` | cron self-evaluation: tests, telemetry health checks, dated reports, retention pruning |
| `src/autocompactor/pi_session_lib.py` | Pi v3 tree-format JSONL -> `TranscriptStats` |
| `src/autocompactor/pi_bridge.py` | never-raise JSON CLI bridging the Pi extension to the Python core (evaluate/prepare/reinject) |
| `src/autocompactor/install_pi.py` | Pi harness adapter installer (copy-with-rewrite TS shim, version-pin) |
| `src/*.py` | thin entrypoint shims (Pi bridge / cron / installer): `pi_bridge`, `nightly_eval`, `install_pi` |
| `src/pi/autocompactor.ts` | Pi TypeScript extension |
| `config.json` | versioned single-namespace tuning; `config.local.json` is the gitignored site-local overlay |
| `tests/` | fixtures + `smoke_test_pi.sh` + `test_*.py` |

## Harness adapters

Pi is the sole adapter. The core is harness-agnostic by design, but only the
Pi adapter ships.

- **Pi** (`@earendil-works/pi-coding-agent`) — `python3 src/install_pi.py`
  installs `src/pi/autocompactor.ts`, which shells out to `src/pi_bridge.py`
  (the shared Python core). State/telemetry live under `~/.autocompactor/pi/`.
  See `HANDOFF.md` ("Pi harness") for the architecture, actuate-vs-advise
  decision, and verified ground-truth pins.
<!-- agent-dispatch:begin routing hash=6d8307801e22f016588774ae010198516e8402aa6a7cd0724a433885e67b981b -->
## §routing — managed by agent-dispatch (do not hand-edit)

Binding rules (enforced by PreToolUse guard — violations are hard-blocked):
- haiku: FORBIDDEN — no dispatch path exists, no override possible.
- sonnet: OVERRIDE-ONLY — requires a live, unexpired grant (`agent-dispatch override grant sonnet …`).
- model param MUST be explicit — missing model is denied (exception: harness built-ins Explore/Plan inherit the frontier session model).

For the full routing policy, fallback chains, and backend health:
  agent-dispatch digest          # live, from the canonical policy file
  agent-dispatch resolve <class> # deterministic tier for a task class

Source of truth: policy/dispatch-policy.jsonc in the agent-dispatch repo (run `agent-dispatch where` for its root).
<!-- agent-dispatch:end -->
