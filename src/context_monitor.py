#!/usr/bin/env python3
"""Entry shim for the Claude UserPromptSubmit hook.

Puts ``src/`` on ``sys.path`` and runs ``autocompactor.context_monitor.main()``.
Hooks invoke this file directly: ``python3 src/context_monitor.py``.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from autocompactor.context_monitor import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
