"""watchmen — orchestrator CLI for the local Claude Code session intelligence pipeline.

Subcommands:
  status                    Dashboard: tracked projects, last-run, what needs analysis
  list                      Auto-detect projects from corpus.db (>=30 prompts)
  track <key> --repo <p>    Track a project so watchmen analyze/curate operates on it
  ingest                    Re-run corpus.py (rebuild corpus.db from all agents)
  analyze <key>             Run analyst (incremental — only days after last_analyst_day)
  curate <key>              Run curator (--regen-claude for stage 3 only)
  runs [--project <key>]    Recent run history
  onboard                   Interactive setup wizard (fresh install)
  reonboard                 Re-run the wizard (existing projects survive, new ones added)
  settings list|show|set    View / update per-project settings (threshold, enabled, repo, notes)
  daemon run|install|uninstall      Foreground run / launchd agent lifecycle
  viewer run|install|uninstall      Foreground run / launchd agent lifecycle (default :8979)
  hooks install|uninstall|status    Claude Code hook lifecycle + inspection
  statusline install|uninstall      💡 watchmen indicator in ~/.claude/settings.json
  plugin update|status              Marketplace clone management
  launchd status                    Inspect installed launchd agents

Old verb-noun-hyphen forms (install-daemon, hooks-status, …) still work but
print a soft deprecation hint to stderr — will be removed in a future release.

Designed to be invoked as `uv run watchmen <subcommand>` or via the script entry in pyproject.toml.
"""

import argparse
import difflib
import os
import random
import sqlite3
import sys
from pathlib import Path

from watchmen import config
from watchmen import state
from watchmen.paths import STATE_DB
# Presentation helpers were inline until Phase 3 — alias them under the
# `_name` convention the rest of cli.py uses so call sites don't churn.
from watchmen.ui import (
    bold as _bold,
    cyan as _cyan,
    dim as _dim,
    green as _green,
    red as _red,
    short_path as _short_path,
    ui_header as _ui_header,
    yellow as _yellow,
)
from watchmen.ui import (
    print_runtime_state_error as _ui_print_runtime_state_error,
)
# Project/path/skill helpers moved to watchmen.util during the Phase 3 split.
from watchmen.util import (
    corpus_db_path as _corpus_db_path,
)
# Skill state-mutation commands now live in commands.control. Re-exported
# under the same names so the argparse dispatch in main() doesn't change.
from watchmen.commands.control import (
    cmd_drop,
    cmd_pin,
    cmd_restore,
    cmd_review,
    cmd_unpin,
)
# Read-only inspection commands. Same re-export pattern.
from watchmen.commands.inspect import (
    cmd_changelog,
    cmd_logs,
    cmd_open,
    cmd_recent,
    cmd_show,
    cmd_why,
)
# Cross-repo digest — the largest single command, lives in its own module.
from watchmen.commands.insights import cmd_insights
# Pipeline commands — status / ingest / analyze / curate / runs / learn /
# metrics. Re-exported here so the argparse dispatch in main() doesn't move.
from watchmen.commands.pipeline import (
    cmd_analyze,
    cmd_curate,
    cmd_ingest,
    cmd_learn,
    cmd_metrics,
    cmd_runs,
    cmd_status,
)
from watchmen.util import find_changelog as _find_changelog


def _print_runtime_state_error(exc: BaseException, *, stderr: bool = True) -> None:
    """Thin wrapper so call sites don't have to thread STATE_DB everywhere.
    The actual rendering lives in watchmen.ui; this just binds the path."""
    _ui_print_runtime_state_error(STATE_DB, exc, stderr=stderr)

ROOT = Path(__file__).parent
SOURCE_ROOT = Path(__file__).parent
DEFAULT_MODEL = "deepseek/deepseek-v4-flash"
VIEWER_DEFAULT_HOST = config.VIEWER_DEFAULT_HOST
VIEWER_DEFAULT_PORT = config.VIEWER_DEFAULT_PORT


class WatchmenParser(argparse.ArgumentParser):
    """Argparse with developer-grade error recovery.

    Stripe/Modal-style CLIs make the next action obvious when a command is
    mistyped. Argparse's default "invalid choice" block is technically correct
    but buries the useful command names in a long choices list, so we surface
    the closest match and a focused help hint.
    """

    def error(self, message: str) -> None:
        if "invalid choice" in message and self.prog == "watchmen":
            bad = message.split("invalid choice:", 1)[1].split("(", 1)[0].strip().strip("'\"")
            known = [cmd for _, commands in _HELP_GROUPS for cmd, _ in commands]
            match = difflib.get_close_matches(bad, known, n=1)
            print(_red(f"Error: unknown command `{bad}`"), file=sys.stderr)
            if match:
                print(f"Did you mean `{match[0]}`?", file=sys.stderr)
            print("Run `watchmen --help` to see available commands.", file=sys.stderr)
            raise SystemExit(2)
        print(_red(f"Error: {message}"), file=sys.stderr)
        print(f"Run `{self.prog} --help` for usage.", file=sys.stderr)
        raise SystemExit(2)


def _version() -> str:
    """Read version from package metadata if installed, else parse pyproject.toml.

    Two source-tree layouts: editable install (ROOT = src/watchmen/, pyproject
    two levels up) and dev checkout pre-install (same layout). We try both
    plausible locations before giving up.
    """
    try:
        from importlib.metadata import version as _v
        return _v("watchmen")
    except Exception:
        pass
    try:
        import tomllib  # 3.11+
        for candidate in (ROOT / "pyproject.toml", ROOT.parents[1] / "pyproject.toml"):
            if candidate.exists():
                with candidate.open("rb") as fh:
                    return tomllib.load(fh)["project"]["version"]
    except Exception:
        pass
    return "0.0.0"


# ─── Watchmen aesthetic helpers ─────────────────────────────────────────────
# Small thematic touches keyed to each character: Manhattan blue for `doctor`,
# Doomsday Clock yellow for `status`, mission-log framing for `runs`. Kept
# tiny so the CLI stays readable on narrow terminals.


def _doomsday_minutes_to_midnight(needs_analysis: int, total: int) -> int:
    """Map project staleness to the iconic Watchmen Doomsday Clock position.

    Curve calibrated so a small fleet with 1-2 stale projects sits at the
    classic "five to midnight" (Watchmen's opening clock position), while
    a fleet where >70% of projects need attention is the harrowing
    one-minute-to-midnight.

    All up to date  → 12 to midnight (safe)
    < 20% stale     → 8 to midnight
    < 40% stale     → 5 to midnight  (canonical Watchmen position)
    < 70% stale     → 2 to midnight
    >= 70% stale    → 1 to midnight  (critical)"""
    if total == 0:
        return 12
    ratio = needs_analysis / total
    if ratio == 0:    return 12
    if ratio < 0.2:   return 8
    if ratio < 0.4:   return 5
    if ratio < 0.7:   return 2
    return 1


# Spelled-out form for the clock line — reads better than "5 minutes" and is
# the exact phrasing used in the comic.
_DOOMSDAY_WORD = {12: "twelve", 8: "eight", 5: "five", 2: "two", 1: "one"}

# Hand glyph by minutes-to-midnight. The minute hand rotates *back* from 12
# toward 11 as we approach doom. We can't capture the rotation precisely in
# a single character, but the variation gives the clock face a real "look".
_DOOMSDAY_HAND = {
    12: "·",   # near 12 — calm dot
    8:  "╲",   # 24° back from vertical
    5:  "┘",   # 30° back, the canonical Watchmen position
    2:  "╲",   # almost vertical again
    1:  "│",   # essentially at 12, the doom moment
}

