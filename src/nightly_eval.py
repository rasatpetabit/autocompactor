#!/usr/bin/env python3
"""Entry shim for the nightly self-evaluation cron job.

Puts ``src/`` on ``sys.path`` and runs ``autocompactor.nightly_eval.main()``.
Cron invokes this file directly: ``python3 src/nightly_eval.py``.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from autocompactor.nightly_eval import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
