"""autocompactor — earlier, instruction-tailored compaction for coding agents.

Boundary-aware timing for when to compact, phase-aware structured instructions
for how to summarize, mechanical artifact extraction for content that should
not be entrusted to a summarizer, local telemetry, and an offline backtester.

This package is import-rooted at ``src/``: the thin entrypoint shims in
``src/*.py`` add ``src/`` to ``sys.path`` and call the matching ``main()``.
Claude Code hooks, the Pi extension, and the nightly cron all invoke those
shims directly (e.g. ``python3 src/context_monitor.py``).
"""

__version__ = "1.0.0"
