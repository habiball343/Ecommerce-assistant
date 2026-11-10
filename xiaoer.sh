#!/usr/bin/env sh
# macOS / Linux: ./xiaoer.sh start | stop | status | test | reset [options]
cd "$(dirname "$0")" || exit 1
if command -v uv >/dev/null 2>&1; then
    exec uv run --no-project scripts/xiaoer.py "$@"
fi
exec python3 scripts/xiaoer.py "$@"
