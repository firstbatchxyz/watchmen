"""Shared tool-calling agent. Used by analyze.py, curate.py, etc.

An Agent is configured with a system prompt + tool specs + tool handlers + a terminal-tool
name. .run(user_msg) runs the multi-turn loop and returns the args of the terminal tool call
(or empty dict if the model gave up without calling it), plus the full message history for logging.

The provider abstraction lives in `providers.py`. This module dispatches
requests through the active provider's URL + auth headers + request/response
translator, so adding a new provider doesn't touch the loop here.
"""

import json
import os
import random
import sys
import time
from pathlib import Path
from typing import Callable

import httpx

from watchmen import providers as _providers

# Retry on these HTTP status codes — 429 (rate limit) and 5xx (server-side
# blips, gateway timeouts, etc.). 408 (request timeout) is treated as
# transient too. Everything else (4xx) is a client-side problem and isn't
# worth retrying.
_RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504, 524}

# Kept for backward compat with any external imports — pre-0.7 callers
# referenced `agent.OPENROUTER_URL` directly. The dispatch path no longer
# uses this constant; the Provider owns the endpoint now.
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


def provider_banner(provider_name: str | None = None, model: str | None = None) -> str:
    """One-line summary of the active provider — printed at analyst /
    curator startup so the user can verify which endpoint and billing
    mode the run will use. Important enough to be prominent: pre-OAuth
    users assumed they were on subscription quota when they weren't, and
    vice versa. Making the resolved provider + endpoint visible at
    startup is the cheapest way to remove that ambiguity.

    Example outputs:
        provider=chatgpt · ChatGPT subscription · model=gpt-5.4-mini
                · endpoint=chatgpt.com/backend-api/codex/responses
        provider=openrouter · OpenRouter API credits · model=deepseek-v4-flash
                · endpoint=openrouter.ai/api/v1/chat/completions

    Returned plain (no Rich markup) so it works equally well via
    `print()` and Rich's `console.print()`.
    """
    from watchmen import config

    name = provider_name or config.active_provider()
    try:
        prov = _providers.get_provider(name)
    except ValueError:
        return f"provider={name} · model={model or '?'}"

    model_str = model or config.default_model()
    # Strip the scheme so it's compact + readable in terminals.
    endpoint = prov.endpoint.replace("https://", "").replace("http://", "")
    return (
        f"provider={name} · {prov.quota_label} · model={model_str} · endpoint={endpoint}"
    )


def load_api_key(provider: str | None = None) -> str:
    """Resolve the API key (or OAuth access token) for the active (or
    named) provider.

    Resolution differs by provider type:
    - **Env-var-based** (openrouter/openai/anthropic): process env →
      ~/.config/watchmen/.env. The wizard writes the file with chmod 0600.
    - **OAuth-based** (claude-pro/chatgpt): delegates to the provider's
      `resolve_api_key()` hook, which reads the macOS keychain entry /
      Codex auth.json on demand. The user never pastes anything.

    Raises RuntimeError with an actionable message rather than returning
    empty — every call site eventually hits an HTTP 401 if we return
    None, and the error trail is harder to follow.
    """
    # Lazy local import to avoid a config↔agent cycle: agent is imported
    # eagerly by several modules; config imports providers; providers
    # imports nothing.
    from watchmen import config

    name = provider or config.active_provider()

    # OAuth providers: defer entirely to the provider's discovery hook.
    if name in config.OAUTH_PROVIDERS:
        from watchmen import providers as _providers
        prov = _providers.get_provider(name)
        token = prov.resolve_api_key(None)
        if token:
            return token
        # Provider-specific actionable hint — knowing what to do next is
        # the difference between a tractable error and a silent failure.
        hint = {
            "claude-pro": (
                "Claude Code OAuth credential not found. Sign in via the "
                "`claude` CLI (or Claude Code desktop) on this machine, "
                "then retry."
            ),
            "chatgpt":    (
                "Codex ChatGPT OAuth credential not found. Run `codex login` "
                "and pick ChatGPT-account auth, then retry."
            ),
        }.get(name, f"OAuth credential not available for {name}")
        raise RuntimeError(hint)

    if name not in config.PROVIDER_KEY_VARS:
        raise RuntimeError(
            f"unknown provider {name!r}; valid: {', '.join(config.ALL_PROVIDERS)}"
        )
    key_var = config.PROVIDER_KEY_VARS[name]

    # Standard resolution: process env → .env file
    found: str | None = None
    if k := os.environ.get(key_var):
        found = k
    else:
        env_path = Path.home() / ".config" / "watchmen" / ".env"
        if env_path.exists():
            prefix = f"{key_var}="
            for line in env_path.read_text().splitlines():
                if line.startswith(prefix):
                    found = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break

    # Give the provider a chance to override or discover the credential
    # from a non-env source — OpenAIProvider uses this to read Codex's
    # stored api-key when OPENAI_API_KEY isn't set, so existing Codex
    # users get credential reuse without an extra paste step.
    from watchmen import providers as _providers
    try:
        prov = _providers.get_provider(name)
        resolved = prov.resolve_api_key(found)
        if resolved:
            return resolved
    except (ValueError, Exception):
        # If the provider hook itself fails, fall back to the env-var
        # value we already found (or raise the standard error below).
        if found:
            return found

    raise RuntimeError(
        f"{key_var} not set. Either `export {key_var}=...` or run "
        f"`watchmen settings api-key --provider {name}` "
        f"(writes ~/.config/watchmen/.env)."
    )


