"""Tests for the provider abstraction.

Covers the three pieces that determine whether multi-provider routing
actually works end-to-end:

1. `config.active_provider()` selection — explicit env var, auto-detect by
   key presence, default fallback. Breaking this would silently route
   curator runs to the wrong backend on installs that use multiple keys.

2. Provider request/response translation — especially Anthropic's Messages
   API ↔ OpenAI chat-completions translation, which is the riskiest part
   of the refactor. We assert the wire shapes for both directions so a
   regression in the Anthropic adapter is caught in unit tests, not
   discovered halfway through a curator run.

3. `agent.chat_call()` end-to-end — dispatches through the provider, posts
   to its endpoint, parses the response. We stub httpx so nothing hits the
   network and assert the right URL + headers + body shape were used.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from watchmen import agent, config, providers


# ─── Provider selection ────────────────────────────────────────────────────


def test_active_provider_explicit_env_wins(monkeypatch):
    """An explicit WATCHMEN_PROVIDER beats the auto-detect heuristic. This
    is the override path power users will reach for when they have keys
    for multiple providers on disk."""
    monkeypatch.setenv("WATCHMEN_PROVIDER", "anthropic")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-xxx")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-yyy")
    assert config.active_provider() == "anthropic"


def test_active_provider_ignores_unknown_explicit(monkeypatch, tmp_path):
    """A WATCHMEN_PROVIDER set to a junk value (typo, stale config) falls
    back to the auto-detect path rather than crashing every command —
    keeps the user out of a `watchmen <anything>` outage if their .env
    drifts."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("WATCHMEN_PROVIDER", "gemini")  # not supported
    monkeypatch.setenv("OPENAI_API_KEY", "sk-real")
    # Unknown explicit value → auto-detect picks first present-key in priority order
    assert config.active_provider() == "openai"


def test_active_provider_auto_detect_priority(monkeypatch, tmp_path):
    """Auto-detect prefers OpenRouter when present (backward compat with
    existing OPENROUTER_API_KEY-only installs), then OpenAI, then Anthropic.
    Critical for the upgrade path — pre-0.7 users must not switch backends
    by accident when the new provider abstraction lands."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("WATCHMEN_PROVIDER", raising=False)

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-only")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    assert config.active_provider() == "anthropic"

    monkeypatch.setenv("OPENAI_API_KEY", "sk-oa")
    assert config.active_provider() == "openai"  # openai outranks anthropic

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or")
    assert config.active_provider() == "openrouter"  # openrouter outranks both


def test_active_provider_default_when_no_keys(monkeypatch, tmp_path):
    """Empty install (no env vars, no .env file) defaults to openrouter so
    the first-run wizard's prompts still match what the docs describe."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for var in ("WATCHMEN_PROVIDER", "OPENROUTER_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    assert config.active_provider() == "openrouter"


