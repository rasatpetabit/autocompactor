#!/usr/bin/env python3
"""Entry shim for the Pi extension bridge (never-raise JSON CLI).

Puts ``src/`` on ``sys.path`` and runs ``autocompactor.pi_bridge.main()``.
The installed Pi extension shells out here: ``python3 src/pi_bridge.py ...``.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from autocompactor.pi_bridge import main  # noqa: E402

if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except Exception:
        pass
    sys.exit(0)
