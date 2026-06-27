#!/usr/bin/env python3
"""
md_inventory.py — fleet-wide size & structure inventory of AI-parsed markdown.

Companion to ``context_inventory``: where that tool accounts for what occupies
the *live* context window of one session, this one accounts for the
*artifacts* that get parsed into agent context across a whole tree of repos —
the AGENTS.md / CLAUDE.md / SKILL.md files that every agent harness loads
nearly verbatim. It answers "which instruction files are the biggest token tax,
where is the bulk concentrated (frontmatter vs body vs code blocks vs a single
giant section), and where is duplicated content" so cuts land on the files that
actually cost.

Architecture (mirrors context_inventory.py):
  * **model / build / render** split — ``build_md_inventory`` returns a plain
    ``MdInventory`` dataclass; ``render_table`` / ``render_json`` /
    ``render_sections`` consume it; ``main`` wires CLI to build+render.
  * **never-raise build** — any failure returns a degraded ``MdInventory``
    with a ``note`` (visible, not silent). Pure read-only filesystem scan.
  * **token heuristic** — reuses ``transcript_lib.CHARS_PER_TOKEN`` (chars/4)
    so estimates are consistent with the rest of the codebase. It is a
    heuristic; real tokenizers vary, but chars/4 is stable and comparable
    across files which is the point.
  * **self-contained** — depends only on the stdlib + ``CHARS_PER_TOKEN``.
    No transcript/session state, no probe artifacts, no network.

Module boundary: md_inventory does NOT import context_inventory or
transcript_lib's context_composition (different concern, no shared mutable
state). It imports only the ``CHARS_PER_TOKEN`` constant.
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field

from autocompactor import transcript_lib

CHARS_PER_TOKEN = transcript_lib.CHARS_PER_TOKEN  # shared chars/4 heuristic

# The filenames every major agent harness parses into context. These are the
# only files this tool scans by default; extend with --names.
DEFAULT_NAMES = ("AGENTS.md", "CLAUDE.md", "SKILL.md")

# Pruned during the walk: vendored deps, git-internal, and linked worktrees.
# Keeps the report on hand-authored instruction files, not noise.
DEFAULT_EXCLUDES = ("node_modules", ".git", ".worktrees", ".venv", "venv", "__pycache__")


# ---------------------------------------------------------------------------
# Dataclasses (the stable API contract)
# ---------------------------------------------------------------------------

@dataclass
class MdSection:
    """One ``##``-headed section of a markdown file."""
    heading: str = ""      # the leading ``## ...`` line, trimmed
    chars: int = 0
    tokens: int = 0        # chars // CHARS_PER_TOKEN

@dataclass
class MdFile:
    """One scanned AI-parsed markdown file."""
    path: str = ""         # absolute path as found
    kind: str = ""         # AGENTS | CLAUDE | SKILL | OTHER (basename minus .md)
    bytes: int = 0
    chars: int = 0
    lines: int = 0
    words: int = 0
    tokens: int = 0        # whole-file chars // CHARS_PER_TOKEN
    fm_tokens: int = 0     # YAML frontmatter chars // CHARS_PER_TOKEN
    fm_description: str = ""  # frontmatter ``description:`` field (always-loaded discovery line)
    sections: list = field(default_factory=list)  # [MdSection] split on ``## ``
    code_tokens: int = 0   # chars inside ``` fences // CHARS_PER_TOKEN

@dataclass
class MdInventory:
    """Aggregate result of a scan."""
    root: str = ""
    names: tuple = ()
    excludes: tuple = ()
    files: list = field(default_factory=list)        # [MdFile] unsorted, as discovered
    by_kind: dict = field(default_factory=dict)      # {kind: {"count": n, "bytes": n, "tokens": n}}
    total_bytes: int = 0
    total_tokens: int = 0
    total_fm_tokens: int = 0                         # sum of always-loaded frontmatter
    degraded: bool = False
    note: str = ""


# ---------------------------------------------------------------------------
# Public builder
# ---------------------------------------------------------------------------