# Random tick-tock taglines per severity. Pooled so consecutive runs feel
# different — the comic itself plays variations on "tick tock tick tock".
_DOOMSDAY_TAGLINES_CLEAR = (
    "all clear.",
    "the city sleeps.",
    "no field activity required.",
    "midnight is far.",
)
_DOOMSDAY_TAGLINES_TICK = (
    "tick. tock.",
    "the clock advances.",
    "minutes are short here.",
    "tick tock tick tock.",
)
_DOOMSDAY_TAGLINES_CRITICAL = (
    "tick. tock. tick.",
    "midnight is near.",
    "the hour grows late.",
    "no one is watching us watch.",
)


# ─── Dr. Manhattan flavor for `watchmen doctor` ─────────────────────────────
# A small atom panel + three pools of in-character quotes (one per severity).
# random.choice rotates the line each run so the doctor command never feels
# canned. Header text stays stable (tests + scripts can grep "Dr. Manhattan").

_MANHATTAN_QUOTES_OK = (
    "I see all the body's mechanisms, intact and predictable.",
    "Nothing here requires my intervention.",
    "The structure holds. No deviation from expected paths.",
    "All is precisely as it should be.",
    "Causality is uninterrupted.",
    "On Mars, I would call this a peaceful arrangement.",
)
_MANHATTAN_QUOTES_WARN = (
    "A pattern frays — observable, not yet consequential.",
    "Minor deviations. The mechanism still turns.",
    "One inconsistency. Time will absorb it.",
    "A small fault. I leave it for you to mend.",
)
_MANHATTAN_QUOTES_FAIL = (
    "The mechanism is impeded. Correction is necessary.",
    "Causality is interrupted here. Attend to it.",
    "I observe a discontinuity.",
    "Something is broken. I cannot fix what I do not touch.",
)




# ─── Release-notes detector ─────────────────────────────────────────────────
# Compares the installed version (read by `_version()`) against the
# last-seen version stored in `~/.watchmen/last_seen_version`. On a bump,
# prints the new CHANGELOG.md entries to stderr so a fresh `git pull` is
# never silent — and so the CLI can announce the new feature itself.
# Quiet on fresh installs (no last-seen file → just record current).

_LAST_SEEN_VERSION_FILE = Path.home() / ".watchmen" / "last_seen_version"


def _parse_changelog(text: str) -> list[tuple[str, str]]:
    """Split CHANGELOG.md into [(version, body), …] in file order
    (top-of-file = newest, the Keep-a-Changelog convention). Each entry's
    body includes the version's section header so it renders cleanly when
    handed to Rich Markdown verbatim."""
    import re as _re
    out: list[tuple[str, str]] = []
    header_re = _re.compile(r"^##\s+\[([^\]]+)\]", _re.MULTILINE)
    matches = list(header_re.finditer(text))
    for i, m in enumerate(matches):
        version = m.group(1).strip()
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        out.append((version, text[start:end].rstrip()))
    return out


def _version_key(v: str) -> tuple[int, ...]:
    """Loose semver tuple for ordered comparison. Splits on dots, casts
    each part to int when possible, falls back to lexical for tags like
    `0.2.0-rc1`. Good enough for the linear-history versioning watchmen
    uses; not a full semver spec."""
    parts = []
    for chunk in v.replace("-", ".").split("."):
        try:
            parts.append((0, int(chunk)))
        except ValueError:
            parts.append((1, chunk))
    return tuple(parts)


def _new_changelog_entries(text: str, current: str, last_seen: str | None) -> list[tuple[str, str]]:
    """Entries strictly newer than `last_seen`, up to and including
    `current`. When `last_seen` is None (fresh install, no record), just
    return the entry for `current` so the announcement is concise."""
    all_entries = _parse_changelog(text)
    if last_seen is None:
        return [e for e in all_entries if e[0] == current][:1]
    last_key = _version_key(last_seen)
    return [e for e in all_entries if _version_key(e[0]) > last_key]


def _show_release_notes_if_bumped(*, interactive: bool | None = None) -> None:
    """If the installed watchmen version is newer than the one this user
    last saw, print the new CHANGELOG.md entries to stderr. Updates the
    tracker after display so the announcement appears exactly once per
    bump. Silently no-ops on errors so changelog parsing can't break CLI
    startup."""
    try:
        if os.environ.get("WATCHMEN_DISABLE_RELEASE_NOTES") == "1":
            return
        # Keep command output script-friendly. Users can always run
        # `watchmen changelog`; automated/non-tty invocations should not get a
        # surprise Markdown block on stderr.
        if interactive is None:
            interactive = sys.stderr.isatty()
        if not interactive:
            return
        current = _version()
        tracker = _LAST_SEEN_VERSION_FILE
        tracker.parent.mkdir(parents=True, exist_ok=True)
        last_seen = tracker.read_text().strip() if tracker.exists() else None
        if last_seen == current:
            return  # already announced this version
        changelog_path = _find_changelog()
        if changelog_path is None:
            tracker.write_text(current)
            return
        entries = _new_changelog_entries(
            changelog_path.read_text(), current, last_seen
        )
        if entries:
            from rich.console import Console
            from rich.markdown import Markdown
            # stderr so changelog output doesn't pollute scripts piping
            # `watchmen show <foo>` etc. into other tools.
            err = Console(stderr=True)
            err.print(
                f"\n[bold]◷ watchmen updated to v{current}[/]"
                + (f"  [dim](was v{last_seen})[/]" if last_seen else "  [dim](first run)[/]")
            )
            for _v, body in entries:
                err.print(Markdown(body))
            err.print("[dim]  · run `watchmen changelog` anytime to re-read[/]\n")
        tracker.write_text(current)
    except Exception:
        # Don't let a changelog formatting blip break any CLI command.
        pass


def _manhattan_atom_panel() -> list[str]:
    """3-line atom panel — small enough to sit above the doctor table without
    consuming the visible terminal. The ⚛ glyph is literally a stylized
    hydrogen atom, the same one Manhattan wears on his forehead in the comic."""
    return [
        "  ┌─────┐",
        "  │  ⚛  │",
        "  └─────┘",
    ]


def _doomsday_ascii(needs: int, total: int) -> list[str]:
    """3-line render: tiny clock face + 12-segment doom-bar + spelled label.

    Each ASCII element is conditional on the calculated minutes-to-midnight,
    so the visual reads as a real, slightly-different clock each time you
    invoke `watchmen status`. Doom-bar fills left-to-right as we approach
    midnight (segments = `12 - minutes_to_midnight`)."""
    n = _doomsday_minutes_to_midnight(needs, total)
    hand = _DOOMSDAY_HAND[n]
    filled = max(0, 12 - n)
    bar = "█" * filled + "░" * (12 - filled)
    word = _DOOMSDAY_WORD[n]
    suffix = "minute" if n == 1 else "minutes"

    if n == 12:
        tagline = random.choice(_DOOMSDAY_TAGLINES_CLEAR)
    elif n <= 2:
        tagline = random.choice(_DOOMSDAY_TAGLINES_CRITICAL)
    else:
        tagline = random.choice(_DOOMSDAY_TAGLINES_TICK)

    return [
        _yellow("  ╭─╮"),
        _yellow(f"  │{hand}│  ") + _yellow(bar) + _yellow(f"  {word} {suffix} to midnight"),
        _yellow("  ╰─╯  ") + _dim(tagline),
    ]


