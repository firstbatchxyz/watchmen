"""Experimental: turn a cross-runtime route decision into a real delegation
via AX (google/ax) instead of only advising the user.

A ``switch-harness`` winner belongs to a different runtime, so watchmen can't
emit a native artifact the source harness can execute (see the advisory path in
``route_rewrite``). When AX is configured — a running ``ax serve`` with wrappers
for the target harness — we instead emit a dispatcher that shells out to
``ax exec --agent <target>``, so the source agent delegates the skill to the
winning harness for real. When AX is not configured, callers fall back to the
advisory line, so default behavior is unchanged.

Gating is env-only, opt-in:

  WATCHMEN_AX_SERVER  gRPC address of a running ``ax serve`` (e.g.
                      ``localhost:8494``). Unset disables AX dispatch.
  WATCHMEN_AX_BIN     path to the ``ax`` binary (default: ``ax`` on PATH).
"""

from __future__ import annotations

import os

# watchmen harness slug -> AX agent id as registered in the running ``ax serve``.
# Only harnesses with an AX wrapper can be dispatch *targets*; anything not
# listed falls back to the advisory path.
HARNESS_TO_AX_AGENT = {
    "claude_code": "claude-code",
    "codex": "codex",
}


def ax_server() -> str | None:
    """gRPC address of the running ``ax serve``, or None when AX dispatch is off."""
    return os.environ.get("WATCHMEN_AX_SERVER") or None


def ax_bin() -> str:
    """Path to the ``ax`` binary (``ax`` on PATH by default)."""
    return os.environ.get("WATCHMEN_AX_BIN", "ax")


def ax_agent_for(harness: str | None) -> str | None:
    """The AX agent id that can run ``harness``'s runtime, or None when AX is
    unconfigured or no wrapper exists for it. This is the availability gate the
    rewriter checks before choosing AX dispatch over the advisory fallback."""
    if not harness or ax_server() is None:
        return None
    return HARNESS_TO_AX_AGENT.get(harness)


def ax_exec_command(*, agent: str, model: str | None, workspace: str | None) -> str:
    """Build the ``ax exec`` invocation a dispatcher subagent should run.

    ``ax exec`` has no ``--workspace`` / ``--model`` flags, so both ride the
    wrapper's ``[workspace]`` / ``[model]`` header convention inside ``--input``.
    The concrete task isn't known until dispatch time, so the returned command
    carries a literal ``<TASK>`` placeholder the subagent replaces with the
    request it was handed.
    """
    headers: list[str] = []
    if model:
        headers.append(f"[model] {model}")
    if workspace:
        headers.append(f"[workspace] {workspace}")
    header_block = ("\n".join(headers) + "\n\n") if headers else ""
    # --once: run a single turn and exit. ax exec is otherwise an interactive
    # REPL that prompts (via a TTY) for the next message after each turn; from
    # an agent's shell tool that prompt can't open /dev/tty and the call fails
    # after the result. --once is the reliable headless path (ax's own non-TTY
    # auto-detect is unreliable when the shell allocates a pseudo-TTY).
    #
    # Single-quoted input preserves newlines and spares the subagent from
    # escaping the task body. (A task containing a single quote would break the
    # quoting — acceptable for the experiment; a wrapper script is the hardening
    # path.)
    return (
        f"{ax_bin()} exec --once --server {ax_server()} --agent {agent} "
        f"--input '{header_block}<TASK>'"
    )
