#!/usr/bin/env python3
"""Entry shim for the standalone context-inventory report (spec §5/§11).

Puts ``src/`` on ``sys.path`` and runs ``autocompactor.context_inventory.main()``.
Run manually: ``python3 src/context_inventory.py --session <path> [--window=N]
[--total=N] [--no-probe]`` — prints a content-free report (token counts +
category/tool/package names only) over the active prefix of the session.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from autocompactor.context_inventory import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())