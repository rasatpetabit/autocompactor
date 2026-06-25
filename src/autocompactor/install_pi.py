#!/usr/bin/env python3
"""Idempotent installer for the autocompactor Pi-harness extension.

Usage:
    python3 src/install_pi.py            # install/update the Pi extension
    python3 src/install_pi.py --remove   # uninstall the extension + version pin
    python3 src/install_pi.py --status   # report install health

Idempotent throughout: re-runs overwrite only our own installed file and leave
everything else in ~/.pi untouched.
"""
import datetime
import json
import os
import subprocess
import sys

from autocompactor import statedir  # noqa: E402

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # src/
SHIM_SRC = os.path.join(HERE, "pi", "autocompactor.ts")
BRIDGE = os.path.join(HERE, "pi_bridge.py")
PLACEHOLDER = "__AUTOCOMPACTOR_BRIDGE_PATH__"
PI_PACKAGE = "@earendil-works/pi-coding-agent"


# ------------- helpers


def extensions_dir() -> str:
    return os.path.expanduser("~/.pi/agent/extensions")


def installed_path() -> str:
    return os.path.join(extensions_dir(), "autocompactor.ts")


def pin_path() -> str:
    return os.path.join(statedir.state_root("pi"), "pi-version-pin.json")


def pi_package_version() -> str:
    candidates = []
    try:
        result = subprocess.run(
            ["npm", "root", "-g"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode == 0:
            root = result.stdout.strip()
            path = os.path.join(
                root, "@earendil-works", "pi-coding-agent", "package.json"
            )
            candidates.append(path)
    except (OSError, subprocess.TimeoutExpired):
        pass

    candidates.append(
        os.path.expanduser(
            "~/.npm-global/lib/node_modules/@earendil-works/pi-coding-agent/package.json"
        )
    )

    for path in candidates:
        try:
            with open(path, "r") as f:
                data = json.load(f)
            return data.get("version", "")
        except (OSError, json.JSONDecodeError):
            continue

    return ""


def render_shim() -> str:
    with open(SHIM_SRC, "r") as f:
        text = f.read()
    if PLACEHOLDER not in text:
        print("WARNING: placeholder not found in shim source (already rewritten?)")
    return text.replace(PLACEHOLDER, BRIDGE)


def write_pin(version: str) -> None:
    try:
        os.makedirs(os.path.dirname(pin_path()), exist_ok=True)
        with open(pin_path(), "w") as f:
            json.dump(
                {
                    "package": PI_PACKAGE,
                    "version": version,
                    "pinned_at": datetime.datetime.now().isoformat(timespec="seconds"),
                    "shim": installed_path(),
                },
                f,
            )
    except Exception as exc:
        print(f"WARNING: failed to write version pin: {exc}")


def read_pin() -> dict:
    try:
        with open(pin_path(), "r") as f:
            return json.load(f)
    except Exception:
        return {}


# ------------- install


def do_install() -> int:
    if not os.path.exists(SHIM_SRC):
        print(f"ERROR: shim source not found: {SHIM_SRC}")
        return 1
    if not os.path.exists(BRIDGE):
        print(f"ERROR: bridge not found: {BRIDGE}")
        return 1

    os.makedirs(extensions_dir(), exist_ok=True)

    shim_text = render_shim()
    tmp = installed_path() + ".tmp"
    # Plain copy/symlink is wrong: the placeholder must be rewritten, so symlinks
    # are deliberately not used.
    with open(tmp, "w") as f:
        f.write(shim_text)
    os.replace(tmp, installed_path())

    version = pi_package_version()
    if version:
        print(f"pinned {PI_PACKAGE} version: {version}")
        write_pin(version)
    else:
        print("WARNING: could not determine installed Pi package version (is Pi installed?)")
        write_pin("")

    print(f"installed Pi extension -> {installed_path()}")
    print(f"bridge path baked in: {BRIDGE}")
    print("Run python3 src/install_pi.py --status for a health check; restart Pi to load the extension.")
    return 0


# ------------- remove


def do_remove() -> int:
    removed = []
    try:
        if os.path.exists(installed_path()):
            os.remove(installed_path())
            removed.append(installed_path())
    except OSError as exc:
        print(f"WARNING: failed to remove shim: {exc}")
        return 1

    try:
        if os.path.exists(pin_path()):
            os.remove(pin_path())
            removed.append(pin_path())
    except OSError as exc:
        print(f"WARNING: failed to remove pin: {exc}")

    if removed:
        print("removed: " + ", ".join(removed))
    else:
        print("nothing to remove")
    return 0


# ------------- status


def do_status() -> int:
    problems = 0

    ip = installed_path()
    if os.path.exists(ip):
        print(f"shim: {ip} (exists)")
        with open(ip, "r") as f:
            text = f.read()
        if PLACEHOLDER in text:
            print("  <-- placeholder unrewritten; re-run python3 src/install_pi.py")
            problems += 1
        if BRIDGE not in text:
            print("  <-- bridge path points elsewhere (other checkout?); re-run install to repoint")
            problems += 1
    else:
        print(f"shim: {ip} (missing)")
        problems += 1

    if os.path.exists(BRIDGE) and os.access(BRIDGE, os.R_OK):
        print(f"bridge: {BRIDGE} (ok)")
    else:
        print(f"bridge: {BRIDGE} (missing or unreadable)")
        problems += 1

    pin = read_pin()
    current = pi_package_version()
    pinned_ver = pin.get("version", "")
    print(f"pin: {pinned_ver} (pinned) vs {current} (current)")
    if pinned_ver and current and pinned_ver != current:
        print("  <-- Pi package version changed since install; re-run tests then reinstall")
        problems += 1

    state_root = statedir.state_root("pi")
    try:
        os.makedirs(state_root, exist_ok=True)
        if os.path.isdir(state_root) and os.access(state_root, os.W_OK):
            print(f"state dir: {state_root} (writable)")
        else:
            print(f"state dir: {state_root} (not writable)")
            problems += 1
    except OSError as exc:
        print(f"state dir: {state_root} (error: {exc})")
        problems += 1

    # Floor-probe freshness (readout-only artifact; written by nightly_eval's
    # floor-probe subsystem). Read-only consumer of floor-probe.json; does NOT
    # touch statedir.py or write anything. Reports fresh/stale/missing against
    # the probe-staleness budget stored in the artifact. Informational only:
    # never flips the --status exit code.
    probe_line = _floor_probe_status_line(state_root)
    print(probe_line)

    if problems:
        print(f"STATUS: {problems} problem(s)")
        return 1
    print("STATUS: OK")
    return 0


def _floor_probe_status_line(state_root):
    """Read-only floor-probe freshness line for the --status health block.

    Reads floor-probe.json (the frozen T9 artifact) under the pi state root and
    compares measured_at against the recorded staleness_budget. Reports
    fresh/stale/missing. Never raises; a missing/parse error yields a
    'missing' line (informational, not a problem that flips --status exit).

    Reads the artifact at os.path.join(state_root, 'floor-probe.json') so the
   AUTOCOMPACTOR_STATE_DIR override is honored at call time, not import time."""
    try:
        import json as _json
        import datetime as _dt
        from autocompactor import config_lib as _cfg
        probe_path = os.path.join(state_root, "floor-probe.json")
        with open(probe_path) as _fh:
            data = _json.load(_fh)
        measured = str(data.get("measured_at", "") or "")
        budget = int(data.get("staleness_budget") or
                     _cfg.cfg.float("PROBE_STALENESS_SECONDS",
                                     default=14 * 86400))
        age = _probe_age_seconds(measured)
        state = "fresh" if (age is None or age <= budget) else "stale"
        return f"floor probe: {state} (measured {measured})"
    except Exception:
        return "floor probe: (missing - run nightly_eval to populate)"


def _probe_age_seconds(measured_at):
    if not measured_at:
        return None
    try:
        ts = datetime.datetime.fromisoformat(measured_at.replace("Z", "+00:00"))
        return max(int((datetime.datetime.now(datetime.timezone.utc) - ts)
                       .total_seconds()), 0)
    except Exception:
        return None


# ------------- main


def main() -> int:
    flags = set(sys.argv[1:])
    known = {"--remove", "--status"}
    unknown = flags - known
    if unknown:
        print(f"unknown flag(s): {', '.join(sorted(unknown))}")
        print(__doc__.strip())
        return 2

    if "--status" in flags:
        return do_status()
    if "--remove" in flags:
        return do_remove()
    return do_install()


if __name__ == "__main__":
    sys.exit(main())