# ─── Subcommands ────────────────────────────────────────────────────────────


def cmd_list(args) -> int:
    from rich.console import Console
    from rich.table import Table

    console = Console()
    detected = state.auto_detect_projects()
    if not detected:
        _ui_header(console, "list")
        console.print("No projects detected.")
        console.print("[dim]Run `watchmen ingest` to populate corpus.db.[/]")
        return 0
    tracked_keys = {p["project_key"] for p in state.list_projects()}

    _ui_header(console, "list", f"{len(detected)} projects with 30+ prompts")
    console.print()
    table = Table(show_header=True, header_style="bold", expand=False, box=None, padding=(0, 2, 0, 0))
    table.add_column("project", style="bold")
    table.add_column("tracked")
    table.add_column("prompts", justify="right")
    table.add_column("sessions", justify="right")
    table.add_column("repo", style="dim")
    for d in detected:
        tracked = "[green]yes[/]" if d["project_key"] in tracked_keys else "[dim]no[/]"
        table.add_row(
            d["project_key"],
            tracked,
            str(d["prompts"]),
            str(d["sessions"]),
            _short_path(d["source_repo"]),
        )
    console.print(table)
    console.print()
    console.print("[dim]Track a project with `watchmen track <key> --repo <path>`.[/]")
    return 0


def cmd_track(args) -> int:
    state.init_db()
    repo = Path(args.repo).expanduser().resolve()
    if not repo.exists():
        print(f"ERROR: source repo does not exist: {repo}")
        return 1
    state.track_project(args.project, str(repo), threshold=args.threshold)
    print(_green(f"Tracking {args.project} → {repo}"))
    # Bootstrap from disk if analyses already exist
    summary = state.sync_from_disk(args.project)
    if summary.get("analyst"):
        print(_dim(f"  synced analyst state — last day {summary['analyst']['last_day']}, {summary['analyst']['files']} day-files"))
    if summary.get("curator"):
        print(_dim(f"  synced curator state — {summary['curator']['skill_count']} skills"))
    return 0


def cmd_sync(args) -> int:
    state.init_db()
    targets = [args.project] if args.project else [p["project_key"] for p in state.list_projects()]
    for key in targets:
        summary = state.sync_from_disk(key)
        a = summary.get("analyst")
        c = summary.get("curator")
        bits = []
        if a: bits.append(f"analyst:last_day={a['last_day']}")
        if c: bits.append(f"curator:{c['skill_count']} skills")
        print(f"  {key:<30}  {'  '.join(bits) if bits else _dim('(no artifacts on disk)')}")
    return 0


def cmd_config(args) -> int:
    print(_dim("config command — placeholder for P3 (will edit ~/.config/watchmen/config.yaml)"))
    return 0


def cmd_viewer(args) -> int:
    state.init_db()
    from watchmen.viewer.server import serve
    serve(host=args.host, port=args.port)
    return 0


def cmd_daemon(args) -> int:
    from watchmen import daemon as _daemon
    return _daemon.run(args)


def cmd_install_daemon(args) -> int:
    from watchmen import service
    return service.install_daemon(model=args.model, interval=args.interval, dry_run=args.dry_run)


def cmd_install_viewer(args) -> int:
    from watchmen import service
    return service.install_viewer(host=args.host, port=args.port, dry_run=args.dry_run)


def cmd_uninstall_daemon(args) -> int:
    from watchmen import service
    return service.uninstall_daemon()


def cmd_uninstall_viewer(args) -> int:
    from watchmen import service
    return service.uninstall_viewer()


def cmd_launchd_status(args) -> int:
    from watchmen import service
    return service.status()


def cmd_install_hooks(args) -> int:
    from watchmen import hooks_setup
    return hooks_setup.install()


def cmd_uninstall_hooks(args) -> int:
    from watchmen import hooks_setup
    return hooks_setup.uninstall()


def cmd_hooks_status(args) -> int:
    from watchmen import hooks_setup
    return hooks_setup.status()


def cmd_update_plugin(args) -> int:
    from watchmen import plugin_setup
    return plugin_setup.update_marketplace()


def cmd_install_statusline(args) -> int:
    from watchmen import plugin_setup
    return plugin_setup.install_statusline(force=args.force)


def cmd_uninstall_statusline(args) -> int:
    from watchmen import plugin_setup
    return plugin_setup.uninstall_statusline()


def cmd_plugin_status(args) -> int:
    from watchmen import plugin_setup
    return plugin_setup.status()


def cmd_onboard(args) -> int:
    from watchmen import onboard
    return onboard.run()


def cmd_reonboard(args) -> int:
    """Re-run the onboarding wizard. Same code path as `onboard` — onboard.run()
    is already idempotent (existing projects show up tracked, get refreshed)."""
    from watchmen import onboard
    print(_dim("Re-running onboarding wizard. Tracked projects survive — new ones are added."))
    return onboard.run()


# ─── Settings ───────────────────────────────────────────────────────────────


_SETTABLE_KEYS = (
    "enabled", "threshold", "repo", "notes",
    "approval_required", "skip_overlapping_skills",
)


def _parse_setting(key: str, value: str) -> tuple[str, object]:
    """Map a CLI-friendly key + value to (db_column, coerced_value).
    Raises ValueError with a human-readable message on bad input."""
    if key == "enabled":
        v = value.strip().lower()
        if v in ("true", "yes", "y", "on", "1"):
            return "enabled", 1
        if v in ("false", "no", "n", "off", "0"):
            return "enabled", 0
        raise ValueError(f"enabled must be true/false (got {value!r})")
    if key == "threshold":
        try:
            n = int(value)
        except ValueError:
            raise ValueError(f"threshold must be an integer (got {value!r})") from None
        if n < 1:
            raise ValueError("threshold must be ≥ 1")
        return "threshold_new_prompts", n
    if key == "repo":
        path = Path(value).expanduser().resolve()
        if not path.exists() or not path.is_dir():
            raise ValueError(f"not a directory: {path}")
        return "source_repo", str(path)
    if key == "notes":
        return "notes", value
    # Boolean settings: approval_required + skip_overlapping_skills both
    # default 0 and accept the same true/false vocabulary as `enabled`.
    if key in ("approval_required", "skip_overlapping_skills"):
        v = value.strip().lower()
        if v in ("true", "yes", "y", "on", "1"):
            return key, 1
        if v in ("false", "no", "n", "off", "0"):
            return key, 0
        raise ValueError(f"{key} must be true/false (got {value!r})")
    raise ValueError(f"unknown setting {key!r}. valid: {', '.join(_SETTABLE_KEYS)}")


def cmd_settings_list(args) -> int:
    state.init_db()
    rows = state.list_projects()
    if not rows:
        print(_dim("No projects tracked yet. Run `watchmen onboard` or `watchmen track <key> --repo <path>`."))
        return 0
    print(_bold(f"\n{len(rows)} tracked project(s):\n"))
    print(f"  {'project':<30} {'state':<8} {'threshold':>9}  {'repo'}")
    print(_dim("  " + "─" * 90))
    for p in rows:
        st = "enabled" if p["enabled"] else _yellow("paused")
        repo = (p["source_repo"] or "").replace(str(Path.home()), "~", 1)
        print(f"  {p['project_key'][:30]:<30} {st:<8} {p['threshold_new_prompts']:>9}  {repo}")
    return 0


