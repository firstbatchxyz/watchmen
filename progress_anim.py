"""Compact Dr. Manhattan progress animation for the analyst stage.

Renders a small radial-burst panel (36×7 grid) with a single ring of streaks,
a sparse starfield, and a breathing center mark. Drives off analyze.py's
stdout — parses "Running on N days" for the total and counts "done in" /
"cached" lines for progress.

Used by onboard.py (single-project path) and cli.py:cmd_analyze.
Multi-project parallel runs in onboard.py keep the existing line-based output
since multiple Live panels can't coexist.
"""

from __future__ import annotations

import math
import queue
import random
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.text import Text


ROOT = Path(__file__).parent
FAST_FPS = 14


_BREATHING_SYMBOLS = ["⊙", "◉", "●", "◉", "⊙", "○"]


def _angle_char(angle_deg: float) -> str:
    angle_deg = angle_deg % 360
    if angle_deg < 11 or angle_deg >= 349: return "─"
    if angle_deg < 34:  return "⟋"
    if angle_deg < 56:  return "╱"
    if angle_deg < 79:  return "╱"
    if angle_deg < 101: return "│"
    if angle_deg < 124: return "╲"
    if angle_deg < 146: return "╲"
    if angle_deg < 169: return "⟍"
    if angle_deg < 191: return "─"
    if angle_deg < 214: return "⟋"
    if angle_deg < 236: return "╱"
    if angle_deg < 259: return "╱"
    if angle_deg < 281: return "│"
    if angle_deg < 304: return "╲"
    if angle_deg < 326: return "╲"
    return "⟍"


def render(done: int, total: int, frame: int, project: str) -> Panel:
    """The compact Manhattan panel. 36-col radial burst over a sparse star
    field, single ring of streaks (count scales gently with progress), and
    a breathing center mark."""
    progress = done / max(total, 1)
    cols, rows = 36, 7
    cx, cy = cols // 2, rows // 2

    cells: list[list[tuple[str, str]]] = [[(" ", "")] * cols for _ in range(rows)]

    # Sparse starfield — 3-5 stars, slow drift via seeded RNG that changes
    # every ~0.7s.
    star_rng = random.Random(frame // 10)
    for _ in range(4):
        x = star_rng.randint(0, cols - 1)
        y = star_rng.randint(0, rows - 1)
        if cells[y][x][0] == " ":
            ch = star_rng.choice(["·", "·", "*", "+"])
            cells[y][x] = (ch, "dim cyan")

    # Single ring of streaks. Counts grow gently with progress.
    rotate = frame * 2.2
    n_streaks = 8 + int(progress * 6)
    for i in range(n_streaks):
        theta = math.radians((i * 360 / n_streaks + rotate) % 360)
        max_r = 2 + (i % 2) + int(progress * 1.5)
        for r in range(1, max_r):
            x = cx + int(round(math.cos(theta) * r * 1.6))
            y = cy + int(round(math.sin(theta) * r * 0.8))
            if 0 <= x < cols and 0 <= y < rows:
                ch = _angle_char(math.degrees(theta))
                style = "bold bright_cyan" if r == 1 else "cyan"
                cells[y][x] = (ch, style)

    # Center — breathing Manhattan mark.
    center_ch = _BREATHING_SYMBOLS[(frame // 3) % len(_BREATHING_SYMBOLS)]
    cells[cy][cx] = (center_ch, "bold bright_white")

    body = Text()
    for row in cells:
        for ch, style in row:
            body.append(ch, style=style or "dim")
        body.append("\n")

    bar_filled = int(progress * 24)
    body.append("\n")
    body.append(f"  {project}", style="bold bright_cyan")
    body.append(f"  day {done}/{total}  ", style="bright_white")
    body.append("━" * bar_filled, style="bright_cyan")
    body.append("─" * (24 - bar_filled), style="dim cyan")
    body.append(f"  {int(progress*100):>3}%\n", style="bright_white")

    return Panel(
        body,
        title="[bold bright_cyan]watchmen analyst[/]",
        border_style="bright_cyan",
        expand=False,
    )


# Stdout patterns from analyze.py:
#   "Running on 24 days, model=..."
#   "  [2026-05-08] 13 prompts... done in 97.4s (7505 chars)"
#   "  [2026-05-09] cached (12 prompts)"
_TOTAL_RE = re.compile(r"Running on (\d+) days")
_DONE_RE = re.compile(r"\[\d{4}-\d{2}-\d{2}\].*(?:done in|cached)")


def run_analyst_with_animation(
    console: Console,
    project_key: str,
    *,
    model: str | None = None,
    from_day: str | None = None,
) -> bool:
    """Run analyze.py as subprocess, drive the Manhattan animation from its
    stdout. Returns True if exit code 0."""
    cmd = [sys.executable, str(ROOT / "analyze.py"), "--project", project_key]
    if model:
        cmd.extend(["--model", model])
    if from_day:
        cmd.extend(["--from-day", from_day])

    proc = subprocess.Popen(
        cmd, cwd=str(ROOT),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1,
    )

    events: queue.Queue = queue.Queue()
    stdout_buf: list[str] = []  # save lines so we can print on failure

    def reader():
        if proc.stdout is None:
            events.put(None)
            return
        for line in proc.stdout:
            stdout_buf.append(line)
            events.put(line)
        events.put(None)

    t = threading.Thread(target=reader, daemon=True)
    t.start()

    total = 0
    done = 0
    frame = 0
    reader_finished = False

    with Live(render(0, 1, 0, project_key), console=console,
              refresh_per_second=FAST_FPS) as live:
        while True:
            # Drain pending stdout events.
            try:
                while True:
                    line = events.get_nowait()
                    if line is None:
                        reader_finished = True
                        break
                    if m := _TOTAL_RE.search(line):
                        total = int(m.group(1))
                    elif total > 0 and _DONE_RE.search(line):
                        done = min(done + 1, total)
            except queue.Empty:
                pass

            live.update(render(done, total or 1, frame, project_key))
            frame += 1
            time.sleep(1 / FAST_FPS)

            if reader_finished and proc.poll() is not None:
                break

        # Final frame at 100% so users see the completion state.
        if proc.returncode == 0 and total > 0:
            live.update(render(total, total, frame + 1, project_key))
            time.sleep(0.5)

    if proc.returncode != 0:
        # Surface the tail of stdout so failures aren't silent.
        tail = "".join(stdout_buf[-8:]).strip()
        console.print(f"[red]✗[/] analyst failed (exit {proc.returncode})")
        if tail:
            console.print(f"[dim]{tail}[/]")
    return proc.returncode == 0
