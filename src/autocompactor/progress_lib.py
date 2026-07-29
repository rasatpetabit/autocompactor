#!/usr/bin/env python3
"""
progress_lib.py — mechanical post-compact plan-position extraction.

Read-only progress surfaces (masterplan state.yml, coord blackboard, todos,
optional plan files). Never raises into the prepare path; callers wrap with
extract_all_safe(). No LLM calls. Telemetry consumers must log ids/ranks only —
never brief/summary text (see content_free_fields).

See docs/masterplan/post-compact-task-continuity/spec.md.
"""

from __future__ import annotations

import os
import re
import time
from typing import Any, Optional

# Rank bands (coding surfaces). mode=wait always outranks mode=code in
# resolve_next_step regardless of numeric rank.
RANK_MASTERPLAN = 95
RANK_OPEN_WORK_WAIT = 85  # informational; wait mode wins separately
RANK_COORD = 72
RANK_TODO = 55
RANK_PLAN_FILE = 35

DEFAULT_MIN_CONFIDENCE = 0.75
DEFAULT_BUDGET_TOKENS = 400
DEFAULT_COORD_MODE = "task_only"  # task_only | off | wave_ok
DEFAULT_PLAN_FILES = False
DEFAULT_AFFINITY = True

_SLUG_RE = re.compile(r"docs/masterplan/([A-Za-z0-9._-]+)")
_TASK_ID_RE = re.compile(r"\bT(?:ask)?\s*#?(\d+)\b", re.I)
_WORKTREE_SLUG_RE = re.compile(r"[\\/]\.worktrees[\\/]([A-Za-z0-9._-]+)")


