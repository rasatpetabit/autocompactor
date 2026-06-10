# ras/autocompactor

Automatic session compaction and cleanup tooling. See [CLAUDE.md](CLAUDE.md) for detailed agent configuration and operational guidance.

## RTK shell-default policy

- Default to RTK for shell commands. Use explicit `rtk` prefixes for noisy or token-heavy commands (`rtk git ...`, `rtk grep ...`, `rtk find ...`, `rtk cargo ...`, `rtk read ...`) instead of relying on Claude-only shell hooks.
- Use `rtk proxy <cmd>` for commands RTK cannot parse or should execute raw, including compound `find` predicates/actions, `node`/`python` orchestration scripts, and custom CLIs.
- Developer tools (`read`, `edit`, `write`) are not shell commands and remain appropriate for precise file operations; the RTK default applies whenever invoking Bash.
- RTK reference: `/home/grojas/.claude/RTK.md`.

