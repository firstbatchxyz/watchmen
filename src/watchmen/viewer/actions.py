"""Next-best-action ranking + web-triggered run dispatch.

Two concerns live here:

1. `next_best_actions()` walks tracked projects, scores them on a few
   signals (stale analysis, pending review queue, friction, untapped),
   and returns a small ranked list the viewer renders as a banner. This
   is the "what should I do next?" surface — the cross-page equivalent
   of `watchmen status`.

2. `start_run()` / `get_run()` / `list_runs()` manage subprocess-launched
   CLI invocations triggered from the browser. Each run gets a UUID, a
   log file under `~/.watchmen/web-runs/`, and a small JSON sidecar with
   pid + status + command. The viewer tails the log with a meta-refresh
   poll until the process exits.

Both pieces are intentionally small + standalone so server.py stays
readable. State files live alongside the daemon's existing storage so
`watchmen recent` / `watchmen runs` will eventually surface these too.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import uuid
from datetime import datetime
from pathlib import Path

from watchmen import metrics, state
from watchmen.paths import BUNDLES_DIR, WATCHMEN_HOME
from watchmen.util import repo_friction_signals

# Where we store per-run metadata + logs. Mirrors the daemon's
# ~/.watchmen/launchd/ + ~/.watchmen/logs/ structure so a future
# `watchmen runs` enhancement can pick these up uniformly.
WEB_RUNS_DIR = WATCHMEN_HOME / "web-runs"

# Whitelisted actions invocable from the browser. Each entry maps to the
# argv suffix appended to `watchmen ...`. All three take a project key as
# the last argv element. Interactive commands (review) stay out — they
# need a tty. Project-less commands (ingest, status) also stay out — the
# `project_key` field would be meaningless and confusing.
RUNNABLE_ACTIONS: dict[str, list[str]] = {
    "analyze": ["analyze"],
    "curate":  ["curate"],
    "learn":   ["learn"],
}


# ── Next-best-action ranking ──────────────────────────────────────────


def _project_keys_with_bundles() -> list[str]:
    base = BUNDLES_DIR
    if not base.exists():
        return []
    return sorted(d.name for d in base.iterdir() if d.is_dir())


def _skills_count(project_key: str) -> int:
    d = BUNDLES_DIR / project_key / "skills"
    if not d.exists():
        return 0
    return sum(1 for x in d.iterdir() if x.is_dir())


def _pending_count(project_key: str) -> int:
    d = BUNDLES_DIR / project_key / "_pending"
    if not d.exists():
        return 0
    return sum(1 for x in d.iterdir() if x.is_dir())


def next_best_actions(
    project_key: str | None = None, limit: int = 5
) -> list[dict]:
    """Score every tracked project and return up to `limit` ranked actions.

    When `project_key` is given, restricts to that project — used by the
    per-project banner. When None, ranks across the whole workspace.

    Each action dict has: severity, kind, title, reason, project_key,
    command (always shown, copyable), run_action (None or key in
    RUNNABLE_ACTIONS — drives the Run button), href (optional non-CLI
    link like /insights for a friction deep-dive).
    """
    try:
        state.init_db()
        projects = state.list_projects()
    except Exception:
        projects = []
    if project_key:
        projects = [p for p in projects if p["project_key"] == project_key]

    actions: list[dict] = []

    for proj in projects:
        key = proj["project_key"]

        # Signal 1: new prompts since last analyst run.
        new_prompts = 0
        last_day = proj.get("last_analyst_day")
        try:
            prog = state.get_project_progress(key)
            new_prompts = prog.get("new_prompts_since_last_analysis", 0) or 0
            needs = bool(prog.get("needs_analysis"))
        except Exception:
            needs = False
        if needs:
            severity = "high" if new_prompts >= 100 else "medium"
            ago = ""
            if last_day:
                try:
                    delta = (datetime.utcnow().date() - datetime.strptime(last_day, "%Y-%m-%d").date()).days
                    ago = f" · last analysis {delta}d ago" if delta > 0 else ""
                except ValueError:
                    pass
            actions.append({
                "severity": severity,
                "kind": "stale_analysis",
                "project_key": key,
                "title": f"{key}: {new_prompts} new prompts to analyze",
                "reason": f"Threshold reached{ago}. Run analysis to refresh the corpus + CLAUDE.md.",
                "command": f"watchmen analyze {key}",
                "run_action": "analyze",
                "href": None,
            })

        # Signal 2: pending review queue. Surface even when small — the
        # whole point is that they sit there forever otherwise.
        n_pending = _pending_count(key)
        if n_pending:
            severity = "high" if n_pending >= 3 else "medium"
            actions.append({
                "severity": severity,
                "kind": "pending_review",
                "project_key": key,
                "title": f"{key}: {n_pending} skill candidate{'s' if n_pending != 1 else ''} awaiting review",
                "reason": "Curator proposed but didn't approve. Use `watchmen review` to keep/drop.",
                "command": f"watchmen review {key}",
                "run_action": None,  # interactive — can't run from web
                "href": f"/p/{key}",
            })

        # Signal 3: tracked, but no skills curated yet.
        if _skills_count(key) == 0 and n_pending == 0:
            try:
                daily = metrics.daily_metrics(key, days=30) or []
                sess_total = sum((d.get("sessions") or 0) for d in daily)
            except Exception:
                sess_total = 0
            if sess_total > 0:
                actions.append({
                    "severity": "medium",
                    "kind": "untapped",
                    "project_key": key,
                    "title": f"{key}: tracked, {sess_total} sessions, 0 skills curated",
                    "reason": "Run the curator to bootstrap a skill bundle from your sessions.",
                    "command": f"watchmen curate {key}",
                    "run_action": "curate",
                    "href": None,
                })

        # Signal 4: high friction.
        try:
            tool_errors, _top_err, frust_count, _samples = repo_friction_signals(key)
        except Exception:
            tool_errors, frust_count = 0, 0
        if tool_errors >= 10 or frust_count >= 5:
            bits = []
            if tool_errors:
                bits.append(f"{tool_errors} tool errors")
            if frust_count:
                bits.append(f"{frust_count} frustration markers")
            actions.append({
                "severity": "medium",
                "kind": "high_friction",
                "project_key": key,
                "title": f"{key}: {' · '.join(bits)} in recent sessions",
                "reason": "Cluster may indicate a missing skill or a brittle adapter. Check the insights digest.",
                "command": f"watchmen insights",
                "run_action": None,
                "href": "/insights",
            })

    # Stable sort by severity (high > medium > low), preserving project
    # discovery order so the same project's items cluster together.
    severity_rank = {"high": 0, "medium": 1, "low": 2}
    actions.sort(key=lambda a: severity_rank.get(a["severity"], 9))
    return actions[:limit]


# ── Web-run dispatch + storage ────────────────────────────────────────


def _runs_dir() -> Path:
    WEB_RUNS_DIR.mkdir(parents=True, exist_ok=True)
    return WEB_RUNS_DIR


_SAFE_KEY = re.compile(r"^[A-Za-z0-9_.-]+$")


def _validate_project(project_key: str) -> str:
    """Defense-in-depth: project keys feed straight into subprocess argv.
    Reject anything that isn't a plain identifier — even though `watchmen
    analyze ...` arg-parses safely, we don't want this surface to grow
    its own injection class."""
    if not project_key or not _SAFE_KEY.match(project_key):
        raise ValueError(f"invalid project key: {project_key!r}")
    return project_key


def start_run(action: str, project_key: str) -> dict:
    """Spawn `watchmen <action> <project>` in a detached subprocess. Logs
    stream to a per-run file. Returns the run-metadata dict; viewer
    redirects to /actions/run/<id> which tails the log."""
    if action not in RUNNABLE_ACTIONS:
        raise ValueError(f"action {action!r} not runnable from web")
    project_key = _validate_project(project_key)

    binary = shutil.which("watchmen")
    if not binary:
        raise RuntimeError(
            "`watchmen` CLI not found on PATH — install with "
            "`pipx install dria-watchmen` or `uv tool install dria-watchmen`."
        )

    rid = uuid.uuid4().hex[:12]
    started_at = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    runs = _runs_dir()
    log_path = runs / f"{rid}.log"
    meta_path = runs / f"{rid}.json"
    argv = [binary, *RUNNABLE_ACTIONS[action], project_key]

    log_fh = open(log_path, "w", buffering=1, encoding="utf-8")
    log_fh.write(
        f"$ {' '.join(argv)}\n"
        f"# watchmen web-run · started {started_at}\n"
        f"# pid: (pending)\n\n"
    )
    log_fh.flush()
    # start_new_session detaches the child so the viewer process can be
    # restarted (or crash) without orphaning or killing the run.
    proc = subprocess.Popen(
        argv,
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        cwd=str(Path.home()),
        env={**os.environ},
    )
    meta = {
        "id": rid,
        "action": action,
        "project_key": project_key,
        "argv": argv,
        "pid": proc.pid,
        "started_at": started_at,
        "log_path": str(log_path),
        "meta_path": str(meta_path),
    }
    meta_path.write_text(json.dumps(meta, indent=2))
    return meta


def _pid_alive(pid: int) -> bool:
    """Is the subprocess still alive? Reaps zombies via waitpid(WNOHANG)
    before falling back to kill(0). A zombie counts as kill(0)-alive on
    Unix until reaped, which would otherwise leave the viewer's
    "running" pill stuck forever after a fast-exiting subprocess."""
    if not pid:
        return False
    try:
        wpid, _status = os.waitpid(pid, os.WNOHANG)
        if wpid == pid:
            # Reaped — process is done.
            return False
    except ChildProcessError:
        # Not our child (already reaped, or detached past our scope).
        pass
    except OSError:
        pass
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False
    except OSError:
        return False


def get_run(run_id: str) -> dict | None:
    runs = _runs_dir()
    meta_path = runs / f"{run_id}.json"
    if not meta_path.exists():
        return None
    meta = json.loads(meta_path.read_text())
    log_path = Path(meta["log_path"])
    log_text = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
    pid = int(meta.get("pid", 0))
    alive = _pid_alive(pid) if pid else False
    meta["alive"] = alive
    meta["status"] = "running" if alive else "done"
    meta["log_text"] = log_text
    meta["log_size"] = log_path.stat().st_size if log_path.exists() else 0
    # Best-effort wall-clock duration in seconds.
    try:
        started_dt = datetime.fromisoformat(meta["started_at"].rstrip("Z"))
        meta["duration_s"] = max(0, int((datetime.utcnow() - started_dt).total_seconds()))
    except Exception:
        meta["duration_s"] = None
    return meta


def list_runs(limit: int = 20) -> list[dict]:
    runs = _runs_dir()
    metas = sorted(runs.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]
    out = []
    for p in metas:
        try:
            m = json.loads(p.read_text())
        except json.JSONDecodeError:
            continue
        pid = int(m.get("pid", 0))
        m["alive"] = _pid_alive(pid) if pid else False
        m["status"] = "running" if m["alive"] else "done"
        out.append(m)
    return out


# Used by smoke tests + a future "tail this run live" feature.
def _wait_for_finish(run_id: str, timeout_s: float = 5.0) -> dict | None:
    """Poll get_run() until status == "done" or timeout. Test-only helper."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        m = get_run(run_id)
        if m and m["status"] == "done":
            return m
        time.sleep(0.05)
    return get_run(run_id)
