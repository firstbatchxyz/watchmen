#!/usr/bin/env bash
# StatusLine command — emit one short line for the current workspace.
#
# Priority:
#   1. Live skill suggestion from the UserPromptSubmit hook (most recent signal)
#   2. Pending brief from the last curator run
#   3. Silent
#
# Self-locates via $0 — CLAUDE_PLUGIN_ROOT isn't set for the global statusLine.

set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

PROJECT_KEY=$(python3 "${ROOT}/bin/resolve_project_key.py" "$PWD" 2>/dev/null)
[ -z "${PROJECT_KEY}" ] && exit 0

STATE_DIR="${HOME}/.watchmen/state"
STATE_FILE="${STATE_DIR}/${PROJECT_KEY}.json"
ACK_FILE="${STATE_DIR}/${PROJECT_KEY}.acknowledged"
SUGGESTION_FILE="${STATE_DIR}/${PROJECT_KEY}.suggestion.json"

# Portable file-mtime: BSD/macOS uses `stat -f %m`, Linux GNU stat uses `-c %Y`.
# `stat -c %Y` fails first on macOS (BSD doesn't know -c); the `||` chain
# transparently falls through to BSD form. Returns 0 if neither works (so
# arithmetic comparison below stays valid).
_mtime() {
  stat -c %Y "$1" 2>/dev/null || stat -f %m "$1" 2>/dev/null || echo 0
}

# 1. Live suggestion from the prompt hook (highest priority).
# Pass the file path through an env var so a path containing quotes can't
# inject Python — defense in depth, even though resolve_project_key.py
# should never produce a malicious one.
if [ -f "${SUGGESTION_FILE}" ]; then
  SKILL=$(WATCHMEN_FILE="${SUGGESTION_FILE}" python3 -c "
import json, os
try:
    d = json.load(open(os.environ['WATCHMEN_FILE']))
    print(d.get('skill_slug', ''))
except Exception:
    pass
" 2>/dev/null)
  if [ -n "${SKILL}" ]; then
    printf '\033[33m💡 /%s fits this kind of task — run it to save time & tokens\033[0m\n' "${SKILL}"
    exit 0
  fi
fi

# 2. Pending brief from curator (unless already acknowledged).
[ ! -f "${STATE_FILE}" ] && exit 0
if [ -f "${ACK_FILE}" ]; then
  state_mtime=$(_mtime "${STATE_FILE}")
  ack_mtime=$(_mtime "${ACK_FILE}")
  [ "${ack_mtime}" -gt "${state_mtime}" ] && exit 0
fi

SUMMARY=$(WATCHMEN_FILE="${STATE_FILE}" python3 -c "
import json, os
try:
    d = json.load(open(os.environ['WATCHMEN_FILE']))
    print(d.get('summary', '').strip())
except Exception:
    pass
" 2>/dev/null)
[ -z "${SUMMARY}" ] && SUMMARY="updates available"

printf '\033[33m💡 watchmen · %s · %s · /watchmen:brief\033[0m\n' "${PROJECT_KEY}" "${SUMMARY}"
