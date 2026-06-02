"""Project path normalization.

Claude Code encodes the working directory into a folder name by flattening
every path separator to "-":

  /Users/x/dev/kai-frontend            → -Users-x-dev-kai-frontend
  C:\\Users\\x\\dev\\kai-frontend      → C--Users-x-dev-kai-frontend

The replacement is lossy — a dir name that already contains "-" (like
`kai-frontend`) ends up indistinguishable from a path separator. We undo
it by walking the real filesystem and trying the longest possible joined
segment at each level.

Codex stores the real cwd verbatim in `session_meta.cwd`, so no decoding needed.

When the original directory no longer exists on disk (project deleted, moved,
etc.), we fall back to a naive decode (each "-" becomes a separator). That
loses ambiguity for dashed dir names, but it's stable — two transcripts from
the same vanished project still group together.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path


PROJECT_ROOT = Path(__file__).parent
WATCHMEN_HOME = Path(os.environ.get("WATCHMEN_HOME", Path.home() / ".watchmen")).expanduser()


def runtime_dir(*parts: str) -> Path:
    """Directory for user-owned watchmen runtime data.

    Source checkouts and installed wheels should stay immutable. Generated
    databases, analyses, and curated artifacts live under ~/.watchmen by
    default, with WATCHMEN_HOME available for tests or alternate installs.
    """
    path = WATCHMEN_HOME.joinpath(*parts)
    path.mkdir(parents=True, exist_ok=True)
    return path


def runtime_path(name: str, *, migrate_legacy: bool = True, legacy_alias: str | None = None) -> Path:
    """Path under WATCHMEN_HOME, optionally copied from the old source-root
    location on first use. Copying preserves existing local data without
    destructively moving files out of a checkout.

    `legacy_alias` covers in-place renames: if the new name doesn't exist on
    disk but the alias does (under WATCHMEN_HOME), move the alias to the new
    name. Used for the kai_claude → bundles rename in 0.5.

    If BOTH the alias and the new name already exist (an old daemon recreated
    the alias after the migration ran, or a stale checkout copy lingered),
    archive the alias to `<alias>.legacy/` so live data in `dest` is untouched
    and the user can manually merge or delete the legacy copy.
    """
    dest = WATCHMEN_HOME / name
    if migrate_legacy and not dest.exists():
        # 1. In-place rename inside WATCHMEN_HOME (kai_claude → bundles)
        if legacy_alias:
            alias_path = WATCHMEN_HOME / legacy_alias
            if alias_path.exists():
                try:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    alias_path.rename(dest)
                    return dest
                except OSError:
                    pass
        # 2. Copy from old source-checkout location into WATCHMEN_HOME
        legacy = PROJECT_ROOT / name
        try:
            if legacy.is_dir():
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(legacy, dest)
            elif legacy.is_file():
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(legacy, dest)
        except OSError:
            # Runtime paths should not make imports or command startup fail.
            pass
    elif migrate_legacy and legacy_alias and dest.exists():
        # 3. Both dest and alias coexist — one-shot archive of the stale alias.
        # The rename is self-deleting (alias disappears after one run) so this
        # path stays silent on every subsequent import.
        alias_path = WATCHMEN_HOME / legacy_alias
        archive_path = WATCHMEN_HOME / f"{legacy_alias}.legacy"
        if alias_path.exists() and not archive_path.exists():
            try:
                alias_path.rename(archive_path)
                import sys as _sys
                print(
                    f"watchmen: archived stale ~/.watchmen/{legacy_alias}/ "
                    f"→ ~/.watchmen/{legacy_alias}.legacy/ "
                    f"(canonical data lives in ~/.watchmen/{name}/)",
                    file=_sys.stderr,
                )
            except OSError:
                pass
    return dest


STATE_DB = runtime_path("state.db")
CORPUS_DB = runtime_path("corpus.db")
# Prompt-intent map cache (#111): local embedding vectors + 2D projections.
# Rebuildable from corpus.db at any time, so it lives beside it rather than in it.
INTENT_DB = runtime_path("intent.db")
EVENTS_DB = runtime_path("events.db")
EVENTS_JSONL = runtime_path("events.jsonl")
ANALYSES_DIR = runtime_path("analyses")
# Renamed from `kai_claude` → `bundles` in 0.5 (Kai attestation scrub).
# `legacy_alias` migrates existing installs by renaming the dir on first import.
BUNDLES_DIR = runtime_path("bundles", legacy_alias="kai_claude")
OUTPUT_DIR = runtime_path("output")
INSIGHTS_DIR = runtime_dir("insights")


def decode_project_dir(encoded: str) -> str:
    """Map a Claude Code encoded project dir back to a real cwd.

    The encoding family is detected from the prefix:
      "-..."        → POSIX, root is "/"
      "<Letter>--"  → Windows, drive letter followed by flattened separators

    For each family we first try a filesystem-grounded resolve (walk the real
    tree, preferring the longest dash-joined segment at each level) so dirs
    like `kai-frontend` survive the round-trip. If that fails (project moved
    or deleted) we fall back to a naive split that's lossy but stable.
    """
    if _looks_windows_encoded(encoded):
        return _decode_windows(encoded)
    if encoded.startswith("-"):
        return _decode_posix(encoded)
    return encoded  # already a real path


def _looks_windows_encoded(encoded: str) -> bool:
    return len(encoded) >= 3 and encoded[0].isalpha() and encoded[1:3] == "--"


def _decode_posix(encoded: str) -> str:
    naive = "/" + encoded.lstrip("-").replace("-", "/")
    resolved = _walk_dashes(Path("/"), list(Path(naive).parts[1:]))
    return str(resolved) if resolved else naive


def _decode_windows(encoded: str) -> str:
    drive = encoded[0].upper()
    tail = encoded[3:]
    root = Path(f"{drive}:\\")
    if root.exists():
        segments = tail.split("-") if tail else []
        resolved = _walk_dashes(root, segments)
        if resolved:
            return str(resolved)
    return f"{drive}:\\" + tail.replace("-", "\\")


def _walk_dashes(root: Path, segments: list[str]) -> Path | None:
    """Walk `root` matching the merged-by-dash `segments` against real dirs.

    Tries the longest possible dash-joined segment at each level so that
    `kai-frontend` (a real dir with a dash) is preferred over the split
    `kai`/`frontend` interpretation."""
    cur = root
    i = 0
    while i < len(segments):
        try:
            children = [p for p in cur.iterdir() if p.is_dir()] if cur.exists() else []
        except (OSError, PermissionError):
            return None
        best = None
        best_j = i
        for j in range(len(segments), i, -1):
            candidate = "-".join(segments[i:j])
            for ch in children:
                if ch.name == candidate:
                    best = ch
                    best_j = j
                    break
            if best:
                break
        if not best:
            return None
        cur = best
        i = best_j
    return cur if cur.exists() else None
