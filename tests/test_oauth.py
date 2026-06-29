"""Tests for OAuth credential reuse (claude-pro + chatgpt providers).

What this exercises:

- `credentials.claude_code.ClaudeCodeCredentials` — parses the JSON blob the
  macOS keychain hands back, surfaces `is_expired()` + `has_inference_scope()`
  correctly. We stub the subprocess call to `security` so the test runs on
  Linux + in CI.
- `credentials.codex.CodexCredentials` — parses both api-key mode and chatgpt
  mode `auth.json` shapes, falls back gracefully on schema drift.
- `ClaudePro` provider — Bearer auth + `anthropic-beta: oauth-2025-04-20`
  header, no `x-api-key`; `resolve_api_key` reads keychain; probe checks
  scopes + expiry without burning quota.
- `ChatGPT` provider — Responses API request translation (system →
  instructions, tool_calls → function_call items, tool result →
  function_call_output), SSE event aggregator handling both `response.completed`
  and the synthesis fallback.
- `agent.load_api_key` end-to-end with OAuth: missing credential surfaces an
  actionable error pointing at `claude` / `codex login`.

Tests that exercise the OAuth path *explicitly* override the conftest
`_isolate_oauth_credentials` autouse fixture by monkeypatching the
credentials module directly. That keeps the rest of the suite isolated
from the developer's local Claude Code / Codex login state.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


# ─── ClaudeCodeCredentials ─────────────────────────────────────────────────


def test_claude_credentials_parses_keychain_blob(monkeypatch):
    """A well-formed keychain payload is parsed into the dataclass with all
    fields populated. Catches regressions in field naming (Anthropic's keychain
    schema has changed once before — `claudeAiOauth` was `claudeOauth` in an
    early Claude Code release)."""
    from watchmen.credentials import claude_code as _cc
    payload = {
        "claudeAiOauth": {
            "accessToken":     "tok-abc",
            "refreshToken":    "ref-xyz",
            "expiresAt":       9_999_999_999_999,
            "scopes":          ["user:inference", "user:profile"],
            "subscriptionType": "team",
            "rateLimitTier":   "default_claude_max_5x",
        }
    }
    monkeypatch.setattr(_cc, "is_claude_code_available", lambda: True)
    monkeypatch.setattr(_cc, "_read_keychain_blob", lambda: json.dumps(payload))
    creds = _cc.ClaudeCodeCredentials.read()
    assert creds is not None
    assert creds.access_token == "tok-abc"
    assert creds.refresh_token == "ref-xyz"
    assert creds.expires_at_ms == 9_999_999_999_999
    assert creds.has_inference_scope()
    assert not creds.is_expired()
    assert creds.subscription_type == "team"


def test_claude_credentials_returns_none_when_unavailable(monkeypatch):
    """No keychain (Linux, or fresh Mac without Claude Code) returns None
    rather than raising — every caller's first move would otherwise need
    a try/except wrapper, and we'd lose the clean "is it available?" UX."""
    from watchmen.credentials import claude_code as _cc
    monkeypatch.setattr(_cc, "is_claude_code_available", lambda: False)
    assert _cc.ClaudeCodeCredentials.read() is None


def test_claude_credentials_returns_none_on_unexpected_schema(monkeypatch):
    """Keychain entry that's not the JSON shape we expect (legacy schema,
    corrupt entry, paranoid security audit replacing the payload) is a
    'no credential' signal — never crash mid-resolution. The expected
    blob has a top-level `claudeAiOauth`; anything missing it returns None."""
    from watchmen.credentials import claude_code as _cc
    monkeypatch.setattr(_cc, "is_claude_code_available", lambda: True)
    monkeypatch.setattr(_cc, "_read_keychain_blob", lambda: '{"differentKey": "x"}')
    assert _cc.ClaudeCodeCredentials.read() is None
    # Also robust to invalid JSON
    monkeypatch.setattr(_cc, "_read_keychain_blob", lambda: "not-json{{")
    assert _cc.ClaudeCodeCredentials.read() is None


