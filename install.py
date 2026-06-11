#!/usr/bin/env python3
"""
install.py — single entry point for installing/maintaining autocompactor.

Idempotent throughout: safe to re-run; replaces any previous autocompactor
hook entries (matched by 'autocompactor' in the command string) and leaves
all other hooks, env keys, and cron lines untouched.

    python3 install.py              # install/update hooks + missing env keys
    python3 install.py --cron       # ... and register the nightly cron job
    python3 install.py --force-env  # overwrite env keys with code defaults
    python3 install.py --remove     # uninstall hooks + our env keys + cron
    python3 install.py --verify     # pytest + smoke + live transcript probe
    python3 install.py --status     # doctor: report install health

Env policy: tuning lives in config.json (+ config.local.json), read at
runtime by config_lib — install no longer seeds AUTOCOMPACTOR_* env keys
(env vars remain available as manual runtime overrides). Install sets only
MISSING native Claude Code keys; --force-env resets them to the defaults
below. --remove deletes AUTOCOMPACTOR_* keys but leaves the native
CLAUDE_* settings with a note.
"""

import glob
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CRON_MARKER = "# autocompactor-nightly"

# Only native Claude Code settings are seeded into settings.json env —
# autocompactor's own tuning lives in config.json and is read at runtime,
# so installs never plant stale AUTOCOMPACTOR_* overrides.
# Native settings: cap Claude Code's own auto-compact ceiling so the
# advisor and the native trigger agree on the window. 500k on a 1M model
# compacts around ~360k (90% of cap minus reserve); on smaller windows
# the model's own limit binds first.
ENV_DEFAULTS = {
    "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "90",
    "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "500000",
}


def settings_path() -> str:
    return os.path.expanduser("~/.claude/settings.json")


def state_dir() -> str:
    return os.path.expanduser("~/.claude/autocompactor")


def hook_config() -> dict:
    return {
        "UserPromptSubmit": [{
            "hooks": [{"type": "command",
                       "command": f"python3 {HERE}/context_monitor.py"}]
        }],
        "PreCompact": [
            {"matcher": "manual",
             "hooks": [{"type": "command",
                        "command": f"python3 {HERE}/precompact_analyzer.py"}]},
            {"matcher": "auto",
             "hooks": [{"type": "command",
                        "command": f"python3 {HERE}/precompact_analyzer.py"}]},
        ],
    }


def _is_ours(group: dict) -> bool:
    return any("autocompactor" in (h.get("command") or "")
               for h in group.get("hooks", []))


def load_settings():
    try:
        with open(settings_path()) as fh:
            return json.load(fh)
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as exc:
        print(f"refusing to touch malformed {settings_path()}: {exc}")
        return None


def write_settings(settings: dict) -> None:
    path = settings_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(settings, fh, indent=2)
    os.replace(tmp, path)


# ---------------------------------------------------------------- hooks

def apply_hooks(settings: dict, remove: bool) -> None:
    hooks = settings.setdefault("hooks", {})
    config = hook_config()
    for event in config:
        kept = [g for g in hooks.get(event, []) if not _is_ours(g)]
        if not remove:
            kept += config[event]
        if kept:
            hooks[event] = kept
        else:
            hooks.pop(event, None)
    if not hooks:
        settings.pop("hooks", None)


def hooks_registered(settings: dict) -> dict:
    """Per-event count of our hook groups currently registered."""
    hooks = settings.get("hooks", {})
    return {event: sum(1 for g in hooks.get(event, []) if _is_ours(g))
            for event in hook_config()}


# ------------------------------------------------------------------ env

def apply_env(settings: dict, force: bool) -> tuple:
    """Merge ENV_DEFAULTS into settings['env']. Returns (set, kept)."""
    env = settings.setdefault("env", {})
    added, kept = [], []
    for key, value in ENV_DEFAULTS.items():
        if key not in env:
            env[key] = value
            added.append(key)
        elif force and env[key] != value:
            env[key] = value
            added.append(key)
        elif env[key] != value:
            kept.append(f"{key}={env[key]} (default {value})")
    return added, kept