def build_md_inventory(root: str = ".", *, names=DEFAULT_NAMES,
                       excludes=DEFAULT_EXCLUDES, include_vendored: bool = False,
                       min_bytes: int = 0) -> MdInventory:
    """Walk *root* for AI-parsed markdown and return an ``MdInventory``.

    Never raises — on any failure returns a degraded inventory with a ``note``.
    Symlinks are de-duplicated by realpath (a skill installed as a symlink to
    another copy is counted once). *excludes* are matched as directory-name
    segments anywhere in the path; pass ``include_vendored=True`` to keep
    ``node_modules``/``.venv`` (e.g. vendored skills inside deps).
    """
    try:
        root_abs = os.path.abspath(root)
        eff_excludes = () if include_vendored else tuple(excludes)
        seen_real = set()
        files: list = []
        for dirpath, dirnames, filenames in os.walk(root_abs, followlinks=True):
            # prune excluded dirs in-place so os.walk doesn't descend into them
            if eff_excludes:
                dirnames[:] = [d for d in dirnames if d not in eff_excludes]
            for fn in filenames:
                if fn not in names:
                    continue
                full = os.path.join(dirpath, fn)
                try:
                    real = os.path.realpath(full)
                except OSError:
                    real = full
                if real in seen_real:
                    continue
                seen_real.add(real)
                mf = _analyze_file(full, fn)
                if mf is None or mf.bytes < min_bytes:
                    continue
                files.append(mf)
        return _aggregate(root_abs, names, eff_excludes, files)
    except Exception as exc:  # never-raise, but VISIBLE
        return MdInventory(
            root=root, names=tuple(names), excludes=tuple(excludes),
            degraded=True, note=f"md_inventory build failed: {exc}",
        )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _kind_for(filename: str) -> str:
    stem = filename[:-3] if filename.endswith(".md") else filename
    if stem in ("AGENTS", "CLAUDE", "SKILL"):
        return stem
    return stem.upper() or "OTHER"

def _analyze_file(path: str, filename: str):
    """Read one file and build an ``MdFile``. Returns None if unreadable."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return None
    size = os.path.getsize(path) if os.path.exists(path) else len(text.encode("utf-8", "replace"))
    chars = len(text)
    lines = text.count("\n") + (1 if text and not text.endswith("\n") else 0)
    words = len(text.split())

    fm_tokens, fm_desc = _frontmatter(text)
    sections = _sections(text)
    code_tokens = _code_fence_tokens(text)

    return MdFile(
        path=path,
        kind=_kind_for(filename),
        bytes=size,
        chars=chars,
        lines=lines,
        words=words,
        tokens=chars // CHARS_PER_TOKEN,
        fm_tokens=fm_tokens,
        fm_description=fm_desc,
        sections=sections,
        code_tokens=code_tokens,
    )

def _frontmatter(text: str):
    """Extract (fm_tokens, description) from a leading YAML ``---`` block.

    fm_tokens counts only the YAML body (not the fences). description is the
    multi-line value of the ``description:`` key, collapsed to one line.
    Returns (0, "") when no frontmatter is present (AGENTS.md/CLAUDE.md never
    carry one).
    """
    if not text.startswith("---\n"):
        return 0, ""
    end = text.find("\n---\n", 4)
    if end < 0:
        return 0, ""
    body = text[4:end]
    fm_tokens = len(body) // CHARS_PER_TOKEN
    desc = ""
    # match "description:" at line start; value may span following indented lines
    lines = body.split("\n")
    for idx, ln in enumerate(lines):
        if ln.startswith("description:"):
            val = ln.split(":", 1)[1].strip()
            j = idx + 1
            while j < len(lines) and lines[j].startswith(("  ", "\t")) and ":" not in lines[j]:
                val += " " + lines[j].strip()
                j += 1
            desc = " ".join(val.split())
            break
    return fm_tokens, desc

def _sections(text: str):
    """Split on ``## `` headings; each section carries its heading + char/token count.

    The pre-heading preamble (frontmatter + any H1/intro before the first H2)
    becomes a leading section headed by its first non-empty line, so no chars
    are dropped from the total.
    """
    parts = _split_keep_headings(text, "## ")
    out = []
    for chunk in parts:
        first = chunk.split("\n", 1)[0].strip()
        if not first:
            continue
        out.append(MdSection(
            heading=first[:72],
            chars=len(chunk),
            tokens=len(chunk) // CHARS_PER_TOKEN,
        ))
    return out

def _split_keep_headings(text: str, prefix: str):
    """Split text into chunks each beginning with a ``prefix`` heading line.

    The slice before the first heading (if any) is its own leading chunk.
    """
    chunks = []
    buf = []
    for ln in text.split("\n"):
        if ln.startswith(prefix):
            if buf:
                chunks.append("\n".join(buf))
                buf = []
        buf.append(ln)
    if buf:
        chunks.append("\n".join(buf))
    return chunks

def _code_fence_tokens(text: str) -> int:
    """Sum of chars inside ``` fences, // CHARS_PER_TOKEN. 0 if none."""
    total = 0
    depth = 0
    for ln in text.split("\n"):
        if ln.lstrip().startswith("```"):
            depth ^= 1
            continue
        if depth:
            total += len(ln) + 1
    return total // CHARS_PER_TOKEN