def _backoff_seconds(attempt: int, retry_after: str | None) -> float:
    """Exponential backoff with jitter, honoring Retry-After if the server set one.

    Jitter spreads simultaneous-retry storms when multiple Stage 2 workers all
    hit the same rate-limit at once. Retry-After (seconds form) is treated as
    a floor — we wait at least that long.
    """
    server_hint = 0.0
    if retry_after:
        try:
            server_hint = float(retry_after)
        except ValueError:
            pass
    backoff = (2 ** attempt) + random.uniform(0.0, 1.0)
    return max(backoff, server_hint)


def call_chat(
    client: httpx.Client,
    url: str,
    headers: dict,
    payload: dict,
    *,
    max_retries: int = 4,
    log: Callable[[str], None] | None = None,
    provider_label: str = "llm",
) -> dict:
    """Single chat-completion call with retry on transient failures.

    Retries on httpx.RequestError (connect failures, read timeouts) AND on
    HTTP 408 / 429 / 5xx status codes. Non-retryable errors (4xx other than
    408/429) raise immediately. Exhausting retries raises HTTPStatusError
    for status failures or the last RequestError for network failures.

    `url` and `headers` come from the configured Provider; `payload` is the
    already-translated request body. `provider_label` is used for retry
    logging so multi-provider deployments can tell which backend was slow.
    """
    last_exc: Exception | None = None
    for attempt in range(max_retries):
        try:
            r = client.post(url, headers=headers, json=payload, timeout=300.0)
            if r.status_code in _RETRYABLE_STATUS and attempt < max_retries - 1:
                delay = _backoff_seconds(attempt, r.headers.get("Retry-After"))
                if log:
                    log(f"{provider_label}: HTTP {r.status_code}, retry in {delay:.1f}s (attempt {attempt + 1}/{max_retries})")
                time.sleep(delay)
                continue
            r.raise_for_status()
            return r.json()
        except httpx.RequestError as e:
            last_exc = e
            if attempt == max_retries - 1:
                raise
            delay = _backoff_seconds(attempt, None)
            if log:
                log(f"{provider_label}: {type(e).__name__} ({e}), retry in {delay:.1f}s (attempt {attempt + 1}/{max_retries})")
            time.sleep(delay)
    # All retries exhausted on a retryable status — surface the last response
    # by letting the loop raise via raise_for_status above; this is unreachable.
    raise RuntimeError(f"exhausted retries; last error: {last_exc}")


