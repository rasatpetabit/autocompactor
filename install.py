#!/usr/bin/env python3
"""
install.py — register autocompactor hooks in ~/.claude/settings.json.

Idempotent: safe to re-run; replaces any previous autocompactor entries
(matched by 'autocompactor' in the command string) and leaves all other
hooks untouched. Prints the resulting hook stanza. Run from the
autocompactor directory on the target machine:

    python3 install.py            # install/update
    python3 install.py --remove   # uninstall
"""

import json
import os
import sys

SETTINGS = os.path.expanduser("~/.claude/settings.json")
HERE = os.path.dirname(os.path.abspath(__file__))

HOOKS = {
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


def main() -> int:
    remove = "--remove" in sys.argv
    try:
        with open(SETTINGS) as fh:
            settings = json.load(fh)
    except FileNotFoundError:
        settings = {}
    except json.JSONDecodeError as exc:
        print(f"refusing to touch malformed {SETTINGS}: {exc}")
        return 1

    hooks = settings.setdefault("hooks", {})
    for event in ("UserPromptSubmit", "PreCompact"):
        kept = [g for g in hooks.get(event, []) if not _is_ours(g)]
        if not remove:
            kept += HOOKS[event]
        if kept:
            hooks[event] = kept
        else:
            hooks.pop(event, None)

    os.makedirs(os.path.dirname(SETTINGS), exist_ok=True)
    tmp = SETTINGS + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(settings, fh, indent=2)
    os.replace(tmp, SETTINGS)

    print(("removed autocompactor hooks from "
           if remove else "installed autocompactor hooks into ") + SETTINGS)
    print(json.dumps({k: v for k, v in hooks.items()
                      if k in ("UserPromptSubmit", "PreCompact")}, indent=2))
    print("\nRun /hooks inside Claude Code to verify registration.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