def _aggregate(root, names, excludes, files):
    """Sort, tally by-kind, and produce the final ``MdInventory``."""
    files.sort(key=lambda f: f.bytes, reverse=True)
    by_kind = {}
    for f in files:
        k = by_kind.setdefault(f.kind, {"count": 0, "bytes": 0, "tokens": 0})
        k["count"] += 1
        k["bytes"] += f.bytes
        k["tokens"] += f.tokens
    return MdInventory(
        root=root,
        names=tuple(names),
        excludes=tuple(excludes),
        files=files,
        by_kind=by_kind,
        total_bytes=sum(f.bytes for f in files),
        total_tokens=sum(f.tokens for f in files),
        total_fm_tokens=sum(f.fm_tokens for f in files),
    )


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _k(n: int) -> str:
    """Compact integer sizing: 1234 -> '1234', 15091 -> '15091' (no unit).

    Kept unit-less for a sortable, copy-pasteable table; a separate column
    carries the 'tok' label. Mirrors the compact-integer style of
    context_inventory._k without the 'k' suffix so raw values stay exact.
    """
    return str(int(n))

def _bar(value: int, total: int, width: int = 24) -> str:
    """Proportional bar, max width *width*."""
    if total <= 0:
        return "░" * width
    frac = min(max(value / total, 0.0), 1.0)
    filled = round(frac * width)
    return "█" * filled + "░" * (width - filled)

def render_table(inv: MdInventory, *, max_rows: int = 0,
                 sort: str = "bytes") -> str:
    """Aligned fixed-width ranking table, largest first.

    sort: bytes | tok | sections | path. max_rows=0 means all rows.
    Columns: rank, bytes, ~tok, fmTok, sections, codeTok, kind, path.
    """
    L = []
    files = _sorted(inv.files, sort)
    if max_rows:
        files = files[:max_rows]
    if inv.degraded:
        L.append(f"(degraded: {inv.note})")
    L.append(f"AI-parsed markdown inventory — {inv.root}")
    L.append(f"files: {len(inv.files)}  total: {inv.total_bytes}B "
             f"~{inv.total_tokens}tok  (frontmatter always-loaded: "
             f"~{inv.total_fm_tokens}tok across "
             f"{sum(1 for f in inv.files if f.fm_tokens)} files)")
    if inv.by_kind:
        kinds = ", ".join(
            f"{k}:{d['count']}/{d['tokens']}t" if False else
            f"{k}:{d['count']}f/{d['tokens']}t"
            for k, d in sorted(inv.by_kind.items(), key=lambda kv: -kv[1]["tokens"])
        )
        L.append(f"by kind: {kinds}")
    L.append("")
    hdr = (f"{'#':>3}  {'bytes':>7}  {'~tok':>6}  {'fmTok':>5}  "
           f"{'sects':>5}  {'codeTok':>7}  {'kind':<7} path")
    L.append(hdr)
    L.append("-" * len(hdr))
    for i, f in enumerate(files, 1):
        L.append(
            f"{i:>3}  {_k(f.bytes):>7}  {_k(f.tokens):>6}  "
            f"{_k(f.fm_tokens):>5}  {len(f.sections):>5}  "
            f"{_k(f.code_tokens):>7}  {f.kind:<7} {_relpath(inv.root, f.path)}"
        )
    return "\n".join(L)