def call_openrouter(
    client: httpx.Client,
    headers: dict,
    payload: dict,
    *,
    max_retries: int = 4,
    log: Callable[[str], None] | None = None,
) -> dict:
    """Legacy entry point kept for tests / external callers that import this
    symbol directly. Posts to OpenRouter's chat-completions endpoint with
    whatever headers + payload were assembled by the caller. New code should
    use `chat_call()` which handles provider routing + translation."""
    return call_chat(
        client, OPENROUTER_URL, headers, payload,
        max_retries=max_retries, log=log, provider_label="openrouter",
    )


def chat_call(
    client: httpx.Client,
    messages: list[dict],
    *,
    model: str,
    tools: list[dict] | None = None,
    provider: str | None = None,
    api_key: str | None = None,
    agent_name: str = "watchmen",
    max_retries: int = 4,
    log: Callable[[str], None] | None = None,
    **extra_payload,
) -> dict:
    """One-shot chat-completions call with provider routing baked in.

    Resolves the active provider (or uses `provider` override), pulls the
    matching API key, translates the OpenAI chat-completions request into
    the provider's wire format, retries on transient failures, and
    translates the response back into the canonical OpenAI shape so callers
    can read `data["choices"][0]["message"]["content"]` regardless of which
    backend served the request.

    Use this for non-Agent callsites (analyze.py per-day loop, insights
    digest, one-off LLM helpers). The Agent class handles the multi-turn
    tool-dispatch loop on top of the same plumbing.

    `extra_payload` is merged into the request body AFTER translation, so
    callers can pass `temperature=0.3`, `max_tokens=2000`, etc. for both
    OpenAI-shape providers and Anthropic (which accepts the same top-level
    keys).
    """
    from watchmen import config

    name = provider or config.active_provider()
    prov = _providers.get_provider(name)
    key = api_key or load_api_key(name)

    headers = prov.headers(key, agent_name=agent_name)
    body = prov.translate_request(model=model, messages=messages, tools=tools or [])
    body = prov.apply_extra_payload(body, extra_payload)

    # Providers with non-chat-completions transports (e.g. streaming-only
    # Responses API for ChatGPT OAuth) override `call()`. We let them own
    # the HTTP round-trip + protocol handling; everything else still goes
    # through the standard call_chat path.
    if getattr(prov, "custom_transport", False):
        raw = prov.call(client, prov.endpoint, headers, body,
                        max_retries=max_retries, log=log, label=name)
    else:
        raw = call_chat(
            client, prov.endpoint, headers, body,
            max_retries=max_retries, log=log, provider_label=name,
        )
    return prov.translate_response(raw)


