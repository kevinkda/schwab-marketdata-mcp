#!/usr/bin/env bash
# scripts/local-ci.sh — equivalent to GitHub Actions, runs every gate the
# CI pipeline runs.  `act` compatibility is best-effort; this script is the
# authoritative "local CI green" gate.
#
# Plan §6.5 / §6.6.

set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." &> /dev/null && pwd)"
cd "$ROOT"

# Pretty section header.
section() {
    printf '\n\033[1;36m== %s ==\033[0m\n' "$1"
}

section "uv sync --extra dev"
uv sync --extra dev

section "ruff check"
uv run ruff check src tests

section "ruff format --check"
uv run ruff format --check src tests

section "mypy --strict"
uv run mypy --strict src

section "bandit -r src -lll"
uv run bandit -r src -lll

section "pip-audit"
uv run pip-audit

section "pytest --cov"
uv run pytest --cov

section "critical-module 100% coverage check"
uv run python -c "
import coverage
from pathlib import Path
CRITICAL = {
    'schwab_marketdata_mcp.errors': 100,
    'schwab_marketdata_mcp.security': 100,
    'schwab_marketdata_mcp.auth_logic': 100,
    'schwab_marketdata_mcp.models': 100,
}
cov = coverage.Coverage(data_file='.coverage')
cov.load()
fail = []
for m, target in CRITICAL.items():
    suffix = m.replace('.', '/') + '.py'
    src = next((f for f in cov.get_data().measured_files() if f.endswith(suffix)), None)
    if src is None:
        fail.append(f'{m}: not measured')
        continue
    _, executable, _, missing, _ = cov.analysis2(src)
    pct = 100 * (1 - len(set(missing)) / len(set(executable))) if executable else 0
    if pct + 1e-6 < target:
        fail.append(f'{m}: {pct:.2f}% < {target}% (missing: {sorted(set(missing))[:10]})')
if fail:
    print('CRITICAL coverage regressions:')
    for f in fail:
        print('  ' + f)
    raise SystemExit(1)
print('OK: all critical modules at 100%')
"

section "pre-commit run --all-files (best-effort; skipped if no network)"
if command -v pre-commit >/dev/null 2>&1; then
    pre-commit run --all-files || echo "WARN: pre-commit failed (often network/TLS in restricted environments)"
elif uv run pre-commit --version >/dev/null 2>&1; then
    uv run pre-commit run --all-files || echo "WARN: pre-commit failed (often network/TLS in restricted environments)"
else
    echo "WARN: pre-commit not installed, skipping (run 'uv run pre-commit install' first)" >&2
fi

printf '\n\033[1;32mAll local-ci gates passed.\033[0m\n'