def render_sections(inv: MdInventory, top_n: int = 20, *, sort: str = "bytes") -> str:
    """Deep per-section breakdown for the top *top_n* files by *sort*.

    Each file: header line (rank/bytes/tok/sections) then one indented line
    per ``##`` section with its token/char weight. Reveals concentration
    (one giant section) without dumping whole-file bodies.
    """
    L = []
    files = _sorted(inv.files, sort)[:top_n]
    if inv.degraded:
        L.append(f"(degraded: {inv.note})")
    for i, f in enumerate(files, 1):
        L.append(f"#{i}  {f.bytes}B ~{f.tokens}tok  fm={f.fm_tokens}tok  "
                 f"sections={len(f.sections)}  code={f.code_tokens}tok  "
                 f"[{f.kind}] {_relpath(inv.root, f.path)}")
        # limit section dump when there are many (e.g. 50+ heading-heavy files)
        show = f.sections if len(f.sections) <= 25 else f.sections[:25]
        for s in show:
            L.append(f"      {_k(s.tokens):>5}tok {s.chars:>6}ch  {s.heading}")
        if len(f.sections) > 25:
            L.append(f"      ... ({len(f.sections) - 25} more sections)")
        L.append("")
    return "\n".join(L)

def render_json(inv: MdInventory, *, sort: str = "bytes") -> str:
    """JSON serialization of the full inventory (machine-readable)."""
    files = _sorted(inv.files, sort)
    payload = {
        "root": inv.root,
        "names": list(inv.names),
        "excludes": list(inv.excludes),
        "degraded": inv.degraded,
        "note": inv.note,
        "total_bytes": inv.total_bytes,
        "total_tokens": inv.total_tokens,
        "total_fm_tokens": inv.total_fm_tokens,
        "by_kind": inv.by_kind,
        "files": [
            {
                "path": f.path, "kind": f.kind, "bytes": f.bytes,
                "chars": f.chars, "lines": f.lines, "words": f.words,
                "tokens": f.tokens, "fm_tokens": f.fm_tokens,
                "fm_description": f.fm_description, "code_tokens": f.code_tokens,
                "sections": [{"heading": s.heading, "chars": s.chars, "tokens": s.tokens}
                             for s in f.sections],
            }
            for f in files
        ],
    }
    return json.dumps(payload, indent=2)

def render_csv(inv: MdInventory, *, sort: str = "bytes") -> str:
    """CSV: one row per file (no section breakdown)."""
    import csv as _csv
    import io as _io
    buf = _io.StringIO()
    w = _csv.writer(buf)
    w.writerow(["rank", "kind", "path", "bytes", "tokens", "fm_tokens",
                "sections", "code_tokens", "fm_description"])
    for i, f in enumerate(_sorted(inv.files, sort), 1):
        w.writerow([i, f.kind, f.path, f.bytes, f.tokens, f.fm_tokens,
                    len(f.sections), f.code_tokens, f.fm_description])
    return buf.getvalue().rstrip("\n")

def _sorted(files, sort: str):
    key = {
        "bytes": lambda f: -f.bytes,
        "tok": lambda f: -f.tokens,
        "sections": lambda f: -len(f.sections),
        "path": lambda f: f.path,
    }.get(sort, lambda f: -f.bytes)
    return sorted(files, key=key)