def remove_env(settings: dict) -> list:
    env = settings.get("env", {})
    removed = [k for k in list(env) if k.startswith("AUTOCOMPACTOR_")]
    for k in removed:
        del env[k]
    if not env:
        settings.pop("env", None)
    return removed


# ----------------------------------------------------------------- cron

def cron_line() -> str:
    return (f"30 3 * * * /usr/bin/python3 {HERE}/nightly_eval.py "
            f">> {state_dir()}/nightly.log 2>&1 {CRON_MARKER}")


def read_crontab() -> list:
    try:
        res = subprocess.run(["crontab", "-l"], capture_output=True,
                             text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return []
    if res.returncode != 0:
        return []
    return res.stdout.splitlines()


def write_crontab(lines: list) -> bool:
    text = "\n".join(lines) + ("\n" if lines else "")
    try:
        res = subprocess.run(["crontab", "-"], input=text, text=True,
                             capture_output=True, timeout=10)
        return res.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def apply_cron(remove: bool) -> str:
    lines = [ln for ln in read_crontab() if CRON_MARKER not in ln]
    if not remove:
        lines.append(cron_line())
    if write_crontab(lines):
        return "removed nightly cron job" if remove else \
               f"registered nightly cron job: {cron_line()}"
    return "WARNING: could not update crontab (is cron installed?)"


def cron_registered() -> bool:
    return any(CRON_MARKER in ln for ln in read_crontab())


# --------------------------------------------------------------- verify

def newest_transcript() -> str:
    pattern = os.path.expanduser("~/.claude/projects/*/*.jsonl")
    files = glob.glob(pattern)
    return max(files, key=os.path.getmtime) if files else ""


def run_verify() -> int:
    failures = 0

    print("== pytest ==")
    rc = subprocess.call([sys.executable, "-m", "pytest", "tests/", "-q"],
                         cwd=HERE)
    failures += rc != 0

    print("\n== smoke test ==")
    rc = subprocess.call(["bash", f"{HERE}/tests/smoke_test.sh"], cwd=HERE)
    failures += rc != 0

    print("\n== live transcript probe ==")
    transcript = newest_transcript()
    if not transcript:
        print("no transcripts under ~/.claude/projects/ — probe skipped")
    else:
        payload = json.dumps({
            "session_id": "install-verify",
            "transcript_path": transcript,
            "cwd": HERE,
            "hook_event_name": "UserPromptSubmit",
            "prompt": "install.py --verify probe",
        })
        try:
            res = subprocess.run(
                [sys.executable, f"{HERE}/context_monitor.py"],
                input=payload, text=True, capture_output=True, timeout=60)
            ok = res.returncode == 0
            if ok and res.stdout.strip():
                json.loads(res.stdout)  # must be JSON when present
            print(f"probe on {os.path.basename(transcript)}: "
                  + ("OK" if ok else f"FAIL rc={res.returncode}"))
            if not ok:
                print(res.stderr[-2000:])
            failures += not ok
        except Exception as exc:
            print(f"probe FAILED: {exc}")
            failures += 1
        finally:
            # the probe writes per-session state; clean it up
            st = os.path.join(state_dir(), "install-verify.state.json")
            try:
                os.remove(st)
            except OSError:
                pass

    print("\n" + ("VERIFY: PASS" if not failures
                  else f"VERIFY: {failures} check(s) FAILED"))
    return 1 if failures else 0


# --------------------------------------------------------------- status

def claude_version() -> str:
    claude_bin = shutil.which("claude") or os.path.expanduser(
        "~/.local/bin/claude")
    try:
        res = subprocess.run([claude_bin, "--version"], capture_output=True,
                             text=True, timeout=60)
        if res.returncode == 0:
            return res.stdout.strip().split("\n")[0]
    except (OSError, subprocess.TimeoutExpired):
        pass
    return ""


def last_validated_version() -> str:
    history = os.path.join(state_dir(), "reports", "nightly_history.jsonl")
    try:
        with open(history) as fh:
            lines = [ln for ln in fh.read().splitlines() if ln.strip()]
        return json.loads(lines[-1]).get("version") or ""
    except Exception:
        return ""


def run_status() -> int:
    problems = 0
    settings = load_settings()
    if settings is None:
        return 1

    print(f"autocompactor status  ({HERE})\n")

    counts = hooks_registered(settings)
    expected = {"UserPromptSubmit": 1, "PreCompact": 2}
    for event, n in counts.items():
        ok = n == expected[event]
        problems += not ok
        print(f"  hooks {event}: {n}/{expected[event]} registered"
              + ("" if ok else "  <-- run: python3 install.py"))

    env = settings.get("env", {})
    missing = [k for k in ENV_DEFAULTS if k not in env]
    tuned = [f"{k}={env[k]}" for k, v in ENV_DEFAULTS.items()
             if k in env and env[k] != v]
    print(f"  env: {len(ENV_DEFAULTS) - len(missing)}/{len(ENV_DEFAULTS)} "
          f"keys present"
          + (f", missing: {', '.join(missing)}" if missing else ""))
    if tuned:
        print(f"  env tuned away from defaults: {', '.join(tuned)}")
    problems += bool(missing)

    print(f"  cron: {'registered' if cron_registered() else 'NOT registered'}"
          f"  (marker {CRON_MARKER!r})")

    sdir = state_dir()
    writable = os.path.isdir(sdir) and os.access(sdir, os.W_OK)
    print(f"  state dir {sdir}: "
          + ("writable" if writable else "MISSING or not writable"))

    reports = sorted(glob.glob(os.path.join(sdir, "reports", "nightly-*.md")))
    if reports:
        import time
        age_h = (time.time() - os.path.getmtime(reports[-1])) / 3600
        flag = "" if age_h < 48 else "  <-- stale; check cron/nightly.log"
        print(f"  newest nightly report: {os.path.basename(reports[-1])} "
              f"({age_h:.0f}h old){flag}")
    else:
        print("  newest nightly report: none yet")

    current, validated = claude_version(), last_validated_version()
    if current:
        drift = (current != validated) if validated else False
        print(f"  claude CLI: {current}"
              + (f"  (last validated by nightly: {validated})"
                 if validated else "  (no nightly validation yet)")
              + ("  <-- version changed; re-run --verify" if drift else ""))
    else:
        print("  claude CLI: not found on PATH")

    print("\n" + ("STATUS: OK" if not problems
                  else f"STATUS: {problems} problem(s)"))
    return 1 if problems else 0


# ----------------------------------------------------------------- main

def main() -> int:
    args = set(sys.argv[1:])
    unknown = args - {"--remove", "--force-env", "--cron",
                      "--verify", "--status"}
    if unknown:
        print(f"unknown flag(s): {', '.join(sorted(unknown))}")
        print(__doc__.strip())
        return 2

    if "--verify" in args:
        return run_verify()
    if "--status" in args:
        return run_status()

    remove = "--remove" in args
    settings = load_settings()
    if settings is None:
        return 1

    apply_hooks(settings, remove)
    if remove:
        removed = remove_env(settings)
        write_settings(settings)
        print(f"removed autocompactor hooks from {settings_path()}")
        if removed:
            print(f"removed env keys: {', '.join(sorted(removed))}")
        for native_key in ("CLAUDE_CODE_AUTO_COMPACT_WINDOW",
                           "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"):
            if native_key in settings.get("env", {}):
                print(f"left {native_key} in place "
                      "(native Claude Code setting; remove by hand if unwanted)")
        print(apply_cron(remove=True))
        return 0

    added, kept = apply_env(settings, force="--force-env" in args)
    write_settings(settings)
    os.makedirs(state_dir(), exist_ok=True)

    print(f"installed autocompactor hooks into {settings_path()}")
    if added:
        print(f"env keys set: {', '.join(sorted(added))}")
    if kept:
        print("env keys kept (tuned; --force-env to reset): "
              + ", ".join(sorted(kept)))
    if "--cron" in args:
        print(apply_cron(remove=False))
    print("\nRun /hooks inside Claude Code to verify registration; "
          "python3 install.py --status for a health check.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
