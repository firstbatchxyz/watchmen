"""watchmen daemon — continuous scheduling loop.

Cycle (default 30 min):
  1. Re-ingest corpus (scan ~/.claude/projects → corpus.db)
  2. For each tracked + enabled project:
     a. Check needs_analysis (new prompts > threshold)
     b. If yes: run incremental analyst
     c. If analyst ran successfully AND last CLAUDE.md regen > 24h: regen stage 3 (CLAUDE.md only)

Stage 1+2 (skill bundles) is intentionally NOT periodic — too expensive to run unattended,
needs human review. Run manually via `watchmen curate <project>`.

Logs to the platform-conventional location (~/Library/Logs on macOS,
%LOCALAPPDATA%\\watchmen\\logs on Windows, ~/.watchmen/logs on Linux) so
scheduler output survives across runs. Graceful shutdown on SIGTERM/SIGINT.

Usage:
  uv run watchmen daemon                  # run forever
  uv run watchmen daemon --once           # single iteration (testing)
  uv run watchmen daemon --interval 600   # check every 10 min
"""

import argparse
import logging
import logging.handlers
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from watchmen import state
from watchmen.paths import ANALYSES_DIR, BUNDLES_DIR
from watchmen.util import classify_run_failure

ROOT = Path(__file__).parent
DEFAULT_INTERVAL = 7200       # 2 hours between analyst checks
DEFAULT_CURATOR_AGE = 86400   # 24 h — minimum age before stage 3 regen is allowed
# Full skill generation is expensive, accumulates durable agent behavior, and
# needs review. Keep scheduling as an explicit opt-in for existing power users,
# but do not run it unattended by default.
DEFAULT_FULL_CURATOR_HOURS = ""
DEFAULT_FULL_CURATOR_MIN_AGE = 28800  # min 8 h between full curator runs per project
def _default_model() -> str:
    """Daemon default model — pulled from active provider so the scheduled
    runs follow whichever auth the user configured."""
    from watchmen import config
    return config.default_model()


# Resolved at import time. The daemon binary boots once and stays running,
# so the value is fixed for the life of the process — restart the daemon
# after switching provider.
DEFAULT_MODEL = _default_model()


def _default_log_path() -> Path:
    """Platform-conventional location for the daemon's primary log file.

    macOS: ~/Library/Logs/watchmen.log (where `Console.app` looks).
    Windows: %LOCALAPPDATA%\\watchmen\\logs\\watchmen.log.
    Linux + everything else: ~/.watchmen/logs/watchmen.log.
    """
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Logs" / "watchmen.log"
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
        return base / "watchmen" / "logs" / "watchmen.log"
    return Path.home() / ".watchmen" / "logs" / "watchmen.log"


DEFAULT_LOG = _default_log_path()

_shutdown = False


def setup_logging(log_path: Path) -> logging.Logger:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("watchmen.daemon")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    fmt = logging.Formatter("[%(asctime)s] %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    fh = logging.handlers.RotatingFileHandler(str(log_path), maxBytes=10_000_000, backupCount=3)
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    return logger


def handle_signal(signum, frame):
    global _shutdown
    _shutdown = True


def _ingest_corpus(log: logging.Logger) -> None:
    log.info("ingest: rescanning ~/.claude/projects")
    r = subprocess.run(
        [sys.executable, "-m", "watchmen.corpus", "scan"],
        cwd=str(ROOT), capture_output=True, text=True, timeout=600,
    )
    if r.returncode != 0:
        log.error("ingest failed: %s", (r.stderr or r.stdout)[:500])
    else:
        last_line = (r.stdout or "").strip().splitlines()[-1] if r.stdout else ""
        log.info("ingest done: %s", last_line)


def _failure_notes(project_key: str, r: "subprocess.CompletedProcess") -> str:
    """Concise runs.notes for a failed daemon run — folds the captured
    stderr/stdout into the shared classifier (which also reads the bundle
    _run.log tail) so a provider rate-limit reads as 'rate_limit: <provider>'
    instead of 'exit 1'."""
    return classify_run_failure(
        project_key, r.returncode, f"{r.stderr or ''}\n{r.stdout or ''}"
    )


def _run_analyst(project_key: str, model: str, log: logging.Logger) -> bool:
    log.info("analyst[%s] starting (incremental)", project_key)
    progress = state.get_project_progress(project_key)
    from_day = progress.get("last_analyst_day")
    cmd = [sys.executable, "-m", "watchmen.analyze", "-p", project_key, "--model", model]
    if from_day:
        cmd.extend(["--from-day", from_day])

    run_id = state.start_run(project_key, "analyst", notes=f"daemon:from_day={from_day}")
    r = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=14400)
    if r.returncode != 0:
        log.error("analyst[%s] failed: %s", project_key, (r.stderr or r.stdout)[:500])
        state.finish_run(run_id, "failed", notes=_failure_notes(project_key, r))
        return False

    analyses_dir = ANALYSES_DIR / project_key
    if analyses_dir.exists():
        day_files = sorted(p.stem for p in analyses_dir.glob("20*.md"))
        if day_files:
            state.update_project(project_key, last_analyst_day=day_files[-1], last_analyst_run=state.now_iso())
    state.finish_run(run_id, "ok")
    last_line = (r.stdout or "").strip().splitlines()[-1] if r.stdout else ""
    log.info("analyst[%s] done: %s", project_key, last_line)
    return True


