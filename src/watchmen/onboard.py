"""Interactive setup wizard for fresh installs.

Walks a new user through the full pipeline: ingest → select projects → track →
analyze → curate → install daemon. Designed for teammates who have months of
~/.claude/projects/ history and have just installed the watchmen engine.

Idempotent: re-running on an already-onboarded machine skips the steps that are
already done and offers to track additional projects.

Usage: `watchmen onboard` (or `kai-mac onboard`).
"""

import os
import shutil
import subprocess
import sys
import webbrowser
from pathlib import Path

from watchmen import config
from watchmen.paths import CORPUS_DB

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table
from rich.text import Text

ROOT = Path(__file__).parent


def _exec_name() -> str:
    """The script entry point — 'watchmen' on the canonical install, 'kai-mac'
    on the legacy local install. Determined by the package name."""
    return Path(sys.argv[0]).name or "watchmen"


def _step(console: Console, n: int, total: int, title: str) -> None:
    console.print()
    console.rule(f"[bold cyan]Step {n}/{total}[/]  •  [bold]{title}[/]")
    console.print()


def show_welcome(console: Console, total_steps: int) -> None:
    from watchmen import banner
    banner.render(console)
    body = Text.from_markup(
        "[bold]watchmen[/] mines your coding-agent session history\n"
        "(Claude Code, Codex, pi.dev), ships skill bundles + a workspace brief\n"
        "per project, and surfaces them back into your sessions via a plugin.\n\n"
        "This wizard walks through setup end-to-end. You can stop at any\n"
        "confirmation gate without leaving partial state behind.\n\n"
        f"[dim]{total_steps} steps. Most takes a few seconds; the LLM passes (analyze + curate)\n"
        f"are the only slow ones — see cost estimate before they run.[/]"
    )
    console.print(Panel(body, title="onboard", border_style="cyan"))


# Per-provider key signup URL surfaced when prompting. None ⇒ no URL shown.
_PROVIDER_SIGNUP = {
    "openrouter": ("https://openrouter.ai/keys", "credit pre-load not required for deepseek-v4-flash"),
    "openai":     ("https://platform.openai.com/api-keys", "billed per-token against your OpenAI org"),
    "anthropic":  ("https://console.anthropic.com/settings/keys", "billed per-token against your Anthropic org"),
}

# Key-format hints used to gently catch paste errors without being rigid —
# users who really paste an unusual key are still given a "save anyway?" path.
_PROVIDER_KEY_PREFIXES = {
    "openrouter": ("sk-or-", "sk_"),
    "openai":     ("sk-",),
    "anthropic":  ("sk-ant-",),
}


def _have_any_provider_key() -> bool:
    """True iff at least one provider has a credential available — env-var
    key or OAuth credential discovered from Claude Code / Codex."""
    from watchmen import config as _config
    return any(_config.provider_key(p) for p in _config.ALL_PROVIDERS)


def _prompt_provider_choice(console: Console) -> str:
    """Pick the LLM provider the wizard will configure a credential for.
    Returns the canonical provider name.

    Surfaces OAuth options (claude-pro, chatgpt) when a matching local
    credential is already present, with subscription-quota framing.
    Falls back to env-var providers (openrouter / openai / anthropic)
    otherwise."""
    from watchmen import config as _config
    from watchmen.credentials import (
        ClaudeCodeCredentials,
        CodexCredentials,
        is_claude_code_available,
    )

    options = list(_config.PROVIDER_KEY_VARS)
    default = "openrouter"

    console.print("[bold]Which LLM provider would you like to use?[/]")
    console.print(
        "  [cyan]openrouter[/]  cheapest default (deepseek-v4-flash), one key gets you many models\n"
        "  [cyan]openai[/]      use your OpenAI API key directly\n"
        "  [cyan]anthropic[/]   use your Anthropic API key directly"
    )

    # Detect OAuth availability — only surface as a wizard option when the
    # credential is actually present on disk, otherwise we'd offer choices
    # that immediately need re-routing to a sign-in step.
    cc_creds = ClaudeCodeCredentials.read() if is_claude_code_available() else None
    if cc_creds and cc_creds.has_inference_scope() and not cc_creds.is_expired():
        options.append("claude-pro")
        default = "claude-pro"  # prefer "free" subscription auth if available
        plan = cc_creds.subscription_type or "unknown"
        console.print(
            f"  [magenta]claude-pro[/]  use your existing Claude {plan} subscription "
            f"(detected via Claude Code) — [bold]no extra API spend[/]"
        )
    cx_creds = CodexCredentials.read()
    if cx_creds and cx_creds.mode == "chatgpt":
        options.append("chatgpt")
        console.print(
            "  [magenta]chatgpt[/]    use your ChatGPT subscription via Codex (experimental — "
            "limited model whitelist, mandatory streaming)"
        )

    choice = Prompt.ask(
        "Provider",
        choices=options,
        default=default,
    )
    return choice


