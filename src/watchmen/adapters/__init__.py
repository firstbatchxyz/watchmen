"""Coding-agent adapters.

Each adapter knows where its agent stores session transcripts on disk and how
to parse one into a (session, prompts, tool_calls) tuple that lines up with
watchmen's normalized schema (see corpus.py for column list).

Contract
--------
An adapter is a module that exposes:

    NAME: str                                # short identifier, stored in sessions.agent
    discover() -> Iterable[FileEntry]        # walk default install path, yield files
    scan(entry: FileEntry) -> ScanResult     # parse one transcript

Where:

    FileEntry = {
        "path": Path,                # the transcript file
        "project_dir": str,          # opaque per-agent project identifier
        "is_subagent": bool,
        "parent_session_id": str | None,
    }

    ScanResult = (session_dict, list[prompt_dict], list[tool_call_dict])

`session_dict` MUST set: session_id, project_dir, transcript_path, is_subagent,
parent_session_id, agent. Everything else is best-effort.

Adapters silently skip themselves when the install isn't present (discover()
returns []) — corpus.py doesn't need to know which agents the user has.
"""

from __future__ import annotations

from . import claude_code, codex, pi, opencode, cursor

# Order: stable for reproducible scans.
ADAPTERS = (claude_code, codex, pi, opencode, cursor)

__all__ = ["ADAPTERS"]
