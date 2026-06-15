#!/usr/bin/env python3
"""Entry shim for the Claude PreCompact hook.

Puts ``src/`` on ``sys.path`` and runs ``autocompactor.precompact_analyzer.main()``.
Hooks invoke this file directly: ``python3 src/precompact_analyzer.py``.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from autocompactor.precompact_analyzer import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
