# Autocompactor Pi-only Pivot Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Drop the Claude Code adapter entirely, complete the Pi parser's field-completion + extract the shared `llm_digest` subsystem first, then delete all pure-Claude modules/tests/hooks and flatten the dual-harness scaffolding into a single Pi-only product.

**Architecture:** The harness-agnostic core (`transcript_lib`, `policy`, `artifacts`, `stats`) stays, fed solely by `pi_session_lib → TranscriptStats`. The Claude PreCompact/UserPromptSubmit/PostToolUse hooks, installer, window-resolver native-ceiling logic, and `config.json` `claude`/`pi` section split are removed; the `pi.*` config overlay is materialized flat at top-level. The `llm_digest` provider helpers move out of `precompact_analyzer.py` into a new harness-agnostic `llm_digest.py` that `pi_bridge` imports.

**Tech Stack:** Python 3 (pytest), TypeScript (bun) for the Pi shim, Pi harness (@earendil-works/pi-coding-agent)

## Global Constraints
- Behavior-preserving deletion: never rename/restructure agnostic functions in a deletion pass.
- Pi parser field-completion + llm_digest extraction (Phase 2) MUST precede any Claude removal.
- Hooks/bridge never raise into the host path — degrade silently, exit 0.
- Telemetry + readouts are content-free (counts/ratios/paths/category-names only); artifacts on disk hold verbatim content intentionally.
- transcript_lib.active_signals() stays the single signal registry.
- Pi gate pin: keep subagent_done/commit as strong gates; drop only todos_done/todo_step.
- Green gate (pytest + PI_SMOKE smoke) green after EVERY task.

### Test-count trajectory (stated per deletion phase)
Baseline: **198 passed**. Deletions and additions, in plan order:
- Phase 2a: +llm_digest extraction tests (~3) → ~201
- Phase 2b: +Pi field-completion tests (~4) → ~205
- Phase 3: −46 Claude-hook-family tests in `test_autocompactor.py`; −`test_install.py` (9, whole file); −`test_compat_pins.py` Claude-contract assertions (file deleted, ~6) → ~144
- Phase 4: −2 todo-signal tests; migrate remaining agnostic-brain tests onto Pi fixtures (count ~unchanged, re-fixtured not deleted) → ~142
- Phase 5: −`test_window_resolver.py` (10, whole file) → ~132
- Phase 6: +equivalence + flat-config tests (~3) → ~135
- Phase 7: docs only, no test change → **~135**

The numbers above are projections; each phase Task records the **actual** `pytest` count after its deletions and states the drift versus the prior phase.

---

### Task 1: Pre-deletion safety tag
**Files:** Modify (git tag only; no source files)
**Interfaces:** Produces — git tag `pre-pi-only-pivot` marking the last dual-harness commit, so the full Claude adapter is recoverable.

- [ ] **Step 1: Verify clean baseline** — confirm tree builds and tests pass before tagging.
  - Run: `python3 -m pytest tests/ -q` → Expected: `198 passed`
  - Run: `PI_SMOKE=1 bash tests/smoke_test_pi.sh` → Expected: `ALL PI SMOKE TESTS PASSED`
- [ ] **Step 2: Create the annotated tag**
  ```bash
  git tag -a pre-pi-only-pivot -m "Last dual-harness (Claude+Pi) commit before Pi-only pivot (spec 2026-06-21)"
  ```
- [ ] **Step 3: Verify the tag exists**
  - Run: `git tag --list pre-pi-only-pivot` → Expected: `pre-pi-only-pivot`
- [ ] **Step 4: Commit** — the tag is the artifact; no file change. Record in WORKLOG only at Phase 7. (No commit this task; the tag itself is durable.)

---

### Task 2a: Extract `llm_digest.py` (kept, harness-agnostic)
**Files:**
- Create: `src/autocompactor/llm_digest.py`
- Modify: `src/autocompactor/pi_bridge.py:49` (repoint import), keep call site `:253-256` unchanged
- Test: `tests/test_llm_digest.py` (new)

**Interfaces:**
- Produces — `llm_digest.llm_digest(transcript_path: str) -> str` (verbatim move of `precompact_analyzer.llm_digest`, `:145-169`), plus the unchanged helpers `_env(name, default="")`, `_llm_timeout() -> float`, `_openai_url(base) -> str`, `_llm_digest_openai(prompt, model, timeout) -> str`, `_llm_digest_command(...) -> str`, `_llm_digest_claude(...) -> str`. `_env` keeps its `config_lib.cfg.str(..., default=default)` body (default harness, unchanged — the harness flatten is Phase 6, not here).
- Consumes — `config_lib.cfg`.

