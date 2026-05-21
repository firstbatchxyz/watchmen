"""Shared runtime config — env-file IO, provider selection, viewer host/port.

`~/.config/watchmen/.env` is the single source of truth for cross-process
settings (provider API keys, viewer port, active provider). Process env vars
override the file.

Helpers here are deliberately simple — no schema, no validation beyond what
the caller does. Adding a real config schema is a P3 item.
"""

import os
from pathlib import Path


def _env_path() -> Path:
    """Resolved per-call so tests that monkeypatch `Path.home` see the
    correct path. The .env file IO is infrequent enough that the extra
    `Path.home()` call doesn't matter."""
    return Path.home() / ".config" / "watchmen" / ".env"


# Kept as a module-level alias for backward compat with any caller that
# imports `config.ENV_PATH`. Tests should use `_env_path()` to get the
# live path that follows Path.home monkeypatching.
ENV_PATH = _env_path()

# Active-provider env var. When set (in process env or the .env file), it
# overrides the auto-detect-from-which-key-is-present fallback below.
PROVIDER_ENV_VAR = "WATCHMEN_PROVIDER"

# Each env-var-based provider stores its key in its own env var so a
# machine can hold multiple keys simultaneously and switch active
# provider without re-pasting.
PROVIDER_KEY_VARS: dict[str, str] = {
    "openrouter": "OPENROUTER_API_KEY",
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
}

# OAuth providers don't take pasted keys — they discover credentials from
# Claude Code's keychain entry / Codex's auth.json. Kept here so
# `active_provider()` can detect them and `provider_key()` knows to
# delegate to the provider's `resolve_api_key()` hook instead of reading
# an env var.
OAUTH_PROVIDERS: tuple[str, ...] = ("claude-pro", "chatgpt")

# Combined set of all known provider names. Used to validate
# WATCHMEN_PROVIDER overrides — an unknown value silently falls back to
# auto-detect rather than crashing every command.
ALL_PROVIDERS: tuple[str, ...] = tuple(PROVIDER_KEY_VARS) + OAUTH_PROVIDERS

# Order matters for the auto-detect path in `active_provider()`:
# - Env-var-based providers come first because they represent explicit
#   user intent (they pasted a key).
# - OAuth providers come last because their presence is "ambient" (the
#   user installed Claude Code / Codex for unrelated reasons, not
#   necessarily to use them via watchmen).
_PROVIDER_PRIORITY = ("openrouter", "openai", "anthropic", "claude-pro", "chatgpt")

# Bumped 8888 → 8979 in 0.2: 8888 collides with Jupyter, which a lot of
# data-science users have permanently bound. 8979 is uncommon, mnemonic
# (8-9-7-9), and well outside the popular dev-tool port range.
VIEWER_DEFAULT_HOST = "127.0.0.1"
VIEWER_DEFAULT_PORT = 8979