def test_claude_credentials_is_expired_with_past_timestamp(monkeypatch):
    """An expired access_token's `is_expired()` must return True so callers
    surface a 'run `claude login` to refresh' hint BEFORE the API 401s
    mid-curator-run."""
    from watchmen.credentials import claude_code as _cc
    monkeypatch.setattr(_cc, "is_claude_code_available", lambda: True)
    monkeypatch.setattr(_cc, "_read_keychain_blob", lambda: json.dumps({
        "claudeAiOauth": {
            "accessToken":  "x",
            "refreshToken": "y",
            "expiresAt":    1,  # epoch zero territory
            "scopes":       ["user:inference"],
        }
    }))
    creds = _cc.ClaudeCodeCredentials.read()
    assert creds.is_expired()


def test_claude_credentials_skips_oauth_when_inference_scope_missing(monkeypatch):
    """A token without `user:inference` (older Claude Code with a narrower
    scope set) is technically present but useless for our API calls. The
    `has_inference_scope()` check lets the provider's probe surface a
    specific error rather than a generic 401."""
    from watchmen.credentials import claude_code as _cc
    monkeypatch.setattr(_cc, "is_claude_code_available", lambda: True)
    monkeypatch.setattr(_cc, "_read_keychain_blob", lambda: json.dumps({
        "claudeAiOauth": {
            "accessToken":  "x",
            "refreshToken": "y",
            "expiresAt":    9_999_999_999_999,
            "scopes":       ["user:profile"],
        }
    }))
    creds = _cc.ClaudeCodeCredentials.read()
    assert not creds.has_inference_scope()


def test_read_blob_none_when_no_source(monkeypatch):
    """With neither a keychain entry nor a `~/.claude/.credentials.json` file
    (both low-level readers stubbed to None by the autouse isolation fixture),
    the composed `_read_blob()` yields None — so read() returns None and
    availability is False, degrading gracefully. We assert the real composition
    helper directly because the autouse fixture stubs the public
    `is_claude_code_available` wrapper itself."""
    from watchmen.credentials import claude_code as _cc
    assert _cc._read_blob() is None


def test_claude_credentials_fall_back_to_file_when_no_keychain(monkeypatch):
    """On a host without a keychain (Linux box / server), read() sources the
    same `claudeAiOauth` payload from `~/.claude/.credentials.json`. This is
    the fallback that lets watchmen run on a Claude subscription off a Mac."""
    from watchmen.credentials import claude_code as _cc
    monkeypatch.setattr(_cc, "_read_keychain_blob", lambda: None)
    monkeypatch.setattr(_cc, "_read_credentials_file", lambda: json.dumps({
        "claudeAiOauth": {
            "accessToken":      "linux-access",
            "refreshToken":     "linux-refresh",
            "expiresAt":        9_999_999_999_999,
            "scopes":           ["user:inference"],
            "subscriptionType": "max",
            "rateLimitTier":    "default_claude_max_5x",
        }
    }))
    creds = _cc.ClaudeCodeCredentials.read()
    assert creds is not None
    assert creds.access_token == "linux-access"
    assert creds.subscription_type == "max"
    assert creds.has_inference_scope()
    assert not creds.is_expired()
    # Composition: with the file present (keychain empty), `_read_blob()`
    # surfaces it, so availability resolves True. (Asserted on the real
    # helper; the autouse fixture stubs the public wrapper.)
    assert _cc._read_blob() is not None


