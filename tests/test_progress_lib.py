"""progress_lib: ranking, affinity, wait supremacy, budget, gated surfaces."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIX = os.path.join(REPO, "tests", "fixtures", "progress")
sys.path.insert(0, os.path.join(REPO, "src"))

from autocompactor import progress_lib, transcript_lib  # noqa: E402


@pytest.fixture
def demo_repo(tmp_path: Path) -> Path:
    """cwd with docs/masterplan/demo-wave-run/state.yml from fixture."""
    src = Path(FIX) / "masterplan_active_state.yml"
    dest_dir = tmp_path / "docs" / "masterplan" / "demo-wave-run"
    dest_dir.mkdir(parents=True)
    (dest_dir / "state.yml").write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    return tmp_path


def _affinity_true_text() -> str:
    p = Path(FIX) / "session_affinity_true.jsonl"
    return p.read_text(encoding="utf-8")


def _affinity_false_text() -> str:
    p = Path(FIX) / "session_affinity_false.jsonl"
    return p.read_text(encoding="utf-8")


def test_masterplan_picks_first_pending_by_wave(demo_repo: Path):
    hits = progress_lib.extract_masterplan(
        str(demo_repo),
        session_text=_affinity_true_text(),
    )
    assert hits
    hit = hits[0]
    assert hit["surface"] == "masterplan"
    assert hit["mode"] == "code"
    assert "t7" in hit["key"]
    assert "T7" in hit["brief"] or "T7" in hit["summary"]
    assert "progress_lib.py" in " ".join(hit.get("files") or [])
    assert "RESUME mid-task" in hit["brief"]


def test_affinity_true_allows_high_confidence(demo_repo: Path):
    hits = progress_lib.extract_masterplan(
        str(demo_repo), session_text=_affinity_true_text())
    assert hits[0]["affinity"] is True
    assert hits[0]["confidence"] >= 0.75
    assert progress_lib.hard_resume_eligible(hits[0]) is True


def test_affinity_false_blocks_hard_resume(demo_repo: Path):
    # Strip active_run so only session text can grant affinity (spec A1).
    state_path = demo_repo / "docs" / "masterplan" / "demo-wave-run" / "state.yml"
    text = state_path.read_text(encoding="utf-8")
    text = text.replace("active_run:\n  wave: 2\n  phase: launching\n", "")
    state_path.write_text(text, encoding="utf-8")
    hits = progress_lib.extract_masterplan(
        str(demo_repo), session_text=_affinity_false_text())
    assert hits, "position still extracted for digest"
    assert hits[0]["affinity"] is False
    assert progress_lib.hard_resume_eligible(hits[0]) is False


def test_missing_cwd_no_masterplan_hits():
    assert progress_lib.extract_masterplan("") == []
    assert progress_lib.extract_masterplan("/nonexistent/path/xyz") == []


def test_wait_mode_beats_masterplan_code(demo_repo: Path):
    st = transcript_lib.TranscriptStats()
    st.open_work = [{
        "kind": "waiting_monitor",
        "summary": "waiting on build Y99",
        "resource_ids": ["Y99"],
        "monitor_cmds": ["yanos-builder show Y99"],
        "next_on_success": "continue T7",
    }]
    st.last_user_task = "unrelated"
    result = progress_lib.extract_all(
        st,
        cwd=str(demo_repo),
        session_text=_affinity_true_text(),
    )
    winner = result["winner"]
    assert winner is not None
    assert winner["mode"] == "wait"
    assert winner["surface"] == "open_work"
    # coding masterplan still in hits
    assert any(h["surface"] == "masterplan" for h in result["hits"])


def test_choose_winner_rank_within_code_mode():
    hits = [
        {"mode": "code", "rank": 55, "confidence": 0.9, "mtime": 1, "key": "todo"},
        {"mode": "code", "rank": 95, "confidence": 0.8, "mtime": 1, "key": "mp"},
    ]
    w = progress_lib.choose_winner(hits)
    assert w["key"] == "mp"


def test_brief_budget_cap():
    long_files = [f"src/file_{i}.py" for i in range(20)]
    brief = progress_lib.format_resume_brief(
        title="x" * 500,
        unit_id="T9",
        files=long_files,
        verify=["cmd"] * 5,
        budget_tokens=50,
    )
    assert "RESUME mid-task" in brief
    # ~50 tokens * 4 chars
    assert len(brief) <= 50 * 4 + 5


def test_content_free_fields_omit_brief_text(demo_repo: Path):
    hits = progress_lib.extract_masterplan(
        str(demo_repo), session_text=_affinity_true_text())
    cf = progress_lib.content_free_fields(hits[0])
    blob = json.dumps(cf)
    assert "RESUME" not in blob
    assert hits[0]["brief"][:20] not in blob
    assert cf["progress_surface"] == "masterplan"
    assert cf["progress_brief_len"] > 0


def test_coord_task_only_skips_wave_only(tmp_path: Path):
    root = tmp_path / "coord"
    job = root / "mp-wave-9-abc"
    job.mkdir(parents=True)
    (job / "job.json").write_text(json.dumps({
        "job_id": "mp-wave-9-abc",
        "goal": "wave 9",
        "state": "open",
    }), encoding="utf-8")
    (job / "tasks").mkdir()
    hits = progress_lib.extract_coord(
        coord_root=str(root), cwd=str(tmp_path), mode="task_only")
    assert hits == []


def test_coord_wave_ok_includes_wave_level(tmp_path: Path):
    root = tmp_path / "coord"
    job = root / "mp-wave-9-abc"
    job.mkdir(parents=True)
    (job / "job.json").write_text(json.dumps({
        "job_id": "mp-wave-9-abc",
        "goal": "wave 9 long enough goal text",
        "state": "open",
    }), encoding="utf-8")
    hits = progress_lib.extract_coord(
        coord_root=str(root), cwd=str(tmp_path), mode="wave_ok")
    assert hits
    assert hits[0]["surface"] == "coord"


def test_plan_files_default_off(demo_repo: Path):
    (demo_repo / "PLAN.md").write_text("- [ ] do a thing\n", encoding="utf-8")
    assert progress_lib.extract_plan_files(str(demo_repo), enabled=False) == []
    hits = progress_lib.extract_plan_files(str(demo_repo), enabled=True)
    assert hits
    assert hits[0]["surface"] == "plan_file"


def test_todos_surface():
    st = transcript_lib.TranscriptStats()
    st.todos = [
        {"content": "done item", "status": "completed"},
        {"content": "ship progress_lib", "status": "pending"},
    ]
    hits = progress_lib.extract_todos(st)
    assert hits
    assert "ship progress_lib" in hits[0]["brief"]


def test_extract_all_safe_never_raises(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("nope")
    monkeypatch.setattr(progress_lib, "extract_masterplan", boom)
    out = progress_lib.extract_all_safe(None, cwd="/tmp")
    assert out["winner"] is None
    assert out["hits"] == []


def test_worktree_cwd_grants_affinity(tmp_path: Path):
    # Simulate .worktrees/<slug> cwd — strip active_run so only path grants affinity.
    wt = tmp_path / ".worktrees" / "demo-wave-run"
    dest = wt / "docs" / "masterplan" / "demo-wave-run"
    dest.mkdir(parents=True)
    src = Path(FIX) / "masterplan_active_state.yml"
    text = src.read_text(encoding="utf-8")
    text = text.replace("active_run:\n  wave: 2\n  phase: launching\n", "")
    (dest / "state.yml").write_text(text, encoding="utf-8")
    hits = progress_lib.extract_masterplan(
        str(wt), session_text="unrelated grok fix")
    assert hits
    assert hits[0]["affinity"] is True
    # Path-only affinity must NOT hard-resume pending tasks (phantom resume).
    assert hits[0].get("path_affinity") is True
    assert hits[0].get("session_bound") is False
    assert progress_lib.hard_resume_eligible(hits[0]) is False


def test_worktree_plus_session_slug_allows_hard_resume(tmp_path: Path):
    wt = tmp_path / ".worktrees" / "demo-wave-run"
    dest = wt / "docs" / "masterplan" / "demo-wave-run"
    dest.mkdir(parents=True)
    src = Path(FIX) / "masterplan_active_state.yml"
    (dest / "state.yml").write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    hits = progress_lib.extract_masterplan(
        str(wt), session_text="continuing demo-wave-run T7 implement")
    assert hits
    assert hits[0].get("session_bound") is True
    assert progress_lib.hard_resume_eligible(hits[0]) is True


def test_in_progress_task_hard_resumes_with_path_only(tmp_path: Path):
    """Live in_progress status is enough even without session slug text."""
    wt = tmp_path / ".worktrees" / "demo-wave-run"
    dest = wt / "docs" / "masterplan" / "demo-wave-run"
    dest.mkdir(parents=True)
    # Force first task to in_progress
    import yaml
    data = yaml.safe_load((Path(FIX) / "masterplan_active_state.yml").read_text())
    data.pop("active_run", None)
    for task in data.get("tasks") or []:
        if isinstance(task, dict):
            task["status"] = "done"
    # mark one in_progress
    for task in data.get("tasks") or []:
        if isinstance(task, dict) and task.get("id") == 7:
            task["status"] = "in_progress"
            break
    else:
        data.setdefault("tasks", []).append(
            {"id": 7, "status": "in_progress", "wave": 2, "title": "live"})
    (dest / "state.yml").write_text(yaml.safe_dump(data), encoding="utf-8")
    hits = progress_lib.extract_masterplan(
        str(wt), session_text="unrelated status?")
    assert hits
    assert str(hits[0].get("status")).lower().replace("-", "_") in (
        "in_progress",)
    assert hits[0].get("session_bound") is False
    assert progress_lib.hard_resume_eligible(hits[0]) is True


def test_hard_resume_respects_progress_resume_and_nextstep(demo_repo: Path):
    hits = progress_lib.extract_masterplan(
        str(demo_repo), session_text=_affinity_true_text())
    hit = hits[0]
    assert progress_lib.hard_resume_eligible(
        hit, progress_resume="advisory") is False
    assert progress_lib.hard_resume_eligible(
        hit, nextstep="off") is False
    assert progress_lib.hard_resume_eligible(
        hit, min_confidence=0.99) is False
