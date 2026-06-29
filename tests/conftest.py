"""Shared test setup — runs once per pytest session.

Inserts `src/` onto sys.path so the test loop works even without a prior
`uv sync` or editable install. Exposes ROOT + SRC as module-level constants
so individual tests can read package files (plist/unit templates, README
hygiene) without re-deriving the path.

Also pins the active LLM provider to "openrouter" for the entire test
session. Without this, a developer who has run `watchmen settings provider
anthropic` (writes to ~/.config/watchmen/.env) would see tests fail —
config.active_provider() reads that file at call time, so any test that
builds an Agent without an explicit provider would pick up whatever the
developer last configured. Pinning here means tests behave the same in
CI, on a fresh checkout, and on a machine with multiple keys configured.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "watchmen"

# The editable install puts `watchmen` on sys.path via uv's .pth file. We
# still nudge `src/` into place so a fresh checkout `pytest tests/` works
# with zero ceremony — useful for CI and one-shot invocations.
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))


@pytest.fixture(autouse=True)
def _pin_provider_to_openrouter(monkeypatch):
    """Default test session to OpenRouter as the active provider so tests
    that build an Agent without explicit `provider=` get the expected
    headers / endpoint regardless of the developer's local .env.

    Tests that need a different provider should set their own
    `monkeypatch.setenv("WATCHMEN_PROVIDER", ...)` — the per-test monkeypatch
    scope wins over this autouse fixture for the duration of that test."""
    monkeypatch.setenv("WATCHMEN_PROVIDER", "openrouter")


@pytest.fixture(autouse=True)
def _isolate_oauth_credentials(monkeypatch):
    """Hide any OAuth credentials a developer has on disk so tests run the
    same on a fresh CI box and on a machine where the dev signed in to
    Claude Code or Codex for unrelated reasons.

    Without this, `active_provider()`'s auto-detect would pick up a real
    `Claude Code-credentials` keychain entry and the suite would diverge
    from CI in subtle ways — exactly the kind of "works on my machine"
    failure the conftest is here to prevent.

    Stubs the *lowest-level* discovery helpers (the platform check, the
    `security` subprocess call, the on-disk path lookup) rather than the
    `read()` classmethods themselves. That way tests that *do* want to
    exercise the OAuth parsing logic can override `_read_keychain_blob`
    or `Path.home` and still flow through the real `read()` code path."""
    try:
        from watchmen.credentials import claude_code as _cc
    except ImportError:
        return  # Pre-OAuth branch / installs without the credentials module
    monkeypatch.setattr(_cc, "is_claude_code_available", lambda: False)
    monkeypatch.setattr(_cc, "_read_keychain_blob", lambda: None)
    # The on-disk path lookup — the Linux/server fallback (and macOS fallback)
    # that reads `~/.claude/.credentials.json`. Without this stub, read() would
    # pick up a real credentials file on a dev box / CI runner that happens to
    # have one (this is exactly how the suite breaks on a machine where the dev
    # is signed in to Claude Code) and isolation would leak.
    monkeypatch.setattr(_cc, "_read_credentials_file", lambda: None)
    # For Codex, the read() helper resolves Path.home() each call. Tests
    # that need a clean slate should monkeypatch Path.home() to a tmp dir
    # — much easier than stubbing the read method outright. We don't add
    # a global Codex stub here because the path-resolution path is already
    # safe by construction (no .codex/auth.json under a temp HOME).