def test_keychain_takes_precedence_over_file(monkeypatch):
    """When both sources exist (a macOS box that also has the on-disk file),
    the keychain wins — it is Claude Code's primary, freshest store."""
    from watchmen.credentials import claude_code as _cc
    monkeypatch.setattr(_cc, "_read_keychain_blob", lambda: json.dumps({
        "claudeAiOauth": {"accessToken": "from-keychain", "refreshToken": "r",
                          "expiresAt": 9_999_999_999_999, "scopes": ["user:inference"]}
    }))
    monkeypatch.setattr(_cc, "_read_credentials_file", lambda: json.dumps({
        "claudeAiOauth": {"accessToken": "from-file", "refreshToken": "r",
                          "expiresAt": 9_999_999_999_999, "scopes": ["user:inference"]}
    }))
    creds = _cc.ClaudeCodeCredentials.read()
    assert creds.access_token == "from-keychain"


# ─── CodexCredentials ──────────────────────────────────────────────────────


def _write_codex_auth(tmp_path: Path, payload: dict) -> Path:
    codex_dir = tmp_path / ".codex"
    codex_dir.mkdir(parents=True)
    f = codex_dir / "auth.json"
    f.write_text(json.dumps(payload))
    return f


def test_codex_credentials_api_key_mode(monkeypatch, tmp_path):
    """Codex `auth_mode: api-key` is the trivial reuse case — the user ran
    `codex login --api-key sk-...` and watchmen should pick that key up
    without asking. Critical for the "you have Codex already" onboarding
    delight moment."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _write_codex_auth(tmp_path, {
        "OPENAI_API_KEY": "sk-from-codex",
        "auth_mode":      "api-key",
        "last_refresh":   "2026-05-10T00:00:00Z",
    })
    from watchmen.credentials import CodexCredentials
    creds = CodexCredentials.read()
    assert creds is not None
    assert creds.mode == "api-key"
    assert creds.api_key == "sk-from-codex"
    assert creds.access_token is None


def test_codex_credentials_chatgpt_mode(monkeypatch, tmp_path):
    """ChatGPT OAuth mode — tokens nested under .tokens. The mode/data
    disagreement guard test below confirms data-presence wins, but the
    happy path should produce a fully-populated ChatGPT-mode credential."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _write_codex_auth(tmp_path, {
        "OPENAI_API_KEY": None,
        "auth_mode":      "chatgpt",
        "tokens": {
            "access_token":  "oauth-tok",
            "refresh_token": "ref-tok",
            "id_token":      "jwt.dummy.payload",
            "account_id":    "acct-1",
        },
        "last_refresh": "2026-05-10T00:00:00Z",
    })
    from watchmen.credentials import CodexCredentials
    creds = CodexCredentials.read()
    assert creds is not None
    assert creds.mode == "chatgpt"
    assert creds.access_token == "oauth-tok"
    assert creds.account_id == "acct-1"
    assert creds.api_key is None


