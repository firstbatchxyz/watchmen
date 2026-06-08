"""The Claude Code and Codex plugin manifests must declare the same version.

`/plugin` only refreshes its cache when the version changes, so a drift where
one harness bumps and the other doesn't would silently strand Codex (or Claude)
users on a stale plugin. CI also guards "payload changed → version bumped"
against the base branch; this is the cheap in-tree check that the two manifests
stay in lockstep.
"""

from __future__ import annotations

import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CLAUDE_MANIFEST = REPO / "plugin" / ".claude-plugin" / "plugin.json"
CODEX_MANIFEST = REPO / "plugin-codex" / ".codex-plugin" / "plugin.json"


def _version(path: Path) -> str:
    return json.loads(path.read_text())["version"]


def test_plugin_manifests_share_one_version():
    claude = _version(CLAUDE_MANIFEST)
    codex = _version(CODEX_MANIFEST)
    assert claude == codex, (
        f"plugin versions drifted: Claude {claude} vs Codex {codex}. "
        "Bump both together so /plugin refreshes the cache on every harness."
    )


def test_plugin_version_is_semver_ish():
    parts = _version(CLAUDE_MANIFEST).split(".")
    assert len(parts) == 3 and all(p.isdigit() for p in parts), \
        "plugin version should be MAJOR.MINOR.PATCH"