def _prompt_for_provider_key(console: Console, provider_name: str) -> bool:
    """Prompt for a credential for `provider_name` and persist the active-
    provider selection. Returns True if a credential is now in scope.

    For OAuth providers (claude-pro / chatgpt) there's nothing to paste —
    the credential is already on disk, we just confirm the choice and
    flip the active provider. For env-var providers, the legacy key-paste
    flow runs."""
    from watchmen import config as _config

    # OAuth path — credentials are already on disk; we just need to mark
    # this provider as active.
    if provider_name in _config.OAUTH_PROVIDERS:
        token = _config.provider_key(provider_name)
        if not token:
            login_cmd = "claude" if provider_name == "claude-pro" else "codex login"
            console.print(
                f"[red]✗[/] {provider_name}: no OAuth credential found on disk. "
                f"Sign in with `{login_cmd}` first, then re-run."
            )
            return False
        _config.set_active_provider(provider_name)
        os.environ[_config.PROVIDER_ENV_VAR] = provider_name
        console.print(f"[green]✓[/] Active provider: [bold]{provider_name}[/]  [dim](OAuth — no key needed)[/]")
        return True

    key_var = _config.PROVIDER_KEY_VARS[provider_name]
    signup_url, signup_note = _PROVIDER_SIGNUP.get(provider_name, (None, None))

    console.print(f"[bold yellow]{key_var} not found.[/]")
    if signup_url:
        console.print(f"[dim]Get one at {signup_url} — {signup_note}.[/]")
    console.print()
    key = Prompt.ask(
        f"Paste your {provider_name} API key (or press enter to skip)",
        password=True,
        default="",
        show_default=False,
    ).strip()
    if not key:
        console.print(f"[dim]Skipped. Set {key_var} in your shell or in ~/.config/watchmen/.env, then re-run onboard.[/]")
        return False

    prefixes = _PROVIDER_KEY_PREFIXES.get(provider_name) or ()
    if prefixes and not any(key.startswith(p) for p in prefixes):
        if not Confirm.ask(
            f"  [yellow]Key doesn't look like a {provider_name} key (starts with {key[:6]}…). Save anyway?[/]",
            default=False,
        ):
            return False

    _config.set_provider_key(provider_name, key)
    _config.set_active_provider(provider_name)
    os.environ[key_var] = key
    os.environ[_config.PROVIDER_ENV_VAR] = provider_name
    env_path = _config.ENV_PATH
    console.print(f"[green]✓[/] Wrote {provider_name} key to {env_path}  [dim](chmod 600)[/]")
    console.print(f"[green]✓[/] Active provider: [bold]{provider_name}[/]")
    return True


# ── Legacy helpers (kept for backward compat with any external callers) ──
def _have_openrouter_key() -> bool:
    if os.environ.get("OPENROUTER_API_KEY"):
        return True
    env_path = Path.home() / ".config" / "watchmen" / ".env"
    try:
        return env_path.exists() and "OPENROUTER_API_KEY=" in env_path.read_text()
    except OSError:
        return False


def _prompt_for_openrouter_key(console: Console) -> bool:
    """Legacy: prompts only for an OpenRouter key. New onboard flows go
    through `_prompt_for_provider_key()` with a provider arg."""
    return _prompt_for_provider_key(console, "openrouter")