def test_codex_credentials_missing_file_returns_none(monkeypatch, tmp_path):
    """Fresh machine without Codex: no auth.json on disk. `read()` returns
    None, doesn't raise — every caller checks `if creds is None`, never
    inside a try/except."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    from watchmen.credentials import CodexCredentials
    assert CodexCredentials.read() is None


def test_codex_credentials_corrupted_json_returns_none(monkeypatch, tmp_path):
    """Broken JSON (mid-write crash, manual edit gone wrong) shouldn't crash
    `watchmen settings provider` either. Same shape as the
    'malformed-keychain returns None' test for Claude Code."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "auth.json").write_text("{not valid json")
    from watchmen.credentials import CodexCredentials
    assert CodexCredentials.read() is None


# ─── ClaudePro provider ────────────────────────────────────────────────────


def test_claude_pro_headers_use_bearer_and_oauth_beta(monkeypatch):
    """Bearer auth + `anthropic-beta: oauth-2025-04-20` is the *whole point*
    of this provider — without the beta header the API rejects the OAuth
    token. Regression here would silently route every call back to the
    api-key code path's 401."""
    from watchmen import providers
    prov = providers.get_provider("claude-pro")
    headers = prov.headers("access-token-x", agent_name="analyst")
    assert headers["Authorization"] == "Bearer access-token-x"
    assert headers["anthropic-beta"] == "oauth-2025-04-20"
    assert "x-api-key" not in headers


def test_claude_pro_resolve_reads_keychain(monkeypatch):
    """The provider's `resolve_api_key()` ignores any `configured` value
    and reads straight from the keychain — env vars don't apply to OAuth.
    A test that passes a fake env key and asserts the keychain token wins
    catches a regression where someone wires up env-var lookup by mistake."""
    from watchmen import providers
    from watchmen.credentials import claude_code as _cc

    monkeypatch.setattr(_cc, "is_claude_code_available", lambda: True)
    monkeypatch.setattr(_cc, "_read_keychain_blob", lambda: json.dumps({
        "claudeAiOauth": {
            "accessToken":  "kc-token",
            "refreshToken": "kc-refresh",
            "expiresAt":    9_999_999_999_999,
            "scopes":       ["user:inference"],
        }
    }))

    prov = providers.get_provider("claude-pro")
    assert prov.resolve_api_key("env-key-value-should-be-ignored") == "kc-token"


def test_claude_pro_probe_does_not_hit_network(monkeypatch):
    """The OAuth probe checks expiry + scopes from local metadata — no
    HTTP probe is needed because the keychain blob has the same info the
    server would. Regression: a future maintainer adding an HTTP fallback
    would burn subscription quota on every doctor / settings render."""
    import httpx
    from watchmen import providers
    from watchmen.credentials import claude_code as _cc

    monkeypatch.setattr(_cc, "is_claude_code_available", lambda: True)
    monkeypatch.setattr(_cc, "_read_keychain_blob", lambda: json.dumps({
        "claudeAiOauth": {
            "accessToken":  "x",
            "refreshToken": "y",
            "expiresAt":    9_999_999_999_999,
            "scopes":       ["user:inference"],
            "subscriptionType": "max",
            "rateLimitTier":    "default_claude_max_20x",
        }
    }))
    # Sentinel — fail loudly if probe tries to make an HTTP request.
    def _no_http(*a, **k):
        pytest.fail("ClaudePro.probe() should not make HTTP calls")
    monkeypatch.setattr(httpx, "get", _no_http)

    prov = providers.get_provider("claude-pro")
    res = prov.probe("x")
    assert res.ok
    assert "max" in res.detail or "claude_max" in res.detail


# ─── ChatGPT provider — Responses API translation ──────────────────────────


def test_chatgpt_translate_request_lifts_system_to_instructions():
    """System message → top-level `instructions`; user message → input
    array with `input_text` content blocks. Critical mapping because the
    Responses API rejects requests with a `system` role inside `input`."""
    from watchmen import providers
    prov = providers.get_provider("chatgpt")
    body = prov.translate_request(
        model="gpt-5.4-mini",
        messages=[
            {"role": "system", "content": "be terse"},
            {"role": "user", "content": "ping"},
        ],
        tools=[],
    )
    assert body["instructions"] == "be terse"
    assert body["model"] == "gpt-5.4-mini"
    assert body["input"][0]["role"] == "user"
    assert body["input"][0]["content"][0] == {"type": "input_text", "text": "ping"}
    # Required transport flags
    assert body["stream"] is True
    assert body["store"] is False
    assert "reasoning" in body and body["reasoning"]["effort"]


def test_chatgpt_translate_request_tool_calls_become_top_level_items():
    """Assistant turn with `tool_calls` (chat-completions shape) must
    serialize as `function_call` items in the Responses API's `input`
    array — they're NOT nested in the assistant message. Replay accuracy
    is critical for multi-turn agent runs."""
    from watchmen import providers
    prov = providers.get_provider("chatgpt")
    body = prov.translate_request(
        model="m",
        messages=[
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "thinking", "tool_calls": [{
                "id": "call-1",
                "function": {"name": "search", "arguments": '{"q":"x"}'},
            }]},
            {"role": "tool", "tool_call_id": "call-1", "content": "result"},
        ],
        tools=[],
    )
    items = body["input"]
    # user → assistant message → function_call → function_call_output
    assert items[0]["role"] == "user"
    assert items[1]["role"] == "assistant"
    fc = items[2]
    assert fc["type"] == "function_call"
    assert fc["call_id"] == "call-1"
    assert fc["name"] == "search"
    fco = items[3]
    assert fco["type"] == "function_call_output"
    assert fco["call_id"] == "call-1"
    assert fco["output"] == "result"


