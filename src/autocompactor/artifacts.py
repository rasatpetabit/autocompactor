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
  initial_prompts  the user's founding request(s), verbatim — re-injected
                   after EVERY compaction so the original goal cannot decay
                   across passes (merge is old-wins: the earliest capture
                   is canonical)
  corrections      user redirects/preferences, verbatim
  error_ledger     deduplicated error texts with occurrence counts
  working_commands commands whose results were clean
  hex_constants    hex literals with surrounding context (protocol work)
  files            edited / read file paths
  open_work        waiting monitors / on-success handoffs (resume after compact)
  progress_position mechanical plan position (masterplan/coord/todo) for hard resume
"""

from __future__ import annotations

import json
import os

from autocompactor import statedir

ART_DIR = os.path.expanduser("~/.autocompactor/pi/artifacts")

PRIORITY = ["initial_prompts", "corrections", "progress_position", "open_work",
            "error_ledger", "working_commands", "hex_constants", "files"]

# Soft ceiling for the progress section (~tokens; chars ≈ tokens*4).
PROGRESS_SECTION_BUDGET_TOKENS = 400


def _artifact_dir(harness: str = "pi") -> str:
    # `harness` accepted but ignored (Pi is the sole adapter).
    try:
        return os.path.join(statedir.state_root(), "artifacts")
    except Exception:
        return ART_DIR


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
        # Old-wins: the earliest captured prompts are the founding goal;
        # a post-compaction re-extraction must never displace them.
        "initial_prompts": (old.get("initial_prompts")
                            or new.get("initial_prompts") or []),
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
        # Latest open_work wins per kind (new supersedes old for same kind).
        "open_work": _merge_open_work(
            old.get("open_work") or [], new.get("open_work") or [], 5),
        # progress_position tracks LIVE work — higher rank wins (not old-wins).
        "progress_position": _merge_progress_position(
            old.get("progress_position"), new.get("progress_position")),
        "files": {
            "edited": _dedupe_keep_last(
                (of.get("edited") or []) + (nf.get("edited") or []), 30),
            "read": _dedupe_keep_last(
                (of.get("read") or []) + (nf.get("read") or []), 30),
        },
    }


def _merge_open_work(old: list, new: list, cap: int = 5) -> list:
    out = []
    by_kind = {}
    for item in list(old or []) + list(new or []):
        if not isinstance(item, dict):
            continue
        kind = item.get("kind") or "open"
        by_kind[kind] = item
    for kind in by_kind:
        out.append(by_kind[kind])
    return out[-cap:]


def _merge_progress_position(old, new):
    """Single-object merge: prefer higher rank, then confidence, then new."""
    candidates = [c for c in (new, old) if isinstance(c, dict) and c]
    if not candidates:
        return new if isinstance(new, dict) else (old if isinstance(old, dict) else None)
    if len(candidates) == 1:
        return candidates[0]

    def key(h):
        try:
            return (int(h.get("rank") or 0), float(h.get("confidence") or 0),
                    float(h.get("mtime") or 0))
        except Exception:
            return (0, 0.0, 0.0)

    return max(candidates, key=key)


def extract(st) -> dict:
    """Mechanical extraction from a TranscriptStats. No LLM calls."""
    pos = getattr(st, "progress_position", None)
    if not isinstance(pos, dict):
        pos = None
    return {
        "initial_prompts": list(getattr(st, "initial_user_prompts", []) or []),
        "corrections": st.corrections,
        "open_work": list(getattr(st, "open_work", []) or [])[-5:],
        "progress_position": pos,
        "error_ledger": [{"error": k, "count": v}
                         for k, v in list(st.error_ledger.items())[-20:]],
        "working_commands": st.working_commands,
        "hex_constants": _dedupe_hex(st.hex_constants),
        "files": {"edited": st.edited_files[-25:],
                  "read": st.read_files[-25:]},
    }


def save(session_id: str, arts: dict, harness: str = "claude") -> dict:
    """Persist; return per-artifact size accounting (chars ~ tokens*4)."""
    sizes = {k: len(json.dumps(v)) for k, v in arts.items()}
    try:
        art_dir = _artifact_dir(harness)
        os.makedirs(art_dir, exist_ok=True)
        with open(os.path.join(art_dir, f"{session_id}.json"), "w",
                  encoding="utf-8") as fh:
            json.dump(arts, fh, indent=1)
        return sizes
    except Exception:
        return {}


def load(session_id: str, harness: str = "claude") -> dict:
    try:
        with open(os.path.join(_artifact_dir(harness), f"{session_id}.json"),
                  encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def _sections(arts: dict) -> dict:
    """Build the per-category re-injection sections (no budgeting applied)."""
    sections = {}
    if arts.get("initial_prompts"):
        sections["initial_prompts"] = (
            "FOUNDING GOAL -- the user's original request(s), verbatim "
            "(this is what the session was started to do; re-anchor on it "
            "before continuing):\n" + "\n".join(
                "- " + p for p in arts["initial_prompts"]))
    if arts.get("corrections"):
        sections["corrections"] = ("USER CORRECTIONS (verbatim, still "
                                   "binding):\n" + "\n".join(
                                       "- " + c for c in arts["corrections"]))
    pos = arts.get("progress_position")
    if isinstance(pos, dict) and pos:
        brief = (pos.get("brief") or pos.get("summary") or "").strip()
        if brief:
            max_chars = PROGRESS_SECTION_BUDGET_TOKENS * 4
            if len(brief) > max_chars:
                brief = brief[: max_chars - 1] + "…"
            surface = pos.get("surface") or "progress"
            key = pos.get("key") or ""
            sections["progress_position"] = (
                f"PLAN POSITION ({surface} {key}) — resume this unit; "
                "do not restart from scratch:\n" + brief)
    if arts.get("open_work"):
        ow_lines = []
        for item in arts["open_work"]:
            if not isinstance(item, dict):
                continue
            kind = item.get("kind") or "open"
            ids = ", ".join(item.get("resource_ids") or []) or "—"
            cmds = "; ".join(item.get("monitor_cmds") or []) or "—"
            nxt = item.get("next_on_success") or "—"
            summary = (item.get("summary") or "")[:200]
            ow_lines.append(
                f"- [{kind}] {summary}\n  resources: {ids}\n"
                f"  monitor: {cmds}\n  on success: {nxt}")
        if ow_lines:
            sections["open_work"] = (
                "OPEN WORK (resume after compact — do not drop):\n"
                + "\n".join(ow_lines))
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
    return sections


def budget_plan(arts: dict, budget_tokens: int = 1500) -> tuple:
    """Predict which artifact categories survive the re-injection budget and
    which get trimmed (lowest-priority first), WITHOUT building the digest.

    Single source of the trimming order so the preservation ledger (owner
    request b) and build_digest() can never disagree about what was kept.
    Returns (kept, dropped) — both PRIORITY-ordered category-name lists."""
    sections = _sections(arts)
    keep = [k for k in PRIORITY if k in sections]
    while keep:
        body = "\n\n".join(sections[k] for k in keep if k in sections)
        if len(body) // 4 <= budget_tokens or len(keep) == 1:
            break
        keep.pop()  # drop lowest priority
    dropped = [k for k in PRIORITY if k in sections and k not in keep]
    return keep, dropped


def build_digest(arts: dict, budget_tokens: int = 1500,
                 stats_line: str = "") -> str:
    """Compose a re-injection digest, trimming lowest-priority artifacts
    first until the (approximate) token budget is met."""
    if not arts:
        return ""
    sections = _sections(arts)
    if not sections:
        return ""
    keep, _ = budget_plan(arts, budget_tokens)
    if not keep:
        return ""
    body = "\n\n".join(sections[k] for k in keep if k in sections)
    if not body.strip():
        return ""
    if len(body) // 4 > budget_tokens and len(keep) == 1:
        max_chars = max(1, budget_tokens * 4)
        if len(body) > max_chars:
            body = body[:max_chars].rstrip() + "\n...[truncated]"
    header = ("[autocompactor] Durable artifacts recovered from before "
              "compaction (mechanically extracted; trust over summary "
              "paraphrase):")
    if stats_line:
        header += f"\n({stats_line})"
    return header + "\n\n" + body


_LEDGER_LABELS = {
    "initial_prompts": "initial prompt(s)",
    "corrections": "corrections",
    "open_work": "open work",
    "error_ledger": "errors",
    "working_commands": "commands",
    "hex_constants": "constants",
    "files": "files",
}


def _counts(arts: dict) -> dict:
    f = arts.get("files") or {}
    return {
        "initial_prompts": len(arts.get("initial_prompts") or []),
        "corrections": len(arts.get("corrections") or []),
        "open_work": len(arts.get("open_work") or []),
        "error_ledger": len(arts.get("error_ledger") or []),
        "working_commands": len(arts.get("working_commands") or []),
        "hex_constants": len(arts.get("hex_constants") or []),
        "files": len(f.get("edited") or []) + len(f.get("read") or []),
    }


def preservation_ledger(arts: dict, sizes: dict = None,
                        budget_tokens: int = 1500,
                        lossy_tokens: int = 0) -> str:
    """Owner request (b): a compress-vs-preserve accounting shown at compaction.

    Names what is extracted VERBATIM to disk (lossless, survives the summary)
    vs what is LEFT to the summarizer (lossy), plus any artifact category
    trimmed to fit the re-injection budget. Counts / category-names / sizes
    only — content-free, like the rest of telemetry. Empty string when there
    is nothing preserved."""
    if not arts:
        return ""
    counts = _counts(arts)
    kept, dropped = budget_plan(arts, budget_tokens)
    preserved = ", ".join(f"{counts[k]} {_LEDGER_LABELS[k]}"
                          for k in PRIORITY
                          if k in kept and counts.get(k, 0) > 0)
    if sizes:
        total_b = sum(sizes.values())
    else:
        total_b = sum(len(json.dumps(v)) for v in arts.values())
    lines = []
    if preserved:
        lines.append("preserved verbatim → disk (survive the summary): "
                     f"{preserved} (~{total_b:,}B)")
    lossy = "assistant reasoning, decisions, open questions"
    tail = ""
    if lossy_tokens and lossy_tokens > 0:
        from autocompactor import policy
        tail = f" (~{policy._fmt_tokens(lossy_tokens)})"
    lines.append(f"left to summarizer (lossy): {lossy}{tail}")
    dropped_real = [k for k in dropped if counts.get(k, 0) > 0]
    if dropped_real:
        lines.append("dropped for budget: " + ", ".join(dropped_real))
    return "\n".join(lines)
