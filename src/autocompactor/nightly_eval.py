#!/usr/bin/env python3
"""
nightly_eval.py — scheduled self-evaluation for autocompactor.

Run nightly from cron:

    30 3 * * * cd /srv/dev/ras/autocompactor && python3 src/nightly_eval.py

Each run:
  1. Runs the test suites (pytest + smoke) — the canary for transcript
     schema drift after harness upgrades.
  2. Aggregates live hook telemetry; realized post-compaction reduction.
  3. Appends one summary line to reports/nightly_history.jsonl and writes
     a human-readable reports/nightly-YYYY-MM-DD.md.
  4. Prunes artifacts/backups/reports older than RETENTION_DAYS.

Local-only and content-free, like all autocompactor telemetry. Always
exits 0 (issues are reported in the outputs, not the exit code).
"""

from __future__ import annotations

import datetime
import glob
import json
import os
import statistics
import subprocess
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # checkout root
import autocompactor.config_lib as config_lib  # noqa: E402
from autocompactor import statedir  # noqa: E402

BASE = statedir.state_root()
REPORTS = os.path.join(BASE, "reports")
HISTORY = os.path.join(REPORTS, "nightly_history.jsonl")
RETENTION_DAYS = 30


def run(cmd: list, timeout: int = 1800, env: dict = None) -> tuple:
    """(exit_code, combined_output) — never raises.

    `env`, when given, is passed through to subprocess.run as the child's
    full environment (callers must include PATH etc.); None inherits the
    parent's, matching the prior behavior.
    """
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout, cwd=REPO, env=env)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except Exception as exc:
        return -1, f"{type(exc).__name__}: {exc}"


def day_events(hours: float = 26.0) -> list:
    """Telemetry events from the last `hours` (26 = daily run + slack)."""
    cutoff = (datetime.datetime.now()
              - datetime.timedelta(hours=hours)).isoformat()
    out = []
    try:
        with open(os.path.join(BASE, "stats", "events.jsonl"),
                  encoding="utf-8") as fh:
            for line in fh:
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if e.get("ts", "") >= cutoff:
                    out.append(e)
    except OSError:
        pass
    return out


def realized_reductions(pre: list, mon: list) -> dict:
    """Realized post-compaction floor, reconstructed from existing telemetry.

    PostCompact can't log `after_tokens` — at hook time the freshly compacted
    transcript has no post-compaction usage figure yet. The floor only becomes
    observable on the first post-compaction model turn. So pair each
    `precompact` event with the first later same-session `monitor_eval` whose
    `context_tokens` is smaller; that smaller figure is the realized after-size
    and `before - after` is the reclaim. Content-free (token counts only)."""
    by_sess = {}
    for m in mon:
        if m.get("context_tokens"):
            by_sess.setdefault(m.get("session_id"), []).append(m)
    for lst in by_sess.values():
        lst.sort(key=lambda m: m.get("ts", ""))
    reclaims = []
    for ev in pre:
        before = ev.get("context_tokens")
        if not before:
            continue
        sid, ts = ev.get("session_id"), ev.get("ts", "")
        after = next((m["context_tokens"] for m in by_sess.get(sid, [])
                      if m.get("ts", "") > ts
                      and m["context_tokens"] < before), None)
        if after:
            reclaims.append(before - after)
    return {"reclaim_n": len(reclaims),
            "reclaim_median": (round(statistics.median(reclaims))
                               if reclaims else None)}


def prune(directory: str, days: int = RETENTION_DAYS) -> int:
    cutoff = time.time() - days * 86400
    removed = 0
    for p in glob.glob(os.path.join(directory, "*")):
        try:
            if os.path.isfile(p) and os.path.getmtime(p) < cutoff:
                os.unlink(p)
                removed += 1
        except OSError:
            pass
    return removed


def main() -> int:
    os.makedirs(REPORTS, exist_ok=True)
    today = datetime.date.today().isoformat()
    window = config_lib.cfg.float("WINDOW", default=200_000)
    hard_pct = config_lib.cfg.float("HARD_PCT", default=0.65)
    hard_tokens = window * hard_pct
    issues, notes = [], []

    # 1. test suites — schema-drift canary
    py_rc, py_out = run([sys.executable, "-m", "pytest", "tests/", "-q"])
    # The Pi smoke test is a no-op (exit 0) unless PI_SMOKE=1, and the file is
    # tests/smoke_test_pi.sh (not the generic tests/smoke_test.sh, which does
    # not exist). Set the gate and use the real path so the canary actually
    # exercises the Pi-bridge contract instead of silently skipping.
    smoke_env = {**os.environ, "PI_SMOKE": "1"}
    sm_rc, sm_out = run(["bash", "tests/smoke_test_pi.sh"], env=smoke_env)
    tests_pass = py_rc == 0 and sm_rc == 0
    if not tests_pass:
        issues.append("TESTS FAILING — likely transcript schema drift; "
                      "pin/inspect the harness version")

    # 2. live telemetry (hooks actually running?)
    evs = day_events()
    mon = [e for e in evs if e.get("type") == "monitor_eval"]
    pre = [e for e in evs if e.get("type") == "precompact"]
    rei = [e for e in evs if e.get("type") == "reinject"]
    recos = sum(1 for e in mon if e.get("recommended"))

    # Realized post-compaction reduction (WI-B): reconstruct the after-size
    # PostCompact can't log, by joining each precompact event to the first
    # later smaller monitor_eval in the same session. This is the verified
    # reclaim figure; "reclaimed ~Z" was previously unverifiable.
    reduc = realized_reductions(pre, mon)

    # 3. outputs
    record = {
        "date": today, "tests_pass": tests_pass,
        "monitor_evals": len(mon), "recommendations": recos,
        "precompact_events": len(pre), "reinjects": len(rei),
        "hard_tokens": hard_tokens,
        "reclaim_n": reduc["reclaim_n"], "reclaim_median": reduc["reclaim_median"],
        "issues": issues, "notes": notes,
    }
    with open(HISTORY, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")

    md = [f"# autocompactor nightly — {today}", "",
          f"- tests: {'PASS' if tests_pass else 'FAIL'}",
          f"- hooks: {len(mon)} evals, {recos} recommendations, "
          f"{len(pre)} precompact, {len(rei)} reinjects"]
    if reduc["reclaim_n"]:
        md.append(
            f"- realized reduction: median {reduc['reclaim_median']:,.0f}t "
            f"reclaimed over {reduc['reclaim_n']} compaction(s) "
            "(precompact → first smaller eval)")
    md.append("")
    md.append("## Issues" if issues else "## No issues")
    md += [f"- {i}" for i in issues]
    if notes:
        md += ["", "## Notes"] + [f"- {n}" for n in notes]
    if not tests_pass:
        md += ["", "## Test output (tail)", "```",
               (py_out + "\n" + sm_out)[-1500:], "```"]
    with open(os.path.join(REPORTS, f"nightly-{today}.md"), "w",
              encoding="utf-8") as fh:
        fh.write("\n".join(md) + "\n")

    # 4. retention
    pruned = sum(prune(os.path.join(BASE, d))
                 for d in ("artifacts", "backups", "reports"))
    if pruned:
        print(f"pruned {pruned} file(s) older than {RETENTION_DAYS}d")

    print(f"nightly eval {today}: tests={'PASS' if tests_pass else 'FAIL'} "
          f"issues={len(issues)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