def _safe_float(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except Exception:
        return default


def _safe_int(v, default: int = 0) -> int:
    try:
        return int(v)
    except Exception:
        return default


def _read_yaml_or_json(path: str) -> Optional[dict]:
    """Best-effort YAML/JSON load. Prefer yaml if installed; JSON fallback."""
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except Exception:
        return None
    if not text.strip():
        return None
    # JSON first (fast path for coord job.json)
    if text.lstrip().startswith("{"):
        try:
            import json
            obj = json.loads(text)
            return obj if isinstance(obj, dict) else None
        except Exception:
            pass
    try:
        import yaml  # type: ignore
        obj = yaml.safe_load(text)
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


def _truncate(s: str, n: int) -> str:
    s = (s or "").replace("\n", " ").strip()
    if len(s) <= n:
        return s
    return s[: max(0, n - 1)] + "…"


def content_free_fields(hit: Optional[dict]) -> dict:
    """Telemetry-safe subset — never includes brief/summary text."""
    if not hit or not isinstance(hit, dict):
        return {
            "progress_surface": "",
            "progress_key": "",
            "progress_mode": "",
            "progress_rank": 0,
            "progress_confidence": 0.0,
            "progress_affinity": False,
            "progress_brief_len": 0,
            "progress_summary_len": 0,
        }
    brief = hit.get("brief") or ""
    summary = hit.get("summary") or ""
    return {
        "progress_surface": str(hit.get("surface") or ""),
        "progress_key": str(hit.get("key") or ""),
        "progress_mode": str(hit.get("mode") or ""),
        "progress_rank": _safe_int(hit.get("rank"), 0),
        "progress_confidence": _safe_float(hit.get("confidence"), 0.0),
        "progress_affinity": bool(hit.get("affinity")),
        "progress_brief_len": len(brief),
        "progress_summary_len": len(summary),
    }


def format_resume_brief(
    *,
    title: str,
    unit_id: str = "",
    wave: Any = None,
    files: Optional[list] = None,
    verify: Optional[list] = None,
    extra_lines: Optional[list] = None,
    budget_tokens: int = DEFAULT_BUDGET_TOKENS,
) -> str:
    """Agent-facing hard-resume brief. Cap ~budget_tokens (chars ≈ tokens*4)."""
    files = list(files or [])[:8]
    verify = list(verify or [])[:3]
    unit = title.strip() or unit_id or "current unit"
    if unit_id and unit_id not in unit:
        unit = f"{unit_id}: {unit}"
    wave_s = f" wave {wave}" if wave is not None and wave != "" else ""
    lines = [
        "RESUME mid-task — do not restart from scratch.",
        "1) git status + diff on files[] first; keep existing in-scope edits.",
        f"2) Continue ONLY this unit:{wave_s} {unit}".rstrip(),
        "3) Stay inside files[] scope; run verify[] before claiming done.",
        "4) If blocked, stop and report the blocker — do not invent a new task.",
    ]
    if files:
        more = max(0, len(list(files or [])) - 8)
        fl = ", ".join(files)
        if more:
            fl += f" (+{more} more)"
        lines.append(f"files[]: {fl}")
    if verify:
        lines.append("verify[]: " + " | ".join(verify))
    if extra_lines:
        lines.extend(str(x) for x in extra_lines if x)
    text = "\n".join(lines)
    # Enforce budget strictly (tests pin small budgets; production default 400).
    max_chars = max(80, int(budget_tokens) * 4)
    if len(text) > max_chars:
        head = "\n".join(lines[:3])
        if len(head) >= max_chars:
            return _truncate(head, max_chars)
        room = max_chars - len(head) - 1
        tail = text[len(head):].lstrip("\n")
        text = head + "\n" + _truncate(tail, room)
    return text


def masterplan_affinity(
    *,
    slug: str,
    task_id: Any = None,
    cwd: str = "",
    session_text: str = "",
    active_run: Any = None,
    owner_live: bool = False,
) -> bool:
    """True when the session appears bound to this masterplan run."""
    slug = (slug or "").strip()
    if not slug:
        return False
    cwd = cwd or ""
    session_text = session_text or ""
    # Worktree / branch path
    m = _WORKTREE_SLUG_RE.search(cwd.replace("\\", "/"))
    if m and m.group(1) == slug:
        return True
    if f"masterplan/{slug}" in cwd.replace("\\", "/"):
        return True
    # Transcript / session cues
    if slug in session_text:
        return True
    if f"docs/masterplan/{slug}" in session_text:
        return True
    if task_id is not None:
        tid = str(task_id)
        if re.search(rf"\bT{re.escape(tid)}\b", session_text):
            return True
        if re.search(rf"\btask\s*{re.escape(tid)}\b", session_text, re.I):
            return True
    if active_run:
        return True
    if owner_live:
        return True
    return False


def _pick_masterplan_task(tasks: list) -> Optional[dict]:
    if not tasks:
        return None
    norm = []
    for t in tasks:
        if not isinstance(t, dict):
            continue
        st = str(t.get("status") or "pending").lower()
        norm.append((st, t))
    for want in ("in_progress", "in-progress", "active", "running"):
        for st, t in norm:
            if st == want:
                return t
    pending = [
        t for st, t in norm
        if st in ("pending", "todo", "ready", "blocked", "qctl")
    ]
    if not pending:
        return None

    def sort_key(t):
        return (_safe_int(t.get("wave"), 10**9), _safe_int(t.get("id"), 10**9))

    pending.sort(key=sort_key)
    return pending[0]


def extract_masterplan(
    cwd: str,
    *,
    session_text: str = "",
    require_affinity: bool = DEFAULT_AFFINITY,
    budget_tokens: int = DEFAULT_BUDGET_TOKENS,
    now: Optional[float] = None,
) -> list[dict]:
    """Read-only discovery of non-archived masterplan state.yml under cwd."""
    if not cwd or not os.path.isdir(cwd):
        return []
    root = os.path.join(cwd, "docs", "masterplan")
    # If cwd is a worktree, also try main checkout sibling for bundles
    candidates = []
    if os.path.isdir(root):
        candidates.append(root)
    # MAIN via .git file? best-effort: walk up for docs/masterplan
    try:
        cur = os.path.abspath(cwd)
        for _ in range(4):
            parent = os.path.dirname(cur)
            if parent == cur:
                break
            alt = os.path.join(parent, "docs", "masterplan")
            if os.path.isdir(alt) and alt not in candidates:
                candidates.append(alt)
            cur = parent
    except Exception:
        pass

    hits: list[dict] = []
    for base in candidates:
        try:
            slugs = sorted(os.listdir(base))
        except Exception:
            continue
        for slug in slugs:
            state_path = os.path.join(base, slug, "state.yml")
            if not os.path.isfile(state_path):
                # also allow state.yaml
                state_path = os.path.join(base, slug, "state.yaml")
                if not os.path.isfile(state_path):
                    continue
            data = _read_yaml_or_json(state_path)
            if not data:
                continue
            status = str(data.get("status") or "").lower()
            phase = str(data.get("phase") or "").lower()
            if status in ("archived", "done") or phase == "archived":
                continue
            tasks = data.get("tasks") or []
            if not isinstance(tasks, list):
                tasks = []
            task = _pick_masterplan_task(tasks)
            all_done = bool(tasks) and all(
                str(t.get("status") or "").lower() in ("done", "completed", "waived")
                for t in tasks if isinstance(t, dict)
            )
            active_run = data.get("active_run")
            mtime = 0.0
            try:
                mtime = os.path.getmtime(state_path)
            except Exception:
                mtime = 0.0

            if task:
                tid = task.get("id")
                files = list(task.get("files") or [])[:8]
                verify = list(
                    task.get("verify_commands")
                    or task.get("verify")
                    or []
                )[:3]
                desc = (
                    task.get("description")
                    or task.get("title")
                    or task.get("summary")
                    or f"task {tid}"
                )
                wave = task.get("wave")
                unit = f"T{tid}"
                summary = _truncate(
                    f"Continue {unit} (wave {wave}): {desc}", 200)
                brief = format_resume_brief(
                    title=str(desc),
                    unit_id=unit,
                    wave=wave,
                    files=files,
                    verify=[str(v) for v in verify],
                    budget_tokens=budget_tokens,
                )
                status_t = str(task.get("status") or "pending")
            elif all_done:
                tid = "finish"
                files, verify, wave = [], [], None
                summary = _truncate(
                    f"All tasks done on {slug} — run finish/verify", 200)
                brief = format_resume_brief(
                    title=f"finish masterplan/{slug} (all tasks done)",
                    unit_id="finish",
                    budget_tokens=budget_tokens,
                    extra_lines=[
                        "Do not re-implement completed tasks; finalize/verify only."
                    ],
                )
                status_t = "pending"
            else:
                # in-progress run with empty tasks (still brainstorm/plan)
                tid = phase or "run"
                files, verify, wave = [], [], None
                topic = data.get("topic") or slug
                summary = _truncate(
                    f"Continue masterplan/{slug} phase={phase}: {topic}", 200)
                brief = format_resume_brief(
                    title=str(topic)[:200],
                    unit_id=f"masterplan/{slug}",
                    budget_tokens=budget_tokens,
                    extra_lines=[f"phase={phase} status={status}"],
                )
                status_t = status or "in-progress"

            affinity = masterplan_affinity(
                slug=str(data.get("slug") or slug),
                task_id=tid if isinstance(tid, (int, str)) else None,
                cwd=cwd,
                session_text=session_text,
                active_run=active_run,
            )
            # Confidence: base high for structured state; lower without affinity
            conf = 0.9 if affinity else 0.55
            if not task and not all_done:
                conf = min(conf, 0.7)
            hit = {
                "surface": "masterplan",
                "key": f"masterplan:{data.get('slug') or slug}:t{tid}",
                "mode": "code",
                "rank": RANK_MASTERPLAN,
                "confidence": conf,
                "affinity": affinity,
                "summary": summary,
                "brief": brief,
                "files": files if task else [],
                "verify": [str(v) for v in verify] if task else [],
                "resource_ids": [str(data.get("slug") or slug), str(tid)],
                "status": status_t,
                "mtime": mtime,
            }
            if require_affinity and not affinity:
                # Still returned for digest visibility; hard-resume gate uses affinity
                hit["confidence"] = min(hit["confidence"], 0.55)
            hits.append(hit)
    # Prefer highest confidence/mtime
    hits.sort(key=lambda h: (h.get("affinity"), h.get("mtime", 0)), reverse=True)
    return hits


def extract_coord(
    *,
    coord_root: str = "",
    cwd: str = "",
    mode: str = DEFAULT_COORD_MODE,
    session_text: str = "",
    budget_tokens: int = DEFAULT_BUDGET_TOKENS,
) -> list[dict]:
    """Coord blackboard. Default task_only — wave-only jobs are observe-only."""
    if mode in ("off", False, None, ""):
        return []
    mode = str(mode).lower()
    root = coord_root or os.path.expanduser(
        "~/.local/state/agent-dispatch/coord")
    if not os.path.isdir(root):
        return []
    hits: list[dict] = []
    try:
        job_dirs = sorted(os.listdir(root))
    except Exception:
        return []
    for name in job_dirs:
        job_path = os.path.join(root, name, "job.json")
        if not os.path.isfile(job_path):
            continue
        data = _read_yaml_or_json(job_path)
        if not data:
            continue
        state = str(data.get("state") or "").lower()
        if state not in ("open", "closing", ""):
            continue
        goal = str(data.get("goal") or "").strip()
        job_id = str(data.get("job_id") or name)
        tasks_dir = os.path.join(root, name, "tasks")
        task_files = []
        if os.path.isdir(tasks_dir):
            try:
                task_files = [
                    f for f in os.listdir(tasks_dir)
                    if f.endswith(".json") or not f.startswith(".")
                ]
            except Exception:
                task_files = []
        has_task_payload = bool(task_files)
        if mode == "task_only" and not has_task_payload:
            # Observe-only: skip nextStep contribution
            continue
        if mode != "wave_ok" and not has_task_payload and len(goal) < 12:
            continue
        # Weak cwd mapping: job name or goal mentions basename
        base = os.path.basename(os.path.abspath(cwd or "")) if cwd else ""
        affinity = False
        if base and (base in goal or base in job_id or base in session_text):
            affinity = True
        if job_id and job_id in (session_text or ""):
            affinity = True
        mtime = 0.0
        try:
            mtime = os.path.getmtime(job_path)
        except Exception:
            pass
        summary = _truncate(f"Resume coord {job_id}: {goal or 'open job'}", 200)
        brief = format_resume_brief(
            title=goal or f"coord job {job_id}",
            unit_id=job_id,
            budget_tokens=budget_tokens,
            extra_lines=[f"coord state={state}"],
        )
        conf = 0.8 if has_task_payload and affinity else (
            0.65 if has_task_payload else 0.45)
        hits.append({
            "surface": "coord",
            "key": f"coord:{job_id}",
            "mode": "code",
            "rank": RANK_COORD,
            "confidence": conf,
            "affinity": affinity,
            "summary": summary,
            "brief": brief,
            "files": [],
            "verify": [],
            "resource_ids": [job_id],
            "status": state or "open",
            "mtime": mtime,
        })
    hits.sort(key=lambda h: h.get("mtime", 0), reverse=True)
    return hits[:5]


def extract_todos(st) -> list[dict]:
    """Best-effort from TranscriptStats.todos — may be empty on Pi."""
    todos = list(getattr(st, "todos", None) or [])
    pending = []
    for t in todos:
        if not isinstance(t, dict):
            continue
        if str(t.get("status") or "").lower() in ("completed", "done", "cancelled"):
            continue
        content = (t.get("content") or t.get("text") or "").strip()
        if content:
            pending.append(content)
    if not pending:
        return []
    first = pending[0]
    brief = format_resume_brief(title=first, unit_id="todo:pending[0]")
    return [{
        "surface": "todo",
        "key": "todo:pending[0]",
        "mode": "code",
        "rank": RANK_TODO,
        "confidence": 0.7,
        "affinity": True,  # in-transcript plan is always session-bound
        "summary": _truncate(f"Continue todo: {first}", 200),
        "brief": brief,
        "files": [],
        "verify": [],
        "resource_ids": ["todo:pending[0]"],
        "status": "pending",
        "mtime": 0.0,
    }]


def extract_open_work_progress(st) -> list[dict]:
    """Lift waiting open_work into ProgressHit mode=wait.

    F1: skip waiting_monitor items whose primary resource id is already
    terminal in the transcript (succeeded/failed/…) so a finished build
    cannot win mode=wait and arm poll loops.
    """
    out = []
    try:
        from autocompactor import transcript_lib
    except Exception:
        transcript_lib = None  # type: ignore
    terminal_ids = set(getattr(st, "terminal_resource_ids", None) or set())
    for item in list(getattr(st, "open_work", None) or []):
        if not isinstance(item, dict):
            continue
        kind = item.get("kind") or ""
        if kind != "waiting_monitor":
            continue
        if transcript_lib is not None and transcript_lib._open_work_item_is_terminal(
                item, terminal_ids):
            continue
        try:
            if transcript_lib is None:
                raise RuntimeError("no transcript_lib")
            brief = transcript_lib.format_open_work_brief(item).strip()
        except Exception:
            brief = (item.get("summary") or "waiting").strip()
        if not brief:
            continue
        ids = list(item.get("resource_ids") or [])
        key = "open_work:waiting:" + (ids[0] if ids else "monitor")
        out.append({
            "surface": "open_work",
            "key": key,
            "mode": "wait",
            "rank": RANK_OPEN_WORK_WAIT,
            "confidence": 0.95,
            "affinity": True,
            "summary": _truncate(brief.split("\n")[0], 200),
            "brief": brief,
            "files": [],
            "verify": [],
            "resource_ids": ids,
            "status": "waiting",
            "mtime": 0.0,
        })
    return out


def extract_plan_files(
    cwd: str,
    *,
    enabled: bool = DEFAULT_PLAN_FILES,
    session_start_ts: float = 0.0,
    budget_tokens: int = DEFAULT_BUDGET_TOKENS,
) -> list[dict]:
    """Optional weak surface — default off."""
    if not enabled or not cwd or not os.path.isdir(cwd):
        return []
    hits = []
    names = ("PLAN.md", "TODO.md", "ROADMAP.md")
    for dirpath, dirnames, filenames in os.walk(cwd):
        # prune heavy / archive trees
        dirnames[:] = [
            d for d in dirnames
            if d not in (".git", "node_modules", ".venv", "__pycache__",
                         ".worktrees")
        ]
        rel = os.path.relpath(dirpath, cwd)
        if "docs/superpowers/plans" in rel.replace("\\", "/"):
            continue
        for name in filenames:
            if name not in names and not (
                name.endswith(".md") and "plan" in name.lower()
            ):
                continue
            path = os.path.join(dirpath, name)
            try:
                mtime = os.path.getmtime(path)
            except Exception:
                continue
            if session_start_ts and mtime < session_start_ts - 86400:
                continue
            try:
                with open(path, encoding="utf-8", errors="replace") as fh:
                    text = fh.read(8000)
            except Exception:
                continue
            if not re.search(r"- \[ \]|TODO|in progress", text, re.I):
                continue
            # first unchecked box
            m = re.search(r"- \[ \] (.+)", text)
            item = m.group(1).strip() if m else "continue plan checklist"
            rel_path = os.path.relpath(path, cwd)
            brief = format_resume_brief(
                title=item, unit_id=rel_path, budget_tokens=budget_tokens)
            hits.append({
                "surface": "plan_file",
                "key": f"plan_file:{rel_path}",
                "mode": "code",
                "rank": RANK_PLAN_FILE,
                "confidence": 0.4,
                "affinity": True,
                "summary": _truncate(f"{rel_path}: {item}", 200),
                "brief": brief,
                "files": [rel_path],
                "verify": [],
                "resource_ids": [rel_path],
                "status": "pending",
                "mtime": mtime,
            })
    hits.sort(key=lambda h: h.get("mtime", 0), reverse=True)
    return hits[:3]


def _session_text_from_stats(st) -> str:
    parts = []
    for p in list(getattr(st, "initial_user_prompts", None) or [])[:3]:
        parts.append(str(p))
    lut = getattr(st, "last_user_task", "") or ""
    if lut:
        parts.append(str(lut))
    for c in list(getattr(st, "corrections", None) or [])[-5:]:
        parts.append(str(c))
    return "\n".join(parts)


def choose_winner(hits: list[dict]) -> Optional[dict]:
    """Wait mode always beats code; within mode higher rank then confidence."""
    if not hits:
        return None
    waits = [h for h in hits if h.get("mode") == "wait"]
    pool = waits if waits else [h for h in hits if h.get("mode") == "code"]
    if not pool:
        pool = hits
    pool = sorted(
        pool,
        key=lambda h: (
            _safe_int(h.get("rank"), 0),
            _safe_float(h.get("confidence"), 0),
            _safe_float(h.get("mtime"), 0),
        ),
        reverse=True,
    )
    return pool[0]


def hard_resume_eligible(
    hit: Optional[dict],
    *,
    progress_resume: str = "autonomous",
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
    nextstep: str = "autonomous",
) -> bool:
    """Whether autonomous coding triggerTurn may fire for this hit."""
    if not hit:
        return False
    if str(nextstep).lower() == "off":
        return False
    if str(progress_resume).lower() != "autonomous":
        return False
    if hit.get("mode") == "wait":
        # Wait uses NEXTSTEP_WAIT path, not coding hard-resume
        return False
    if not hit.get("affinity"):
        return False
    if _safe_float(hit.get("confidence"), 0) < float(min_confidence):
        return False
    if hit.get("surface") in ("masterplan", "coord") and not hit.get("affinity"):
        return False
    return bool((hit.get("brief") or "").strip())


def extract_all(
    st=None,
    *,
    cwd: str = "",
    session_text: str = "",
    coord_root: str = "",
    progress_plan_files: bool = DEFAULT_PLAN_FILES,
    progress_coord: str = DEFAULT_COORD_MODE,
    progress_affinity: bool = DEFAULT_AFFINITY,
    budget_tokens: int = DEFAULT_BUDGET_TOKENS,
    now: Optional[float] = None,
) -> dict:
    """
    Run all extractors and return:
      { hits, winner, progress_position, content_free }
    """
    now = now if now is not None else time.time()
    text = session_text or (_session_text_from_stats(st) if st is not None else "")
    hits: list[dict] = []
    hits.extend(extract_open_work_progress(st) if st is not None else [])
    hits.extend(extract_masterplan(
        cwd,
        session_text=text,
        require_affinity=progress_affinity,
        budget_tokens=budget_tokens,
        now=now,
    ))
    hits.extend(extract_coord(
        coord_root=coord_root,
        cwd=cwd,
        mode=progress_coord,
        session_text=text,
        budget_tokens=budget_tokens,
    ))
    if st is not None:
        hits.extend(extract_todos(st))
    hits.extend(extract_plan_files(
        cwd,
        enabled=progress_plan_files,
        budget_tokens=budget_tokens,
    ))
    winner = choose_winner(hits)
    # progress_position artifact is the best coding hit for digest; wait also stored
    position = winner
    return {
        "hits": hits,
        "winner": winner,
        "progress_position": position,
        "content_free": content_free_fields(position),
    }


def extract_all_safe(st=None, **kwargs) -> dict:
    """Never-raise wrapper for prepare path."""
    try:
        return extract_all(st, **kwargs)
    except Exception:
        return {
            "hits": [],
            "winner": None,
            "progress_position": None,
            "content_free": content_free_fields(None),
        }
