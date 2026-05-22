"""Provider abstraction — auth, endpoint, request/response shape per LLM provider.

Watchmen historically only called OpenRouter, which speaks the OpenAI
chat-completions wire format. As of 0.7 we support OpenAI and Anthropic
direct in addition to OpenRouter. OpenAI is wire-compatible with OpenRouter
(same JSON, different URL + auth header). Anthropic uses its Messages API,
which has a different request/response shape — we translate to/from the
OpenAI chat-completions shape so the agent loop in agent.py stays
provider-agnostic.

As of 0.8 we also support two **subscription-quota** auth paths:
- `claude-pro` — reuses the Claude Code OAuth token from the macOS
  keychain to call api.anthropic.com with the `oauth-2025-04-20` beta
  header. Billed against the user's Claude Pro/Team/Max subscription.
- `chatgpt` — reuses the Codex CLI's ChatGPT-account OAuth token to call
  the Responses API at chatgpt.com/backend-api/codex/responses. Billed
  against the user's ChatGPT subscription. Experimental: the Responses
  API uses a different wire shape than chat-completions and only certain
  models are whitelisted (gpt-5.5, gpt-5.4, gpt-5.4-mini, gpt-5.3-codex,
  gpt-5.2). Streaming is mandatory — we aggregate the SSE event stream
  into a final chat-completions-shaped response so the agent loop stays
  provider-agnostic.

Adding a new provider should be one Provider subclass here, not a refactor
of agent.py.
"""

from __future__ import annotations

import json
from dataclasses import dataclass


PROVIDER_NAMES = ("openrouter", "openai", "anthropic", "claude-pro", "chatgpt")


# Tracks which "this credential came from somewhere surprising" warnings
# have already been emitted in the current process, so we don't spam the
# log on every analyst day / curator stage. Cleared by tests via
# `_warn_once.clear()`.
_WARNED_KEYS: set[str] = set()


def _warn_once(key: str, message: str) -> None:
    """Emit a stderr warning once per process keyed by `key`. Used when a
    Provider discovers a credential from a non-obvious source (e.g.
    OpenAI provider falling back to Codex's stored api-key)."""
    import sys
    if key in _WARNED_KEYS:
        return
    _WARNED_KEYS.add(key)
    print(message, file=sys.stderr, flush=True)


_warn_once.clear = _WARNED_KEYS.clear  # type: ignore[attr-defined]

# Human-friendly display labels — used by doctor + onboarding so the UI
# reads "OpenRouter key" / "OpenAI key" / "Anthropic key" instead of
# the lowercase identifier.
PROVIDER_DISPLAY = {
    "openrouter": "OpenRouter",
    "openai":     "OpenAI",
    "anthropic":  "Anthropic",
    "claude-pro": "Claude Pro/Team/Max",
    "chatgpt":    "ChatGPT (experimental)",
}


def display_name(provider: str) -> str:
    """Title-cased display label, falling back to the raw name."""
    return PROVIDER_DISPLAY.get(provider, provider)


# ─── Canonical wire format ────────────────────────────────────────────────
#
# Everything outside this module sees the OpenAI chat-completions shape:
#   request:  {"model", "messages", "tools"}
#   response: {"choices": [{"message": {"content", "tool_calls"}}],
#              "usage": {"prompt_tokens", "completion_tokens",
#                        "prompt_tokens_details": {"cached_tokens"}},
#              "model"}
# Translators in this module convert provider-native shapes to/from that.


@dataclass(frozen=True)
class ProbeResult:
    ok: bool
    detail: str


