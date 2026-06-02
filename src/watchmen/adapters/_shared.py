"""Shared helpers used across the per-agent adapters.

Adapters live as separate modules (`claude_code.py`, `codex.py`, `pi.py`,
`opencode.py`) because their on-disk session formats diverge wildly. But a
few extraction primitives are genuinely cross-cutting — anything driven by
*what skills look like on disk* rather than by the specific transcript
schema — so they live here instead of being copy-pasted.

The big one is `extract_skill_from_path`. Claude Code is the only agent
with a first-class `Skill` tool primitive that records the slug it's
invoking (`Skill(skill='foo')`). In every other agent — Codex, pi.dev,
OpenCode — skills are just SKILL.md files on disk, and "invocation"
manifests as the model reading that file via the normal read/bash tool.
The deterministic, schema-free signal that a skill was activated is
therefore: a tool call whose path argument matches `…/skills/<slug>/SKILL.md`.

We extract the slug from that path here so every non-Claude-Code adapter
populates `tool_calls.skill_name` the same way the Claude Code adapter
does. Prune telemetry, dashboard sparklines, and the prune judge's
per-skill `usage_count` all key on that column, so this is the difference
between "watchmen sees skill use across all agents" and "watchmen only
sees skill use in Claude Code".
"""

from __future__ import annotations

import re

# Match `.../skills/<slug>/SKILL.md` anywhere in a path string. The slug
# must look like a real identifier (no slashes, no whitespace), which
# keeps `.../skills/SKILL.md` and `.../skills/sub/dir/SKILL.md` from
# producing false positives.
#
# Covers all the real-world locations skills live in:
#   ~/.claude/skills/<slug>/SKILL.md
#   ~/.codex/skills/<slug>/SKILL.md
#   ~/.codex/skills/.system/<slug>/SKILL.md   (Codex's "system" namespace)
#   ~/.pi/skills/<slug>/SKILL.md
#   ~/.watchmen/bundles/<project>/skills/<slug>/SKILL.md  (watchmen-managed)
#   <repo>/.claude/skills/<slug>/SKILL.md
_SKILL_PATH_RE = re.compile(r"[/\\]skills[/\\](?:\.system[/\\])?([A-Za-z0-9_.-]+)[/\\]SKILL\.md\b")


def extract_skill_from_path(value) -> str | None:
    """Return the skill slug if `value` references a SKILL.md file, else None.

    `value` is whatever the adapter has in hand at a tool-call site —
    typically the path string passed to a `read` / `bash` tool. We accept
    arbitrary types (None, dicts, lists, ints) and return None for any
    non-string input, so adapter sites can pass `block.get("path")` or
    `args.get("command")` without pre-validation.
    """
    if not isinstance(value, str) or not value:
        return None
    m = _SKILL_PATH_RE.search(value)
    return m.group(1) if m else None


# --- Error-signature normalization (friction ledger, #110) ---------------
#
# `tool_calls.is_error` tells us *that* a call failed; the friction ledger
# needs to know *how often the same failure recurs*. To group "the same
# mistake in different words" we fold each error message into a stable
# signature: lowercase the most meaningful line and replace the volatile
# bits (paths, numbers, hashes, quoted strings) with placeholders. Two
# failures with the same shape then share a signature and stack up in the
# ledger.
#
# Two real-world facts drive the shape of this function (measured against
# the local corpus, ~880 errored results across three repos):
#   1. ~15% of errored tool_results are *user-driven* — a cancelled parallel
#      batch, a rejected edit, an interrupt. Those are not the agent's
#      recurring mistakes, so we return None and the adapter records the row
#      as a plain error with no signature (it stays out of the ledger).
#   2. Bash failures arrive as "Exit code N\n<the actual error>". The exit
#      code alone is a useless bucket (282 "Exit code 1" rows collapse to
#      one meaningless group), so we look past it; and for Python tracebacks
#      the signal is the *last* line (the exception), not "Traceback (most
#      recent call last):".

# User-initiated outcomes — an error flag, but not agent friction. Matched
# case-insensitively against the raw text before any folding.
_NON_FRICTION_MARKERS = (
    "doesn't want to proceed",
    "the tool use was rejected",
    "tool use was rejected",
    "cancelled: parallel tool call",
    "sibling tool call errored",
    "request interrupted by user",
    "[request interrupted",
)

_TOOL_USE_ERR_RE = re.compile(r"</?tool_use_error>", re.IGNORECASE)
_EXIT_CODE_RE = re.compile(r"^exit code\s+\d+\s*$", re.IGNORECASE)
# Lines that "look like" the actual error in a multi-line tool output. Shell
# tools (especially Codex) emit stdout first and the failure last, so when a
# payload has several lines we prefer the *last* error-looking one over the
# first line (which is usually noise).
_ERRORISH_RE = re.compile(
    r"\b(error|fatal|exception|traceback|denied|not found|no such|cannot|"
    r"can't|failed|timed out|unable to|invalid|unexpected|refused|missing|"
    r"permission|errno)\b",
    re.IGNORECASE,
)
# Volatile spans, folded in this order (paths before digits so the digits
# inside a path don't get folded first and break the path match).
_QUOTED_RE = re.compile(r"""(['"])(?:(?!\1).)*\1""")
_PATH_RE = re.compile(r"(?:~|\.{0,2})?(?:/[\w.\-]+){2,}/?|[A-Za-z]:\\[\w.\\\-]+")
_HEX_RE = re.compile(r"\b(?:[0-9a-f]{8,}|[0-9a-f-]{16,})\b", re.IGNORECASE)
_NUM_RE = re.compile(r"\b\d+\b")
_WS_RE = re.compile(r"\s+")
_SIG_MAXLEN = 100