- [ ] **Step 1: Write the failing test**
  ```python
  # tests/test_llm_digest.py
  import os, tempfile
  from autocompactor import llm_digest

  def test_openai_url_normalizes():
      assert llm_digest._openai_url("http://h/v1") == "http://h/v1/chat/completions"
      assert llm_digest._openai_url("http://h/v1/chat/completions") == "http://h/v1/chat/completions"
      assert llm_digest._openai_url("http://h") == "http://h/v1/chat/completions"

  def test_llm_digest_disabled_returns_empty(monkeypatch):
      # No provider configured + unreadable path -> never raises, returns "".
      monkeypatch.delenv("AUTOCOMPACTOR_LLM_CMD", raising=False)
      assert llm_digest.llm_digest("/nonexistent/path.jsonl") == ""

  def test_pi_bridge_imports_llm_digest_from_new_module():
      import autocompactor.pi_bridge as pb
      assert pb.llm_digest.__module__ == "autocompactor.llm_digest"
  ```
- [ ] **Step 2: Run test to verify it fails**
  - Run: `python3 -m pytest tests/test_llm_digest.py -q` → Expected: FAIL (`ModuleNotFoundError: autocompactor.llm_digest`).
- [ ] **Step 3: Implement** — create the new module by moving the subsystem verbatim from `precompact_analyzer.py:51-169` (the `_env` through `llm_digest` block), with the imports it needs.
  ```python
  #!/usr/bin/env python3
  """llm_digest.py — optional cheap-model "what must survive compaction" digest.

  Harness-agnostic. Extracted from precompact_analyzer.py in the Pi-only pivot;
  one live consumer (pi_bridge). Never raises into the caller — returns "".
  """
  import json
  import shlex
  import subprocess
  import urllib.request

  from autocompactor import config_lib


  def _env(name: str, default: str = "") -> str:
      if not name.startswith("AUTOCOMPACTOR_"):
          import os
          return os.environ.get(name, default)
      return config_lib.cfg.str(name[len("AUTOCOMPACTOR_"):], default=default)


  def _llm_timeout() -> float:
      try:
          return float(_env("AUTOCOMPACTOR_LLM_TIMEOUT", "45"))
      except ValueError:
          return 45.0


  def _openai_url(base: str) -> str:
      base = base.rstrip("/")
      if base.endswith("/chat/completions"):
          return base
      if base.endswith("/v1"):
          return base + "/chat/completions"
      return base + "/v1/chat/completions"


  def _llm_digest_openai(prompt: str, model: str, timeout: float) -> str:
      base = _env("AUTOCOMPACTOR_LLM_BASE_URL")
      if not base:
          return ""
      payload = {
          "model": model,
          "messages": [
              {"role": "system", "content": "Return terse bullets only."},
              {"role": "user", "content": prompt},
          ],
          "temperature": 0,
          "max_tokens": int(float(_env("AUTOCOMPACTOR_LLM_MAX_TOKENS", "512"))),
      }
      raw_extra = _env("AUTOCOMPACTOR_LLM_EXTRA_JSON")
      if raw_extra:
          try:
              extra = json.loads(raw_extra)
              if isinstance(extra, dict):
                  payload.update(extra)
          except Exception:
              pass
      req = urllib.request.Request(
          _openai_url(base),
          data=json.dumps(payload).encode("utf-8"),
          headers={
              "Content-Type": "application/json",
              "Authorization": "Bearer " + _env(
                  "AUTOCOMPACTOR_LLM_API_KEY",
                  _env("OPENAI_API_KEY", "EMPTY"),
              ),
          },
          method="POST",
      )
      with urllib.request.urlopen(req, timeout=timeout) as resp:
          data = json.loads(resp.read().decode("utf-8"))
      return ((data.get("choices") or [{}])[0]
              .get("message", {}).get("content", "").strip())


  def _llm_digest_command(prompt: str, model: str, timeout: float) -> str:
      template = _env("AUTOCOMPACTOR_LLM_CMD")
      if not template:
          return ""
      uses_prompt_arg = "{prompt}" in template
      rendered = template.format(model=model, prompt=prompt)
      cmd = shlex.split(rendered)
      res = subprocess.run(
          cmd,
          input=None if uses_prompt_arg else prompt,
          capture_output=True,
          text=True,
          timeout=timeout,
      )
      return res.stdout.strip() if res.returncode == 0 else ""


  def _llm_digest_claude(prompt: str, model: str, timeout: float) -> str:
      res = subprocess.run(
          ["claude", "-p", "--model", model, prompt],
          capture_output=True, text=True, timeout=timeout,
      )
      return res.stdout.strip() if res.returncode == 0 else ""


  def llm_digest(transcript_path: str) -> str:
      """Optional: ask a configured cheap model what must survive compaction."""
      try:
          import os
          with open(os.path.expanduser(transcript_path), encoding="utf-8") as fh:
              tail_lines = fh.readlines()[-120:]
          prompt = (
              "Below are the most recent entries of a coding-session transcript "
              "(JSONL). List, as terse bullets, the facts that MUST survive a "
              "context compaction: current task, file paths touched, key "
              "decisions, working commands, unresolved errors. Bullets only.\n\n"
              + "".join(tail_lines)[-30_000:]
          )
          model = _env("AUTOCOMPACTOR_LLM_MODEL", "haiku")
          timeout = _llm_timeout()
          provider = _env("AUTOCOMPACTOR_LLM_PROVIDER", "claude").lower()
          if _env("AUTOCOMPACTOR_LLM_CMD"):
              provider = "command"
          if provider in ("openai", "openai-compatible", "vllm"):
              return _llm_digest_openai(prompt, model, timeout)
          if provider == "command":
              return _llm_digest_command(prompt, model, timeout)
          return _llm_digest_claude(prompt, model, timeout)
      except Exception:
          return ""
  ```
  Then repoint `pi_bridge.py:49`:
  ```python
  from autocompactor.llm_digest import llm_digest                 # noqa: E402
  ```
  Leave `precompact_analyzer.py:51-169` in place for now (deleted with the rest of the hook in Phase 3) — it still imports cleanly so the test suite stays green; only the *consumer* import moves here.