class Provider:
    """Base provider. Subclass for each backend."""

    name: str = "base"
    endpoint: str = ""
    # Default model for this provider when WATCHMEN_DEFAULT_MODEL is unset.
    # Each provider has different model naming conventions so the picker
    # can't just default to one global string.
    default_model: str = ""

    # Set True by subclasses that use a non-chat-completions transport
    # (e.g. streaming-only Responses API). When True, agent.call_chat
    # delegates to `Provider.call()` instead of doing the standard
    # JSON POST itself.
    custom_transport: bool = False

    # True for providers that bill against a flat-rate subscription quota
    # instead of per-token API credits. Drives the startup banner + the
    # onboarding cost panel — for subscription providers we don't print a
    # dollar estimate because there is none.
    is_subscription_quota: bool = False
    # Free-form human-readable phrase used in the banner. e.g.
    # "Claude Pro/Team/Max subscription" or "OpenRouter API credits".
    quota_label: str = "API credits"

    def resolve_api_key(self, configured: str | None) -> str | None:
        """Allow a provider to discover credentials beyond the standard
        env-var path (e.g. read the macOS keychain, parse ~/.codex/auth.json).

        Called by agent.load_api_key when the standard `WATCHMEN_PROVIDER_KEY`
        resolution returns nothing. Default behavior is to return whatever
        the caller passed — pass-through for env-var-based providers.

        Returning None signals "no credential available" — the agent
        layer surfaces an actionable error with the env-var hint."""
        return configured

    def headers(self, api_key: str, *, agent_name: str = "") -> dict[str, str]:
        """Auth + content headers for a chat-completions call."""
        return {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

    def translate_request(
        self, *, model: str, messages: list[dict], tools: list[dict]
    ) -> dict:
        """Translate the canonical chat-completions request into the
        provider's wire format. Default is identity (OpenAI-compatible)."""
        return {"model": model, "messages": messages, "tools": tools}

    def translate_response(self, raw: dict) -> dict:
        """Translate provider response back into the canonical chat-completions
        shape. Default is identity."""
        return raw

    def apply_extra_payload(self, body: dict, extra: dict) -> dict:
        """Merge caller-supplied chat-completions kwargs (temperature,
        max_tokens, response_format, ...) into the translated request body.

        Default behavior merges verbatim — fine for OpenAI-shape providers
        (openrouter/openai) and for Anthropic (which accepts the same
        top-level `temperature` / `max_tokens` keys). Providers whose wire
        format uses different parameter names override to rename/drop
        unsupported kwargs."""
        for k, v in extra.items():
            if v is not None:
                body[k] = v
        return body

    def call(self, client, url: str, headers: dict, body: dict, *,
             max_retries: int = 4, log=None, label: str = "") -> dict:
        """Custom transport hook. Default returns None to signal "use the
        standard chat_call path"; override for streaming/SSE providers.

        Returns either the raw response dict (which goes through
        `translate_response` before reaching the agent loop) or None to
        opt out and use the default JSON POST."""
        return None

    def probe(self, api_key: str, *, timeout: float = 10.0) -> ProbeResult:
        """Live-validate the key by hitting an inexpensive endpoint."""
        raise NotImplementedError


# ─── OpenRouter ────────────────────────────────────────────────────────────


class OpenRouterProvider(Provider):
    name = "openrouter"
    endpoint = "https://openrouter.ai/api/v1/chat/completions"
    default_model = "deepseek/deepseek-v4-flash"
    quota_label = "OpenRouter API credits"

    def headers(self, api_key: str, *, agent_name: str = "") -> dict[str, str]:
        return {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            # OpenRouter app attribution.
            "HTTP-Referer": "https://github.com/firstbatchxyz/watchmen",
            "X-Title": f"watchmen:{agent_name}" if agent_name else "watchmen",
        }

    def probe(self, api_key: str, *, timeout: float = 10.0) -> ProbeResult:
        import httpx
        try:
            r = httpx.get(
                "https://openrouter.ai/api/v1/auth/key",
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=timeout,
            )
        except httpx.RequestError as e:
            return ProbeResult(False, f"connection error: {type(e).__name__}")
        if r.status_code == 200:
            try:
                info = (r.json() or {}).get("data") or {}
            except ValueError:
                info = {}
            usage = info.get("usage")
            limit = info.get("limit")
            if usage is not None and limit is not None and limit > 0:
                return ProbeResult(True, f"valid · credits used ${float(usage):.2f} of ${float(limit):.2f}")
            if usage is not None and limit is None:
                return ProbeResult(True, f"valid · credits used ${float(usage):.2f} (no hard limit)")
            return ProbeResult(True, "valid")
        if r.status_code == 401:
            try:
                msg = (r.json().get("error") or {}).get("message", "")
            except (ValueError, AttributeError):
                msg = ""
            return ProbeResult(False, f"401 · {msg or 'unauthorized'}")
        return ProbeResult(False, f"HTTP {r.status_code} · {r.text[:120]}")


# ─── OpenAI ────────────────────────────────────────────────────────────────


class OpenAIProvider(Provider):
    name = "openai"
    endpoint = "https://api.openai.com/v1/chat/completions"
    # gpt-5-mini is the modern cost-efficient default with tool use support.
    # Override with WATCHMEN_DEFAULT_MODEL=gpt-5 etc. for higher-quality runs.
    default_model = "gpt-5-mini"
    quota_label = "OpenAI API credits"

    def resolve_api_key(self, configured: str | None) -> str | None:
        """Fall back to Codex's stored key if `OPENAI_API_KEY` isn't set
        in env / .env. Lets Codex users (who already ran `codex login
        --api-key sk-...`) skip re-pasting into watchmen. Only api-key
        mode is reused here; chatgpt-OAuth mode is a separate provider.

        Emits a one-time stderr line when the Codex fallback fires so
        the user isn't surprised which credential is in flight — this
        is the most opaque path in credential resolution and silently
        spending against someone else's billed key would be a bad
        surprise."""
        if configured:
            return configured
        try:
            from watchmen.credentials import CodexCredentials
            creds = CodexCredentials.read()
            if creds and creds.mode == "api-key" and creds.api_key:
                _warn_once(
                    "openai-codex-fallback",
                    "watchmen: reusing OPENAI_API_KEY from ~/.codex/auth.json "
                    "(Codex CLI). This bills against your OpenAI org. "
                    "Set OPENAI_API_KEY in your env to override.",
                )
                return creds.api_key
        except Exception:
            return None
        return None

    def probe(self, api_key: str, *, timeout: float = 10.0) -> ProbeResult:
        import httpx
        try:
            r = httpx.get(
                "https://api.openai.com/v1/models",
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=timeout,
            )
        except httpx.RequestError as e:
            return ProbeResult(False, f"connection error: {type(e).__name__}")
        if r.status_code == 200:
            try:
                count = len((r.json() or {}).get("data") or [])
                return ProbeResult(True, f"valid · {count} models accessible")
            except ValueError:
                return ProbeResult(True, "valid")
        if r.status_code == 401:
            return ProbeResult(False, "401 · invalid or revoked key")
        return ProbeResult(False, f"HTTP {r.status_code} · {r.text[:120]}")


# ─── Anthropic ─────────────────────────────────────────────────────────────


class AnthropicProvider(Provider):
    name = "anthropic"
    endpoint = "https://api.anthropic.com/v1/messages"
    default_model = "claude-haiku-4-5-20251001"
    quota_label = "Anthropic API credits"
    # Cap on output tokens per turn — Anthropic requires this field, OpenAI
    # treats it as optional. We pick a generous default; callers that need
    # less can rely on the model stopping early.
    _max_tokens_per_turn = 8192

    def headers(self, api_key: str, *, agent_name: str = "") -> dict[str, str]:
        return {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }

    def translate_request(
        self, *, model: str, messages: list[dict], tools: list[dict]
    ) -> dict:
        """Chat-completions → Messages API.

        Key differences:
        - system is a top-level field, not a message
        - messages only contain user/assistant
        - tool results come back as content blocks inside a user message
        - tool calls go out as content blocks inside an assistant message
        - tool schema uses `input_schema` not `parameters`
        """
        system_parts: list[str] = []
        out_msgs: list[dict] = []

        for m in messages:
            role = m.get("role")
            if role == "system":
                if m.get("content"):
                    system_parts.append(m["content"])
                continue

            if role == "tool":
                # OpenAI uses {role: tool, tool_call_id, content}; Anthropic
                # wraps the result in a user message with a tool_result block.
                out_msgs.append({
                    "role": "user",
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": m.get("tool_call_id", ""),
                        "content": m.get("content", ""),
                    }],
                })
                continue

            if role == "assistant":
                blocks: list[dict] = []
                text = m.get("content") or ""
                if text:
                    blocks.append({"type": "text", "text": text})
                for tc in (m.get("tool_calls") or []):
                    try:
                        tc_args = json.loads(tc["function"].get("arguments") or "{}")
                    except (KeyError, json.JSONDecodeError):
                        tc_args = {}
                    blocks.append({
                        "type": "tool_use",
                        "id": tc.get("id", ""),
                        "name": tc.get("function", {}).get("name", ""),
                        "input": tc_args,
                    })
                # Anthropic rejects empty content arrays; fall back to a
                # single empty text block if the assistant message somehow
                # carries neither text nor tool calls.
                if not blocks:
                    blocks = [{"type": "text", "text": ""}]
                out_msgs.append({"role": "assistant", "content": blocks})
                continue

            # user (or anything else): pass through
            out_msgs.append({"role": role or "user", "content": m.get("content", "")})

        anthropic_tools: list[dict] = []
        for t in (tools or []):
            fn = t.get("function") if t.get("type") == "function" else t
            if not fn:
                continue
            anthropic_tools.append({
                "name": fn.get("name", ""),
                "description": fn.get("description", ""),
                "input_schema": fn.get("parameters") or {"type": "object", "properties": {}},
            })

        body = {
            "model": model,
            "max_tokens": self._max_tokens_per_turn,
            "messages": out_msgs,
        }
        if system_parts:
            body["system"] = "\n\n".join(system_parts)
        if anthropic_tools:
            body["tools"] = anthropic_tools
        return body

    def translate_response(self, raw: dict) -> dict:
        """Messages API → chat-completions shape.

        Anthropic returns `content` as a list of blocks (text + tool_use).
        The rest of agent.py reads `choices[0].message.content` and
        `choices[0].message.tool_calls`, so we fold the blocks into that
        shape.
        """
        text_parts: list[str] = []
        tool_calls: list[dict] = []
        for block in (raw.get("content") or []):
            btype = block.get("type")
            if btype == "text":
                text_parts.append(block.get("text") or "")
            elif btype == "tool_use":
                tool_calls.append({
                    "id": block.get("id", ""),
                    "type": "function",
                    "function": {
                        "name": block.get("name", ""),
                        "arguments": json.dumps(block.get("input") or {}),
                    },
                })

        usage = raw.get("usage") or {}
        return {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": "".join(text_parts),
                    "tool_calls": tool_calls or None,
                },
                "finish_reason": raw.get("stop_reason"),
            }],
            "model": raw.get("model"),
            "usage": {
                "prompt_tokens": int(usage.get("input_tokens") or 0),
                "completion_tokens": int(usage.get("output_tokens") or 0),
                "prompt_tokens_details": {
                    "cached_tokens": int(usage.get("cache_read_input_tokens") or 0),
                },
            },
        }

    def probe(self, api_key: str, *, timeout: float = 10.0) -> ProbeResult:
        import httpx
        try:
            r = httpx.get(
                "https://api.anthropic.com/v1/models",
                headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"},
                timeout=timeout,
            )
        except httpx.RequestError as e:
            return ProbeResult(False, f"connection error: {type(e).__name__}")
        if r.status_code == 200:
            try:
                count = len((r.json() or {}).get("data") or [])
                return ProbeResult(True, f"valid · {count} models accessible")
            except ValueError:
                return ProbeResult(True, "valid")
        if r.status_code in (401, 403):
            return ProbeResult(False, f"{r.status_code} · invalid or revoked key")
        return ProbeResult(False, f"HTTP {r.status_code} · {r.text[:120]}")


