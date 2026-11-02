#!/usr/bin/env bash
set -euo pipefail
project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export CODEX_HOME="$(cd "$project_root/../.codex" && pwd)"
if [[ -f "$project_root/watcher.py" && ! -L "$project_root/watcher.py" ]]; then
  python3 "$project_root/watcher.py" ensure
fi
exec codex -C "$project_root" "$@"