def cmd_settings_show(args) -> int:
    state.init_db()
    p = state.get_project(args.project)
    if not p:
        print(f"ERROR: project '{args.project}' not tracked. Run `watchmen list` to see candidates.")
        return 1
    print(_bold(f"\n{args.project}\n"))
    for k in ("source_repo", "enabled", "threshold_new_prompts", "notes",
              "approval_required", "skip_overlapping_skills",
              "last_analyst_day", "last_analyst_run",
              "last_curator_run", "last_curator_skill_count",
              "created_at", "updated_at"):
        v = p.get(k)
        if isinstance(v, int) and k == "enabled":
            v = "true" if v else "false"
        print(f"  {k:<28}  {v if v is not None else _dim('(unset)')}")
    return 0


def cmd_settings_set(args) -> int:
    state.init_db()
    if not state.get_project(args.project):
        print(f"ERROR: project '{args.project}' not tracked.")
        return 1
    try:
        column, value = _parse_setting(args.key, args.value)
    except ValueError as ex:
        print(f"ERROR: {ex}")
        return 1
    state.update_project(args.project, **{column: value})
    print(_green(f"✓ {args.project}: {args.key} = {value}"))
    return 0


def _check_openrouter_key(key: str) -> tuple[bool, str]:
    """Probe OpenRouter's /auth/key endpoint with the given key. Returns
    (ok, human_message). Used by `watchmen settings api-key [--check]` to
    surface bad keys BEFORE they reach the analyst/curator and turn into
    silent 401s halfway through a run."""
    import httpx
    try:
        r = httpx.get(
            "https://openrouter.ai/api/v1/auth/key",
            headers={"Authorization": f"Bearer {key}"},
            timeout=10.0,
        )
    except httpx.RequestError as e:
        return False, f"connection error: {type(e).__name__}"
    if r.status_code == 200:
        try:
            info = (r.json() or {}).get("data") or {}
        except ValueError:
            info = {}
        usage = info.get("usage")
        limit = info.get("limit")
        if usage is not None and limit is not None and limit > 0:
            return True, f"valid — credits used ${float(usage):.2f} of ${float(limit):.2f}"
        if usage is not None and limit is None:
            return True, f"valid — credits used ${float(usage):.2f} (no hard limit)"
        return True, "valid"
    if r.status_code == 401:
        try:
            msg = (r.json().get("error") or {}).get("message", "")
        except (ValueError, AttributeError):
            msg = ""
        return False, f"401 — {msg or 'unauthorized'}"
    return False, f"HTTP {r.status_code} — {r.text[:120]}"


def _read_current_api_key() -> str | None:
    """Return the OpenRouter API key from env or ~/.config/watchmen/.env."""
    return config.read_env_var("OPENROUTER_API_KEY")


def _write_api_key(key: str) -> Path:
    """Persist the OpenRouter key to ~/.config/watchmen/.env, preserving unrelated lines."""
    return config.write_env_var("OPENROUTER_API_KEY", key)


def cmd_settings_port(args) -> int:
    """Get or set the viewer port. Writes to ~/.config/watchmen/.env so the
    setting survives restarts and is honored by every layer (CLI defaults,
    onboard, curator-generated viewer URLs)."""
    from rich.console import Console
    console = Console()

    if args.value is None:
        current = config.viewer_port()
        source = "default" if current == config.VIEWER_DEFAULT_PORT and not config.read_env_var("WATCHMEN_VIEWER_PORT") else "config"
        console.print(f"viewer port: [bold]{current}[/] [dim]({source})[/]")
        if source == "default":
            console.print("  [dim]set with: watchmen settings port <N>[/]")
        return 0

    try:
        port = int(args.value)
    except ValueError:
        console.print(f"[red]✗[/] port must be an integer (got {args.value!r})")
        return 1
    if not (1024 <= port <= 65535):
        console.print("[red]✗[/] port must be in 1024–65535")
        return 1

    path = config.write_env_var("WATCHMEN_VIEWER_PORT", str(port))
    console.print(f"[green]✓[/] viewer port set to [bold]{port}[/]")
    console.print(f"  [dim]wrote → {path}[/]")
    # The launchd plist is baked at install time — port changes don't propagate
    # until reinstall. Make the next step obvious.
    try:
        from watchmen import service
        if service.is_viewer_loaded():
            console.print(f"  [yellow]![/] viewer {service.BACKEND_NAME} agent is running on its old port — run [bold]watchmen viewer install[/] to move it")
    except Exception:
        pass
    return 0


def cmd_settings_api_key(args) -> int:
    """Set or check the OpenRouter API key. Live-validates against OpenRouter's
    /auth/key endpoint so a bad key gets caught BEFORE the analyst/curator
    silently 401s halfway through a run."""
    from rich.console import Console
    from rich.prompt import Confirm, Prompt
    console = Console()

    current = _read_current_api_key()
    if current:
        ok, info = _check_openrouter_key(current)
        marker = "[green]✓[/]" if ok else "[red]✗[/]"
        console.print(f"{marker} current key: {info}  [dim]({current[:8]}…{current[-4:]})[/]")
    else:
        console.print("[dim]no key currently set[/]")

    if args.check:
        return 0 if (current and ok) else 1

    if args.set:
        new_key = args.set.strip()
    else:
        console.print()
        new_key = Prompt.ask(
            "Paste new OpenRouter API key (enter to keep current)",
            password=True, default="", show_default=False,
        ).strip()
    if not new_key:
        console.print("[dim]no change.[/]")
        return 0

    ok, info = _check_openrouter_key(new_key)
    if ok:
        path = _write_api_key(new_key)
        console.print(f"[green]✓[/] {info}")
        console.print(f"  wrote → {path} [dim](chmod 600)[/]")
        return 0
    console.print(f"[red]✗[/] new key rejected: {info}")
    if not Confirm.ask("Save anyway?", default=False):
        return 1
    path = _write_api_key(new_key)
    console.print(f"[yellow]![/] saved despite rejection → {path}")
    return 0


# ─── Round 1: inspection + provenance commands ─────────────────────────────


# ─── Round 2: pin / drop control ────────────────────────────────────────────
# Pinned skills are frozen — the curator skips re-running their per-skill
# agent on the next run and keeps the existing bundle untouched.
# Dropped skills are removed AND blocked — the candidate finder still
# proposes anything (it's an LLM, we can't muzzle it cleanly), but
# curate.py filters its output against the blocklist before Stage 2 and
# deletes any leftover bundle dir for the blocked slug.

# ─── Round 3: fast feedback loop + interactive review ──────────────────────


