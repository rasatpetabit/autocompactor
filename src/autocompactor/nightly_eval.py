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
PI_BASE = statedir.state_root("pi")
REPORTS = os.path.join(BASE, "reports")
HISTORY = os.path.join(REPORTS, "nightly_history.jsonl")
RETENTION_DAYS = 30
FLOOR_PROBE_NAME = "floor-probe.json"
FLOOR_PROBE_PATH = os.path.join(PI_BASE, FLOOR_PROBE_NAME)


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



# --- Floor probe (spec section 7: readout-only per-package tool-schema cost) ---
#
# Tool schemas are generated from zod AT LOAD and are not measurable from
# on-disk source, so the probe SPAWNS isolated pi instances toggling layers
# (the 2026-06-09 investigation's method) and diffs first-request token
# counts to attribute per-package tool-schema cost.
#
# OBSERVATIONAL ONLY: floor-probe.json feeds the READOUT (inventory per-package
# breakdown + reducible-floor advisory + T10 freshness) ONLY; the live decision
# uses the residual base (context_inventory.decision_floor_terms), NEVER this
# artifact. Tests assert no decision/policy module reads floor-probe.json.
#
# Frozen schema (all consumers - inventory-core tools_system read, T10
# freshness, T6 label - MUST use these exact keys):
#   {"per_package": {name: tokens}, "measured_at": <ISO-8601 UTC>,
#    "pi_version": <str>, "staleness_budget": <seconds int>}

_FIRST_REQUEST_TIMEOUT = 120


def _parse_first_request_input(jsonl_text):
    """Extract the first assistant turn usage.input token count from
    `pi --mode json` output. Returns int, or None if not found. Never raises."""
    try:
        for line in jsonl_text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if obj.get("type") in ("message_end", "turn_end"):
                msg = obj.get("message") or {}
                usage = msg.get("usage") if isinstance(msg, dict) else None
                if isinstance(usage, dict):
                    inp = usage.get("input")
                    if isinstance(inp, (int, float)) and inp > 0:
                        return int(inp)
            if obj.get("type") == "agent_end":
                for m in obj.get("messages", []) or []:
                    if (m.get("role") == "assistant"
                            and isinstance(m.get("usage"), dict)
                            and isinstance(m["usage"].get("input"), (int, float))
                            and m["usage"]["input"] > 0):
                        return int(m["usage"]["input"])
    except Exception:
        return None
    return None


def spawn_first_request_tokens(label, *, env_overrides=None,
                                provider=None, model=None,
                                extra_args=None, timeout=_FIRST_REQUEST_TIMEOUT):
    """Spawn an isolated `pi --mode json -p 'hi'` and return the first assistant
    turn usage.input token count, or None on any failure. Tests monkeypatch
    this to inject canned results. Never raises."""
    try:
        env = dict(os.environ)
        if env_overrides:
            for k, v in env_overrides.items():
                if v is None:
                    env.pop(k, None)
                else:
                    env[k] = str(v)
        argv = ["pi", "--mode", "json"]
        if provider:
            argv += ["--provider", provider]
        if model:
            argv += ["--model", model]
        if extra_args:
            argv += list(extra_args)
        argv += ["-p", "hi"]
        proc = subprocess.run(argv, capture_output=True, text=True,
                              timeout=timeout, env=env)
        return _parse_first_request_input(proc.stdout)
    except Exception:
        return None


def run_floor_probe(*, spawn=spawn_first_request_tokens,
                    packages=None, pi_version=None):
    """Orchestrate the isolation-spawn diffs and return the per_package map
    {name: tokens}. The injected `spawn(label, *, env_overrides, provider,
    model, extra_args, timeout)` makes the heavy spawn step testable without
    spawning pi. Returns an empty dict if the full-run baseline is missing or
    every per-package diff is unavailable (never raises - best-effort)."""
    per_package = {}
    try:
        full_tok = spawn("full", env_overrides=None, extra_args=None)
        if full_tok is None or full_tok <= 0:
            return per_package
        if packages is None:
            packages = _default_probe_packages()
        for name, env_over, extra in packages:
            tok = spawn(name, env_overrides=env_over, extra_args=extra)
            if tok is None or tok <= 0:
                continue
            diff = full_tok - tok
            if diff > 0:
                per_package[name] = int(diff)
    except Exception:
        return per_package
    return per_package


