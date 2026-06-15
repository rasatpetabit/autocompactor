#!/usr/bin/env python3
"""Entry shim for the offline backtester / events aggregator.

Puts ``src/`` on ``sys.path`` and runs ``autocompactor.analyze_corpus.main()``.
Run as: ``python3 src/analyze_corpus.py --root <dir> [--events]``.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from autocompactor.analyze_corpus import main  # noqa: E402

if __name__ == "__main__":
    main()
