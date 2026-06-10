#!/usr/bin/env python3
"""
stats.py — autocompactor telemetry.

Appends one JSON line per event to ~/.claude/autocompactor/stats/events.jsonl.
Local-only; nothing leaves the machine. Used to tune thresholds and phase
addenda against real behavior (analyze with analyze_corpus.py --events).

Event types:
  monitor_eval : every UserPromptSubmit — context_tokens, occupancy,
                 signals, stale_frac, recommended, suppressed_by_cooldown
  precompact   : every compaction — trigger, context_tokens, phase,
                 had_staged, had_user_instructions, instr_chars
The post-compaction context size is recovered offline by joining the next
monitor_eval for the same session (analyze_corpus computes reduction ratios).
"""

import datetime
import json
import os
import socket

import statedir

STATS_DIR = os.path.expanduser("~/.claude/autocompactor/stats")


def _stats_dir(harness: str = "claude") -> str:
    try:
        return os.path.join(statedir.state_root(harness), "stats")
    except Exception:
        return STATS_DIR


def log_event(event: dict, harness: str = "claude") -> None:
    """Best-effort append; never raise into the hook path."""
    try:
        event = dict(event)
        harness = event.setdefault("harness", harness)
        stats_dir = _stats_dir(harness)
        os.makedirs(stats_dir, exist_ok=True)
        event.setdefault("ts", datetime.datetime.now().isoformat(timespec="seconds"))
        event.setdefault("host", socket.gethostname())
        with open(os.path.join(stats_dir, "events.jsonl"), "a",
                  encoding="utf-8") as fh:
            fh.write(json.dumps(event, default=str) + "\n")
    except Exception:
        pass