# ─── Claude Pro (Anthropic OAuth via keychain) ─────────────────────────────


class ClaudePro(AnthropicProvider):
    """Anthropic Messages API with OAuth-bearer auth — routes against the
    user's Claude Pro/Team/Max subscription quota instead of per-token API
    credit.

    Differences from `AnthropicProvider`:
    - Auth header is `Authorization: Bearer <oauth-access-token>` instead
      of `x-api-key`. The token comes from Claude Code's macOS keychain
      entry (read by `credentials.claude_code`).
    - Adds the `anthropic-beta: oauth-2025-04-20` header — without it the
      token is rejected by api.anthropic.com.
    - `resolve_api_key()` reads the keychain on-demand, so the user doesn't
      need to paste or persist a key. Token rotation is handled by Claude
      Code itself; we re-read on every call so refreshes propagate.
    - Probe checks token presence + non-expiry + the `user:inference`
      scope; an HTTP probe would burn quota and we don't need it (a 401
      mid-run is rare since Claude Code refreshes proactively).
    """

    name = "claude-pro"
    # Use the same Anthropic Messages endpoint as the api-key path; only
    # the auth header differs.
    endpoint = "https://api.anthropic.com/v1/messages"
    # Default to the cheapest model that still produces good analyst /
    # curator output — Haiku is the right pick on subscription quota
    # since the rate-limit-tier math favors it.
    default_model = "claude-haiku-4-5-20251001"
    is_subscription_quota = True
    quota_label = "Claude Pro/Team/Max subscription"

    def resolve_api_key(self, configured: str | None) -> str | None:
        """Read the OAuth token straight from Claude Code's keychain
        entry. `configured` (env var / .env) is ignored — for the
        subscription-quota provider, the user shouldn't need to paste
        anything; the credential is wherever Claude Code put it."""
        from watchmen.credentials import ClaudeCodeCredentials
        creds = ClaudeCodeCredentials.read()
        if creds is None:
            return None
        return creds.access_token

    def headers(self, api_key: str, *, agent_name: str = "") -> dict[str, str]:
        """Bearer auth + the OAuth beta header. The `anthropic-version`
        is still required; what the beta header changes is the *meaning*
        of the bearer token (it accepts a Claude Pro OAuth access_token
        instead of an Anthropic API key)."""
        return {
            "Authorization": f"Bearer {api_key}",
            "anthropic-version": "2023-06-01",
            "anthropic-beta": "oauth-2025-04-20",
            "content-type": "application/json",
        }

    def probe(self, api_key: str, *, timeout: float = 10.0) -> ProbeResult:
        """Validate the credential without hitting the API — for OAuth we
        already have rich local metadata (expiry, scopes) so an HTTP
        probe would burn quota for redundant info.

        `api_key` here is the access_token; we re-read the full credential
        to also check `expiresAt` + `scopes` since those drive the verdict."""
        from watchmen.credentials import ClaudeCodeCredentials
        creds = ClaudeCodeCredentials.read()
        if creds is None:
            return ProbeResult(False, "Claude Code credential not found — sign in with `claude` first")
        if creds.is_expired():
            return ProbeResult(False, "OAuth token expired — refresh by running Claude Code, then retry")
        if not creds.has_inference_scope():
            return ProbeResult(False, "token missing `user:inference` scope — upgrade Claude Code or re-login")
        bits = []
        if creds.subscription_type:
            bits.append(f"plan {creds.subscription_type}")
        if creds.rate_limit_tier:
            bits.append(creds.rate_limit_tier)
        meta = " · ".join(bits) if bits else "valid"
        return ProbeResult(True, f"OAuth · {meta}")