- [ ] **Step 4: Run test to verify it passes**
  - Run: `python3 -m pytest tests/test_llm_digest.py tests/test_pi_bridge.py -q` → Expected: PASS
  - Run: `python3 -m pytest tests/ -q` → Expected: PASS (~201; +3 vs 198)
  - Run: `PI_SMOKE=1 bash tests/smoke_test_pi.sh` → Expected: `ALL PI SMOKE TESTS PASSED`
- [ ] **Step 5: Commit**
  ```bash
  git add src/autocompactor/llm_digest.py src/autocompactor/pi_bridge.py tests/test_llm_digest.py
  git commit -m "Extract llm_digest subsystem into kept harness-agnostic module"
  ```

---

### Task 2b: Pi parser field-completion (`assistant_text_chars` / `user_prompt_chars` / `summary_chars`)
**Files:**
- Modify: `src/autocompactor/pi_session_lib.py` — assistant branch `:278-302` (accumulate `assistant_text_chars`), user branch `:328-335` (accumulate `user_prompt_chars`), and a pre-segment summary scan inside `analyze()` (`:232-237` region)
- Test: `tests/test_pi_session_lib.py` (extend)

**Interfaces:**
- Produces — `pi_session_lib.analyze(path)` now populates `st.assistant_text_chars` (text **+ thinking**, via `_message_text(msg, include_thinking=True)`), `st.user_prompt_chars` (genuine user turns, excluding `/`-commands and `<command-name>`), and `st.summary_chars` (single-sourced: `compactionSummary` role **>** `compaction` entry, never both). `st.skill_chars`/`st.skill_names` stay 0/`[]` (Spec A scope).
- Consumes — `transcript_lib.context_composition` reads these via `getattr(...,0)` (`:512-552`), already present.

- [ ] **Step 1: Write the failing test**
  ```python
  # tests/test_pi_session_lib.py (append)
  from autocompactor import pi_session_lib

  FIX = "tests/fixtures/pi"

  def test_assistant_text_chars_includes_thinking():
      st = pi_session_lib.analyze(f"{FIX}/real_shapes.jsonl")
      # real_shapes.jsonl:5 carries a thinking block; field must count it.
      assert st.assistant_text_chars > 0

  def test_user_prompt_chars_populated():
      st = pi_session_lib.analyze(f"{FIX}/linear.jsonl")
      assert st.user_prompt_chars > 0

  def test_summary_chars_single_source():
      st = pi_session_lib.analyze(f"{FIX}/with_compaction.jsonl")
      # with_compaction.jsonl:10-11 has BOTH a compactionSummary role and a
      # compaction entry; summary_chars counts the summary once, never both.
      assert st.summary_chars > 0

  def test_skills_remain_zero_spec0():
      st = pi_session_lib.analyze(f"{FIX}/linear.jsonl")
      assert st.skill_chars == 0 and st.skill_names == []
  ```
- [ ] **Step 2: Run test to verify it fails**
  - Run: `python3 -m pytest tests/test_pi_session_lib.py -q -k "thinking or prompt_chars or summary_chars"` → Expected: FAIL (fields default 0; assertions `> 0` fail).
- [ ] **Step 3: Implement** — three accumulations in `analyze()`.
  In the assistant branch (after `:282`, inside `if role == "assistant":`):
  ```python
            st.assistant_text_chars += len(_message_text(msg, include_thinking=True))
  ```
  In the user branch (`:328-335`), after the `if text and not text.startswith("/")...` guard:
  ```python
          elif role == "user":
              text = _message_text(msg).strip()
              if text and not text.startswith("/") and "<command-name>" not in text:
                  st.user_prompt_chars += len(text)
                  st.last_user_task = text[:500]
                  if is_recent:
                      st.recent_words |= transcript_lib._content_words(text)
                  if transcript_lib.CORRECTION_RE.search(text):
                      st.corrections.append(text[:200])
  ```
  Single-sourced summary scan — insert into `analyze()` right after `st.compaction_count = compaction_count` (`:237`), scanning the **full path** (summary sits before the active segment):
  ```python
      # summary_chars: single-source the carried compaction summary.
      # Prefer an explicit `compactionSummary`-role message; fall back to the
      # `compaction` entry's own summary text. Never count both (double-count
      # guard, spec §9). Scan the full path, not the active segment — the
      # summary lives just before the cut.
      summary_text = ""
      for entry in full_path:
          m = _message(entry)
          if m.get("role") == "compactionSummary":
              summary_text = _message_text(m, include_thinking=True)
              break
      if not summary_text:
          for entry in full_path:
              if entry.get("type") == "compaction":
                  summary_text = _message_text(_message(entry), include_thinking=True)
                  break
      st.summary_chars = len(summary_text)
  ```
