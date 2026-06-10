#!/usr/bin/env python3
"""
nightly_eval.py — scheduled self-evaluation for autocompactor.

Run nightly from cron (no Claude Code environment needed — thresholds are
read from ~/.claude/settings.json directly):

    30 3 * * * cd /srv/dev/ras/autocompactor && python3 nightly_eval.py

Each run:
  1. Runs the test suites (pytest + smoke) — the canary for transcript
     schema drift after Claude Code upgrades.
  2. Backtests the last day's transcripts; writes a dated JSON report to
     ~/.claude/autocompactor/reports/.
  3. Aggregates live hook telemetry (--events equivalent).
  4. Health checks: hooks firing at all, forced compactions beating the
     hard nag, ceiling violations, dead signals, CLI version changes,
     auto-trigger drift vs. the ~0.675*ceiling estimate, rapid-refill-
     breaker symptoms, native-microcompaction rollout markers.
  5. Appends one summary line to reports/nightly_history.jsonl and writes
     a human-readable reports/nightly-YYYY-MM-DD.md.
  6. Prunes artifacts/backups/reports older than RETENTION_DAYS.

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

REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)

BASE = os.path.expanduser("~/.claude/autocompactor")
REPORTS = os.path.join(BASE, "reports")
HISTORY = os.path.join(REPORTS, "nightly_history.jsonl")
SETTINGS = os.path.expanduser("~/.claude/settings.json")
RETENTION_DAYS = 30
CEILING_SLACK = 40_000   # auto-compact may overshoot the ceiling mid-turn
# Both observed auto-trigger models (proportional ~0.675*window and
# absolute window-65k reserve) predict ~135k at a 200k ceiling; the
# estimate is only trustworthy up to 200k, hence the min() at use site.
EXPECTED_TRIGGER_FRAC = 0.675
TRIGGER_DEVIATION = 25_000   # |auto-pre median - estimate| worth a note
MICRO_MARKER = "[Old tool result content cleared]"   # native microcompaction


def settings_env() -> dict:
    try:
        with open(SETTINGS, encoding="utf-8") as fh:
            return json.load(fh).get("env", {}) or {}
    except Exception:
        return {}


def run(cmd: list, timeout: int = 1800) -> tuple:
    """(exit_code, combined_output) — never raises."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout, cwd=REPO)
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
    env = settings_env()
    window = float(env.get("AUTOCOMPACTOR_WINDOW", 200_000))
    hard_pct = float(env.get("AUTOCOMPACTOR_HARD_PCT", 0.65))
    hard_tokens = window * hard_pct
    ceiling = float(env.get("CLAUDE_CODE_AUTO_COMPACT_WINDOW", 0)) or None
    issues, notes = [], []

    # 1. test suites — schema-drift canary
    py_rc, py_out = run([sys.executable, "-m", "pytest", "tests/", "-q"])
    sm_rc, sm_out = run(["bash", "tests/smoke_test.sh"])
    tests_pass = py_rc == 0 and sm_rc == 0
    if not tests_pass:
        issues.append("TESTS FAILING — likely transcript schema drift; "
                      "pin/inspect the Claude Code version")

    # CLI version change detection (cron PATH lacks ~/.local/bin)
    claude_bin = next((p for p in (
        os.path.expanduser("~/.local/bin/claude"), "claude")
        if p == "claude" or os.path.exists(p)), "claude")
    rc, ver_out = run([claude_bin, "--version"], timeout=60)
    version = (ver_out.strip().split("\n")[0]
               if rc == 0 and ver_out.strip() else "unknown")
    prev_version = None
    try:
        with open(HISTORY, encoding="utf-8") as fh:
            lines = fh.read().strip().splitlines()
            if lines:
                prev_version = json.loads(lines[-1]).get("version")
    except Exception:
        pass
    if prev_version and version != prev_version:
        notes.append(f"Claude Code version changed: {prev_version} -> "
                     f"{version}")
        if not tests_pass:
            issues.append("version change + failing tests: treat as "
                          "schema break until proven otherwise")

    # 2. one-day backtest
    report_path = os.path.join(REPORTS, f"backtest-{today}.json")
    bt_rc, bt_out = run([sys.executable, "analyze_corpus.py",
                         "--root", "~/.claude/projects", "--days", "1",
                         "--json", report_path])
    summary_txt = "\n".join(l for l in bt_out.splitlines()
                            if not l.strip().startswith("- ")
                            and "skipped" not in l).strip()
    auto_pre, manual_n, dead = [], 0, []
    sessions_n = compactions_n = breaker_suspects = 0
    expected_trigger = (EXPECTED_TRIGGER_FRAC * min(ceiling, 200_000)
                        if ceiling else None)
    try:
        with open(report_path, encoding="utf-8") as fh:
            sessions = json.load(fh)["sessions"]
        sessions_n = len(sessions)
        for s in sessions:
            autos_n = 0
            for c in s.get("compactions", []):
                compactions_n += 1
                if c.get("trigger") == "auto":
                    auto_pre.append(c["before"])
                    autos_n += 1
                elif c.get("trigger") == "manual":
                    manual_n += 1
            # breaker symptom: repeated auto-compactions, then context
            # climbing well past the trigger with no further compaction —
            # the upstream rapid-refill breaker may have disabled
            # autocompact for the rest of the session
            post_peak = s.get("post_last_compaction_peak")
            if (expected_trigger and autos_n >= 2 and post_peak
                    and post_peak > expected_trigger + CEILING_SLACK):
                breaker_suspects += 1
        # health check: ceiling enforcement
        if ceiling:
            over = [p for p in auto_pre if p > ceiling + CEILING_SLACK]
            if over:
                issues.append(
                    f"{len(over)} auto-compaction(s) exceeded the "
                    f"{ceiling:,.0f}t ceiling (max {max(over):,.0f}t) — "
                    "CLAUDE_CODE_AUTO_COMPACT_WINDOW may not be applying")
        # health check: auto-trigger drift vs. the model estimate
        if expected_trigger and len(auto_pre) >= 3:
            med = statistics.median(auto_pre)
            if abs(med - expected_trigger) > TRIGGER_DEVIATION:
                notes.append(
                    f"auto-trigger median {med:,.0f}t is >"
                    f"{TRIGGER_DEVIATION / 1000:.0f}k from the "
                    f"~{expected_trigger:,.0f}t estimate — retune "
                    "AUTOCOMPACTOR_HARD_PCT to stay ahead of the real "
                    "trigger")
        if breaker_suspects:
            issues.append(
                f"{breaker_suspects} session(s) show rapid-refill-breaker "
                "symptoms: repeated auto-compactions, then context past "
                f"~{expected_trigger:,.0f}t with no further compaction — "
                "autocompact may have been disabled mid-session")
    except Exception as exc:
        if bt_rc != 0:
            issues.append(f"backtest failed: {bt_out[-300:]}")
        else:
            notes.append(f"report parse: {type(exc).__name__}: {exc}")
    for line in bt_out.splitlines():
        if "never fired" in line:
            dead.append(line.strip("* ").strip())

    # 3. live telemetry (hooks actually running?)
    evs = day_events()
    mon = [e for e in evs if e.get("type") == "monitor_eval"]
    pre = [e for e in evs if e.get("type") == "precompact"]
    rei = [e for e in evs if e.get("type") == "reinject"]
    had_sessions = sessions_n > 0
    if had_sessions and not mon:
        issues.append("sessions ran in the last day but ZERO monitor_eval "
                      "telemetry — hooks may be unregistered or crashing")
    recos = sum(1 for e in mon if e.get("recommended"))

    # The purpose metric: of the auto-compactions the hooks saw, how many
    # got an advance recommendation in the same session beforehand?
    auto_events = [e for e in pre if e.get("trigger") == "auto"]
    unwarned = 0
    for ev in auto_events:
        sid, ts = ev.get("session_id"), ev.get("ts", "")
        prior_pre = [p.get("ts", "") for p in pre
                     if p.get("session_id") == sid and p.get("ts", "") < ts]
        floor = max(prior_pre) if prior_pre else ""
        warned = any(m.get("session_id") == sid and m.get("recommended")
                     and floor < m.get("ts", "") < ts for m in mon)
        if not warned:
            unwarned += 1
    if auto_events and unwarned > len(auto_events) * 0.5:
        issues.append(
            f"{unwarned}/{len(auto_events)} auto-compactions arrived with "
            "no advance recommendation — thresholds/signals are not "
            "engaging before the trigger; inspect occupancy_seen in "
            "--events and per-signal precision")

    # 4. native-microcompaction rollout watch. The cleared-content marker
    # is statsig-gated upstream (off for this account as of 2026-06); if
    # it starts appearing, per-turn tool-result clearing changes the
    # compaction economics and thresholds need a rethink. The autocompactor
    # dev project is excluded — its sessions *discuss* the literal marker.
    micro_n = 0
    cutoff_t = time.time() - 26 * 3600
    for p in glob.glob(os.path.expanduser("~/.claude/projects/*/*.jsonl")):
        if "-home-grojas-autocompactor" in p:
            continue
        try:
            if os.path.getmtime(p) < cutoff_t:
                continue
            with open(p, "rb") as fh:
                prev = b""
                while True:
                    buf = fh.read(1 << 20)
                    if not buf:
                        break
                    if MICRO_MARKER.encode() in prev + buf:
                        micro_n += 1
                        break
                    prev = buf[-64:]
        except OSError:
            continue
    if micro_n:
        notes.append(
            f"native-microcompaction marker in {micro_n} recent "
            "transcript(s) — upstream rollout may have reached this "
            "account; per-turn tool-result clearing changes the math, "
            "revisit thresholds")

    # 5. outputs
    record = {
        "date": today, "version": version, "tests_pass": tests_pass,
        "sessions": sessions_n, "compactions": compactions_n,
        "auto_n": len(auto_pre),
        "auto_pre_median": (round(statistics.median(auto_pre))
                            if auto_pre else None),
        "auto_pre_max": max(auto_pre) if auto_pre else None,
        "manual_n": manual_n,
        "monitor_evals": len(mon), "recommendations": recos,
        "precompact_events": len(pre), "reinjects": len(rei),
        "auto_seen_by_hooks": len(auto_events), "auto_unwarned": unwarned,
        "hard_tokens": hard_tokens, "ceiling": ceiling,
        "expected_trigger": expected_trigger,
        "breaker_suspects": breaker_suspects,
        "micro_marker_sessions": micro_n,
        "dead_signals": dead, "issues": issues, "notes": notes,
    }
    with open(HISTORY, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")

    md = [f"# autocompactor nightly — {today}", "",
          f"- CLI: {version}   tests: {'PASS' if tests_pass else 'FAIL'}",
          f"- sessions {sessions_n}, compactions {compactions_n} "
          f"(auto {len(auto_pre)}, manual {manual_n})"]
    if auto_pre:
        line = (f"- auto trigger: median {statistics.median(auto_pre):,.0f}t, "
                f"max {max(auto_pre):,.0f}t (hard nag {hard_tokens:,.0f}t")
        line += f", ceiling {ceiling:,.0f}t)" if ceiling else ")"
        md.append(line)
    md.append(f"- hooks: {len(mon)} evals, {recos} recommendations, "
              f"{len(pre)} precompact, {len(rei)} reinjects")
    if expected_trigger:
        md.append(f"- watches: expected trigger ~{expected_trigger:,.0f}t, "
                  f"breaker suspects {breaker_suspects}, "
                  f"micro markers {micro_n}")
    md.append("")
    md.append("## Issues" if issues else "## No issues")
    md += [f"- {i}" for i in issues]
    if notes:
        md += ["", "## Notes"] + [f"- {n}" for n in notes]
    if summary_txt:
        md += ["", "## Backtest summary", "```", summary_txt, "```"]
    if not tests_pass:
        md += ["", "## Test output (tail)", "```",
               (py_out + "\n" + sm_out)[-1500:], "```"]
    with open(os.path.join(REPORTS, f"nightly-{today}.md"), "w",
              encoding="utf-8") as fh:
        fh.write("\n".join(md) + "\n")

    # 6. retention
    pruned = sum(prune(os.path.join(BASE, d))
                 for d in ("artifacts", "backups", "reports"))
    if pruned:
        print(f"pruned {pruned} file(s) older than {RETENTION_DAYS}d")

    print(f"nightly eval {today}: tests={'PASS' if tests_pass else 'FAIL'} "
          f"sessions={sessions_n} compactions={compactions_n} "
          f"issues={len(issues)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
