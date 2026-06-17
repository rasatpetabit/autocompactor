#!/usr/bin/env python3
"""
precompact_analyzer.py — Claude Code PreCompact hook.

Fires when a compaction (manual or auto) is about to run. Receives
{session_id, transcript_path, cwd, trigger: "manual"|"auto",
 custom_instructions} on stdin.

Does three things:
  1. Backs up the full transcript to ~/.claude/autocompactor/backups/
     (compaction is lossy; the JSONL is your audit trail).
  2. Builds preservation instructions from transcript analysis — preferring
     instructions staged by context_monitor.py moments earlier, falling
     back to a fresh analysis (covers autocompact and cold /compact).
  3. Returns hookSpecificOutput.customInstructions so the summarizer is
     told exactly what to keep. If you typed `/compact <your own notes>`,
     your notes are kept and ours are appended.
  4. Shows a quick analysis summary (trigger, context, phase, signals,
     artifact sizes, instruction source) as a systemMessage before the
     compaction runs, and reuses it as the post-compaction digest header.

Optional deeper analysis: set AUTOCOMPACTOR_LLM=1 to call a configured
LLM for a smarter digest of what to preserve. Defaults remain off and,
when enabled without overrides, use `claude -p --model haiku`. Local users
can point this at an OpenAI-compatible endpoint or command via env vars
without changing public repo defaults. Hooks have a 60s timeout, so this
adds latency + token spend of its own.
"""

import datetime
import json
import os
import shlex
import shutil
import subprocess
import sys
import urllib.request

from autocompactor import config_lib, artifacts, policy, window_resolver  # noqa: E402
from autocompactor.transcript_lib import (analyze, active_signals,  # noqa: E402
                                          append_artifact_restatement,
                                          build_preservation_instructions,
                                          context_composition,
                                          detect_phase)
from autocompactor.stats import log_event, run_hook  # noqa: E402

STATE_DIR = os.path.expanduser("~/.claude/autocompactor")
BACKUP_DIR = os.path.join(STATE_DIR, "backups")


def _env(name: str, default: str = "") -> str:
    """LLM knobs resolve env-first, then config.local.json (gitignored).

    Public config.json should not carry site-local model names,
    endpoints, or command paths — those live in config.local.json so
    they also reach env-less processes (non-interactive Pi launches).
    Non-AUTOCOMPACTOR names (e.g. OPENAI_API_KEY) stay pure env.
    """
    if not name.startswith("AUTOCOMPACTOR_"):
        return os.environ.get(name, default)
    return config_lib.cfg.str(
        name[len("AUTOCOMPACTOR_"):], default=default)


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
        tail_lines = []
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


