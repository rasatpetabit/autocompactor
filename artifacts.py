#!/usr/bin/env python3
"""
artifacts.py — durable session artifacts (technique adapted from
@davidorex/pi-custom-compactor).

Idea: facts that regexes can extract should never depend on an LLM
summarizer's goodwill. At compaction time we mechanically extract them
from the transcript (zero token cost), persist to disk, and re-inject a
budgeted digest ONCE on the first prompt after compaction. This differs
from pi-custom-compactor's per-LLM-call injection: Claude Code hooks
can't intercept every model call, and a one-shot post-compaction
injection lands in the freshly compacted context and persists from there
— same durability, no steady-state token tax.

Artifact classes (priority order — higher survives budget trimming first):
  corrections      user redirects/preferences, verbatim
  error_ledger     deduplicated error texts with occurrence counts
  working_commands commands whose results were clean
  hex_constants    hex literals with surrounding context (protocol work)
  files            edited / read file paths
"""

from __future__ import annotations

import json
import os

ART_DIR = os.path.expanduser("~/.claude/autocompactor/artifacts")

PRIORITY = ["corrections", "error_ledger", "working_commands",
            "hex_constants", "files"]


def _dedupe_hex(items: list) -> list:
    import re
    seen, out = set(), []
    for ctx in items:
        key = tuple(sorted(re.findall(r"0x[0-9A-Fa-f]+", ctx)))
        if key not in seen:
            seen.add(key)
            out.append(ctx)
    return out


def _dedupe_keep_last(seq, n: int) -> list:
    out, seen = [], set()
    for item in reversed(list(seq)):
        if item not in seen:
            seen.add(item)
            out.append(item)
        if len(out) >= n:
            break
    return list(reversed(out))


def merge(old: dict, new: dict) -> dict:
    """Union of a previously saved artifact set and a fresh extraction.

    Continuous-extraction support: the monitor re-extracts from the FULL
    current transcript on every prompt, so `new` supersedes `old` for
    anything still in context; `old` contributes only facts that a
    compaction (or transcript truncation) already removed. Error counts
    use max(), not sum — both sides counted the overlapping window."""
    if not old:
        return new
    if not new:
        return old
    led = {e.get("error"): e.get("count", 1)
           for e in old.get("error_ledger") or []}
    for e in new.get("error_ledger") or []:
        led[e.get("error")] = max(led.get(e.get("error"), 0),
                                  e.get("count", 1))
    of, nf = old.get("files") or {}, new.get("files") or {}
    return {
        "corrections": _dedupe_keep_last(
            (old.get("corrections") or []) + (new.get("corrections") or []),
            30),
        "error_ledger": [{"error": k, "count": v}
                         for k, v in list(led.items())[-30:]],
        "working_commands": _dedupe_keep_last(
            (old.get("working_commands") or [])
            + (new.get("working_commands") or []), 20),
        "hex_constants": _dedupe_hex(
            (old.get("hex_constants") or [])
            + (new.get("hex_constants") or []))[-20:],
        "files": {
            "edited": _dedupe_keep_last(
                (of.get("edited") or []) + (nf.get("edited") or []), 30),
            "read": _dedupe_keep_last(
                (of.get("read") or []) + (nf.get("read") or []), 30),
        },
    }


def extract(st) -> dict:
    """Mechanical extraction from a TranscriptStats. No LLM calls."""
    return {
        "corrections": st.corrections,
        "error_ledger": [{"error": k, "count": v}
                         for k, v in list(st.error_ledger.items())[-20:]],
        "working_commands": st.working_commands,
        "hex_constants": _dedupe_hex(st.hex_constants),
        "files": {"edited": st.edited_files[-25:],
                  "read": st.read_files[-25:]},
    }


def save(session_id: str, arts: dict) -> dict:
    """Persist; return per-artifact size accounting (chars ~ tokens*4)."""
    os.makedirs(ART_DIR, exist_ok=True)
    sizes = {k: len(json.dumps(v)) for k, v in arts.items()}
    with open(os.path.join(ART_DIR, f"{session_id}.json"), "w",
              encoding="utf-8") as fh:
        json.dump(arts, fh, indent=1)
    return sizes


def load(session_id: str) -> dict:
    try:
        with open(os.path.join(ART_DIR, f"{session_id}.json"),
                  encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def build_digest(arts: dict, budget_tokens: int = 1500,
                 stats_line: str = "") -> str:
    """Compose a re-injection digest, trimming lowest-priority artifacts
    first until the (approximate) token budget is met."""
    if not arts:
        return ""
    sections = {}
    if arts.get("corrections"):
        sections["corrections"] = ("USER CORRECTIONS (verbatim, still "
                                   "binding):\n" + "\n".join(
                                       "- " + c for c in arts["corrections"]))
    if arts.get("error_ledger"):
        sections["error_ledger"] = ("ERRORS SEEN THIS SESSION (do not "
                                    "re-attempt known-bad paths):\n"
                                    + "\n".join(
                f"- [{e['count']}x] {e['error']}" for e in arts["error_ledger"]))
    if arts.get("working_commands"):
        sections["working_commands"] = ("KNOWN-WORKING COMMANDS:\n"
                                        + "\n".join(
                "- " + c for c in arts["working_commands"]))
    if arts.get("hex_constants"):
        sections["hex_constants"] = ("CONSTANTS DISCOVERED (verbatim "
                                     "context):\n" + "\n".join(
                "- " + h for h in arts["hex_constants"]))
    f = arts.get("files") or {}
    if f.get("edited") or f.get("read"):
        sections["files"] = ("FILES: edited=" + ", ".join(f.get("edited", []))
                             + " | read=" + ", ".join(f.get("read", [])))

    keep = list(PRIORITY)
    while keep:
        body = "\n\n".join(sections[k] for k in keep if k in sections)
        if len(body) // 4 <= budget_tokens or len(keep) == 1:
            break
        keep.pop()  # drop lowest priority
    if not keep:
        return ""
    header = ("[autocompactor] Durable artifacts recovered from before "
              "compaction (mechanically extracted; trust over summary "
              "paraphrase):")
    if stats_line:
        header += f"\n({stats_line})"
    return header + "\n\n" + body