# ─── ChatGPT (OpenAI Responses API via Codex OAuth) ────────────────────────


class ChatGPT(Provider):
    """OpenAI Responses API with ChatGPT-account OAuth — routes against
    the user's ChatGPT subscription quota via Codex's backend endpoint.

    **Experimental.** The Responses API differs significantly from
    chat-completions: streaming is mandatory, the request body has a
    different shape (`instructions` top-level, `input` array of typed
    content blocks, `reasoning.effort`), and tool calls use
    `function_call` items rather than `tool_calls`. We translate
    bidirectionally so the agent loop stays provider-agnostic, but
    SSE-stream aggregation adds complexity and the model whitelist is
    narrower than the public API.

    Available models (discovered via the backend /models endpoint):
    `gpt-5.5`, `gpt-5.4`, `gpt-5.4-mini`, `gpt-5.3-codex`, `gpt-5.2`.
    """

    name = "chatgpt"
    endpoint = "https://chatgpt.com/backend-api/codex/responses"
    default_model = "gpt-5.4-mini"
    custom_transport = True
    is_subscription_quota = True
    quota_label = "ChatGPT subscription"

    # Identifies our traffic as the Codex CLI so the backend accepts the
    # request. Bumping versions periodically keeps us in lockstep with
    # the official client.
    _ORIGINATOR = "codex_cli_rs"
    _VERSION = "0.130.0"
    # Reasoning effort default — `low` keeps per-call latency reasonable.
    # Curator runs benefit from `medium` or higher but the default favors
    # the analyst's per-day cadence.
    _DEFAULT_REASONING_EFFORT = "low"

    def resolve_api_key(self, configured: str | None) -> str | None:
        """Read the OAuth access_token from `~/.codex/auth.json`."""
        from watchmen.credentials import CodexCredentials
        creds = CodexCredentials.read()
        if creds is None or creds.mode != "chatgpt":
            return None
        return creds.access_token

    def headers(self, api_key: str, *, agent_name: str = "") -> dict[str, str]:
        """Full Codex-CLI-equivalent header set. Empirically the backend
        rejects requests missing `originator` + `version` + the
        `OpenAI-Beta: responses=experimental` flag. The account-id header
        is sourced from the same credential and stitched in inside the
        call path (not here, since `headers()` doesn't know about the
        credential beyond the access_token)."""
        from watchmen.credentials import CodexCredentials
        creds = CodexCredentials.read()
        account_id = creds.account_id if creds else ""
        return {
            "Authorization": f"Bearer {api_key}",
            "chatgpt-account-id": account_id or "",
            "originator": self._ORIGINATOR,
            "version": self._VERSION,
            "OpenAI-Beta": "responses=experimental",
            # `session_id` correlates streamed events for the backend;
            # any UUID works. Using the agent-name-derived id keeps logs
            # grep-able to which sub-agent made the call.
            "session_id": _session_id_for(agent_name or "watchmen"),
            "Accept": "text/event-stream",
            "content-type": "application/json",
        }

    def translate_request(
        self, *, model: str, messages: list[dict], tools: list[dict]
    ) -> dict:
        """Chat-completions → Responses API.

        Mapping:
        - `system` message → `instructions` top-level string. Multiple
          system messages get concatenated with `\\n\\n` (agent.py only
          emits one but the spec allows several).
        - User/assistant `messages` → `input` array. Each item is
          `{role, content: [{type, text|...}]}` with content types like
          `input_text`, `output_text`, `function_call`,
          `function_call_output` depending on role + payload.
        - `tools` (OpenAI chat-completions shape) → `tools` (Responses
          shape, which is similar but uses `type: "function"` at the top
          level and unwraps the nested `function` object).
        - `tool_choice` defaults to `auto`.
        """
        instructions_parts: list[str] = []
        input_items: list[dict] = []

        for m in messages:
            role = m.get("role")
            if role == "system":
                if m.get("content"):
                    instructions_parts.append(m["content"])
                continue
            if role == "user":
                input_items.append({
                    "role": "user",
                    "content": [{"type": "input_text", "text": m.get("content", "")}],
                })
                continue
            if role == "assistant":
                content_blocks: list[dict] = []
                if m.get("content"):
                    content_blocks.append({"type": "output_text", "text": m["content"]})
                input_items.append({
                    "role": "assistant",
                    "content": content_blocks,
                })
                # Function calls are emitted as separate top-level items
                # in the Responses API, not nested inside the assistant
                # message. Replay them as items so the model can see its
                # own tool-call history.
                for tc in (m.get("tool_calls") or []):
                    try:
                        tc_args = tc["function"].get("arguments") or "{}"
                    except (KeyError, AttributeError):
                        tc_args = "{}"
                    input_items.append({
                        "type": "function_call",
                        "call_id": tc.get("id", ""),
                        "name": tc.get("function", {}).get("name", ""),
                        "arguments": tc_args,
                    })
                continue
            if role == "tool":
                # Tool result message — Responses API uses a top-level
                # `function_call_output` item correlated by `call_id`.
                input_items.append({
                    "type": "function_call_output",
                    "call_id": m.get("tool_call_id", ""),
                    "output": m.get("content", ""),
                })
                continue

        responses_tools = []
        for t in (tools or []):
            fn = t.get("function") if t.get("type") == "function" else t
            if not fn:
                continue
            responses_tools.append({
                "type": "function",
                "name": fn.get("name", ""),
                "description": fn.get("description", ""),
                "parameters": fn.get("parameters") or {"type": "object", "properties": {}},
            })

        body = {
            "model": model,
            "instructions": "\n\n".join(instructions_parts) or " ",
            "input": input_items,
            "stream": True,
            "store": False,
            "reasoning": {"effort": self._DEFAULT_REASONING_EFFORT},
        }
        if responses_tools:
            body["tools"] = responses_tools
        return body

    def apply_extra_payload(self, body: dict, extra: dict) -> dict:
        """The codex/responses OAuth endpoint accepts a stricter kwarg
        surface than both chat-completions and the public Responses API.
        Empirically (probed against
        `chatgpt.com/backend-api/codex/responses` on 2026-05-22) it
        rejects all three kwargs the chat-completions judge passes:

        - `max_tokens` / `max_output_tokens`: returns
          ``{"detail": "Unsupported parameter: max_output_tokens"}``.
          The endpoint does not accept any length-cap parameter; codex
          itself sends none, relying on `reasoning.effort` + streaming
          aggregation to bound output. Dropped here.
        - `temperature`: reasoning models on this endpoint control
          determinism via `reasoning.effort` (set in `translate_request`),
          not `temperature`.
        - `response_format`: replaced in the Responses API by a nested
          `text.format` block we don't emit. Structured-output
          enforcement on chatgpt would need separate plumbing.

        Dropping silently means callers (the skillmesh judge in
        particular) can keep passing chat-completions kwargs for the
        openrouter/openai path without crashing chatgpt. Unknown kwargs
        fall through verbatim — room for future Responses-API-native
        params (e.g. `parallel_tool_calls`) without another override."""
        dropped = ("temperature", "response_format", "max_tokens", "max_output_tokens")
        for k, v in extra.items():
            if v is None:
                continue
            if k in dropped:
                continue
            body[k] = v
        return body

    def call(self, client, url: str, headers: dict, body: dict, *,
             max_retries: int = 4, log=None, label: str = "") -> dict:
        """Stream the Responses API SSE event stream + aggregate into a
        chat-completions-shaped response dict.

        Strategy:
        - POST with `stream=true`; iterate lines as they arrive.
        - SSE event lines look like:
            event: response.output_text.delta
            data: {...JSON...}
          We accumulate `data` blobs by type. The terminal event is
          `response.completed`, which carries the full final response
          including `output` items + `usage`. If that event arrives we
          use it directly; otherwise we synthesize one from the partial
          state we collected.
        - The aggregated result is returned in Responses-API shape;
          `translate_response` then folds it into chat-completions.
        """
        import time
        import httpx
        from watchmen.agent import _backoff_seconds, _RETRYABLE_STATUS

        last_exc: Exception | None = None
        for attempt in range(max_retries):
            try:
                with client.stream("POST", url, headers=headers, json=body, timeout=300.0) as r:
                    if r.status_code in _RETRYABLE_STATUS and attempt < max_retries - 1:
                        delay = _backoff_seconds(attempt, r.headers.get("Retry-After"))
                        if log:
                            log(f"{label}: HTTP {r.status_code}, retry in {delay:.1f}s")
                        time.sleep(delay)
                        continue
                    r.raise_for_status()
                    return _aggregate_responses_sse(r.iter_lines())
            except httpx.RequestError as e:
                last_exc = e
                if attempt == max_retries - 1:
                    raise
                delay = _backoff_seconds(attempt, None)
                if log:
                    log(f"{label}: {type(e).__name__}, retry in {delay:.1f}s")
                time.sleep(delay)
        raise RuntimeError(f"exhausted retries; last error: {last_exc}")

    def translate_response(self, raw: dict) -> dict:
        """Responses API output → chat-completions.

        Responses output is a list of items. We collect:
        - `message` items → text content (concatenate output_text blocks)
        - `function_call` items → tool_calls
        - `usage` → prompt/completion token counts
        """
        text_parts: list[str] = []
        tool_calls: list[dict] = []

        for item in (raw.get("output") or []):
            itype = item.get("type")
            if itype == "message":
                for part in (item.get("content") or []):
                    if part.get("type") == "output_text":
                        text_parts.append(part.get("text") or "")
            elif itype == "function_call":
                tool_calls.append({
                    "id": item.get("call_id") or item.get("id", ""),
                    "type": "function",
                    "function": {
                        "name": item.get("name", ""),
                        "arguments": item.get("arguments") or "{}",
                    },
                })

        usage = raw.get("usage") or {}
        return {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": "".join(text_parts),
                    "tool_calls": tool_calls or None,
                },
                "finish_reason": raw.get("status"),
            }],
            "model": raw.get("model"),
            "usage": {
                "prompt_tokens": int(usage.get("input_tokens") or 0),
                "completion_tokens": int(usage.get("output_tokens") or 0),
                "prompt_tokens_details": {
                    "cached_tokens": int((usage.get("input_tokens_details") or {}).get("cached_tokens") or 0),
                },
            },
        }

    def probe(self, api_key: str, *, timeout: float = 10.0) -> ProbeResult:
        """Validate via the Codex backend `/models` endpoint — confirms
        the token is alive + the user has Codex access without burning
        inference quota."""
        import httpx
        from watchmen.credentials import CodexCredentials
        creds = CodexCredentials.read()
        if creds is None or creds.mode != "chatgpt":
            return ProbeResult(False, "Codex ChatGPT credential not found — run `codex login` first")
        try:
            r = httpx.get(
                f"https://chatgpt.com/backend-api/codex/models?client_version={self._VERSION}",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "chatgpt-account-id": creds.account_id or "",
                    "originator": self._ORIGINATOR,
                    "version": self._VERSION,
                },
                timeout=timeout,
            )
        except httpx.RequestError as e:
            return ProbeResult(False, f"connection error: {type(e).__name__}")
        if r.status_code == 200:
            try:
                models = (r.json() or {}).get("models") or []
                slugs = ", ".join(m.get("slug", "?") for m in models[:4])
                return ProbeResult(True, f"OAuth · {len(models)} models · {slugs}")
            except ValueError:
                return ProbeResult(True, "OAuth · valid")
        if r.status_code in (401, 403):
            return ProbeResult(False, f"{r.status_code} · token expired or invalid — `codex login` to refresh")
        return ProbeResult(False, f"HTTP {r.status_code} · {r.text[:120]}")