def check_prerequisites(console: Console) -> bool:
    if not _have_any_provider_key():
        provider_name = _prompt_provider_choice(console)
        if not _prompt_for_provider_key(console, provider_name):
            console.print(
                "[red]✗[/] Can't continue without an API key. "
                "Re-run `watchmen init` (or `watchmen settings api-key`) once you have one."
            )
            return False

    missing = []
    if not shutil.which("uv"):
        missing.append(("uv", "Install via: brew install uv  (or curl -LsSf https://astral.sh/uv/install.sh | sh)"))
    if not shutil.which("git"):
        missing.append(("git", "Should already be installed — `xcode-select --install` on macOS"))

    if missing:
        console.print("[bold red]Missing prerequisites:[/]")
        for name, hint in missing:
            console.print(f"  • [red]{name}[/]  {hint}")
        return False
    console.print("[green]✓[/] Prerequisites look good.")
    return True


def run_ingest(console: Console) -> bool:
    import sqlite3

    rc = stream_subprocess(
        console,
        "Scanning coding-agent transcripts into corpus.db",
        [sys.executable, "-m", "watchmen.corpus", "scan"],
    )
    if rc != 0:
        return False

    try:
        with sqlite3.connect(str(CORPUS_DB)) as conn:
            sessions = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
            prompts = conn.execute("SELECT COUNT(*) FROM prompts").fetchone()[0]
    except sqlite3.Error:
        sessions, prompts = 0, 0
    console.print(f"  [dim]→ {sessions:,} sessions, {prompts:,} prompts in corpus.db[/]")
    return True


def project_candidates(console: Console) -> list[dict]:
    from watchmen import state
    detected = state.auto_detect_projects()
    tracked = {p["project_key"]: p for p in state.list_projects()}

    rows: list[dict] = []
    for d in detected:
        key = d["project_key"]
        rows.append({
            "project_key": key,
            "decoded_path": d.get("source_repo"),
            "session_count": d.get("sessions", 0),
            "prompt_count": d.get("prompts", 0),
            "tracked": key in tracked,
            "source_repo": tracked.get(key, {}).get("source_repo"),
        })
    rows.sort(key=lambda r: r["prompt_count"], reverse=True)
    return rows


def select_projects(console: Console, candidates: list[dict]) -> list[dict]:
    table = Table(show_header=True, header_style="bold magenta", box=None)
    table.add_column("#", style="dim", width=3)
    table.add_column("Project")
    table.add_column("Sessions", justify="right")
    table.add_column("Prompts", justify="right")
    table.add_column("Status")
    for i, c in enumerate(candidates, 1):
        status = "[green]tracked[/]" if c["tracked"] else "[dim]new[/]"
        table.add_row(
            str(i),
            c["project_key"],
            str(c["session_count"]),
            f"{c['prompt_count']:,}",
            status,
        )
    console.print(table)
    console.print()
    console.print("[dim]Enter numbers separated by commas (e.g. '1,3,4'), or 'all', or press enter to skip.[/]")
    console.print("[dim]Already-tracked projects get a CLAUDE.md / skill refresh; new ones are added.[/]")
    choice = Prompt.ask("Which projects to onboard", default="")
    choice = choice.strip().lower()
    if not choice:
        return []
    if choice == "all":
        return candidates
    picked: list[dict] = []
    for tok in choice.split(","):
        tok = tok.strip()
        if not tok.isdigit():
            continue
        idx = int(tok) - 1
        if 0 <= idx < len(candidates):
            picked.append(candidates[idx])
    return picked


def prompt_for_repo(console: Console, project: dict) -> str | None:
    if project["source_repo"]:
        console.print(f"  [dim]already tracked at[/] {project['source_repo']}")
        return project["source_repo"]
    # Try to guess from decoded_path
    suggested = project.get("decoded_path") or ""
    guess = suggested if suggested and Path(suggested).exists() else ""
    while True:
        path = Prompt.ask(
            f"  Path for [bold]{project['project_key']}[/]",
            default=guess,
        )
        path = path.strip()
        if not path:
            return None
        expanded = Path(path).expanduser().resolve()
        if not expanded.exists() or not expanded.is_dir():
            console.print(f"  [yellow]not a directory: {expanded}[/]")
            continue
        return str(expanded)