def test_chatgpt_translate_response_folds_message_and_function_call():
    """Responses API output items → chat-completions `choices[0].message`.
    Mix of `message` items (text) and `function_call` items (tool calls)
    must aggregate correctly so the agent loop sees a single canonical
    response shape regardless of how many output items came back."""
    from watchmen import providers
    prov = providers.get_provider("chatgpt")
    out = prov.translate_response({
        "model": "gpt-5.4-mini",
        "output": [
            {"type": "message", "content": [{"type": "output_text", "text": "ok"}]},
            {"type": "function_call", "call_id": "c1", "name": "lookup",
             "arguments": '{"q":"x"}'},
        ],
        "usage": {"input_tokens": 3, "output_tokens": 5,
                  "input_tokens_details": {"cached_tokens": 1}},
    })
    msg = out["choices"][0]["message"]
    assert msg["content"] == "ok"
    assert msg["tool_calls"][0]["function"]["name"] == "lookup"
    assert json.loads(msg["tool_calls"][0]["function"]["arguments"]) == {"q": "x"}
    assert out["usage"]["prompt_tokens"] == 3
    assert out["usage"]["completion_tokens"] == 5
    assert out["usage"]["prompt_tokens_details"]["cached_tokens"] == 1


# ─── ChatGPT SSE aggregator ────────────────────────────────────────────────


def _sse_event(event_type: str, data: dict) -> list[str]:
    """Build a matching `event:` + `data:` line pair. Real Responses-API
    SSE events carry `type` inside the JSON body (the `event:` header is
    advisory), so we fold the event_type into the data dict too — that's
    what the aggregator actually keys on."""
    body = {"type": event_type, **data}
    return [f"event: {event_type}", f"data: {json.dumps(body)}", ""]


def test_chatgpt_sse_aggregator_uses_response_completed(monkeypatch):
    """When the stream's terminal `response.completed` event arrives, we
    use its `.response` payload wholesale — that's the simplest +
    most-correct path (the server has already merged deltas for us)."""
    from watchmen.providers import _aggregate_responses_sse
    final = {
        "model": "gpt-5.4-mini",
        "output": [{"type": "message", "content": [{"type": "output_text", "text": "hi"}]}],
        "usage": {"input_tokens": 1, "output_tokens": 2},
    }
    lines = (
        _sse_event("response.created", {"response": {"id": "r1"}})
        + _sse_event("response.completed", {"response": final})
    )
    out = _aggregate_responses_sse(iter(lines))
    assert out == final


def test_chatgpt_sse_aggregator_synthesizes_from_deltas_on_truncated_stream():
    """If the stream ends without `response.completed` (network blip,
    partial write), the aggregator falls back to synthesizing the output
    from accumulated text deltas + completed function_call items. We want
    a best-effort result rather than losing the work."""
    from watchmen.providers import _aggregate_responses_sse
    lines = (
        _sse_event("response.created", {"response": {"id": "r1", "model": "m"}})
        + _sse_event("response.output_text.delta", {"delta": "hel"})
        + _sse_event("response.output_text.delta", {"delta": "lo"})
        # stream ends without response.completed
    )
    out = _aggregate_responses_sse(iter(lines))
    assert out["output"][0]["type"] == "message"
    assert out["output"][0]["content"][0]["text"] == "hello"


