"""
statedir.py — Namespace autocompactor state roots by harness.

Supported harnesses:
  'claude'  -> ~/.claude/autocompactor  (unchanged default)
  'pi'      -> ~/.autocompactor/pi

Env override: AUTOCOMPACTOR_STATE_DIR, when set, overrides ALL harness
resolution — every harness resolves to that value.  Downstream Pi
subsystems consume state_root() rather than hard-coding paths.
"""

import os


def state_root(harness: str = "claude") -> str:
    """Return the absolute state-root directory for *harness*.

    Resolution order:
    1. If AUTOCOMPACTOR_STATE_DIR is set in the environment, return it
       (override for all harnesses — useful in CI / tests).
    2. harness == 'claude'  ->  ~/.claude/autocompactor
    3. harness == 'pi'      ->  ~/.autocompactor/pi
    4. Unknown harness: raise ValueError.

    The returned path is NOT guaranteed to exist; callers that need the
    directory should create it (e.g. os.makedirs(path, exist_ok=True)).
    """
    override = os.environ.get("AUTOCOMPACTOR_STATE_DIR")
    if override:
        return override

    home = os.path.expanduser("~")

    if harness == "claude":
        return os.path.join(home, ".claude", "autocompactor")
    elif harness == "pi":
        return os.path.join(home, ".autocompactor", "pi")
    else:
        raise ValueError(
            f"Unknown harness {harness!r}. "
            "Expected one of: 'claude', 'pi'."
        )
