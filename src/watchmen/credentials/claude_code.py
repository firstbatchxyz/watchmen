"""Read Claude Code's stored OAuth credential.

Claude Code stores its OAuth state in the macOS keychain under service
`Claude Code-credentials`, account `Claude Code`. The payload is a JSON
blob with two top-level keys:

  {
    "claudeAiOauth": {
      "accessToken":     "...",
      "refreshToken":    "...",
      "expiresAt":       1779204894081,   // ms since epoch
      "scopes":          ["user:inference", ...],
      "subscriptionType": "team",
      "rateLimitTier":   "default_claude_max_5x"
    },
    "mcpOAuth": {...}
  }

With the `user:inference` scope and the `anthropic-beta: oauth-2025-04-20`
request header, this token can be used to call api.anthropic.com directly
— traffic is billed against the user's Claude subscription quota rather
than per-token API credits. This is the whole point of the integration.

On macOS the credential lives in the keychain (read via `security`). On
Linux (and any non-macOS host) Claude Code writes the same `claudeAiOauth`
payload to `~/.claude/.credentials.json` (mode 0600); we read that file as a
fallback so watchmen can run on a Claude subscription from a Linux box or
server, not just a Mac. macOS still prefers the keychain and falls back to
the file if the keychain has no entry. Windows libsecret/DPAPI stores remain
out of scope; when neither source yields a blob, `read()` returns None and
`is_claude_code_available()` returns False so the rest of the code degrades
gracefully.
"""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path


_KEYCHAIN_SERVICE = "Claude Code-credentials"


def _credentials_file() -> Path:
    """On-disk credential location used on non-macOS hosts (and as a macOS
    fallback). Resolved per call so tests can monkeypatch `Path.home()` /
    this helper to point at a fixture file."""
    return Path.home() / ".claude" / ".credentials.json"


@dataclass(frozen=True)
class ClaudeCodeCredentials:
    """In-memory view of the keychain payload's `claudeAiOauth` block.

    All fields are immutable; refresh callers create a new instance rather
    than mutating an existing one, so anything that holds a reference to
    the old credential keeps seeing the old token (matters when async
    runs are in flight during a refresh)."""

    access_token: str
    refresh_token: str
    # Milliseconds since epoch, matching the keychain blob's format. Kept
    # raw so we don't lose precision if the value comes back as float.
    expires_at_ms: int
    scopes: tuple[str, ...]
    subscription_type: str | None
    rate_limit_tier: str | None

    @classmethod
    def read(cls) -> "ClaudeCodeCredentials | None":
        """Pull the current credential from the keychain.

        Returns None if:
        - no credential source is available (no keychain entry on macOS,
          no `~/.claude/.credentials.json` on other hosts)
        - the blob exists but doesn't have a `claudeAiOauth` block
          (corrupt / legacy / unexpected schema — bail rather than guess)

        Never raises. Callers that need to distinguish "unavailable" from
        "available but expired" should chain a `.is_expired()` check.
        """
        raw = _read_blob()
        if raw is None:
            return None
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return None
        oauth = payload.get("claudeAiOauth") or {}
        access = oauth.get("accessToken")
        refresh = oauth.get("refreshToken")
        if not access or not refresh:
            return None
        return cls(
            access_token=access,
            refresh_token=refresh,
            expires_at_ms=int(oauth.get("expiresAt") or 0),
            scopes=tuple(oauth.get("scopes") or ()),
            subscription_type=oauth.get("subscriptionType"),
            rate_limit_tier=oauth.get("rateLimitTier"),
        )

    def is_expired(self, *, leeway_seconds: int = 60) -> bool:
        """True if the access token is past (or near) its expiry.

        Leeway: small grace so a request that would race the expiry boundary
        gets routed through a refresh path instead of failing mid-flight.
        Caller decides what to do when expired — for v0.7 we surface a
        clear "run `claude login` to refresh" error; auto-refresh is a
        follow-up."""
        if not self.expires_at_ms:
            return False  # Unknown expiry — trust the token until proven otherwise
        now_ms = int(time.time() * 1000)
        return self.expires_at_ms <= (now_ms + leeway_seconds * 1000)

    def has_inference_scope(self) -> bool:
        """The `user:inference` scope is what authorizes Anthropic API
        calls. A token without it (e.g. an older Claude Code version with
        a narrower scope set) would 401 on /v1/messages; we surface that
        early rather than during a curator run."""
        return "user:inference" in self.scopes


def is_claude_code_available() -> bool:
    """True iff a Claude Code credential blob is readable from any supported
    source — the macOS keychain or the `~/.claude/.credentials.json` file.

    Cheap to call — the keychain probe is one subprocess invocation that
    no-ops instantly on hosts without `security`, and the file check is a
    single read. Used by onboard / settings UI to decide whether to surface
    the "use your Claude subscription" option."""
    return _read_blob() is not None


def _read_blob() -> str | None:
    """Return the raw credential JSON from the best available source.

    Tries the macOS keychain first (a fast no-op on hosts without the
    `security` binary, e.g. Linux), then falls back to the on-disk
    `~/.claude/.credentials.json` file. Returns None when neither yields a
    blob. Keeping the keychain call unconditional (rather than gating on
    `sys.platform`) means a single code path serves macOS and Linux and lets
    tests stub either source independently."""
    blob = _read_keychain_blob()
    if blob is not None:
        return blob
    return _read_credentials_file()


def _read_credentials_file() -> str | None:
    """Read `~/.claude/.credentials.json`, the non-macOS credential store
    (and macOS fallback). Returns the file text, or None on any error
    (missing file, unreadable). Never raises and never logs the contents —
    the blob holds a live OAuth token."""
    try:
        return _credentials_file().read_text(encoding="utf-8") or None
    except OSError:
        return None


def _read_keychain_blob() -> str | None:
    """Run `security find-generic-password -s 'Claude Code-credentials' -w`.

    `-w` outputs only the password (the JSON blob). Returns None on any
    failure — missing entry, security exit non-zero, etc. We do NOT log
    the output; even partial credential leakage into our log files is
    something the user can't easily clean up."""
    try:
        r = subprocess.run(
            ["security", "find-generic-password", "-s", _KEYCHAIN_SERVICE, "-w"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    out = (r.stdout or "").strip()
    return out or None