# ─── agent.load_api_key OAuth integration ─────────────────────────────────


def test_load_api_key_oauth_returns_keychain_token(monkeypatch):
    """When the active provider is `claude-pro`, `load_api_key()` must
    pull the token from the keychain via the provider's `resolve_api_key`
    hook — NOT fall through to env-var lookup. Regression catches a
    common mistake: forgetting the OAuth branch in the resolver."""
    from watchmen import agent
    from watchmen.credentials import claude_code as _cc
    monkeypatch.setenv("WATCHMEN_PROVIDER", "claude-pro")
    monkeypatch.setattr(_cc, "is_claude_code_available", lambda: True)
    monkeypatch.setattr(_cc, "_read_keychain_blob", lambda: json.dumps({
        "claudeAiOauth": {
            "accessToken":  "tok-from-keychain",
            "refreshToken": "r",
            "expiresAt":    9_999_999_999_999,
            "scopes":       ["user:inference"],
        }
    }))
    assert agent.load_api_key("claude-pro") == "tok-from-keychain"


def test_load_api_key_oauth_missing_credential_raises_actionable(monkeypatch):
    """OAuth provider with no credential available must raise a clear
    error pointing at the matching CLI (`claude` for claude-pro,
    `codex login` for chatgpt). A generic "credential not set" message
    would leave users guessing which CLI to run."""
    from watchmen import agent
    monkeypatch.setenv("WATCHMEN_PROVIDER", "claude-pro")
    # The conftest fixture already stubs is_claude_code_available → False,
    # so this should hit the OAuth-missing branch unambiguously.
    with pytest.raises(RuntimeError, match=r"`claude`"):
        agent.load_api_key("claude-pro")


def test_openai_resolver_falls_back_to_codex_api_key(monkeypatch, tmp_path):
    """The Codex api-key reuse path: when `OPENAI_API_KEY` isn't in env /
    .env, the OpenAI provider's `resolve_api_key` reads ~/.codex/auth.json
    and returns the stored key. This is the cheap-win path advertised in
    the PR description, so a regression breaks the "Codex users get free
    setup" delight moment."""
    from watchmen import agent
    from watchmen.credentials import codex as _cx

    # Don't expose any env-var key
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    # Override the conftest stub so this test actually exercises the path
    real_read = _cx.CodexCredentials.read
    _write_codex_auth(tmp_path, {
        "OPENAI_API_KEY": "sk-from-codex",
        "auth_mode":      "api-key",
    })
    monkeypatch.setattr(_cx.CodexCredentials, "read", classmethod(lambda cls: real_read.__func__(cls)))

    monkeypatch.setenv("WATCHMEN_PROVIDER", "openai")
    assert agent.load_api_key("openai") == "sk-from-codex"


# ─── Provider registry ─────────────────────────────────────────────────────


def test_oauth_providers_registered_and_addressable():
    """`get_provider()` resolves the new OAuth providers + their display
    names render correctly. Anchors the registry against accidental name
    typos that would break the menu / settings UI."""
    from watchmen import providers
    assert providers.get_provider("claude-pro").name == "claude-pro"
    assert providers.get_provider("chatgpt").name == "chatgpt"
    assert "Claude Pro" in providers.display_name("claude-pro")
    assert "ChatGPT" in providers.display_name("chatgpt")


def test_set_provider_key_rejects_oauth_providers():
    """`config.set_provider_key('claude-pro', '...')` must raise — there's
    no env var to write. Surfacing this as ValueError lets CLI + viewer
    show a meaningful 'use claude login instead' message instead of
    silently writing junk env file entries."""
    from watchmen import config
    with pytest.raises(ValueError, match="OAuth"):
        config.set_provider_key("claude-pro", "x")
    with pytest.raises(ValueError, match="OAuth"):
        config.set_provider_key("chatgpt", "x")