def _regen_claude_md(project_key: str, model: str, log: logging.Logger) -> bool:
    proj = state.get_project(project_key)
    if not proj:
        return False
    log.info("regen-claude[%s] starting (stage 3 only)", project_key)
    cmd = [sys.executable, "-m", "watchmen.curate",
           "--project", project_key, "--repo", proj["source_repo"],
           "--model", model, "--skip-finder", "--skip-skills"]
    run_id = state.start_run(project_key, "curator-claude-only", notes="daemon")
    r = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=3600)
    if r.returncode != 0:
        log.error("regen-claude[%s] failed: %s", project_key, (r.stderr or r.stdout)[:500])
        state.finish_run(run_id, "failed", notes=_failure_notes(project_key, r))
        return False
    state.update_project(project_key, last_curator_run=state.now_iso())
    state.finish_run(run_id, "ok", notes="claude.md regen")
    log.info("regen-claude[%s] done", project_key)
    return True


def _run_full_curator(project_key: str, model: str, log: logging.Logger) -> bool:
    """Full curator: stages 1+2+3 — finds candidates, builds skill bundles, regens CLAUDE.md."""
    proj = state.get_project(project_key)
    if not proj:
        return False
    log.info("full-curator[%s] starting (stages 1+2+3)", project_key)
    cmd = [sys.executable, "-m", "watchmen.curate",
           "--project", project_key, "--repo", proj["source_repo"], "--model", model]
    run_id = state.start_run(project_key, "curator-full", notes="daemon-scheduled")
    r = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=21600)  # 6 hour ceiling
    if r.returncode != 0:
        log.error("full-curator[%s] failed: %s", project_key, (r.stderr or r.stdout)[:500])
        state.finish_run(run_id, "failed", notes=_failure_notes(project_key, r))
        return False
    skills_dir = BUNDLES_DIR / project_key / "skills"
    skill_count = sum(1 for d in skills_dir.iterdir() if d.is_dir()) if skills_dir.exists() else 0
    state.update_project(project_key, last_curator_run=state.now_iso(), last_curator_skill_count=skill_count)
    state.finish_run(run_id, "ok", notes=f"{skill_count} skills")
    log.info("full-curator[%s] done — %d skills", project_key, skill_count)
    return True