def show_cost_estimate(console: Console, selected: list[dict]) -> None:
    total_prompts = sum(p["prompt_count"] for p in selected)
    model_label = config.default_model()

    from watchmen import providers as _providers
    active = config.active_provider()
    prov = _providers.get_provider(active)
    time_estimate = f"[bold]{15 * len(selected)} – {90 * len(selected)} min[/] total."

    if prov.is_subscription_quota:
        # Subscription-quota providers (Claude Pro / ChatGPT OAuth) don't
        # have a per-token $ cost. Highlight that runs draw against the
        # subscription's rate-limit window instead.
        billing = (
            f"Billing: [bold]{prov.quota_label}[/] · "
            "no API spend — usage counts toward your rate-limit window.\n"
        )
        cost_line = ""
    else:
        # Per-token providers — give a rough range. We can't price every
        # model precisely without burning a /models lookup, so we report
        # an order-of-magnitude bracket based on prompt volume and let
        # the actual cost panel after each turn surface the real number.
        low = len(selected) * 2 + total_prompts * 0.0005
        high = len(selected) * 8 + total_prompts * 0.002
        billing = (
            f"Billing: [bold]{prov.quota_label}[/] on [cyan]{model_label}[/].\n"
        )
        cost_line = f"Estimated cost: [bold]${low:.1f} – ${high:.1f}[/]\n"

    body = Text.from_markup(
        f"[bold]{len(selected)} project(s)[/], [bold]{total_prompts:,}[/] historical prompts.\n\n"
        f"{billing}"
        f"{cost_line}"
        f"Estimated time: {time_estimate}\n\n"
        "[dim]LLM passes run sequentially. Hit Ctrl-C to bail mid-stream — anything\n"
        "already written stays on disk; you can resume any time with `watchmen analyze`\n"
        "+ `watchmen curate`.[/]"
    )
    title = "Run preview" if prov.is_subscription_quota else "Cost preview"
    console.print(Panel(body, title=title, border_style="yellow"))


