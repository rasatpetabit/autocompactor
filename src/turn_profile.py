#!/usr/bin/env python3
"""Entry shim for the turn profiler diagnostic.

Puts ``src/`` on ``sys.path`` and runs ``autocompactor.turn_profile.main()``.
Run manually: ``python3 src/turn_profile.py --session <path> [--json] [--rollup]``.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from autocompactor.turn_profile import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
