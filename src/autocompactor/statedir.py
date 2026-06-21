"""
statedir.py — autocompactor state root (single namespace).

State root:
  ~/.autocompactor/pi

Env override: AUTOCOMPACTOR_STATE_DIR, when set, overrides resolution.
Downstream Pi subsystems consume state_root() rather than hard-coding paths.
"""

import os


def state_root(*args, **kwargs) -> str:
    """Return the absolute state-root directory.

    Resolution order:
    1. If AUTOCOMPACTOR_STATE_DIR is set in the environment, return it
       (override — useful in CI / tests).
    2. ~/.autocompactor/pi

    Positional/keyword args (e.g. a legacy `harness`) are accepted but
    ignored, for call-site compatibility.

    The returned path is NOT guaranteed to exist; callers that need the
    directory should create it (e.g. os.makedirs(path, exist_ok=True)).
    """
    override = os.environ.get("AUTOCOMPACTOR_STATE_DIR")
    if override:
        return override
    return os.path.join(os.path.expanduser("~"), ".autocompactor", "pi")