def cmd_doctor(args) -> int:
    """Health check: API key, OpenRouter reachability, corpus, tracked projects,
    daemon/viewer state, hooks, latest run age, disk free.

    Used to self-diagnose a broken install — single screen of ✓/✗ rows. Returns
    0 if everything is green, 1 if any required check fails.

    The subtitle keeps the historical "Dr. Manhattan" anchor while the actual
    surface stays closer to a modern diagnostic CLI: compact title, scannable
    table, and a single severity summary."""
    from rich.console import Console
    from rich.table import Table
    console = Console()

    _ui_header(console, "doctor", "Dr. Manhattan diagnostics")
    console.print()

    fails = 0
    warns = 0
    table = Table(show_header=True, header_style="bold", expand=False, box=None, padding=(0, 2, 0, 0))
    table.add_column("check", style="bold")
    table.add_column("status", justify="center", width=4)
    table.add_column("detail")

    def row(label: str, ok: bool, detail: str, severity: str = "fail") -> None:
        nonlocal fails, warns
        if ok:
            mark = "[green]✓[/]"
        elif severity == "warn":
            mark = "[yellow]![/]"
            warns += 1
        else:
            mark = "[red]✗[/]"
            fails += 1
        table.add_row(label, mark, detail)

    # 1. OpenRouter API key
    current = _read_current_api_key()
    if not current:
        row("OpenRouter key", False, "not set — run `watchmen settings api-key`")
    else:
        ok, info = _check_openrouter_key(current)
        row("OpenRouter key", ok, info)

    # 2. corpus.db
    corpus_db = _corpus_db_path()
    if not corpus_db.exists():
        row("corpus.db", False, "missing — run `watchmen ingest`")
    else:
        import sqlite3
        cc = sqlite3.connect(corpus_db)
        n_sessions = cc.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        n_prompts = cc.execute("SELECT COUNT(*) FROM prompts").fetchone()[0]
        cc.close()
        if n_sessions == 0:
            row("corpus.db", False, "no sessions ingested yet — run `watchmen ingest`")
        else:
            row("corpus.db", True, f"{n_sessions:,} sessions / {n_prompts:,} prompts")

    # 3. tracked projects
    state.init_db()
    projects = state.list_projects()
    if not projects:
        row("tracked projects", False, "0 — run `watchmen init` or `watchmen track`")
    else:
        row("tracked projects", True, f"{len(projects)} project(s)")

    # 4. daemon/viewer service state (launchd on macOS, systemd --user on Linux)
    from watchmen import service
    daemon_loaded = service.is_daemon_loaded()
    viewer_loaded = service.is_viewer_loaded()
    backend = service.BACKEND_NAME
    row(f"daemon ({backend})", daemon_loaded, "loaded" if daemon_loaded else "not loaded — `watchmen daemon install`", severity="warn")
    row(f"viewer ({backend})", viewer_loaded, "loaded" if viewer_loaded else "not loaded — `watchmen viewer install`", severity="warn")

    # 5. viewer responding
    try:
        import httpx
        r = httpx.get(f"http://{VIEWER_DEFAULT_HOST}:{VIEWER_DEFAULT_PORT}/", timeout=2.0)
        viewer_up = r.status_code < 500
        viewer_detail = f"http://{VIEWER_DEFAULT_HOST}:{VIEWER_DEFAULT_PORT}/ → {r.status_code}"
    except Exception as e:
        viewer_up = False
        viewer_detail = f"not responding ({type(e).__name__}) — `watchmen viewer install`"
    row("viewer", viewer_up, viewer_detail, severity="warn")

    # 6. hooks installed
    try:
        from watchmen import hooks_setup
        import json as _json
        settings = _json.loads(hooks_setup.SETTINGS_FILE.read_text()) if hooks_setup.SETTINGS_FILE.exists() else {}
        wired = sum(
            1 for entries in (settings.get("hooks") or {}).values()
            for e in entries
            for h in e.get("hooks") or []
            if "watchmen" in (h.get("command") or "")
        )
        row("Claude Code hooks", wired > 0, f"{wired} watchmen entries wired" if wired else "not wired — `watchmen hooks install`", severity="warn")
    except Exception as e:
        row("Claude Code hooks", False, f"could not read settings.json ({type(e).__name__})", severity="warn")

    # 7. latest run age
    runs = state.recent_runs(limit=1)
    if runs:
        last = runs[0]
        from datetime import datetime, timezone
        try:
            t = datetime.fromisoformat(last["started_at"])
            if t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc)
            age = datetime.now(timezone.utc) - t
            hours = age.total_seconds() / 3600
            age_str = f"{hours:.1f}h ago" if hours < 48 else f"{age.days}d ago"
            row("latest run", True, f"{last['kind']} for {last['project_key']} — {age_str} ({last['status']})")
        except Exception:
            row("latest run", True, f"{last['kind']} for {last['project_key']}")
    else:
        row("latest run", False, "no runs recorded yet", severity="warn")

    # 8. disk free
    import shutil
    free = shutil.disk_usage(ROOT).free
    free_gb = free / 1024**3
    row("disk free (cwd)", free_gb > 1.0, f"{free_gb:.1f} GiB")

    console.print(table)
    console.print()
    if fails == 0 and warns == 0:
        console.print(f"[green]healthy[/]  [dim]{random.choice(_MANHATTAN_QUOTES_OK)}[/]")
    elif fails == 0:
        console.print(f"[yellow]{warns} warning(s)[/]  [dim]{random.choice(_MANHATTAN_QUOTES_WARN)}[/]")
    else:
        console.print(f"[red]{fails} failure(s)[/]  [yellow]{warns} warning(s)[/]  [dim]{random.choice(_MANHATTAN_QUOTES_FAIL)}[/]")
    return 1 if fails else 0



def cmd_init(args) -> int:
    """Alias for onboard — `init` is the discoverable name; `onboard` kept as a
    hidden alias for muscle memory."""
    from watchmen import onboard
    return onboard.run()


def _deprecate(new_form: str, fn):
    """Wrap a subcommand handler so it prints a soft deprecation hint to stderr
    before delegating. Exit code is unchanged — callers don't break.

    Why dual-form: noun-verb (`daemon install`) is more discoverable + tab-
    completion-friendly than verb-noun-with-hyphens (`install-daemon`), but
    every teammate's scripts + launchd plists use the old form. We keep both
    working and nudge usage toward the new shape via this line."""
    def wrapper(args):
        sys.stderr.write(f"\033[90m[deprecated, use 'watchmen {new_form}']\033[0m\n")
        return fn(args)
    return wrapper


def _add_daemon_run_args(p) -> None:
    """Foreground-daemon args. Same set for old `watchmen daemon` and new
    `watchmen daemon run`."""
    p.add_argument("--once", action="store_true", help="single cycle then exit")
    p.add_argument("--interval", type=int, default=7200, help="seconds between analyst cycles (default 7200 = 2h)")
    p.add_argument("--curator-age", type=int, default=86400)
    p.add_argument("--curator-hours", default="2,14", help="local-time hours when full curator runs (default '2,14' = 2am + 2pm)")
    p.add_argument("--full-curator-min-age", type=int, default=28800, help="min seconds between full curator runs per project (default 8h)")
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--log-file", default=str(Path.home() / "Library" / "Logs" / "watchmen.log"))


def _add_daemon_install_args(p) -> None:
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--interval", type=int, default=7200, help="seconds between analyst cycles (default 7200 = 2h)")
    p.add_argument("--dry-run", action="store_true", help="print plist without installing")


def _add_viewer_run_args(p) -> None:
    p.add_argument("--host", default=config.VIEWER_DEFAULT_HOST)
    p.add_argument("--port", type=int, default=config.viewer_port())


def _add_viewer_install_args(p) -> None:
    p.add_argument("--host", default=config.VIEWER_DEFAULT_HOST)
    p.add_argument("--port", type=int, default=config.viewer_port())
    p.add_argument("--dry-run", action="store_true")


def _add_statusline_install_args(p) -> None:
    p.add_argument("--force", action="store_true", help="overwrite a non-watchmen statusLine entry")