- [ ] **Step 4: Run test to verify it passes**
  - Run: `python3 -m pytest tests/test_pi_session_lib.py -q` → Expected: PASS
  - Run: `python3 -m pytest tests/ -q` → Expected: PASS (~205; +4 vs Task 2a)
  - Run: `PI_SMOKE=1 bash tests/smoke_test_pi.sh` → Expected: `ALL PI SMOKE TESTS PASSED`
- [ ] **Step 5: Commit**
  ```bash
  git add src/autocompactor/pi_session_lib.py tests/test_pi_session_lib.py
  git commit -m "Populate assistant_text_chars/user_prompt_chars/summary_chars in Pi parser"
  ```

---

### Task 3: Delete pure-Claude modules, hooks, installer, and Claude-contract tests
**Files:**
- Delete: `src/autocompactor/context_monitor.py`, `src/context_monitor.py`
- Delete: `src/autocompactor/install.py`, `src/install.py`
- Delete: `src/precompact_analyzer.py` (shim)
- Modify: `src/autocompactor/precompact_analyzer.py` — remove the Claude-hook portion `:172-410` (`_run`/`_run_precompact`/`_run_postcompact`/`main`) AND the now-orphaned `llm_digest` subsystem `:51-169` (moved to `llm_digest.py` in 2a); the file becomes empty of live code → **delete the file entirely**
- Delete tests: in `tests/test_autocompactor.py` remove the 46 Claude-hook-family tests (`test_monitor_*`, `test_analyzer_*`, `test_postcompact_*`, `test_posttooluse_*`, `test_userpromptsubmit_*`, `test_run_hook_*`, `test_hooks_*` Claude variants, `test_find_last_boundary_offset_*`, `test_find_compactions_*`, `test_current_context_tokens_*`, `test_backtest_*`); delete `tests/test_install.py` (9 tests, whole file); delete `tests/test_compat_pins.py` (imports `context_monitor`/`precompact_analyzer`, Claude STATE_DIR pins — whole file)
- Modify: `tests/smoke_test.sh` (Claude smoke) — delete the file; `smoke_test_pi.sh` is the kept oracle

**This is a pure-deletion task — TDD inverts: the green gate proves nothing broke.**

- [ ] **Step 1: Confirm no live importer remains before deleting** — the only `precompact_analyzer` importers must be files deleted in this same task.
  - Run: `grep -rln "precompact_analyzer" src/ tests/` → Expected: only `tests/test_autocompactor.py` (deleted families), `tests/test_compat_pins.py` (deleted), `src/precompact_analyzer.py` (deleted), `src/autocompactor/context_monitor.py` (deleted), `src/autocompactor/install.py` (deleted), and the comment-only ref in `transcript_lib.py:807`. **`pi_bridge.py` must NOT appear** (repointed in 2a). If it does, stop — 2a is incomplete.
  - Run: `grep -rln "import context_monitor\|from autocompactor import.*context_monitor\|context_monitor" src/ tests/` → Expected: only deleted files.
- [ ] **Step 2: Perform the deletions**
  ```bash
  git rm src/autocompactor/context_monitor.py src/context_monitor.py \
         src/autocompactor/install.py src/install.py \
         src/precompact_analyzer.py src/autocompactor/precompact_analyzer.py \
         tests/test_install.py tests/test_compat_pins.py tests/smoke_test.sh
  ```
  Then in `tests/test_autocompactor.py`, delete each function in the 46-test Claude-hook families listed above (leave the agnostic-brain tests — `test_active_signals_*`, `test_context_composition_*`, `test_detect_phase_*`, `test_build_preservation_*`, etc. — in place; they migrate in Phase 4). Also remove now-dead top-of-file imports of `context_monitor`/`precompact_analyzer` from `test_autocompactor.py`.
  Update the comment at `transcript_lib.py:807` ("Shared by both PreCompact paths (Claude precompact_analyzer + Pi bridge…") to name only the Pi bridge.