def _default_probe_packages():
    """Default isolation recipes: per-package cost = full - (full minus the
    package tool schemas). Excluding a package is done by toggling
    PI_CODING_AGENT_DIR / extension discovery. A recipe that fails to actually
    exclude a package simply yields a ~0 diff and contributes 0 (degrades
    gracefully). The operator may override via the `packages` argument."""
    return [
        ("pi-subagents", {"PI_CODING_AGENT_DIR": None}, ["--no-extensions"]),
        ("context-mode", {"PI_CODING_AGENT_DIR": None}, ["--no-extensions"]),
    ]


def write_floor_probe(per_package, *, pi_version=None):
    """Write floor-probe.json with the FROZEN schema. measured_at is an
    ISO-8601 UTC string; staleness_budget consumes the probe-staleness config
    key (defaults to 14 days). Never raises; returns the path written or None."""
    try:
        os.makedirs(PI_BASE, exist_ok=True)
        rec = {
            "per_package": {str(k): int(v) for k, v in per_package.items()},
            "measured_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "pi_version": str(pi_version or _detect_pi_version() or ""),
            "staleness_budget": int(config_lib.cfg.float(
                "PROBE_STALENESS_SECONDS", default=14 * 86400)),
        }
        with open(FLOOR_PROBE_PATH, "w") as fh:
            json.dump(rec, fh, indent=2, sort_keys=True)
        return FLOOR_PROBE_PATH
    except Exception:
        return None


def _detect_pi_version():
    """Best-effort `pi --version` capture. Returns '' on any failure."""
    try:
        proc = subprocess.run(["pi", "--version"], capture_output=True,
                               text=True, timeout=10)
        return (proc.stdout or proc.stderr or "").strip()
    except Exception:
        return ""


def floor_probe_is_fresh():
    """Read-only freshness check for install_pi --status (T10). Returns
    (state, measured_at) where state in {'fresh','stale','missing'}.
    Never raises."""
    try:
        with open(FLOOR_PROBE_PATH) as fh:
            data = json.load(fh)
        measured = data.get("measured_at", "")
        budget = int(data.get("staleness_budget") or
                     config_lib.cfg.float("PROBE_STALENESS_SECONDS",
                                           default=14 * 86400))
        age = _probe_age_seconds(measured)
        if age is None or age <= budget:
            return "fresh", measured
        return "stale", measured
    except Exception:
        return "missing", ""


def _probe_age_seconds(measured_at):
    if not measured_at:
        return None
    try:
        ts = datetime.datetime.fromisoformat(measured_at.replace("Z", "+00:00"))
        return max(int((datetime.datetime.now(datetime.timezone.utc) - ts)
                       .total_seconds()), 0)
    except Exception:
        return None
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

    # 4. floor probe (spec §7) — readout-only per-package tool-schema cost.
    #    Best-effort: a spawn failure (no `pi` on PATH, env fragility) writes no
    #    artifact and never breaks the nightly eval. Observational only: feeds
    #    the readout, never the decision (which uses the live residual base).
    try:
        per_pkg = run_floor_probe()
        if per_pkg:
            write_floor_probe(per_pkg)
    except Exception as _e:
        print(f"floor probe skipped: {_e}")

    # 5. retention
    pruned = sum(prune(os.path.join(BASE, d))
                 for d in ("artifacts", "backups", "reports"))
    if pruned:
        print(f"pruned {pruned} file(s) older than {RETENTION_DAYS}d")

    print(f"nightly eval {today}: tests={'PASS' if tests_pass else 'FAIL'} "
          f"issues={len(issues)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