# Subcommand groupings rendered by _print_grouped_help. Order = display order.
# Each entry: (subcommand, one-line description). Hidden aliases are NOT listed.
_HELP_GROUPS: list[tuple[str, list[tuple[str, str]]]] = [
    ("Get started", [
        ("init",       "5-minute interactive setup wizard"),
        ("doctor",     "diagnose your install — API key, corpus, services"),
        ("settings",   "view / update OpenRouter key + per-project settings"),
    ]),
    ("Pipeline", [
        ("status",     "dashboard: tracked projects + last-run summary"),
        ("analyze",    "run analyst on a project (incremental by default)"),
        ("curate",     "run curator (skill bundles + CLAUDE.md)"),
        ("runs",       "recent run history"),
        ("metrics",    "daily token / cost / uptake rollup for a project"),
    ]),
    ("Project inventory", [
        ("list",       "auto-detect projects from corpus"),
        ("track",      "add a project to tracking"),
        ("ingest",     "re-scan ~/.claude/projects into corpus.db"),
        ("sync",       "bootstrap state from existing artifacts on disk"),
    ]),
    ("Background services", [
        ("daemon",     "run / install / uninstall the scheduling daemon"),
        ("viewer",     "run / install / uninstall the local web viewer"),
        ("hooks",      "install / uninstall / inspect Claude Code hooks"),
        ("statusline", "install / uninstall the 💡 watchmen indicator"),
        ("plugin",     "manage the Claude Code plugin marketplace clone"),
        ("launchd",    "inspect installed launchd agents"),
    ]),
    ("Inspect", [
        ("show",       "list / view curated bundles (project, skill, file)"),
        ("why",        "provenance for a skill: source sessions + curator rationale"),
        ("recent",     "git log of curator artifact changes (last N days)"),
        ("insights",   "cross-repo digest — sessions, skills, patterns, friction"),
        ("changelog",  "render the watchmen CHANGELOG.md"),
        ("open",       "open the viewer in your browser"),
        ("logs",       "tail launchd logs (daemon | viewer | all)"),
    ]),
    ("Control", [
        ("pin",        "freeze a skill from regeneration (curator skips it)"),
        ("unpin",      "remove a skill from the pin list"),
        ("drop",       "remove a skill bundle + add to blocklist"),
        ("restore",    "remove a slug from the blocklist"),
        ("learn",      "fast cycle: analyze + light curator (~$0.50)"),
        ("review",     "interactive walk: keep/drop/pin per skill"),
    ]),
]

_COMMON_WORKFLOWS: list[tuple[str, str, str]] = [
    ("First run", "watchmen init", "guided setup, ingest, track, analyze, curate"),
    ("Daily check", "watchmen status", "what changed and what to run next"),
    ("Catch up one repo", "watchmen learn <project>", "incremental analysis + CLAUDE.md refresh"),
    ("Inspect output", "watchmen show <project>", "skills, CLAUDE.md, curator files"),
    ("Debug install", "watchmen doctor", "API key, corpus, services, hooks"),
]


def _print_grouped_help(parser: argparse.ArgumentParser) -> None:
    """Custom help renderer that groups subcommands into sections.

    Argparse can't group subparsers natively — its --help renders a flat list
    of choices that reads like an unsorted soup. We render our own help block
    while leaving argparse parsing alone."""
    print(f"{_bold('watchmen')} v{_version()}  {_dim('local coding-agent memory and skill curation')}\n")
    print(f"{_bold('Usage')}")
    print("  watchmen <command> [options]\n")

    print(_bold("Common workflows"))
    for label, command, desc in _COMMON_WORKFLOWS:
        print(f"  {label:<18} {_cyan(command):<34} {_dim(desc)}")
    print()

    print(_bold("Commands"))
    for group_name, commands in _HELP_GROUPS:
        print(_dim(f"{group_name}:"))
        for cmd, desc in commands:
            print(f"  {cmd:<12}  {desc}")
        print()
    print(_dim("Run `watchmen <command> --help` for command-specific options."))
    print(_dim("Use `watchmen changelog` for release notes."))


def _is_first_run() -> bool:
    """Heuristic: 'fresh install' = no tracked projects AND no corpus.db.
    Used to nudge first-time users toward `watchmen init`."""
    if not STATE_DB.exists() and not _corpus_db_path().exists():
        return True
    try:
        state.init_db()
        return not state.list_projects()
    except Exception:
        return True


def _bare_default() -> int:
    """What `watchmen` (no subcommand) does. Fresh installs see a banner + the
    `init` nudge; users with state see `status` directly."""
    if _is_first_run():
        try:
            from watchmen import banner
            from rich.console import Console
            banner.render(Console())
        except Exception:
            pass
        print(_bold("First run? Get started in 5 minutes:"))
        print(f"  {_dim('$')} watchmen init       # interactive setup wizard")
        print(f"  {_dim('$')} watchmen --help     # full command reference")
        print()
        return 0
    return cmd_status(argparse.Namespace())