- [ ] **Step 3: Run the green gate (the deletion's "test")**
  - Run: `python3 -m pytest tests/ -q` → Expected: PASS. Record the count: ~144 (drift −57 vs ~201 in Task 2b: −46 families −9 install −~6 compat_pins +adjustments). State the actual number observed.
  - Run: `PI_SMOKE=1 bash tests/smoke_test_pi.sh` → Expected: `ALL PI SMOKE TESTS PASSED`
- [ ] **Step 4: Verify no Claude hook references linger**
  - Run: `grep -rln "UserPromptSubmit\|PreCompact\|PostToolUse\|_run_precompact" src/ tests/` → Expected: no matches in `src/`; only spec/plan docs if any.
- [ ] **Step 5: Commit**
  ```bash
  git add -A
  git commit -m "Delete Claude adapter: hooks, installer, monitor, precompact_analyzer + their tests"
  ```

---

### Task 4: Slice the Claude front-end out of `transcript_lib`, drop todo signals, quarantine `analyze_corpus`, re-fixture agnostic tests
**Files:**
- Modify: `src/autocompactor/transcript_lib.py` — remove the Claude-JSONL front-end (`load_transcript`, `find_last_boundary_offset`, `current_context_tokens`, the `isMeta` skill scan); drop the two todo signals at `active_signals` `:581-584`
- Modify: `src/autocompactor/pi_session_lib.py:353-358` — remove the `if st.todos:` todo-boolean derivation (todos never populated on Pi; signals dropped)
- Delete: `src/autocompactor/analyze_corpus.py` + `src/analyze_corpus.py` shim (Claude backtester; Pi backtester is Spec B, out of scope) — OR quarantine if any kept module imports it (verify first)
- Modify: `tests/test_autocompactor.py` — delete the two todo-signal tests (`test ... todos_done`, `test ... todo_step`, lines ~120/133/135/149); re-fixture remaining agnostic-brain tests so they build `TranscriptStats` from `pi_session_lib.analyze(tests/fixtures/pi/*.jsonl)` instead of a Claude transcript

**Highest-risk phase; behavior-preserving deletion only, no renames. The green gate is the regression oracle.**

- [ ] **Step 1: Verify the Claude front-end functions have no kept-module callers**
  - Run: `grep -rln "load_transcript\|find_last_boundary_offset\|current_context_tokens" src/autocompactor/` → Expected: only `transcript_lib.py` itself (callers were `context_monitor`/`precompact_analyzer`, already deleted). If `pi_bridge.py` appears, it uses `pi_session_lib.analyze` instead — confirm before removing.
  - Run: `grep -rln "analyze_corpus" src/ tests/` → Expected: only the shim + its own tests; confirm `pi_bridge`/`nightly_eval` do not import it (if `nightly_eval` does, defer its slice to Phase 5).
- [ ] **Step 2: Perform the slice + signal drop**
  - In `transcript_lib.py`: delete `load_transcript`, `find_last_boundary_offset`, `current_context_tokens`, and the `isMeta` skill-scan helper bodies. Delete the two todo branches at `active_signals` (`:581-584`):
  ```python
      # (removed) todos_done / todo_step — Pi never populates st.todos; Spec 0
      # drops both signals. subagent_done/commit remain the strong Pi gates.
  ```
  - In `pi_session_lib.py`, remove `:353-358`:
  ```python
      # (removed) todo-boolean derivation — todos_done/todo_step signals dropped.
  ```
  - Delete the Claude backtester:
  ```bash
  git rm src/autocompactor/analyze_corpus.py src/analyze_corpus.py
  ```
  - In `tests/test_autocompactor.py`: delete the two todo-signal tests; rewrite the remaining agnostic-brain tests to source `st` from `pi_session_lib.analyze("tests/fixtures/pi/<fixture>.jsonl")`. Example migration shape:
  ```python
      from autocompactor import pi_session_lib, transcript_lib as tl
      st = pi_session_lib.analyze("tests/fixtures/pi/with_compaction.jsonl")
      sigs = dict(tl.active_signals(st))
      assert "todos_done" not in sigs and "todo_step" not in sigs
  ```
- [ ] **Step 3: Run the green gate**
  - Run: `python3 -m pytest tests/ -q` → Expected: PASS (~142; drift −2 todo tests; migrated tests count unchanged). State the actual number.
  - Run: `PI_SMOKE=1 bash tests/smoke_test_pi.sh` → Expected: `ALL PI SMOKE TESTS PASSED`
- [ ] **Step 4: Verify the registry has exactly the kept signals**
  - Run: `grep -n "sigs.append" src/autocompactor/transcript_lib.py` → Expected: `commit`, `tests_pass`, `error_resolved`, `subagent_done`, `idle_gap`, `stale_output`, `burn_rate` — and NO `todos_done`/`todo_step`.
- [ ] **Step 5: Commit**
  ```bash
  git add -A
  git commit -m "Slice Claude transcript front-end + todo signals; re-fixture agnostic tests onto Pi"
  ```

---

### Task 5: Slim `window_resolver` and `nightly_eval` of Claude-only logic
**Files:**
- Modify: `src/autocompactor/window_resolver.py` — delete native-ceiling/learned-tier Claude machinery: `native_ceiling_from_settings` (`:102-108`), `pct_override_from_settings` (`:111-123`), `native_auto_estimate` (`:126-134`), `readout_anchors` (`:137-155`), and the `native_ceiling` cap branch + Claude/small-session-clamp asymmetry in `resolve_window` (`:182-218`). Keep the minimal Pi residue: `resolve_window` returning `effective = runtime_context_window − reserve` (the authoritative Pi path), or inline that into `pi_bridge.cmd_evaluate` (`:130-136`)
- Modify: `src/autocompactor/nightly_eval.py` — remove Claude-only checks (auto-warning-coverage native-ceiling epoch filter, native-microcompaction markers, `analyze_corpus` backtest invocation if present)
- Delete: `tests/test_window_resolver.py` (10 tests, whole file — all assert Claude native-ceiling behavior)
- Modify: `tests/test_nightly_eval.py` — drop Claude-only check assertions, keep Pi-applicable health checks

**Pure-deletion / slim task — green gate is the oracle.**

- [ ] **Step 1: Confirm window_resolver Pi callers + nightly_eval scope**
  - Run: `grep -rn "resolve_window\|readout_anchors\|native_ceiling\|native_auto_estimate" src/autocompactor/pi_bridge.py` → Expected: only `resolve_window(...)` at `:130-136` (Pi passes `runtime_context_window`; the native-ceiling args are unused on Pi). `readout_anchors`/`native_*` must NOT appear.
  - Run: `grep -rn "native\|analyze_corpus\|microcompaction" src/autocompactor/nightly_eval.py` → Expected: enumerate the Claude-only check sites to remove.
- [ ] **Step 2: Perform the slim**
  - In `window_resolver.py`, delete the four settings/estimate functions and the `native`-cap branch; reduce `resolve_window` to the runtime/configured Pi path:
  ```python
  def resolve_window(configured_window: float, observed_peak: int,
                     runtime_context_window: int | None = None,
                     reserve: int = 0) -> WindowResolution:
      configured = max(float(configured_window or 0), 1.0)
      reserve = max(_int_or_none(reserve) or 0, 0)
      runtime = _int_or_none(runtime_context_window)
      tier_values = tiers("pi")
      if runtime:
          learned, source = _nearest_tier(runtime, tier_values), "runtime"
          effective = float(max(runtime - reserve, 1))
      else:
          learned, source = _nearest_tier(int(configured), tier_values), "configured"
          effective = float(max(configured - reserve, 1))
      return WindowResolution(
          effective_window=effective, configured_window=configured,
          learned_window=int(learned), learned_tier=tier_label(int(learned)),
          window_source=source, runtime_context_window=runtime, reserve=reserve)
  ```
  Drop the now-unused `WindowResolution` native-ceiling fields (`native_ceiling`, `native_ceiling_blocks_learned_window`) and their `event_fields()` entries. Update `pi_bridge.cmd_evaluate:130-136` to call `resolve_window` without `harness=`/`native_ceiling=`.
  - Delete `tests/test_window_resolver.py`:
  ```bash
  git rm tests/test_window_resolver.py
  ```
  - In `nightly_eval.py` + `test_nightly_eval.py`, remove the Claude-only checks/assertions identified in Step 1.
- [ ] **Step 3: Run the green gate**
  - Run: `python3 -m pytest tests/ -q` → Expected: PASS (~132; drift −10 window_resolver file). State the actual number.
  - Run: `PI_SMOKE=1 bash tests/smoke_test_pi.sh` → Expected: `ALL PI SMOKE TESTS PASSED`
- [ ] **Step 4: Verify no native-ceiling references remain in src**
  - Run: `grep -rn "native_ceiling\|CLAUDE_CODE_AUTO_COMPACT_WINDOW\|pct_override" src/` → Expected: no matches.
- [ ] **Step 5: Commit**
  ```bash
  git add -A
  git commit -m "Slim window_resolver to Pi runtime-window path; drop Claude native-ceiling logic from nightly_eval"
  ```

---

### Task 6: Full flatten — single-namespace config, drop `harness=` params, repoint state dirs, strip TS shim Pi/CFG reads
**Files:**
- Modify: `config.json` — materialize the FULL effective Pi config (every key Pi reads = `pi.*` overlaid on top-level) as flat top-level; delete the `claude` and `pi` sections. **STALE_FRAC trap:** top-level becomes `0.90`? NO — the effective Pi value: `pi` section omits `STALE_FRAC`, so top-level `0.90` was the effective Pi value already → flat top-level keeps `0.90`. But `pi_bridge` default is `0.50`; the flat file must explicitly carry `0.90` so removing the section can't silently revert to the code default.
- Modify: `src/autocompactor/config_lib.py` — drop harness-section precedence (`_try_float` `:116-123`, `str`/`raw` harness branches), drop `AUTOCOMPACTOR_PI_*` from `_env_chain`/`_env_chain_windowed` (`:69-95`); precedence becomes `env → config.local.json → config.json → default`
- Modify: `src/autocompactor/policy.py` — drop `PolicyInput.harness` (`:316`) + `forced_auto` native anchor in `readout_line` (`:109-117`); remove `harness=` from `resolve_policy_config` call chain or default it to `"pi"`
- Modify: `src/autocompactor/stats.py:25-32` — repoint `STATS_DIR` fallback to `~/.autocompactor/pi/stats`; drop `harness` field
- Modify: `src/autocompactor/artifacts.py:34-44` — repoint `ART_DIR` + `_artifact_dir` fallback to `~/.autocompactor/pi/artifacts`
- Modify: `src/autocompactor/statedir.py` — remove harness-namespacing machinery; keep literal `~/.autocompactor/pi/`, keep `state_root(*args)`-compatible signature
- Modify: `src/pi/autocompactor.ts:50-61,76-86` — remove `CFG?.pi` lookups in `cfgNum` and `AUTOCOMPACTOR_PI_*` env reads; single `AUTOCOMPACTOR_*` namespace
- Modify: remove `harness=`/`harness="pi"` params at `pi_bridge`/`policy`/`stats`/`config_lib` call sites
- Test: `tests/test_config_lib.py`, `tests/test_statedir.py`, `tests/test_policy.py`, plus a new effective-Pi-config equivalence test

**Interfaces:**
- Produces — `config_lib.cfg.float(name, default=...)`/`str(...)` with no `harness` arg (or a defaulted-to-pi compat shim); flat `config.json`; all state/artifacts under `~/.autocompactor/pi/`.

- [ ] **Step 1: Write the failing equivalence + flat-config test** — snapshot EVERY key Pi reads (not just `pi`-section keys) and assert the flat config yields identical effective values.
  ```python
  # tests/test_config_lib.py (append)
  from autocompactor.config_lib import cfg

  # Every config key the Pi path reads (pi_bridge, policy, config_lib, TS shim).
  PI_KEYS_FLOAT = {
      "WINDOW": 200000, "RESERVE": 40000, "SOFT_PCT": 0.25, "HARD_PCT": 0.90,
      "COOLDOWN": 20000, "STALE_FRAC": 0.90, "POST_FLOOR": 70000,
      "MIN_SAVINGS": 30000, "ARTIFACT_BUDGET": 1500,
  }
  PI_KEYS_STR = {"MODE": "actuate", "PROFILE": "economy",
                 "OBSERVE_ONLY": "error_resolved,tests_pass,idle_gap"}

  def test_flat_config_preserves_effective_pi_values():
      # STALE_FRAC canary: pi section omitted it, top-level 0.90 was effective;
      # flat config must carry 0.90 (not revert to pi_bridge's 0.50 default).
      assert cfg.float("STALE_FRAC", default=0.50) == 0.90
      for k, v in PI_KEYS_FLOAT.items():
          assert cfg.float(k, default=-1) == v, k
      for k, v in PI_KEYS_STR.items():
          assert cfg.str(k, default="") == v, k

  def test_no_harness_sections_or_pi_env_prefix():
      import json, os
      from autocompactor import config_lib
      data = json.load(open(config_lib._CONFIG))
      assert "claude" not in data and "pi" not in data
  ```
  (Use the effective Pi values from the pre-flatten snapshot: `SOFT_PCT=0.25` if the `pi` `SOFT_PCT_WIDE` is the active one for the live window, else the flat `SOFT_PCT`; capture the real snapshot in Step 2 before editing.)
- [ ] **Step 2: Snapshot effective Pi values BEFORE editing, then run test to verify it fails**
  - Run (snapshot): `python3 -c "from autocompactor.config_lib import cfg; print({k: cfg.float(k, harness='pi', default=None) for k in ['WINDOW','RESERVE','SOFT_PCT','HARD_PCT','COOLDOWN','STALE_FRAC','POST_FLOOR','MIN_SAVINGS','ARTIFACT_BUDGET']}); print({k: cfg.str(k, harness='pi') for k in ['MODE','PROFILE','OBSERVE_ONLY']})"` → record the dict; fill the test's expected values from it.
  - Run: `python3 -m pytest tests/test_config_lib.py -q -k "flat_config or harness_sections"` → Expected: FAIL (`claude`/`pi` sections still present; `harness=` still required).
- [ ] **Step 3: Implement the flatten**
  - `config.json` → flat, sections removed, effective Pi values materialized:
  ```json
  {
    "_comment": "autocompactor config (Pi sole adapter). Flat single-namespace; AUTOCOMPACTOR_* env overrides; site-local in config.local.json.",
    "MODE": "actuate",
    "PROFILE": "economy",
    "WINDOW": 200000,
    "RESERVE": 40000,
    "SOFT_PCT": 0.50,
    "SOFT_PCT_WIDE": 0.25,
    "HARD_PCT": 0.90,
    "HARD_PCT_WIDE": 0.40,
    "STALE_FRAC": 0.90,
    "COOLDOWN": 20000,
    "POST_FLOOR": 70000,
    "MIN_SAVINGS": 30000,
    "MAX_FULL_PARSE_MB": 8,
    "OBSERVE_ONLY": "error_resolved,tests_pass,idle_gap",
    "ARTIFACT_BUDGET": 1500,
    "AUTO_WINDOW_TIERS": [200000, 300000, 512000, 1000000],
    "AUTO_WINDOW_PROMOTE_FRAC": 0.95
  }
  ```
  (Fill the exact numbers from the Step-2 snapshot; the above is the expected materialization — `MODE=actuate`/`STALE_FRAC=0.90` are the canaries.)
  - `config_lib.py`: delete `_env_chain` Pi prefix, `_env_chain_windowed` Pi prefix, harness-section branches in `_try_float`/`str`/`raw`; `harness` param becomes inert (default `"pi"`, ignored) for call-site compat.
  - `policy.py`: drop `PolicyInput.harness`; in `readout_line` drop the `forced_auto` block (`:109-117`) and its params.
  - `stats.py`: `STATS_DIR = os.path.expanduser("~/.autocompactor/pi/stats")`; drop `harness` field from `log_event`.
  - `artifacts.py`: `ART_DIR = os.path.expanduser("~/.autocompactor/pi/artifacts")`.
  - `statedir.py`: collapse `state_root` to return `~/.autocompactor/pi` (honoring `AUTOCOMPACTOR_STATE_DIR`), keep `*args` signature.
  - `src/pi/autocompactor.ts`: `cfgNum` reads `CFG?.[key]` only (drop `CFG?.pi?.[key]`); thresholds read `num("AUTOCOMPACTOR_<KEY>", cfgNum(...))` (drop `AUTOCOMPACTOR_PI_*`).
  - Remove `harness=`/`harness="pi"` kwargs at all call sites flagged by `grep -rn "harness=" src/autocompactor/`.
- [ ] **Step 4: Run the green gate + smoke state-path assertion**
  - Run: `python3 -m pytest tests/ -q` → Expected: PASS (~135; +3 equivalence/flat tests). State the actual number.
  - Run: `PI_SMOKE=1 bash tests/smoke_test_pi.sh` → Expected: `ALL PI SMOKE TESTS PASSED`, and all smoke-run state/artifacts land under `~/.autocompactor/pi/`.
  - Run: `grep -rn "harness=\|AUTOCOMPACTOR_PI_\|CFG?.pi\|~/.claude" src/` → Expected: no matches.
- [ ] **Step 5: Commit**
  ```bash
  git add -A
  git commit -m "Full flatten: single-namespace config, drop harness params, repoint state to ~/.autocompactor/pi"
  ```

---

### Task 7: Docs rewrite + WORKLOG entry
**Files:**
- Delete: `CLAUDE.md`
- Modify: `AGENTS.md` — single adapter; architecture table drops deleted modules (`context_monitor`, `install`, `analyze_corpus`, `precompact_analyzer`), adds `llm_digest.py`, drops the Claude/Pi gating split; "two adapters ship" → "Pi is sole adapter"; agnostic-core framing stays
- Modify: `HANDOFF.md` + `README` — reposition as **Pi context compactor**, preserving the decision record (Claude removal rationale §2, Pi-field gap, Spec A flag)
- Modify: `WORKLOG.md` — dated entry: scope + why (channel-fighting → Pi-only), not what

**Docs-only task — green gate confirms no code regressed.**

- [ ] **Step 1: Delete CLAUDE.md**
  ```bash
  git rm CLAUDE.md
  ```
- [ ] **Step 2: Rewrite AGENTS.md** — in the architecture table, remove the deleted-module rows, add:
  ```
  | `src/autocompactor/llm_digest.py` | optional cheap-model "must-survive" digest (harness-agnostic; consumed by pi_bridge) |
  ```
  Replace the "Harness adapters" section's "Two adapters ship" with "Pi is the sole adapter" and delete the Claude bullet + the Pi/Claude signal-gating split paragraph in "Operating notes".
- [ ] **Step 3: Update HANDOFF.md + README** — reframe the product line as "Pi context compactor"; keep the pi-custom-compactor evaluation and the §2 Pi-field-gap decision record.
- [ ] **Step 4: Add the WORKLOG entry**
  ```
  ## 2026-06-21 — Pi-only pivot
  Dropped the Claude Code adapter entirely; Pi is now the sole product. Why:
  the Claude adapter's history was channel-fighting (systemMessage redraw,
  additionalContext relay, PreCompact hookSpecificOutput rejection, cooldown
  starvation) and Claude only ever advised — it cannot invoke /compact. Pi
  actuates via ctx.compact(). Extracted llm_digest to a kept module; completed
  the Pi parser's assistant/user/summary field-completion before any removal.
  Full scaffolding flatten: single-namespace config, state under ~/.autocompactor/pi.
  ```
- [ ] **Step 5: Final green gate + commit**
  - Run: `python3 -m pytest tests/ -q` → Expected: PASS (~135, unchanged from Task 6). State the actual number.
  - Run: `PI_SMOKE=1 bash tests/smoke_test_pi.sh` → Expected: `ALL PI SMOKE TESTS PASSED`
  ```bash
  git add -A
  git commit -m "Docs: rewrite for Pi-only product; delete CLAUDE.md; WORKLOG entry"
  ```
