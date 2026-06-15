#!/usr/bin/env python3
"""Entry shim for the Claude Code installer.

Puts ``src/`` on ``sys.path`` and runs ``autocompactor.install.main()``.
Run as: ``python3 src/install.py [--status | --verify | --cron | --remove | --force-env]``.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from autocompactor.install import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