def _run() -> int:
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0

    transcript = data.get("transcript_path") or ""
    session_id = data.get("session_id") or "unknown"
    trigger = data.get("trigger") or "auto"
    user_instructions = (data.get("custom_instructions") or "").strip()
    cwd = data.get("cwd") or ""

    # 1. Backup (best effort).
    if transcript and os.path.exists(os.path.expanduser(transcript)):
        os.makedirs(BACKUP_DIR, exist_ok=True)
        ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        dest = os.path.join(BACKUP_DIR, f"{session_id}-{ts}-{trigger}.jsonl")
        try:
            shutil.copy2(os.path.expanduser(transcript), dest)
        except OSError:
            pass

    # 2. Preservation instructions: staged > fresh analysis.
    staged = ""
    state_file = os.path.join(STATE_DIR, f"{session_id}.state.json")
    try:
        with open(state_file) as fh:
            staged = json.load(fh).get("staged_instructions", "")
    except Exception:
        pass

    if staged:
        instructions = staged
    elif transcript and os.path.exists(os.path.expanduser(transcript)):
        instructions = build_preservation_instructions(analyze(transcript), cwd)
    else:
        instructions = ""

    if _env("AUTOCOMPACTOR_LLM") == "1" and transcript:
        extra = llm_digest(transcript)
        if extra:
            instructions += "\n\nAdditional must-preserve facts:\n" + extra

    # Never clobber instructions the user typed with /compact <notes>.
    if user_instructions:
        instructions = user_instructions + "\n\n" + instructions

    st = analyze(transcript) if transcript else None

    # Mechanical artifact extraction (pi-custom-compactor technique):
    # structured facts go to disk with zero LLM involvement; the summary
    # no longer needs to carry them, and the monitor re-injects a digest
    # on the first post-compaction prompt.
    art_sizes = {}
    if st is not None:
        arts = artifacts.merge(artifacts.load(session_id),
                               artifacts.extract(st))
        art_sizes = artifacts.save(session_id, arts)
        # Founding-goal restatement + on-disk-artifacts NOTE (shared with the
        # Pi bridge so the two paths can't drift).
        instructions = append_artifact_restatement(instructions, arts)

    # mark for one-shot re-injection + leave a stats line for the digest
    state_file = os.path.join(STATE_DIR, f"{session_id}.state.json")
    try:
        with open(state_file) as fh:
            state2 = json.load(fh)
    except Exception:
        state2 = {}
    state2["pending_reinject"] = True
    state2["compaction_count"] = state2.get("compaction_count", 0) + 1

    # Quick analysis summary: shown to the user before the compaction runs
    # (systemMessage) and reused as the post-compaction digest header.
    # Content-free by convention — counts/ratios/phases, no transcript text.
    summary = ""
    phase = detect_phase(st) if st else None
    resolution = None
    comp = {}                 # context composition (telemetry + ledger)
    art_kept, art_dropped = [], []
    if st is not None:
        try:
            configured_window = config_lib.cfg.float("WINDOW", default=200_000)
        except ValueError:
            configured_window = 200_000.0
        # Same effective-window clamp as the monitor: sessions that never
        # exceeded what a 200k model can hold are judged against 200k.
        peak = max(st.usage_series) if st.usage_series else st.context_tokens
        peak = max(peak, int(state2.get("peak_ctx", 0) or 0))
        resolution = window_resolver.resolve_window(
            configured_window=configured_window,
            observed_peak=peak,
            harness="claude",
            native_ceiling=window_resolver.native_ceiling_from_settings())
        window = resolution.effective_window
        pcfg = policy.resolve_policy_config("claude", int(window))
        soft_t, hard_t = policy.advisory_band(pcfg)
        sigs = [desc for _, desc in active_signals(st, window=window,
                                                   hard_tokens=hard_t)]
        native_auto, model_window = window_resolver.readout_anchors(resolution)
        parts = [
            f"compaction #{state2['compaction_count']} ({trigger})",
            policy.readout_line(st.context_tokens, soft_t, hard_t,
                                native_auto, model_window),
            f"phase: {phase}",
        ]
        if sigs:
            parts.append("signals: " + "; ".join(sigs))
        if art_sizes:
            parts.append(f"artifacts to disk: {len(art_sizes)} classes, "
                         f"{sum(art_sizes.values()):,}B")
        parts.append("instructions: "
                     + ("staged by monitor" if staged else "fresh analysis")
                     + f" ({len(instructions):,} chars)"
                     + (", user notes kept" if user_instructions else ""))
        summary = " | ".join(parts)
        # Composition (what's in the window) + preservation ledger (what we
        # extracted verbatim vs left to the lossy summarizer) — owner requests
        # (a) and (b), surfaced at the compaction the user is about to run.
        comp = context_composition(st, st.context_tokens)
        comp_line = policy.composition_line(comp)
        if comp_line:
            summary += "\n  └ " + comp_line
        art_kept, art_dropped = artifacts.budget_plan(arts)
        ledger = artifacts.preservation_ledger(
            arts, art_sizes, lossy_tokens=comp.get("assistant", 0))
        if ledger:
            summary += "\n" + ledger
    state2["last_compaction_stats"] = summary
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(state_file, "w") as fh:
            json.dump(state2, fh)
    except Exception:
        pass

    log_event({
        "type": "precompact", "session_id": session_id, "trigger": trigger,
        "context_tokens": st.context_tokens if st else None,
        "phase": phase,
        "had_staged": bool(staged),
        "had_user_instructions": bool(user_instructions),
        "instr_chars": len(instructions),
        "artifact_chars": art_sizes,
        "composition": comp or None,
        "artifacts_kept": art_kept,
        "artifacts_dropped": art_dropped,
        **(resolution.event_fields() if resolution else {}),
    })
    out = {}
    if instructions.strip():
        out["hookSpecificOutput"] = {
            "hookEventName": "PreCompact",
            "customInstructions": instructions,
        }
    if summary:
        out["systemMessage"] = "autocompactor: " + summary
    if not out:
        return 0
    print(json.dumps(out))
    return 0


def main() -> int:
    """Never-raise wrapper (hook contract): any failure degrades to exit 0."""
    return run_hook("precompact_analyzer", _run)


if __name__ == "__main__":
    sys.exit(main())
