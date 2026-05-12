"""Demo of polished Watchmen-atmosphere progress animations for the analyst.

Run:
    uv run python demo_animations.py

Two refined options now: A (Dr. Manhattan, polished) and B (Rorschach, framed).
Each simulates a 30-day analyst run in fast-forward (~12s).
"""

from __future__ import annotations

import math
import random
import time

from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.text import Text


console = Console()
TOTAL = 30
DAY_DURATION = 0.4  # seconds per simulated day
FAST_FPS = 14


# ────────────────────────────────────────────────────────────────────────────
# Option A — Dr. Manhattan: cosmic stillness, polished
# ────────────────────────────────────────────────────────────────────────────

_BREATHING_SYMBOLS = ["⊙", "◉", "●", "◉", "⊙", "○"]
_STARFIELD_CHARS = ["·", "·", "·", "*", "·", " ", "·", "+", " "]


def _angle_char(angle_deg: float) -> str:
    """ASCII char whose orientation matches an angle. 16 buckets for smoother
    diagonals than the previous 8-bucket version."""
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


def _render_manhattan(done: int, total: int, frame: int, project: str) -> Panel:
    """Compact Manhattan: 36×7 grid, single ring of streaks, sparse stars,
    breathing center. Slim enough to sit at the top of the analyst output
    without dominating the terminal."""
    progress = done / max(total, 1)
    cols, rows = 36, 7
    cx, cy = cols // 2, rows // 2

    cells: list[list[tuple[str, str]]] = [[(" ", "")] * cols for _ in range(rows)]

    # Sparse starfield — 3-5 stars, slow rotation.
    star_rng = random.Random(frame // 10)
    for _ in range(4):
        x = star_rng.randint(0, cols - 1)
        y = star_rng.randint(0, rows - 1)
        if cells[y][x][0] == " ":
            ch = star_rng.choice(["·", "·", "*", "+"])
            cells[y][x] = (ch, "dim cyan")

    # Single ring of streaks. Count grows gently with progress.
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
                if r == 1:
                    style = "bold bright_cyan"
                else:
                    style = "cyan"
                cells[y][x] = (ch, style)

    # Center — breathing mark.
    center_ch = _BREATHING_SYMBOLS[(frame // 3) % len(_BREATHING_SYMBOLS)]
    cells[cy][cx] = (center_ch, "bold bright_white")

    body = Text()
    for row in cells:
        for ch, style in row:
            body.append(ch, style=style or "dim")
        body.append("\n")

    # Compact one-line footer.
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


def demo_manhattan(project: str = "kai-frontend"):
    console.rule("[bold bright_cyan]Option A — Dr. Manhattan, polished[/]")
    days_per_frame = 1.0 / (DAY_DURATION * FAST_FPS)
    with Live(_render_manhattan(0, TOTAL, 0, project), console=console,
              refresh_per_second=FAST_FPS, screen=False) as live:
        for f in range(int(TOTAL * FAST_FPS * DAY_DURATION) + 6):
            done = min(TOTAL, int(f * days_per_frame))
            live.update(_render_manhattan(done, TOTAL, f, project))
            time.sleep(1 / FAST_FPS)
    console.print()


# ────────────────────────────────────────────────────────────────────────────
# Option B — Iconic Watchmen smiley with growing blood drip
# ────────────────────────────────────────────────────────────────────────────

# The smiley template. `B` marks the column where the blood drip falls.
# Row index 3 (the B that sits on the eye row) is where the LEFT EYE lives —
# until the blood reaches it, then the blood crosses through it (matches the
# iconic comic-cover composition). The right eye is the static ●.
_SMILEY_TEMPLATE = """\
            B
          ╭─B─────╮
         ╱  B      ╲
        │   B   ●   │
        │   B       │
        │   ╲_____╱ │
         ╲         ╱
          ╰───────╯"""
_EYE_ROW = 3  # template row index where the left eye lives (in the B column)
# When the blood drip hasn't reached a B cell yet, fall back to the natural
# face glyph for that row instead of leaving a gap. Row indices match the
# template: 0=above face, 1=top border, 2=upper interior, 3=eye row,
# 4=lower interior.
_B_DEFAULT_GLYPH = {0: " ", 1: "─", 2: " ", 3: "●", 4: " "}


# Drip glyphs — index 0 is the topmost (newest forming drop), later indices
# are continuations of the streak. We use a tear-shaped top, then thin
# vertical lines for the streak body.
_DRIP_GLYPHS = ["●", "│", "│", "│", "│", "│", "╲"]


def _render_rorschach(done: int, total: int, frame: int, project: str) -> Panel:
    """Iconic yellow smiley with a red blood drip that grows from top to
    bottom as days complete. The drip's visible length scales with progress;
    a subtle horizontal wobble (per fast-tier frame) gives it weight."""
    progress = done / max(total, 1)

    template_lines = _SMILEY_TEMPLATE.splitlines()
    # Identify the columns where B markers live, per row.
    drip_positions: dict[int, int] = {}
    for i, line in enumerate(template_lines):
        idx = line.find("B")
        if idx != -1:
            drip_positions[i] = idx

    # Total drip slots (one per row that has a B marker).
    drip_rows = sorted(drip_positions.keys())
    visible_drops = int(round(progress * len(drip_rows)))

    # Optional horizontal wobble at the bottom of the streak (the falling tip
    # sways one column left/right per ~3 frames). Apply only to the LAST
    # visible drop, so the rest of the streak stays anchored.
    wobble = 0
    if visible_drops > 1 and progress < 1.0:
        wobble = (-1, 0, 1, 0)[(frame // 3) % 4]

    body = Text()
    for i, line in enumerate(template_lines):
        for col, ch in enumerate(line):
            if ch == "B":
                # Is the blood drip visible at this row yet?
                row_idx = drip_rows.index(i) if i in drip_rows else -1
                drip_here = 0 <= row_idx < visible_drops
                if drip_here:
                    glyph = _DRIP_GLYPHS[min(row_idx, len(_DRIP_GLYPHS) - 1)]
                    body.append(glyph, style="bold bright_red on yellow")
                else:
                    default = _B_DEFAULT_GLYPH.get(i, " ")
                    if i == 0:
                        # Above face — terminal-default background.
                        body.append(default, style="")
                    elif default == "─":
                        # Border glyph — black on yellow.
                        body.append(default, style="bold black on yellow")
                    elif default == "●":
                        # Eye glyph.
                        body.append(default, style="bold black on yellow")
                    else:
                        body.append(default, style="on yellow")
            else:
                # Face glyphs — black on yellow. Outside the face (left of `╭`
                # or right of `╮`) is plain background, no yellow fill.
                if ch == " ":
                    # Background space: inside the face if surrounded by face,
                    # else terminal default. The template's leading spaces
                    # are always background (default), interior spaces are
                    # yellow fill.
                    # Heuristic: count face-border chars to left of this col
                    # on this row. Odd = inside, even = outside.
                    left = line[:col]
                    border_chars = sum(1 for c in left if c in "╭╮╰╯╱╲│")
                    inside = border_chars % 2 == 1
                    body.append(" ", style="on yellow" if inside else "")
                else:
                    body.append(ch, style="bold black on yellow")
        body.append("\n")

    # Footer (no yellow background — terminal-default to keep the smiley
    # visually contained).
    bar_filled = int(progress * 40)
    body.append("\n")
    body.append(f"  {project}".ljust(25), style="bold yellow")
    body.append(f"  day {done}/{total}".ljust(17), style="bright_white")
    body.append("━" * bar_filled, style="bright_red")
    body.append("─" * (40 - bar_filled), style="dim yellow")
    body.append(f"  {int(progress*100):>3}%\n", style="bold bright_white")

    return Panel(
        body,
        title="[bold yellow]watchmen analyst[/]",
        subtitle="[dim italic yellow]\"who watches the watchmen?\"[/]",
        border_style="yellow",
        expand=False,
    )


def demo_rorschach(project: str = "kai-frontend"):
    console.rule("[bold yellow]Option B — Smiley with growing blood drip[/]")
    days_per_frame = 1.0 / (DAY_DURATION * FAST_FPS)
    with Live(_render_rorschach(0, TOTAL, 0, project), console=console,
              refresh_per_second=FAST_FPS, screen=False) as live:
        for f in range(int(TOTAL * FAST_FPS * DAY_DURATION) + 6):
            done = min(TOTAL, int(f * days_per_frame))
            live.update(_render_rorschach(done, TOTAL, f, project))
            time.sleep(1 / FAST_FPS)
    console.print()


# ────────────────────────────────────────────────────────────────────────────

def main():
    console.print("\n[bold]Polished Watchmen progress animations.[/]")
    console.print("[dim]Each simulates a 30-day analyst run in ~12s.[/]\n")
    time.sleep(1.0)
    demo_manhattan()
    time.sleep(0.8)
    demo_rorschach()
    console.print("\n[bold]Done.[/] A, B, or any tweaks?\n")


if __name__ == "__main__":
    main()
