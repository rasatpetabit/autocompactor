"""Pytest bootstrap: put ``src/`` on ``sys.path`` so the ``autocompactor``
package is importable from every test module.

Test modules still keep their own ``sys.path.insert`` calls (harmless; they
target the checkout root for ``config.json`` lookups). This conftest guarantees
``import autocompactor`` resolves regardless of how pytest is launched.
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))        # tests/
_REPO_ROOT = os.path.dirname(_HERE)                        # checkout root
_SRC = os.path.join(_REPO_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)