# A few error classes are really *one* recurring mistake fragmented by a
# volatile command/argument that the generic folder can't fully collapse
# (each denied bash command differs, each blocked sleep wraps a different
# follow-up). Canonicalize them to a single bucket so the ledger counts the
# *class*, not each instance. Applied to the chosen line, case-insensitively,
# first match wins; bypasses the generic fold.
_CANONICAL = (
    (re.compile(r"permission to use (\w+).*denied", re.IGNORECASE),
     lambda m: f"permission to use {m.group(1).lower()} denied"),
    (re.compile(r"^blocked: sleep\b", re.IGNORECASE),
     lambda m: "blocked: sleep+command (use monitor to wait on a condition)"),
)


def _error_text(content) -> str:
    """Flatten an adapter's error payload (str, or a list of content blocks
    like Claude Code's `[{type:'text', text:...}]`) into a single string."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                parts.append(str(block.get("text", "") or block.get("content", "")))
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(p for p in parts if p)
    if content is None:
        return ""
    return str(content)


def normalize_error_signature(content) -> str | None:
    """Fold a tool-call error into a stable grouping signature, or None.

    Returns None when the payload is empty or represents a *user-driven*
    outcome (cancel / reject / interrupt) rather than an agent mistake — the
    caller then records the row as a plain error with a NULL signature, so it
    counts toward error rates but never surfaces in the friction ledger.

    Otherwise returns a lowercased, placeholder-folded one-liner suitable for
    `GROUP BY`: `<path>`, `<n>`, `<hex>`, `<str>` stand in for the volatile
    spans so "the same mistake in different words" collapses to one bucket.
    """
    text = _error_text(content).strip()
    if not text:
        return None

    low = text.lower()
    if any(marker in low for marker in _NON_FRICTION_MARKERS):
        return None

    # Strip the <tool_use_error>…</tool_use_error> wrapper Claude Code puts
    # around its structured tool errors so the inner message is the signature.
    text = _TOOL_USE_ERR_RE.sub("", text).strip()
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return None

    # "Exit code N" alone is a useless bucket; drop a leading exit-code line so
    # the real error underneath drives the signature.
    if _EXIT_CODE_RE.match(lines[0]):
        lines = lines[1:]
        if not lines:
            return "shell command failed (exit code)"

    # Pick the most meaningful line.
    if any(ln.lower().startswith("traceback (most recent call last)") for ln in lines):
        # The exception is the last line of a Python traceback.
        chosen = lines[-1]
    elif len(lines) > 1:
        # Multi-line output (typically shell): prefer the last error-looking
        # line — the failure usually trails the stdout it produced.
        errorish = [ln for ln in lines if _ERRORISH_RE.search(ln)]
        chosen = errorish[-1] if errorish else lines[0]
    else:
        chosen = lines[0]

    for pat, repl in _CANONICAL:
        m = pat.search(chosen)
        if m:
            return repl(m)

    sig = chosen.lower()
    sig = _QUOTED_RE.sub("<str>", sig)
    sig = _PATH_RE.sub("<path>", sig)
    sig = _HEX_RE.sub("<hex>", sig)
    sig = _NUM_RE.sub("<n>", sig)
    sig = _WS_RE.sub(" ", sig).strip()
    # Reject signatures with no real content — bare separators / punctuation
    # ("---", "===", ">") that leaked out of stdout aren't recurring mistakes.
    if not sig or not any(c.isalpha() for c in sig):
        return None
    if len(sig) > _SIG_MAXLEN:
        sig = sig[:_SIG_MAXLEN].rstrip() + "…"
    return sig


def extract_skill_from_args(args) -> str | None:
    """Look through a tool-call's arguments object for a SKILL.md reference.

    Adapters call this with whatever shape the agent uses for tool args:
    a dict (Codex `function_call.arguments` after JSON-parse, pi
    `toolCall.arguments`), a bare string (a bash command line that may
    contain a path), a list, or None. We walk the structure shallowly
    and return the first slug we find.

    Why not deep-walk? Tool args in practice are flat: either a path
    string, a command string with the path as an argv token, or a small
    dict with one or two keys. A shallow walk catches every real case
    and won't accidentally pull a slug out of, say, a large `output`
    field on a tool result.
    """
    if isinstance(args, str):
        return extract_skill_from_path(args)
    if isinstance(args, dict):
        for v in args.values():
            slug = extract_skill_from_path(v) if isinstance(v, str) else None
            if slug:
                return slug
    if isinstance(args, list):
        for v in args:
            slug = extract_skill_from_path(v) if isinstance(v, str) else None
            if slug:
                return slug
    return None