def _turn_cost(model: str, usage: dict) -> float:
    """Best-effort per-turn cost in USD from an OpenRouter usage block.

    Returns 0.0 if pricing data isn't available — the budget ceiling won't
    fire spuriously when we can't price a model accurately. Imported lazily
    so module import doesn't trigger a 30s OpenRouter /models fetch.
    """
    if not usage or not model:
        return 0.0
    try:
        from watchmen.model_prices import turn_cost_usd
    except Exception:
        return 0.0
    prompt = int(usage.get("prompt_tokens", 0) or 0)
    completion = int(usage.get("completion_tokens", 0) or 0)
    cache_read = int(((usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0))
    # OpenRouter folds cached tokens into prompt_tokens; subtract so we don't double-count.
    fresh_input = max(prompt - cache_read, 0)
    try:
        return turn_cost_usd(model, fresh_input, 0, 0, cache_read, completion)
    except Exception:
        return 0.0


class CostCeilingReached(RuntimeError):
    """Raised when an Agent run crosses its `max_cost_usd` budget mid-loop."""


class Agent:
    def __init__(
        self,
        name: str,
        model: str,
        system_prompt: str,
        tool_specs: list[dict],
        tool_handlers: dict[str, Callable],
        terminal_tool: str,
        client: httpx.Client | None = None,
        api_key: str | None = None,
        log_path: Path | None = None,
        result_max_chars: int = 30000,
        max_cost_usd: float | None = None,
        provider: str | None = None,
    ):
        self.name = name
        self.model = model
        self.system_prompt = system_prompt
        self.tool_specs = tool_specs
        self.tool_handlers = tool_handlers
        self.terminal_tool = terminal_tool
        self.client = client or httpx.Client(timeout=300.0)

        # Lazy import — agent.py is imported eagerly in many places and
        # config.py imports providers.py; keeping this off the module-level
        # import path stops a circular import in some test orderings.
        from watchmen import config

        self.provider_name = provider or config.active_provider()
        self.provider = _providers.get_provider(self.provider_name)
        self.api_key = api_key or load_api_key(self.provider_name)
        self.headers = self.provider.headers(self.api_key, agent_name=name)
        self.endpoint = self.provider.endpoint

        self.log_path = log_path
        self.result_max_chars = result_max_chars
        # Per-run cumulative cost ceiling. None = unlimited (the historical
        # behavior). When set, the loop aborts with CostCeilingReached as
        # soon as the running total crosses the threshold — protects against
        # runaway agents in scheduled curator runs.
        self.max_cost_usd = max_cost_usd
        self.cumulative_cost_usd = 0.0

    def _log(self, msg: str) -> None:
        if self.log_path:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")

    def run(self, user_msg: str, max_iter: int = 24) -> tuple[dict, list[dict]]:
        messages: list[dict] = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": user_msg},
        ]
        terminal_args: dict = {}
        for it in range(max_iter):
            payload = self.provider.translate_request(
                model=self.model,
                messages=messages,
                tools=self.tool_specs,
            )
            # See comment in chat_call() — providers with streaming
            # transports (ChatGPT/Codex Responses API) own the HTTP round
            # trip themselves. Others go through the standard chat_call.
            if getattr(self.provider, "custom_transport", False):
                raw = self.provider.call(
                    self.client, self.endpoint, self.headers, payload,
                    log=self._log, label=self.provider_name,
                )
            else:
                raw = call_chat(
                    self.client,
                    self.endpoint,
                    self.headers,
                    payload,
                    log=self._log,
                    provider_label=self.provider_name,
                )
            data = self.provider.translate_response(raw)

            # Track per-turn cost; honor the budget ceiling before doing
            # any tool dispatch so we don't burn another iteration.
            turn_cost = _turn_cost(self.model, data.get("usage") or {})
            self.cumulative_cost_usd += turn_cost
            if self.max_cost_usd is not None and self.cumulative_cost_usd >= self.max_cost_usd:
                self._log(
                    f"[{self.name}] cost ceiling reached: ${self.cumulative_cost_usd:.4f} "
                    f">= ${self.max_cost_usd:.4f}; aborting loop"
                )
                print(
                    f"  [agent={self.name}] cost ceiling reached "
                    f"(${self.cumulative_cost_usd:.4f} >= ${self.max_cost_usd:.4f}); aborting",
                    file=sys.stderr,
                )
                break

            msg = data["choices"][0]["message"]
            clean = {"role": "assistant", "content": msg.get("content") or ""}
            if msg.get("tool_calls"):
                clean["tool_calls"] = msg["tool_calls"]
            messages.append(clean)

            tool_calls = msg.get("tool_calls") or []
            if not tool_calls:
                break

            ended = False
            for tc in tool_calls:
                fn = tc["function"]["name"]
                try:
                    args = json.loads(tc["function"].get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                self._log(f"[{self.name}][iter {it}] {fn}({list(args.keys())})")

                if fn == self.terminal_tool:
                    terminal_args = args
                    messages.append({"role": "tool", "tool_call_id": tc["id"], "content": "ok"})
                    ended = True
                    continue

                handler = self.tool_handlers.get(fn)
                if handler is None:
                    result = f"unknown tool: {fn}"
                else:
                    try:
                        out = handler(**args)
                        result = out if isinstance(out, str) else json.dumps(out, default=str)
                    except Exception as e:
                        result = f"ERROR: {type(e).__name__}: {e}"
                messages.append({"role": "tool", "tool_call_id": tc["id"], "content": result[:self.result_max_chars]})

            if ended:
                break

        return terminal_args, messages
