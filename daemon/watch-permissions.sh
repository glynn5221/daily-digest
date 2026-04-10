#!/bin/bash
# Watches ALL scheduled-tasks.json files and patches them when modified.
# Runs as a LaunchAgent daemon — started at login, restarts on failure.

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
DAEMON_DIR="$(cd "$(dirname "$0")" && pwd)"
SESSIONS_DIR="$HOME/Library/Application Support/Claude/claude-code-sessions"

# Run once at startup to fix any existing state
python3 "$DAEMON_DIR/patch-permissions.py"

# Watch the entire sessions directory for any scheduled-tasks.json changes
# --recursive watches all subdirectories
# --include filters to only our target filename
# --event Updated,Created catches writes and new files
exec fswatch \
  --recursive \
  --include 'scheduled-tasks\.json$' \
  --exclude '.*' \
  --event Updated \
  --event Created \
  "$SESSIONS_DIR" | python3 "$DAEMON_DIR/patch-permissions.py" --watch