# ─── SSE aggregation helper ────────────────────────────────────────────────


def _aggregate_responses_sse(lines) -> dict:
    """Walk a Responses API SSE stream + return the final response dict.

    Each event is two lines: `event: <name>` then `data: <json>` (plus a
    blank separator). The terminal event is `response.completed`, whose
    `data.response` field carries the complete final-state payload. If
    the stream ends without `response.completed` (network blip, partial
    write), we fall back to whatever we last saw on `response.created`
    or `response.in_progress` + accumulated text deltas — better than
    raising and losing the work.
    """
    last_response: dict = {}
    text_chunks: list[str] = []
    function_call_items: dict = {}  # id → accumulated item

    for line in lines:
        if not line:
            continue
        if isinstance(line, bytes):
            try:
                line = line.decode("utf-8")
            except UnicodeDecodeError:
                continue
        if not line.startswith("data:"):
            continue
        data_str = line[len("data:"):].strip()
        if not data_str or data_str == "[DONE]":
            continue
        try:
            data = json.loads(data_str)
        except json.JSONDecodeError:
            continue
        etype = data.get("type", "")

        if etype in ("response.created", "response.in_progress", "response.completed"):
            # Each of these carries a full snapshot at `.response`.
            resp = data.get("response") or {}
            if resp:
                last_response = resp

        if etype == "response.output_text.delta":
            delta = data.get("delta")
            if delta:
                text_chunks.append(delta)

        if etype == "response.output_item.added":
            item = data.get("item") or {}
            if item.get("type") == "function_call":
                function_call_items[item.get("id") or item.get("call_id") or ""] = dict(item)

        if etype == "response.function_call_arguments.delta":
            item_id = data.get("item_id", "")
            if item_id in function_call_items:
                function_call_items[item_id].setdefault("arguments", "")
                function_call_items[item_id]["arguments"] += (data.get("delta") or "")

        if etype == "response.output_item.done":
            item = data.get("item") or {}
            if item.get("type") == "function_call":
                # `done` items carry the fully-formed arguments string,
                # superseding whatever we accumulated via deltas.
                function_call_items[item.get("id") or item.get("call_id") or ""] = dict(item)

    # If we got a final response payload, prefer that wholesale — it
    # already contains the merged output items + usage. Otherwise
    # synthesize from accumulated pieces.
    if last_response.get("output"):
        return last_response

    synthesized_output = []
    if text_chunks:
        synthesized_output.append({
            "type": "message",
            "content": [{"type": "output_text", "text": "".join(text_chunks)}],
        })
    for item in function_call_items.values():
        synthesized_output.append(item)
    synth = dict(last_response)
    synth["output"] = synthesized_output
    return synth


def _session_id_for(name: str) -> str:
    """Deterministic-but-distinct UUID-shaped string from an agent name.
    The Codex backend accepts any 36-char UUID-formatted token here; we
    derive one from `name` so logs grep cleanly to a sub-agent."""
    import hashlib
    h = hashlib.sha256(name.encode("utf-8")).hexdigest()
    # 8-4-4-4-12
    return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


# ─── Registry ──────────────────────────────────────────────────────────────


_PROVIDERS: dict[str, Provider] = {
    "openrouter": OpenRouterProvider(),
    "openai": OpenAIProvider(),
    "anthropic": AnthropicProvider(),
    "claude-pro": ClaudePro(),
    "chatgpt": ChatGPT(),
}


def get_provider(name: str) -> Provider:
    """Return the Provider instance for `name`. Raises ValueError on unknown."""
    if name not in _PROVIDERS:
        raise ValueError(
            f"unknown provider: {name!r} (valid: {', '.join(PROVIDER_NAMES)})"
        )
    return _PROVIDERS[name]


def all_providers() -> dict[str, Provider]:
    """Map of provider name → instance. Used by CLI listings and doctor."""
    return dict(_PROVIDERS)