def _should_run_full_curator(now: datetime, last_run: datetime | None, scheduled_hours: list[int], min_age_seconds: int) -> bool:
    """Run full curator iff: current hour is in scheduled_hours AND last run was > min_age_seconds ago."""
    if now.hour not in scheduled_hours:
        return False
    if last_run is None:
        return True
    return (now - last_run).total_seconds() >= min_age_seconds


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def cycle_once(
    log: logging.Logger,
    model: str,
    curator_age_seconds: int,
    scheduled_curator_hours: list[int],
    full_curator_min_age: int,
) -> None:
    state.init_db()
    now = datetime.now(timezone.utc)
    log.info("─── cycle start (utc=%s, local_hour=%d) ───", now.isoformat(timespec="seconds"), datetime.now().hour)
    _ingest_corpus(log)

    projects = [p for p in state.list_projects() if p.get("enabled", 1)]
    if not projects:
        log.info("no enabled tracked projects, nothing to do")
        return

    local_now = datetime.now()  # for scheduled-hours check, use local time

    for p in projects:
        key = p["project_key"]
        progress = state.get_project_progress(key)
        if progress.get("error"):
            log.warning("[%s] %s — skipping", key, progress["error"])
            continue

        new_prompts = progress.get("new_prompts_since_last_analysis", 0)
        threshold = p.get("threshold_new_prompts", 30)
        log.info("[%s] new_prompts=%s threshold=%s", key, new_prompts, threshold)

        analyst_ran = False
        if new_prompts >= threshold:
            analyst_ran = _run_analyst(key, model, log)
        else:
            log.info("[%s] below threshold — skipping analyst", key)

        last_curator = _parse_iso(p.get("last_curator_run"))
        skills_dir = BUNDLES_DIR / key / "skills"
        has_bundles = skills_dir.exists() and any(d.is_dir() for d in skills_dir.iterdir())

        # Optional scheduled full curator — disabled unless --curator-hours is set.
        if _should_run_full_curator(local_now, _parse_iso(p.get("last_curator_run")) and
                                    _parse_iso(p.get("last_curator_run")).replace(tzinfo=None),
                                    scheduled_curator_hours, full_curator_min_age):
            log.info("[%s] scheduled full curator window (hour=%d, last=%s)", key, local_now.hour, p.get("last_curator_run"))
            _run_full_curator(key, model, log)
            continue  # full curator covers stage 3 too; skip regen below

        # Otherwise: light stage-3-only regen if analyst added new content AND last regen is stale
        too_old = (
            last_curator is None
            or (now - last_curator).total_seconds() > curator_age_seconds
        )
        if analyst_ran and has_bundles and too_old:
            _regen_claude_md(key, model, log)
        elif analyst_ran and not has_bundles:
            log.info("[%s] no skill bundles yet — run `watchmen curate %s` when ready", key, key)

    log.info("─── cycle end ───\n")


def run(args) -> int:
    log = setup_logging(Path(args.log_file).expanduser())
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    scheduled_hours = [int(h.strip()) for h in args.curator_hours.split(",") if h.strip()]
    log.info(
        "watchmen daemon starting (interval=%ss, model=%s, curator_age=%ss, "
        "full_curator_hours=%s, full_curator_min_age=%ss, log=%s)",
        args.interval, args.model, args.curator_age, scheduled_hours,
        args.full_curator_min_age, args.log_file,
    )
    log.info("pid=%d cwd=%s", os.getpid(), os.getcwd())

    if args.once:
        try:
            cycle_once(log, args.model, args.curator_age, scheduled_hours, args.full_curator_min_age)
        except Exception as e:
            log.exception("cycle failed: %s", e)
            return 1
        return 0

    while not _shutdown:
        try:
            cycle_once(log, args.model, args.curator_age, scheduled_hours, args.full_curator_min_age)
        except Exception as e:
            log.exception("cycle failed: %s", e)

        # Sleep with shutdown polling
        slept = 0
        while slept < args.interval and not _shutdown:
            time.sleep(min(5, args.interval - slept))
            slept += 5

    log.info("watchmen daemon shutting down")
    return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="run one cycle and exit (testing)")
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL, help=f"seconds between cycles (default {DEFAULT_INTERVAL} = {DEFAULT_INTERVAL//3600}h)")
    parser.add_argument("--curator-age", type=int, default=DEFAULT_CURATOR_AGE, help="stage-3 regen allowed only if last CLAUDE.md is older than this (default 24h)")
    parser.add_argument(
        "--curator-hours",
        default=DEFAULT_FULL_CURATOR_HOURS,
        help="optional local-time hours for full skill curation, e.g. '2,14' (disabled by default)",
    )
    parser.add_argument("--full-curator-min-age", type=int, default=DEFAULT_FULL_CURATOR_MIN_AGE, help=f"minimum seconds between full curator runs per project (default {DEFAULT_FULL_CURATOR_MIN_AGE} = {DEFAULT_FULL_CURATOR_MIN_AGE//3600}h)")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--log-file", default=str(DEFAULT_LOG))
    args = parser.parse_args()
    sys.exit(run(args))


if __name__ == "__main__":
    main()
