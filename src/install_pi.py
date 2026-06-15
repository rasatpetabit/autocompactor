#!/usr/bin/env python3
"""Entry shim for the Pi extension installer.

Puts ``src/`` on ``sys.path`` and runs ``autocompactor.install_pi.main()``.
Run as: ``python3 src/install_pi.py [--status | --remove]``.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from autocompactor.install_pi import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