def _relpath(root: str, path: str) -> str:
    try:
        r = os.path.relpath(path, root)
        return "." + os.sep + r if not os.path.isabs(r) else r
    except ValueError:
        return path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse(argv):
    args = {
        "root": ".",
        "names": list(DEFAULT_NAMES),
        "excludes": list(DEFAULT_EXCLUDES),
        "include_vendored": False,
        "format": "table",
        "sort": "bytes",
        "max_rows": 0,
        "min_bytes": 0,
        "sections": 0,
    }

    def _split_list(v):
        return [p.strip() for p in v.split(",") if p.strip()]

    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("-h", "--help"):
            _print_help()
            return None
        # support both --flag=value and --flag value
        def val(flag):
            nonlocal i
            if "=" in flag:
                return flag.split("=", 1)[1]
            i += 1
            return argv[i] if i < len(argv) else ""
        if a.startswith("--root"):
            args["root"] = val(a)
        elif a.startswith("--names"):
            args["names"] = _split_list(val(a))
        elif a.startswith("--exclude"):
            args["excludes"] = _split_list(val(a))
        elif a in ("--include-vendored", "--vendored"):
            args["include_vendored"] = True
        elif a.startswith("--format"):
            args["format"] = val(a)
        elif a.startswith("--sort"):
            args["sort"] = val(a)
        elif a.startswith("--max"):
            args["max_rows"] = int(val(a) or 0)
        elif a.startswith("--min-bytes"):
            args["min_bytes"] = int(val(a) or 0)
        elif a.startswith("--sections"):
            args["sections"] = int(val(a) or 0)
        else:
            # bare positional => treat as root if root still default
            if args["root"] == "." and not a.startswith("-"):
                args["root"] = a
            else:
                print(f"md_inventory: unknown option {a!r} "
                      f"(see --help)", file=sys.stderr)
        i += 1
    return args

def _print_help():
    print(
        "usage: md_inventory [root] [--names=a,b,c] [--exclude=a,b] "
        "[--include-vendored]\n"
        "                     [--format=table|json|csv|sections] "
        "[--sort=bytes|tok|sections|path]\n"
        "                     [--max=N] [--min-bytes=N] [--sections=N]\n"
        "\n"
        "Inventory AI-parsed markdown (AGENTS.md/CLAUDE.md/SKILL.md) under "
        "<root> (default: cwd).\n"
        "Reports size, token estimate (chars/4), frontmatter token cost, "
        "section count, and code-block\n"
        "token cost per file, largest first.\n"
        "\n"
        "  --names=a,b,c        filenames to scan (default: "
        "AGENTS.md,CLAUDE.md,SKILL.md)\n"
        "  --exclude=a,b        dir names to prune (default: "
        "node_modules,.git,.worktrees,.venv,venv,__pycache__)\n"
        "  --include-vendored   do NOT prune node_modules/.venv (scan vendored "
        "skills too)\n"
        "  --format=table       table (default). json / csv are "
        "machine-readable.\n"
        "  --format=sections    per-section breakdown of the top "
        "--sections=N files\n"
        "  --sort=bytes         bytes (default) | tok | sections | path\n"
        "  --max=N              cap rows (table) / files (sections)\n"
        "  --min-bytes=N        drop files smaller than N bytes\n"
        "  --sections=N         with --format=sections: how many top files "
        "to detail (default 20)\n"
        "\n"
        "Entry point: `mdinventory` (pyproject [project.scripts])."
    )

def main(argv=None) -> int:
    opts = _parse(list(sys.argv[1:] if argv is None else argv))
    if opts is None:
        return 0
    fmt = opts["format"]
    if fmt not in ("table", "json", "csv", "sections"):
        print(f"md_inventory: unknown --format={fmt!r} "
              f"(table|json|csv|sections)", file=sys.stderr)
        return 2
    if fmt == "sections":
        opts["sections"] = opts["sections"] or 20
    inv = build_md_inventory(
        opts["root"],
        names=tuple(opts["names"]),
        excludes=tuple(opts["excludes"]),
        include_vendored=opts["include_vendored"],
        min_bytes=opts["min_bytes"],
    )
    if fmt == "table":
        print(render_table(inv, max_rows=opts["max_rows"], sort=opts["sort"]))
    elif fmt == "json":
        print(render_json(inv, sort=opts["sort"]))
    elif fmt == "csv":
        print(render_csv(inv, sort=opts["sort"]))
    elif fmt == "sections":
        print(render_sections(inv, top_n=opts["sections"], sort=opts["sort"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