def main(argv: list[str] | None = None) -> int:
    parser = WatchmenParser(prog="watchmen", description=__doc__.split("\n")[0], add_help=False)
    # Override the argparse-generated help with our grouped renderer.
    parser.format_help = lambda: ""  # type: ignore[method-assign]
    parser.print_help = lambda *a, **kw: _print_grouped_help(parser)  # type: ignore[method-assign]
    parser.add_argument("-h", "--help", action="help", help=argparse.SUPPRESS)
    parser.add_argument("--version", action="version", version=f"watchmen {_version()}")
    sub = parser.add_subparsers(dest="cmd")

    # Primary canonical entry point — first-time users discover this via --help.
    sub.add_parser("init", help="5-minute interactive setup wizard").set_defaults(func=cmd_init)
    p_doc = sub.add_parser("doctor", help="diagnose your install — API key, corpus, services")
    p_doc.set_defaults(func=cmd_doctor)

    p_open = sub.add_parser("open", help="open the viewer in your default browser")
    p_open.add_argument("project", nargs="?", help="optional project key — jumps to its page")
    p_open.add_argument("--host", default=config.VIEWER_DEFAULT_HOST)
    p_open.add_argument("--port", type=int, default=config.viewer_port())
    p_open.set_defaults(func=cmd_open)

    p_logs = sub.add_parser("logs", help="tail launchd logs (daemon | viewer | all)")
    p_logs.add_argument("name", choices=("daemon", "viewer", "all"), nargs="?", default="all")
    p_logs.add_argument("-f", "--follow", action="store_true", help="tail -F (follow appended lines)")
    p_logs.add_argument("-n", "--lines", type=int, default=50, help="initial lines to print (default 50)")
    p_logs.set_defaults(func=cmd_logs)

    p_show = sub.add_parser("show", help="list / view curated bundles (no args = all projects)")
    p_show.add_argument("project", nargs="?", help="project key (omit to list all)")
    p_show.add_argument("target", nargs="?", help="skill slug or file name (CLAUDE.md, _curation_log.md, …)")
    p_show.add_argument("--raw", action="store_true", help="plain text output (skip Markdown/JSON pretty-printing)")
    p_show.set_defaults(func=cmd_show)

    p_why = sub.add_parser("why", help="provenance for a skill: source sessions + curator rationale")
    p_why.add_argument("project")
    p_why.add_argument("skill", help="skill slug (kebab-case) or display name")
    p_why.set_defaults(func=cmd_why)

    p_recent = sub.add_parser("recent", help="git log of curator artifact changes (no project = all)")
    p_recent.add_argument("project", nargs="?", help="project key (omit for all curated projects)")
    p_recent.add_argument("--days", type=int, default=7, help="lookback window in days (default 7)")
    p_recent.add_argument("--limit", type=int, default=10, help="max commits per project (default 10)")
    p_recent.set_defaults(func=cmd_recent)

    # ── Control commands (Round 2) ─────────────────────────────────────────
    p_pin = sub.add_parser("pin", help="freeze a skill from regeneration")
    p_pin.add_argument("project")
    p_pin.add_argument("skill", help="skill slug or display name")
    p_pin.set_defaults(func=cmd_pin)
    p_unpin = sub.add_parser("unpin", help="remove a skill from the pin list")
    p_unpin.add_argument("project")
    p_unpin.add_argument("skill")
    p_unpin.set_defaults(func=cmd_unpin)
    p_drop = sub.add_parser("drop", help="remove a skill bundle + add to blocklist")
    p_drop.add_argument("project")
    p_drop.add_argument("skill", help="skill slug or display name")
    p_drop.set_defaults(func=cmd_drop)
    p_restore = sub.add_parser("restore", help="remove a slug from the blocklist")
    p_restore.add_argument("project")
    p_restore.add_argument("skill")
    p_restore.set_defaults(func=cmd_restore)

    p_learn = sub.add_parser("learn", help="fast feedback loop: analyze + light curator")
    p_learn.add_argument("project")
    p_learn.add_argument("--full", action="store_true", help="run full curator (Stage 1+2+3) instead of just CLAUDE.md refresh")
    p_learn.add_argument("--model", default=DEFAULT_MODEL)
    p_learn.set_defaults(func=cmd_learn)

    p_review = sub.add_parser("review", help="interactive walk: keep/drop/pin every skill")
    p_review.add_argument("project")
    p_review.set_defaults(func=cmd_review)

    sub.add_parser("status", help="dashboard view").set_defaults(func=cmd_status)
    sub.add_parser("list", help="auto-detect projects from corpus").set_defaults(func=cmd_list)

    p_track = sub.add_parser("track", help="add a project to tracking")
    p_track.add_argument("project", help="project key (used to filter corpus by project_dir substring)")
    p_track.add_argument("--repo", required=True, help="absolute path to source repo on disk")
    p_track.add_argument("--threshold", type=int, default=30, help="min new prompts to trigger run")
    p_track.set_defaults(func=cmd_track)

    sub.add_parser("ingest", help="re-scan ~/.claude/projects into corpus.db").set_defaults(func=cmd_ingest)

    p_sync = sub.add_parser("sync", help="bootstrap state from existing analyses/ + bundles/ on disk")
    p_sync.add_argument("--project", help="just one project (default: all tracked)")
    p_sync.set_defaults(func=cmd_sync)

    p_an = sub.add_parser("analyze", help="run analyst (incremental by default)")
    p_an.add_argument("project")
    p_an.add_argument("--full", action="store_true", help="full re-run (ignore prior thesis)")
    p_an.add_argument("--repo", help="override repo path (only needed if not tracked)")
    p_an.add_argument("--model", default=DEFAULT_MODEL)
    p_an.set_defaults(func=cmd_analyze)

    p_cu = sub.add_parser("curate", help="run curator (skill bundles + CLAUDE.md)")
    p_cu.add_argument("project")
    p_cu.add_argument("--regen-claude", action="store_true", help="rerun stage 3 only (use existing skills)")
    p_cu.add_argument("--model", default=DEFAULT_MODEL)
    p_cu.add_argument("--skip-overlap", action="store_true",
        help="drop candidates that duplicate installed harness skills entirely "
             "(default: propose them as enhancements). Per-project alternative: "
             "`watchmen settings set <p> skip_overlapping_skills true`")
    p_cu.add_argument("--approval-required", dest="approval_required", action="store_true",
        help="route new bundles to bundles/<p>/_pending/ for review via "
             "`watchmen review`. Per-project alternative: "
             "`watchmen settings set <p> approval_required true`")
    p_cu.set_defaults(func=cmd_curate)

    p_runs = sub.add_parser("runs", help="recent run history")
    p_runs.add_argument("--project", help="filter by project_key")
    p_runs.add_argument("--limit", type=int, default=20)
    p_runs.set_defaults(func=cmd_runs)

    # `config` was a P3 placeholder — kept as hidden alias until removed entirely.
    sub.add_parser("config", help=argparse.SUPPRESS).set_defaults(func=cmd_config)

    # ── daemon (noun) ──────────────────────────────────────────────────────
    p_daemon = sub.add_parser("daemon", help="run / install / uninstall the watchmen daemon")
    daemon_sub = p_daemon.add_subparsers(dest="daemon_cmd")
    p_drun = daemon_sub.add_parser("run", help="run scheduling loop in the foreground")
    _add_daemon_run_args(p_drun)
    p_drun.set_defaults(func=cmd_daemon)
    p_dins = daemon_sub.add_parser("install", help="install launchd agent for autostart on login")
    _add_daemon_install_args(p_dins)
    p_dins.set_defaults(func=cmd_install_daemon)
    daemon_sub.add_parser("uninstall", help="remove the launchd agent").set_defaults(func=cmd_uninstall_daemon)
    p_daemon.set_defaults(func=lambda a: (p_daemon.print_help() or 1))

    # ── viewer (noun) ──────────────────────────────────────────────────────
    p_viewer = sub.add_parser("viewer", help="run / install / uninstall the local web viewer")
    viewer_sub = p_viewer.add_subparsers(dest="viewer_cmd")
    p_vrun = viewer_sub.add_parser("run", help=f"start the viewer in the foreground ({config.viewer_base_url()})")
    _add_viewer_run_args(p_vrun)
    p_vrun.set_defaults(func=cmd_viewer)
    p_vins = viewer_sub.add_parser("install", help="install launchd agent for autostart on login")
    _add_viewer_install_args(p_vins)
    p_vins.set_defaults(func=cmd_install_viewer)
    viewer_sub.add_parser("uninstall", help="remove the launchd agent").set_defaults(func=cmd_uninstall_viewer)
    p_viewer.set_defaults(func=lambda a: (p_viewer.print_help() or 1))

    # ── hooks (noun) ───────────────────────────────────────────────────────
    p_hooks = sub.add_parser("hooks", help="install / uninstall / inspect Claude Code hooks")
    hooks_sub = p_hooks.add_subparsers(dest="hooks_cmd")
    hooks_sub.add_parser("install", help="wire watchmen_observe.sh into ~/.claude/settings.json").set_defaults(func=cmd_install_hooks)
    hooks_sub.add_parser("uninstall", help="remove watchmen entries from ~/.claude/settings.json").set_defaults(func=cmd_uninstall_hooks)
    hooks_sub.add_parser("status", help="show which hook events are wired up").set_defaults(func=cmd_hooks_status)
    p_hooks.set_defaults(func=lambda a: (p_hooks.print_help() or 1))

    # ── statusline (noun) ──────────────────────────────────────────────────
    p_sl = sub.add_parser("statusline", help="install / uninstall the 💡 watchmen indicator")
    sl_sub = p_sl.add_subparsers(dest="statusline_cmd")
    p_slin = sl_sub.add_parser("install", help="wire the 💡 watchmen indicator into ~/.claude/settings.json")
    _add_statusline_install_args(p_slin)
    p_slin.set_defaults(func=cmd_install_statusline)
    sl_sub.add_parser("uninstall", help="remove the watchmen statusLine entry").set_defaults(func=cmd_uninstall_statusline)
    p_sl.set_defaults(func=lambda a: (p_sl.print_help() or 1))

    # ── plugin (noun) ──────────────────────────────────────────────────────
    p_plug = sub.add_parser("plugin", help="manage the watchmen Claude Code plugin marketplace clone")
    plug_sub = p_plug.add_subparsers(dest="plugin_cmd")
    plug_sub.add_parser("update", help="git pull the marketplace clone so /plugin install picks up the latest").set_defaults(func=cmd_update_plugin)
    plug_sub.add_parser("status", help="show plugin marketplace + cache + statusLine state").set_defaults(func=cmd_plugin_status)
    p_plug.set_defaults(func=lambda a: (p_plug.print_help() or 1))

    # ── launchd (noun) ─────────────────────────────────────────────────────
    p_ld = sub.add_parser("launchd", help="inspect the watchmen launchd agents")
    ld_sub = p_ld.add_subparsers(dest="launchd_cmd")
    ld_sub.add_parser("status", help="show installed/loaded launchd agents").set_defaults(func=cmd_launchd_status)
    p_ld.set_defaults(func=lambda a: (p_ld.print_help() or 1))

    # ── deprecated aliases ─────────────────────────────────────────────────
    # Old verb-noun-with-hyphens forms. Keep working, print soft deprecation
    # line. Plan: remove after 1-2 releases once teammates' scripts update.
    # All deprecated aliases use argparse.SUPPRESS so they don't pollute --help,
    # but still parse correctly for existing scripts.
    #
    # Each tuple = (old_subcommand, new_form, handler, optional arg-adder).
    # The arg-adder is for the few aliases that need to accept the same args
    # as the new form (install-daemon, install-viewer, install-statusline).
    _DEPRECATED_ALIASES: list[tuple[str, str, "object", "object"]] = [
        ("install-daemon",      "daemon install",       cmd_install_daemon,      _add_daemon_install_args),
        ("install-viewer",      "viewer install",       cmd_install_viewer,      _add_viewer_install_args),
        ("uninstall-daemon",    "daemon uninstall",     cmd_uninstall_daemon,    None),
        ("uninstall-viewer",    "viewer uninstall",     cmd_uninstall_viewer,    None),
        ("launchd-status",      "launchd status",       cmd_launchd_status,      None),
        ("install-hooks",       "hooks install",        cmd_install_hooks,       None),
        ("uninstall-hooks",     "hooks uninstall",      cmd_uninstall_hooks,     None),
        ("hooks-status",        "hooks status",         cmd_hooks_status,        None),
        ("install-statusline",  "statusline install",   cmd_install_statusline,  _add_statusline_install_args),
        ("uninstall-statusline","statusline uninstall", cmd_uninstall_statusline, None),
        ("update-plugin",       "plugin update",        cmd_update_plugin,       None),
        ("plugin-status",       "plugin status",        cmd_plugin_status,       None),
    ]
    for old, new_form, handler, arg_adder in _DEPRECATED_ALIASES:
        p_alias = sub.add_parser(old, help=argparse.SUPPRESS)
        if arg_adder is not None:
            arg_adder(p_alias)
        p_alias.set_defaults(func=_deprecate(new_form, handler))

    # `onboard` / `reonboard` are hidden aliases — `init` is the canonical name.
    sub.add_parser("onboard", help=argparse.SUPPRESS).set_defaults(func=cmd_onboard)
    sub.add_parser("reonboard", help=argparse.SUPPRESS).set_defaults(func=cmd_reonboard)

    p_settings = sub.add_parser("settings", help="view / update per-project settings")
    settings_sub = p_settings.add_subparsers(dest="settings_cmd")
    settings_sub.add_parser("list", help="show all tracked projects + their settings").set_defaults(func=cmd_settings_list)
    p_show = settings_sub.add_parser("show", help="show one project's full settings")
    p_show.add_argument("project")
    p_show.set_defaults(func=cmd_settings_show)
    p_set = settings_sub.add_parser("set", help=f"update a setting. keys: {', '.join(_SETTABLE_KEYS)}")
    p_set.add_argument("project")
    p_set.add_argument("key", choices=_SETTABLE_KEYS)
    p_set.add_argument("value")
    p_set.set_defaults(func=cmd_settings_set)
    p_apikey = settings_sub.add_parser("api-key", help="set or check the OpenRouter API key (live-validated against /auth/key)")
    p_apikey.add_argument("--check", action="store_true", help="check current key without changing it")
    p_apikey.add_argument("--set", metavar="KEY", help="set a key non-interactively (for scripting)")
    p_apikey.set_defaults(func=cmd_settings_api_key)
    p_port = settings_sub.add_parser("port", help="get or set the viewer port (writes to ~/.config/watchmen/.env)")
    p_port.add_argument("value", nargs="?", help="new port (omit to print current)")
    p_port.set_defaults(func=cmd_settings_port)
    p_settings.set_defaults(func=lambda a: (p_settings.print_help() or 1))

    p_metrics = sub.add_parser("metrics", help="daily efficiency rollup (no project = global rollup)")
    p_metrics.add_argument("project", nargs="?", help="project key (omit for global rollup across all projects)")
    p_metrics.add_argument("--days", type=int, default=30, help="window length (default 30)")
    p_metrics.set_defaults(func=cmd_metrics)

    p_insights = sub.add_parser(
        "insights",
        help="cross-repo digest: sessions, skills, patterns, friction (pairs with Anthropic's /insights)",
    )
    p_insights.add_argument("--no-llm", action="store_true",
        help="skip the LLM digest entirely (faster, no API call)")
    p_insights.add_argument("--regenerate", action="store_true",
        help="force a fresh digest run, skipping the view/regenerate prompt")
    p_insights.add_argument("--view", action="store_true",
        help="render the latest saved digest, skipping the prompt")
    p_insights.add_argument("--list", dest="list_digests", action="store_true",
        help="list all saved digests in ~/.watchmen/insights/ and exit")
    p_insights.add_argument("--model", default=DEFAULT_MODEL,
        help=f"OpenRouter model for the digest (default: {DEFAULT_MODEL})")
    p_insights.set_defaults(func=cmd_insights)

    sub.add_parser(
        "changelog",
        help="render the watchmen CHANGELOG.md (the auto-announcement on version bumps is the inline summary; this is the full list)",
    ).set_defaults(func=cmd_changelog)

    args = parser.parse_args(argv)
    # Auto-apply any pending corpus.db schema migrations before dispatching
    # to a handler. Idempotent + cheap — a single PRAGMA when the schema
    # is current. Means users only need to pull + rerun to pick up schema
    # changes; no separate `watchmen ingest --full` step required.
    try:
        from watchmen import corpus as _corpus
        _corpus.migrate_schema()
    except Exception:
        pass
    # Announce release notes on the first run after a version bump. Silent
    # on subsequent runs of the same version. `watchmen changelog` is the
    # on-demand alternative if the auto-announcement scrolled off.
    _show_release_notes_if_bumped()
    if not args.cmd:
        return _bare_default()
    try:
        return args.func(args)
    except sqlite3.OperationalError as e:
        if "unable to open database file" in str(e):
            _print_runtime_state_error(e)
            return 1
        raise
    except PermissionError as e:
        _print_runtime_state_error(e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
