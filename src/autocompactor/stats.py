#!/usr/bin/env python3
"""
stats.py — autocompactor telemetry.

Appends one JSON line per event to ~/.claude/autocompactor/stats/events.jsonl.
Local-only; nothing leaves the machine. Used to tune thresholds and phase
addenda against real behavior.

Event types:
  monitor_eval : every UserPromptSubmit — context_tokens, occupancy,
                 signals, stale_frac, recommended, suppressed_by_cooldown
  precompact   : every compaction — trigger, context_tokens, phase,
                 had_staged, had_user_instructions, instr_chars
The post-compaction context size is recovered offline by joining the next
monitor_eval for the same session.
"""

import datetime
import json
import os
import socket

from autocompactor import statedir

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


def run_hook(name: str, fn, *args, harness: str = "claude", **kwargs) -> int:
    """Run a hook entrypoint so it can never raise into the hook path.

    The contract: "hooks must never raise; every failure degrades to exit 0
    with no traceback." Individual modules guard their own risky operations,
    but this is the belt-and-suspenders backstop — any exception that escapes
    `fn` becomes exit 0 plus a content-free `hook_skip` breadcrumb (error
    class name only, never transcript text), so a hook that silently stops
    working is still observable in telemetry.
    """
    try:
        rc = fn(*args, **kwargs)
        return int(rc) if isinstance(rc, int) else 0
    except Exception as exc:
        log_event({"type": "hook_skip", "hook": name,
                   "error": type(exc).__name__}, harness=harness)
        return 0