def test_provider_key_returns_none_for_unset(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert config.provider_key("anthropic") is None


def test_provider_key_returns_none_for_unknown_provider():
    assert config.provider_key("gemini") is None


def test_default_model_respects_explicit_override(monkeypatch):
    """WATCHMEN_DEFAULT_MODEL lets users pin a specific model regardless of
    provider — the override path most likely to be used in tests + CI."""
    monkeypatch.setenv("WATCHMEN_DEFAULT_MODEL", "gpt-5")
    monkeypatch.setenv("WATCHMEN_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    assert config.default_model() == "gpt-5"


def test_default_model_falls_back_to_provider_default(monkeypatch, tmp_path):
    """Without an override, each provider's `default_model` attribute drives
    the choice — so `watchmen analyze` picks deepseek on openrouter,
    gpt-5-mini on openai, claude-haiku on anthropic without user input."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("WATCHMEN_DEFAULT_MODEL", raising=False)
    monkeypatch.setenv("WATCHMEN_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")
    assert config.default_model() == providers.get_provider("anthropic").default_model


# ─── Provider registry ─────────────────────────────────────────────────────


def test_get_provider_known():
    assert providers.get_provider("openrouter").name == "openrouter"
    assert providers.get_provider("openai").name == "openai"
    assert providers.get_provider("anthropic").name == "anthropic"


def test_get_provider_unknown_raises():
    with pytest.raises(ValueError, match="unknown provider"):
        providers.get_provider("gemini")


# ─── OpenRouter / OpenAI: identity translation ─────────────────────────────


def test_openrouter_translate_request_is_identity():
    """OpenRouter and OpenAI both speak the chat-completions wire format —
    no translation needed. The translate_request default is identity; a
    regression would silently break the OpenRouter path for every user."""
    prov = providers.get_provider("openrouter")
    body = prov.translate_request(
        model="deepseek/deepseek-v4-flash",
        messages=[{"role": "user", "content": "hi"}],
        tools=[{"type": "function", "function": {"name": "noop"}}],
    )
    assert body == {
        "model": "deepseek/deepseek-v4-flash",
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [{"type": "function", "function": {"name": "noop"}}],
    }


def test_openrouter_headers_carry_app_attribution():
    """OpenRouter app-attribution headers (HTTP-Referer + X-Title) appear
    in the developer dashboard. We want watchmen runs to be identifiable
    so traffic spikes get attributed to the right tool."""
    prov = providers.get_provider("openrouter")
    h = prov.headers("sk-or-test", agent_name="analyst")
    assert h["Authorization"] == "Bearer sk-or-test"
    assert h["HTTP-Referer"] == "https://github.com/firstbatchxyz/watchmen"
    assert h["X-Title"] == "watchmen:analyst"


def test_openai_headers_minimal():
    """OpenAI ignores referer/title headers; sending them is harmless but
    cluttery. We keep its header set minimal so production logs are clean."""
    prov = providers.get_provider("openai")
    h = prov.headers("sk-test")
    assert h == {"Authorization": "Bearer sk-test", "Content-Type": "application/json"}


# ─── Anthropic: Messages API translation ───────────────────────────────────


def test_anthropic_headers_use_x_api_key():
    """Anthropic uses `x-api-key`, not `Authorization: Bearer`. Getting this
    wrong is the single most common source of 401s when migrating between
    providers — explicit test so a regression here can't slip through."""
    prov = providers.get_provider("anthropic")
    h = prov.headers("sk-ant-test")
    assert h["x-api-key"] == "sk-ant-test"
    assert h["anthropic-version"] == "2023-06-01"
    assert "Authorization" not in h


def test_anthropic_request_lifts_system_to_top_level():
    """Anthropic's Messages API rejects `system` messages inside the
    `messages` list — `system` must be a top-level field. The translator
    moves it; if this regresses, every analyst run on Anthropic 400s."""
    prov = providers.get_provider("anthropic")
    body = prov.translate_request(
        model="claude-haiku-4-5-20251001",
        messages=[
            {"role": "system", "content": "you are an analyst"},
            {"role": "user", "content": "summarize today"},
        ],
        tools=[],
    )
    assert body["system"] == "you are an analyst"
    assert body["messages"] == [{"role": "user", "content": "summarize today"}]
    assert "max_tokens" in body  # required field — without it Anthropic rejects


def test_anthropic_request_translates_tool_schema():
    """OpenAI tools use `{"function": {"name", "parameters"}}`; Anthropic
    uses `{"name", "input_schema"}`. The translator unwraps and renames —
    a regression here breaks every curator skill-finder call."""
    prov = providers.get_provider("anthropic")
    body = prov.translate_request(
        model="claude-haiku-4-5-20251001",
        messages=[{"role": "user", "content": "go"}],
        tools=[{
            "type": "function",
            "function": {
                "name": "search",
                "description": "search the corpus",
                "parameters": {"type": "object", "properties": {"q": {"type": "string"}}},
            },
        }],
    )
    assert body["tools"] == [{
        "name": "search",
        "description": "search the corpus",
        "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}},
    }]


def test_anthropic_request_translates_assistant_tool_calls_to_blocks():
    """The agent loop replays prior turns. An assistant message with
    OpenAI-style `tool_calls` must convert to Anthropic content blocks on
    each replay, otherwise multi-turn tool dispatch never converges."""
    prov = providers.get_provider("anthropic")
    body = prov.translate_request(
        model="m",
        messages=[
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "thinking...", "tool_calls": [{
                "id": "call_1",
                "type": "function",
                "function": {"name": "search", "arguments": json.dumps({"q": "foo"})},
            }]},
            {"role": "tool", "tool_call_id": "call_1", "content": "result"},
        ],
        tools=[],
    )
    asst = body["messages"][1]
    assert asst["role"] == "assistant"
    assert any(b["type"] == "text" and b["text"] == "thinking..." for b in asst["content"])
    tool_use = next(b for b in asst["content"] if b["type"] == "tool_use")
    assert tool_use["id"] == "call_1"
    assert tool_use["name"] == "search"
    assert tool_use["input"] == {"q": "foo"}

    # Tool result becomes a user message with a tool_result block — required
    # so the model can correlate the result with its earlier call.
    tool_msg = body["messages"][2]
    assert tool_msg["role"] == "user"
    assert tool_msg["content"][0]["type"] == "tool_result"
    assert tool_msg["content"][0]["tool_use_id"] == "call_1"


def test_anthropic_response_folds_blocks_into_openai_shape():
    """Agent.run reads `choices[0].message.content` and `.tool_calls` —
    the response translator must reshape Anthropic's content-block list
    into that flat OpenAI form, or the entire dispatch loop breaks."""
    prov = providers.get_provider("anthropic")
    raw = {
        "id": "msg_1",
        "model": "claude-haiku-4-5-20251001",
        "content": [
            {"type": "text", "text": "I'll search."},
            {"type": "tool_use", "id": "tu_1", "name": "search", "input": {"q": "foo"}},
        ],
        "stop_reason": "tool_use",
        "usage": {"input_tokens": 12, "output_tokens": 5, "cache_read_input_tokens": 3},
    }
    out = prov.translate_response(raw)
    msg = out["choices"][0]["message"]
    assert msg["content"] == "I'll search."
    assert msg["tool_calls"][0]["function"]["name"] == "search"
    assert json.loads(msg["tool_calls"][0]["function"]["arguments"]) == {"q": "foo"}
    assert out["usage"]["prompt_tokens"] == 12
    assert out["usage"]["completion_tokens"] == 5
    assert out["usage"]["prompt_tokens_details"]["cached_tokens"] == 3


def test_anthropic_response_no_tool_calls_returns_none():
    """When the model only produces text (no tool use), tool_calls must be
    None (not empty list) — agent.run uses truthiness to decide whether
    to dispatch tools, and an empty list would still be falsy but the
    OpenAI wire format conventionally omits the field entirely."""
    prov = providers.get_provider("anthropic")
    out = prov.translate_response({
        "model": "m",
        "content": [{"type": "text", "text": "all done"}],
        "usage": {"input_tokens": 1, "output_tokens": 2},
    })
    assert out["choices"][0]["message"]["tool_calls"] is None


# ─── End-to-end: chat_call dispatches through the right provider ──────────


class _StubResponse:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload
        self.headers = {}
        self.text = json.dumps(payload)

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx
            raise httpx.HTTPStatusError("err", request=None, response=self)


def test_chat_call_uses_openai_endpoint_when_provider_openai(monkeypatch):
    """End-to-end smoke for the OpenAI direct path. We stub httpx so no
    real call goes out, and assert the URL + auth header match OpenAI's
    public API rather than OpenRouter's."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-oa")
    monkeypatch.setenv("WATCHMEN_PROVIDER", "openai")
    client = MagicMock()
    client.post.return_value = _StubResponse(200, {
        "choices": [{"message": {"content": "ok", "tool_calls": None}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        "model": "gpt-5-mini",
    })

    out = agent.chat_call(client, [{"role": "user", "content": "hi"}], model="gpt-5-mini")

    assert client.post.called
    args, kwargs = client.post.call_args
    assert args[0] == "https://api.openai.com/v1/chat/completions"
    assert kwargs["headers"]["Authorization"] == "Bearer sk-test-oa"
    assert out["choices"][0]["message"]["content"] == "ok"


def test_chat_call_uses_anthropic_endpoint_when_provider_anthropic(monkeypatch):
    """End-to-end smoke for the Anthropic direct path — checks the URL,
    the x-api-key header (NOT Authorization), and that the response
    translator was applied before returning to the caller."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("WATCHMEN_PROVIDER", "anthropic")
    client = MagicMock()
    client.post.return_value = _StubResponse(200, {
        "model": "claude-haiku-4-5-20251001",
        "content": [{"type": "text", "text": "hello"}],
        "usage": {"input_tokens": 4, "output_tokens": 2},
    })

    out = agent.chat_call(client, [{"role": "user", "content": "hi"}], model="claude-haiku-4-5-20251001")

    args, kwargs = client.post.call_args
    assert args[0] == "https://api.anthropic.com/v1/messages"
    assert kwargs["headers"]["x-api-key"] == "sk-ant-test"
    assert "Authorization" not in kwargs["headers"]
    # Response went through Anthropic translator → OpenAI shape
    assert out["choices"][0]["message"]["content"] == "hello"


# ─── settings model command ───────────────────────────────────────────────


def test_settings_model_persists_and_clears(monkeypatch, tmp_path, capsys):
    """`watchmen settings model <name>` should persist the override and
    `--clear` should remove it. This is the menu-driven equivalent of
    `export WATCHMEN_DEFAULT_MODEL=...` — if the persistence breaks,
    daemon-scheduled runs silently revert to the provider default."""
    from watchmen import cli

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("WATCHMEN_DEFAULT_MODEL", raising=False)
    monkeypatch.setenv("WATCHMEN_PROVIDER", "openrouter")

    # Set
    rc = cli.main(["settings", "model", "gpt-5"])
    assert rc == 0
    assert config.read_env_var("WATCHMEN_DEFAULT_MODEL") == "gpt-5"
    assert config.default_model() == "gpt-5"

    # Clear
    rc = cli.main(["settings", "model", "--clear"])
    assert rc == 0
    assert config.read_env_var("WATCHMEN_DEFAULT_MODEL") is None
    # Falls back to the active provider's default
    assert config.default_model() == providers.get_provider("openrouter").default_model


def test_clear_env_var_returns_false_for_missing_key(tmp_path, monkeypatch):
    """`clear_env_var` should distinguish "nothing to clear" from "cleared".
    The settings menu uses this to print "no override was set" vs the
    success message — getting this confused would mislead users."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    # No .env file at all
    assert config.clear_env_var("NOT_THERE") is False
    # File exists but key isn't in it
    config.write_env_var("OTHER", "x")
    assert config.clear_env_var("NOT_THERE") is False
    # Key present → returns True
    assert config.clear_env_var("OTHER") is True
    assert config.read_env_var("OTHER") is None


def test_settings_menu_back_and_cancel_never_pass_through_as_data():
    """Regression: questionary 2.x interprets `Choice("Back", value=None)`
    as "use the title as the value", so picking Back leaked the literal
    string "Back" into downstream code that expected a project key /
    provider name. The fix uses explicit sentinel values; this test scans
    the source to make sure no future `value=None` slips back in on a
    nav-action Choice."""
    from pathlib import Path as _Path
    src = (_Path(__file__).parent.parent / "src" / "watchmen" / "commands" / "settings_menu.py").read_text()
    # Strip line comments before scanning so the regression-explainer
    # comment in the module doesn't trip the check.
    code_only = "\n".join(
        line.split("#", 1)[0] for line in src.splitlines()
    )
    assert 'value=None' not in code_only, (
        "settings_menu has a Choice with value=None — questionary treats "
        "that as 'use title as value', which leaks the literal nav-label "
        "(e.g. 'Back') into downstream handlers. Use _BACK / _CANCEL instead."
    )


def test_interactive_settings_falls_back_in_non_tty(monkeypatch, capsys):
    """Running `watchmen settings` with stdin/stdout piped (CI, scripts)
    must NOT block on questionary — instead print the flat-subcommand
    cheatsheet so users aren't stuck."""
    from watchmen.commands import settings_menu
    # Force the non-TTY branch
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    rc = settings_menu.run_interactive_settings()
    assert rc == 0
    out = capsys.readouterr().out
    assert "non-interactive shell detected" in out
    # Must surface every flat subcommand, otherwise the fallback is useless
    for sub in ("list", "show", "set ", "api-key", "provider", "model", "port"):
        assert sub in out, f"fallback missing `settings {sub}`"


# ─── original chat_call test (kept below the new ones) ────────────────────


def test_viewer_settings_page_exposes_provider_and_model_sections(monkeypatch, tmp_path):
    """The /settings page renders one card per provider, a provider switch
    form, and a default-model panel with set/clear actions. Catches template
    regressions and missing context fields exposed by `get_settings()`."""
    from fastapi.testclient import TestClient
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("WATCHMEN_PROVIDER", "openrouter")
    from watchmen.viewer.server import app
    r = TestClient(app).get("/settings")
    assert r.status_code == 200
    html = r.text
    for needle in (
        'name="provider"',                # per-provider key forms carry hidden provider field
        'OpenRouter API key',
        'OpenAI API key',
        'Anthropic API key',
        'action="/settings/provider"',
        'action="/settings/model"',
        'name="action" value="set"',      # model form's two submit buttons
        'name="action" value="clear"',
    ):
        assert needle in html, f"settings page missing: {needle}"


def test_viewer_settings_model_post_set_and_clear(monkeypatch, tmp_path):
    """POST /settings/model with action=set persists the override; action=clear
    removes it. Regression guard against the form's two-submit-button
    pattern silently degrading (e.g. if a template change drops the
    action input names)."""
    from fastapi.testclient import TestClient
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("WATCHMEN_PROVIDER", "openrouter")
    monkeypatch.delenv("WATCHMEN_DEFAULT_MODEL", raising=False)
    from watchmen.viewer.server import app
    client = TestClient(app)

    # Set
    r = client.post(
        "/settings/model",
        data={"value": "gpt-5", "action": "set"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "ok:" in r.headers["location"]
    assert config.read_env_var("WATCHMEN_DEFAULT_MODEL") == "gpt-5"

    # Clear
    r = client.post(
        "/settings/model",
        data={"action": "clear"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "ok:" in r.headers["location"]
    assert config.read_env_var("WATCHMEN_DEFAULT_MODEL") is None

    # Clear with nothing to clear — should err
    r = client.post(
        "/settings/model",
        data={"action": "clear"},
        follow_redirects=False,
    )
    assert "err:" in r.headers["location"]


# ─── reset command ────────────────────────────────────────────────────────


def _build_fake_project(root: Path, project_key: str, *, with_pins: bool = True) -> dict:
    """Plant the files cmd_reset would clear so we can exercise the wipe
    logic against a realistic on-disk layout. Returns a paths dict so
    tests can assert presence/absence later."""
    bdir = root / "bundles" / project_key
    adir = root / "analyses" / project_key
    (bdir / "skills" / "ship-pr").mkdir(parents=True)
    (bdir / "skills" / "ship-pr" / "SKILL.md").write_text("# ship-pr\n")
    (bdir / "_pending").mkdir()
    (bdir / "_pending" / "marker").write_text("x")
    (bdir / "CLAUDE.md").write_text("# CLAUDE\n")
    (bdir / "AGENTS.md").write_text("# AGENTS\n")
    (bdir / "_candidates.json").write_text("[]")
    (bdir / "_curation_log.md").write_text("log\n")
    (bdir / "_index.md").write_text("idx\n")
    adir.mkdir(parents=True)
    (adir / "_running.md").write_text("thesis\n")
    (adir / "2026-05-19.md").write_text("day\n")
    if with_pins:
        (bdir / "_pinned.json").write_text('["ship-pr"]')
        (bdir / "_blocklist.json").write_text('["bad-skill"]')
    return {"bdir": bdir, "adir": adir}


def test_reset_clears_artifacts_and_state(monkeypatch, tmp_path):
    """`watchmen reset <project>` must remove analyses + bundles (except
    pins/blocklist) and clear the state.db last-ran markers. End-to-end
    test against a real on-disk layout — regression catches a half-finished
    reset that leaves stale CLAUDE.md or stale `last_curator_run`
    timestamps behind."""
    from watchmen import cli, state

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(cli, "ROOT", tmp_path)

    # Init state with a tracked project that has run markers populated
    state.init_db()
    state.track_project("kai", str(tmp_path / "repo"), threshold=30)
    state.update_project(
        "kai",
        last_analyst_day="2026-05-18",
        last_analyst_run="2026-05-18T12:00:00",
        last_curator_run="2026-05-18T13:00:00",
        last_curator_skill_count=7,
    )

    paths = _build_fake_project(tmp_path, "kai")

    rc = cli.main(["reset", "kai", "--yes"])
    assert rc == 0

    # Artifacts removed
    assert not paths["bdir"].joinpath("skills").exists()
    assert not paths["bdir"].joinpath("_pending").exists()
    assert not paths["bdir"].joinpath("CLAUDE.md").exists()
    assert not paths["bdir"].joinpath("_candidates.json").exists()
    assert not paths["adir"].exists()

    # Pins / blocklist preserved by default
    assert paths["bdir"].joinpath("_pinned.json").exists()
    assert paths["bdir"].joinpath("_blocklist.json").exists()

    # State markers reset
    proj = state.get_project("kai")
    assert proj["last_analyst_day"] is None
    assert proj["last_analyst_run"] is None
    assert proj["last_curator_run"] is None
    assert (proj["last_curator_skill_count"] or 0) == 0
    # Config preserved. Use os.sep so the assertion holds on Windows
    # (path ends with `\repo`) as well as POSIX (`/repo`).
    import os as _os
    assert proj["source_repo"].endswith(_os.sep + "repo")
    assert proj["threshold_new_prompts"] == 30


def test_reset_wipe_all_removes_pins_and_blocklist(monkeypatch, tmp_path):
    """`--wipe-all` is the full-nuke escape hatch — it must also clear the
    user-steering files. Without this flag those survive (covered by the
    test above); regression here catches the inverse: wipe-all silently
    leaving them behind."""
    from watchmen import cli, state

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    state.init_db()
    state.track_project("kai", str(tmp_path / "repo"))
    paths = _build_fake_project(tmp_path, "kai")

    rc = cli.main(["reset", "kai", "--yes", "--wipe-all"])
    assert rc == 0
    assert not paths["bdir"].joinpath("_pinned.json").exists()
    assert not paths["bdir"].joinpath("_blocklist.json").exists()


def test_reset_dry_run_touches_nothing(monkeypatch, tmp_path):
    """`--dry-run` must list the same target set the real run would touch
    but never delete anything. Critical guard — users will reach for this
    flag when they're nervous, and a silent destructive behavior here
    would be a trust-breaker."""
    from watchmen import cli, state

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    state.init_db()
    state.track_project("kai", str(tmp_path / "repo"))
    state.update_project("kai", last_analyst_day="2026-05-18")
    paths = _build_fake_project(tmp_path, "kai")

    rc = cli.main(["reset", "kai", "--dry-run"])
    assert rc == 0

    # Everything still on disk after dry-run
    assert paths["bdir"].joinpath("skills").exists()
    assert paths["bdir"].joinpath("CLAUDE.md").exists()
    assert paths["adir"].joinpath("_running.md").exists()
    # State markers still in place
    assert state.get_project("kai")["last_analyst_day"] == "2026-05-18"


def test_reset_rejects_untracked_project(monkeypatch, tmp_path, capsys):
    """A typo in the project key shouldn't silently succeed with a
    'nothing to do' message — we want a clear error so users don't think
    their data got wiped when nothing was actually targeted."""
    from watchmen import cli, state

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    state.init_db()
    rc = cli.main(["reset", "not-a-real-project", "--yes"])
    assert rc == 1
    assert "not tracked" in capsys.readouterr().out


def test_chat_call_extra_payload_overrides_get_merged(monkeypatch):
    """Caller-supplied kwargs (temperature, max_tokens) flow into the request
    body for both shapes — insights.py relies on this to pin temperature=0.3
    regardless of provider."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.setenv("WATCHMEN_PROVIDER", "openrouter")
    client = MagicMock()
    client.post.return_value = _StubResponse(200, {
        "choices": [{"message": {"content": "x"}}],
        "usage": {},
    })

    agent.chat_call(
        client,
        [{"role": "user", "content": "hi"}],
        model="deepseek/deepseek-v4-flash",
        temperature=0.3,
        max_tokens=500,
    )
    _, kwargs = client.post.call_args
    body = kwargs["json"]
    assert body["temperature"] == 0.3
    assert body["max_tokens"] == 500


def test_chatgpt_apply_extra_payload_drops_chat_completions_kwargs():
    """Regression for the distill 400 bug: callers (skillmesh's semantic
    judge) pass chat-completions kwargs that are illegal on the
    codex/responses OAuth endpoint. Probed empirically against
    `chatgpt.com/backend-api/codex/responses` — the endpoint rejects
    `max_output_tokens` outright with
    `{"detail":"Unsupported parameter: max_output_tokens"}`, so the
    chatgpt provider must drop `max_tokens` entirely (not rename it)
    along with `temperature` and `response_format`."""
    prov = providers.get_provider("chatgpt")
    body = prov.translate_request(
        model="gpt-5.4-mini",
        messages=[{"role": "user", "content": "hi"}],
        tools=[],
    )
    body = prov.apply_extra_payload(body, {
        "temperature": 0,
        "max_tokens": 900,
        "response_format": {"type": "json_object"},
        "max_output_tokens": 1200,
    })
    assert "temperature" not in body
    assert "response_format" not in body
    assert "max_tokens" not in body
    assert "max_output_tokens" not in body


def test_chatgpt_apply_extra_payload_passes_unknown_kwargs_through():
    """Unrecognized kwargs fall through unchanged so future Responses-API
    params (e.g. `parallel_tool_calls`) work without another provider
    override."""
    prov = providers.get_provider("chatgpt")
    body = prov.translate_request(
        model="gpt-5.4-mini",
        messages=[{"role": "user", "content": "hi"}],
        tools=[],
    )
    body = prov.apply_extra_payload(body, {"parallel_tool_calls": False})
    assert body["parallel_tool_calls"] is False


def test_default_apply_extra_payload_skips_none_values():
    """`None`-valued kwargs are dropped, matching the pre-refactor merge
    behavior. chat_call relies on this to support optional caller params."""
    prov = providers.get_provider("openrouter")
    body = {"model": "m", "messages": []}
    body = prov.apply_extra_payload(body, {"temperature": None, "max_tokens": 100})
    assert "temperature" not in body
    assert body["max_tokens"] == 100
