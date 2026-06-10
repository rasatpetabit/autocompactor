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

Optional deeper analysis: set AUTOCOMPACTOR_LLM=1 to shell out to
`claude -p` with a cheap model for a smarter digest of what to preserve.
Off by default — hooks have a 60s timeout and this adds latency + token
spend of its own.
"""

import datetime
import json
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from transcript_lib import analyze, build_preservation_instructions, detect_phase  # noqa: E402
import artifacts  # noqa: E402
from stats import log_event  # noqa: E402

STATE_DIR = os.path.expanduser("~/.claude/autocompactor")
BACKUP_DIR = os.path.join(STATE_DIR, "backups")


def llm_digest(transcript_path: str) -> str:
    """Optional: ask a cheap model what must survive compaction."""
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
        res = subprocess.run(
            ["claude", "-p", "--model", "haiku", prompt],
            capture_output=True, text=True, timeout=45,
        )
        return res.stdout.strip() if res.returncode == 0 else ""
    except Exception:
        return ""


def main() -> int:
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

    if os.environ.get("AUTOCOMPACTOR_LLM") == "1" and transcript:
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
        instructions += (
            "\n\nNOTE: the following are preserved on disk and will be "
            "re-injected after compaction -- do NOT spend summary space "
            "duplicating them: user corrections, error texts, working "
            "commands, discovered constants, file lists. Focus the summary "
            "on what regexes cannot extract: decisions and rationale, plan "
            "position, failed approaches, open questions.")

    # mark for one-shot re-injection + leave a stats line for the digest
    state_file = os.path.join(STATE_DIR, f"{session_id}.state.json")
    try:
        with open(state_file) as fh:
            state2 = json.load(fh)
    except Exception:
        state2 = {}
    state2["pending_reinject"] = True
    state2["last_compaction_stats"] = (
        f"compaction #{state2.get('compaction_count', 0) + 1}, trigger="
        f"{trigger}, context_before~{st.context_tokens:,}t" if st else "")
    state2["compaction_count"] = state2.get("compaction_count", 0) + 1
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(state_file, "w") as fh:
            json.dump(state2, fh)
    except Exception:
        pass

    log_event({
        "type": "precompact", "session_id": session_id, "trigger": trigger,
        "context_tokens": st.context_tokens if st else None,
        "phase": detect_phase(st) if st else None,
        "had_staged": bool(staged),
        "had_user_instructions": bool(user_instructions),
        "instr_chars": len(instructions),
        "artifact_chars": art_sizes,
    })
    if not instructions.strip():
        return 0

    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreCompact",
            "customInstructions": instructions,
        }
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
