#!/usr/bin/env bash
# scripts/notifier-self-test.sh — fire a synthetic Schwab MCP notification.
#
# Plan §3.4.1 / §7 — run once after configuring cron / launchd to confirm
# the notification path actually reaches the desktop session.

set -euo pipefail

MSG="Schwab MCP self-test — if you see this, notifications work."

case "$(uname -s)" in
    Darwin)
        if command -v osascript >/dev/null 2>&1; then
            osascript -e "display notification \"${MSG}\" with title \"Schwab MCP\" sound name \"Sosumi\""
            echo "[ok] osascript fired (check the notification center)"
        else
            echo "[skip] osascript not found on PATH" >&2
            exit 1
        fi
        ;;
    Linux)
        if command -v notify-send >/dev/null 2>&1; then
            notify-send -u critical "Schwab MCP" "${MSG}"
            echo "[ok] notify-send fired (check your DE)"
        else
            echo "[skip] notify-send not found; install libnotify-bin" >&2
            exit 1
        fi
        ;;
    *)
        echo "[skip] only macOS / Linux supported in v1" >&2
        exit 1
        ;;
esac

# Also write the desktop fallback file so the user can confirm the file path.
DESKTOP="${HOME}/Desktop"
if [ -d "${DESKTOP}" ]; then
    cat > "${DESKTOP}/SCHWAB_REAUTH_NEEDED.md" <<EOF
# Schwab MCP — self-test marker

This file was created by \`scripts/notifier-self-test.sh\` to confirm the
fallback markdown channel is working.  You can safely delete it.

If you actually see this file appear unprompted in the future, run:

    uv run python -m schwab_marketdata_mcp.auth login_flow

EOF
    echo "[ok] wrote ${DESKTOP}/SCHWAB_REAUTH_NEEDED.md"
else
    echo "[skip] ${DESKTOP} does not exist (headless host?)" >&2
fi