def read_env_var(key: str, default: str | None = None) -> str | None:
    """Look up a config value: process env first, then ~/.config/watchmen/.env."""
    if v := os.environ.get(key):
        return v
    p = _env_path()
    if p.exists():
        for line in p.read_text().splitlines():
            if line.startswith(f"{key}="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return default


def write_env_var(key: str, value: str) -> Path:
    """Persist a `key=value` line to the global env file, replacing any prior line
    for the same key. Preserves unrelated lines. chmods to 0600 to keep secrets
    off other users' eyes. Returns the path written."""
    p = _env_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    lines = p.read_text().splitlines() if p.exists() else []
    new_lines = [ln for ln in lines if not ln.startswith(f"{key}=")]
    new_lines.append(f"{key}={value}")
    p.write_text("\n".join(new_lines) + "\n")
    p.chmod(0o600)
    return p


def clear_env_var(key: str) -> bool:
    """Remove a `key=...` line from the env file. Returns True if a line was
    removed, False if the key wasn't present. Used by the settings menu's
    "clear override" flow so callers can distinguish a no-op from a real
    rollback."""
    p = _env_path()
    if not p.exists():
        return False
    lines = p.read_text().splitlines()
    new_lines = [ln for ln in lines if not ln.startswith(f"{key}=")]
    if len(new_lines) == len(lines):
        return False
    p.write_text("\n".join(new_lines) + ("\n" if new_lines else ""))
    p.chmod(0o600)
    return True


def viewer_port() -> int:
    """Current viewer port — WATCHMEN_VIEWER_PORT env / config file / default."""
    raw = read_env_var("WATCHMEN_VIEWER_PORT")
    if raw:
        try:
            return int(raw)
        except ValueError:
            pass
    return VIEWER_DEFAULT_PORT


def viewer_base_url() -> str:
    """Canonical viewer base URL — used by curate.py + onboard.py + cli.doctor
    to construct deep links into the viewer that always reflect the user's
    currently-configured port."""
    return f"http://{VIEWER_DEFAULT_HOST}:{viewer_port()}"


# ─── Provider selection ────────────────────────────────────────────────────


def active_provider() -> str:
    """Return the currently active LLM provider name.

    Resolution order:
    1. `WATCHMEN_PROVIDER` env / `.env` — explicit selection, takes precedence.
       Both env-var-based providers (openrouter/openai/anthropic) and
       OAuth providers (claude-pro/chatgpt) are accepted here.
    2. First env-var-based provider with a configured key, in priority
       order (openrouter > openai > anthropic). Keeps existing
       OpenRouter-only installs working without re-running onboard.
    3. First OAuth provider with a credential available on disk, in
       priority order (claude-pro > chatgpt). "Ambient" auth — the user
       installed Claude Code / Codex for unrelated reasons, so OAuth
       lands here only when no explicit env-var key was set.
    4. "openrouter" as the absolute default.
    """
    explicit = read_env_var(PROVIDER_ENV_VAR)
    if explicit and explicit in ALL_PROVIDERS:
        return explicit
    # Env-var-based providers — explicit user intent
    for name in PROVIDER_KEY_VARS:
        if read_env_var(PROVIDER_KEY_VARS[name]):
            return name
    # OAuth providers — discovered credentials
    for name in OAUTH_PROVIDERS:
        if _has_oauth_credential(name):
            return name
    return "openrouter"


def _has_oauth_credential(provider: str) -> bool:
    """Cheap presence check for an OAuth provider's credential. Imports
    providers lazily to avoid a config→providers→config import cycle in
    some test orderings."""
    try:
        from watchmen import providers as _providers
        prov = _providers.get_provider(provider)
        return prov.resolve_api_key(None) is not None
    except Exception:
        # A missing keychain / malformed auth.json / unsupported platform
        # should never crash the resolver — auto-detect just skips this
        # provider and tries the next.
        return False


def provider_key(provider: str) -> str | None:
    """API key (or OAuth access token) for `provider`, or None if unset.

    For env-var-based providers, reads `PROVIDER_KEY_VARS[provider]`.
    For OAuth providers, delegates to the provider's `resolve_api_key()`
    hook (reads the keychain / auth.json on demand)."""
    if provider in PROVIDER_KEY_VARS:
        return read_env_var(PROVIDER_KEY_VARS[provider])
    if provider in OAUTH_PROVIDERS:
        try:
            from watchmen import providers as _providers
            return _providers.get_provider(provider).resolve_api_key(None)
        except Exception:
            return None
    return None


def set_active_provider(provider: str) -> Path:
    """Persist the active-provider selection to ~/.config/watchmen/.env.

    Caller is responsible for validating `provider` against the known list
    (the agent code rejects unknown names at the next call site)."""
    return write_env_var(PROVIDER_ENV_VAR, provider)


def set_provider_key(provider: str, key: str) -> Path:
    """Persist an API key for `provider` to the global env file.

    OAuth providers (claude-pro / chatgpt) don't take pasted keys —
    their credential lives in Claude Code's keychain entry / Codex's
    auth.json. Calling set_provider_key() on those raises so the CLI
    can surface a clear "use `claude login` / `codex login` instead"
    message rather than silently writing a junk env var."""
    if provider in OAUTH_PROVIDERS:
        raise ValueError(
            f"{provider!r} uses OAuth — its credential is sourced from "
            f"{'Claude Code (`claude login`)' if provider == 'claude-pro' else 'Codex (`codex login`)'}. "
            f"There is no API key to paste."
        )
    var = PROVIDER_KEY_VARS.get(provider)
    if not var:
        raise ValueError(f"unknown provider: {provider!r}")
    return write_env_var(var, key)


def default_model() -> str:
    """Default model name to use when no `--model` flag is passed.

    Resolution:
    1. `WATCHMEN_DEFAULT_MODEL` env / `.env` — explicit override (lets users
       swap between e.g. gpt-5 and gpt-5-mini without editing code).
    2. The active provider's per-provider default.
    """
    explicit = read_env_var("WATCHMEN_DEFAULT_MODEL")
    if explicit:
        return explicit
    # Lazy import — providers.py imports nothing from config, so this is one-way.
    from watchmen import providers
    return providers.get_provider(active_provider()).default_model


def distill_default_model() -> str:
    """Model used for semantic skill distillation.

    Distill asks for strict structured judgments and tends to do better on a
    stronger model. We do not hardcode model names here because they go stale;
    instead we respect the user's globally configured default. Users who want a
    different model for distill specifically can set WATCHMEN_DISTILL_MODEL or
    pass `watchmen distill --model ...`.
    """
    explicit = read_env_var("WATCHMEN_DISTILL_MODEL")
    if explicit:
        return explicit
    return default_model()