def stream_subprocess(console: Console, label: str, cmd: list[str]) -> int:
    """Run a long subprocess, tailing its stdout under a status spinner.
    Returns the exit code."""
    proc = subprocess.Popen(
        cmd,
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    last_line = ""
    with console.status(f"[bold]{label}[/]") as status:
        if proc.stdout is None:
            proc.wait()
            return proc.returncode
        for line in proc.stdout:
            stripped = line.rstrip()
            if not stripped:
                continue
            last_line = stripped[:120]
            status.update(f"[bold]{label}[/] · [dim]{last_line}[/]")
        proc.wait()
    if proc.returncode == 0:
        console.print(f"[green]✓[/] {label}")
    else:
        console.print(f"[red]✗[/] {label} (exit {proc.returncode})")
        if last_line:
            console.print(f"  [dim]last:[/] {last_line}")
    return proc.returncode


def run_analyze(console: Console, project_key: str) -> bool:
    rc = stream_subprocess(
        console,
        f"Analyzing {project_key}",
        [sys.executable, "-m", "watchmen.analyze", "--project", project_key],
    )
    return rc == 0


def run_curate(console: Console, project_key: str) -> bool:
    from watchmen import state
    proj = state.get_project(project_key)
    if not proj:
        console.print(f"[red]✗[/] {project_key} not tracked, can't curate")
        return False
    rc = stream_subprocess(
        console,
        f"Curating {project_key}",
        [sys.executable, "-m", "watchmen.curate", "--project", project_key, "--repo", proj["source_repo"]],
    )
    return rc == 0


def _run_pipeline_silent(project_key: str, source_repo: str, console: Console, label: str) -> dict:
    """Run analyst→curator pipeline for one project from a worker thread.
    Uses subprocess.run (no spinner) so multiple pipelines don't clobber each
    other's console output. Prints start/done lines via console.print (which is
    thread-safe in Rich), tagged with `label` so the user can follow progress.

    Returns a dict with timings + ok flags for the caller's summary."""
    import time as _t
    result = {
        "project_key": project_key,
        "label": label,
        "analyst_ok": False, "analyst_secs": 0.0, "analyst_last": "",
        "curator_ok": False, "curator_secs": 0.0, "curator_last": "",
    }

    console.print(f"  {label} [bold]{project_key}[/]: analyst started")
    t0 = _t.time()
    r = subprocess.run(
        [sys.executable, "-m", "watchmen.analyze", "--project", project_key],
        cwd=str(ROOT), capture_output=True, text=True,
    )
    result["analyst_secs"] = _t.time() - t0
    result["analyst_ok"] = r.returncode == 0
    out = (r.stdout or "").strip().splitlines()
    result["analyst_last"] = out[-1] if out else ((r.stderr or "").strip()[:120])
    marker = "[green]✓[/]" if result["analyst_ok"] else "[red]✗[/]"
    console.print(f"  {marker} {label} {project_key} analyst in {result['analyst_secs']:.0f}s")
    if not result["analyst_ok"]:
        console.print(f"    [dim]{result['analyst_last'][:120]}[/]")
        return result

    console.print(f"  {label} [bold]{project_key}[/]: curator started")
    t0 = _t.time()
    r = subprocess.run(
        [sys.executable, "-m", "watchmen.curate", "--project", project_key, "--repo", source_repo],
        cwd=str(ROOT), capture_output=True, text=True,
    )
    result["curator_secs"] = _t.time() - t0
    result["curator_ok"] = r.returncode == 0
    out = (r.stdout or "").strip().splitlines()
    result["curator_last"] = out[-1] if out else ((r.stderr or "").strip()[:120])
    marker = "[green]✓[/]" if result["curator_ok"] else "[red]✗[/]"
    console.print(f"  {marker} {label} {project_key} curator in {result['curator_secs']:.0f}s")
    if not result["curator_ok"]:
        console.print(f"    [dim]{result['curator_last'][:120]}[/]")
    return result


def run_pipeline_parallel(console: Console, selected: list, concurrency: int = 3) -> list[dict]:
    """Run analyst+curator for each selected project, up to `concurrency`
    projects at once. Each per-project worker runs its analyst+curator
    sequentially (they need to share the thesis) but pipelines for different
    projects run independently.

    Conservative concurrency=3 by default — each per-project curator already
    has internal Stage 2 parallelism, so 3 projects × 4 skill workers × ~2
    critic-per-skill = ~24 OpenRouter calls in flight. Comfortable for
    deepseek-v4-flash rate limits."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from watchmen import state
    console.print(f"\nRunning {len(selected)} projects in parallel (concurrency={concurrency})…\n")
    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = []
        for i, proj in enumerate(selected, 1):
            proj_state = state.get_project(proj["project_key"])
            if not proj_state:
                console.print(f"  [red]✗[/] [{i}/{len(selected)}] {proj['project_key']}: not tracked, skipping")
                continue
            label = f"[{i}/{len(selected)}]"
            futures.append(pool.submit(
                _run_pipeline_silent,
                proj["project_key"], proj_state["source_repo"], console, label,
            ))
        for fut in as_completed(futures):
            try:
                results.append(fut.result())
            except Exception as e:
                console.print(f"  [red]✗[/] pipeline error: {type(e).__name__}: {e}")
    return results


def install_autostart(console: Console) -> None:
    from watchmen import service
    prompt = f"\nInstall {service.BACKEND_NAME} autostart for daemon + viewer?"
    if Confirm.ask(prompt, default=True):
        service.install_daemon()
        service.install_viewer()
    else:
        console.print(f"[dim]Skipped. You can run `{_exec_name()} daemon install` later.[/]")


def install_hooks_if_wanted(console: Console) -> None:
    from watchmen import hooks_setup
    if Confirm.ask("\nWire Claude Code hooks (real-time event capture)?", default=True):
        hooks_setup.install()
    else:
        console.print(f"[dim]Skipped. Run `{_exec_name()} hooks install` later if you want hook capture.[/]")


def show_plugin_install(console: Console) -> None:
    body = Text.from_markup(
        "Inside any Claude Code session, run these three commands to install\n"
        "the plugin that surfaces watchmen findings without leaving the TUI:\n\n"
        "[bold cyan]  /plugin marketplace add firstbatchxyz/watchmen[/]\n"
        "[bold cyan]  /plugin install watchmen@watchmen[/]\n"
        "[bold cyan]  /reload-plugins[/]\n\n"
        f"Then wire the [bold]💡 indicator[/] into your statusLine:\n"
        f"[bold cyan]  {_exec_name()} statusline install[/]"
    )
    console.print(Panel(body, title="Install the Claude Code plugin", border_style="cyan"))


def show_summary(console: Console) -> None:
    viewer_url = config.viewer_base_url()
    body = Text.from_markup(
        f"All set. Browse your generated skills + CLAUDE.md + run diffs at:\n\n"
        f"  [bold link]{viewer_url}[/]\n\n"
        f"What runs from here:\n"
        f"  • [bold]every 2h[/] — incremental analyst checks for new prompts\n"
        f"  • [bold]when you choose[/] — `watchmen curate <project>` proposes skill updates\n"
        f"  • [bold]on each curator run[/] — plugin state updates, statusLine indicator activates\n\n"
        f"[dim]Tail the daemon log: tail -f ~/Library/Logs/watchmen.log (or kai-mac.log on legacy)[/]"
    )
    console.print(Panel(body, title="🎉 You're done", border_style="green"))
    if Confirm.ask("\nOpen the viewer in your browser now?", default=True):
        try:
            webbrowser.open(viewer_url)
        except Exception:
            pass


def run() -> int:
    console = Console()
    TOTAL = 6

    # First-install safety: state.db won't have its schema yet on a totally
    # fresh clone. init_db is idempotent (CREATE TABLE IF NOT EXISTS).
    from watchmen import state
    state.init_db()

    show_welcome(console, TOTAL)

    _step(console, 1, TOTAL, "Prerequisites check")
    if not check_prerequisites(console):
        return 1
    if not Confirm.ask("\nReady to continue?", default=True):
        console.print("[dim]Bye for now. Re-run anytime.[/]")
        return 0

    _step(console, 2, TOTAL, "Ingest your coding-agent history")
    if not run_ingest(console):
        return 1

    _step(console, 3, TOTAL, "Pick projects to onboard")
    candidates = project_candidates(console)
    if not candidates:
        console.print("[yellow]No projects detected in your corpus. Have you used a supported coding agent (Claude Code, Codex, pi.dev) yet?[/]")
        return 0
    selected = select_projects(console, candidates)
    if not selected:
        console.print("[dim]No projects selected. Exiting cleanly — re-run when you're ready.[/]")
        return 0

    _step(console, 4, TOTAL, "Track + run the pipeline")
    from watchmen import state
    for proj in selected:
        repo = prompt_for_repo(console, proj)
        if not repo:
            console.print(f"[yellow]Skipping {proj['project_key']} — no repo path given.[/]")
            continue
        if not proj["tracked"]:
            state.track_project(proj["project_key"], repo)
            console.print(f"[green]✓[/] Tracked [bold]{proj['project_key']}[/] → {repo}")

    selected = [p for p in selected if state.get_project(p["project_key"])]
    if not selected:
        console.print("[yellow]Nothing tracked. Exiting.[/]")
        return 0

    show_cost_estimate(console, selected)
    if Confirm.ask("\nRun analyze + curate now?", default=True):
        if len(selected) == 1:
            # Single project — keep the pretty live-spinner UX.
            key = selected[0]["project_key"]
            run_analyze(console, key)
            run_curate(console, key)
        else:
            # Multi-project — parallelize per-project pipelines.
            results = run_pipeline_parallel(console, selected, concurrency=3)
            ok_a = sum(1 for r in results if r["analyst_ok"])
            ok_c = sum(1 for r in results if r["curator_ok"])
            tot_a = sum(r["analyst_secs"] for r in results)
            tot_c = sum(r["curator_secs"] for r in results)
            console.print(
                f"\n  pipeline complete: {ok_a}/{len(selected)} analysts ok, "
                f"{ok_c}/{len(selected)} curators ok  "
                f"(serial-equiv {tot_a + tot_c:.0f}s)"
            )
    else:
        console.print(f"[dim]Skipped. Run `{_exec_name()} analyze <key>` + `{_exec_name()} curate <key>` later.[/]")

    _step(console, 5, TOTAL, "Set up autostart + hooks")
    install_hooks_if_wanted(console)
    install_autostart(console)

    _step(console, 6, TOTAL, "Install the Claude Code plugin")
    show_plugin_install(console)

    console.print()
    show_summary(console)
    return 0


if __name__ == "__main__":
    sys.exit(run())
