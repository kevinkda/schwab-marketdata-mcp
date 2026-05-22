#!/usr/bin/env bash
# scripts/ensure-uv-lock-tracked.sh — local pre-commit hook helper.
# Plan §3.3.4 / §8.1 — guard against the .gitignore footgun where
# uv.lock would be excluded by an over-broad lock glob.
set -euo pipefail
if ! git ls-files --error-unmatch uv.lock >/dev/null 2>&1; then
    echo "ERROR: uv.lock is not tracked. Check .gitignore reverse rule '!uv.lock'." >&2
    exit 1
fi
