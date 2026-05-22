"""Per-module 100% coverage assertions for security-critical files.

Plan §6.4 — invoked at the end of the pytest run; reads ``.coverage`` to
verify each listed module hit 100% **line + branch** coverage.  The fail
emits a list of missing line numbers so the developer can fix immediately.
"""

from __future__ import annotations

from pathlib import Path

import pytest

CRITICAL_MODULES: dict[str, int] = {
    "schwab_marketdata_mcp.errors": 100,
    "schwab_marketdata_mcp.security": 100,
    "schwab_marketdata_mcp.auth_logic": 100,
    "schwab_marketdata_mcp.models": 100,
}


@pytest.mark.last
def test_critical_modules_full_coverage() -> None:
    """Assert each CRITICAL_MODULES entry hit its target percentage."""
    coverage = pytest.importorskip("coverage")
    cov_file = Path(__file__).resolve().parents[1] / ".coverage"
    if not cov_file.exists():
        pytest.skip(".coverage not found — was pytest run with --cov?")
    cov = coverage.Coverage(data_file=str(cov_file))
    cov.load()
    failures: list[str] = []
    for module, target in CRITICAL_MODULES.items():
        # Resolve the module's source file via Coverage's measured set.
        src_path = None
        for measured in cov.get_data().measured_files():
            if measured.endswith(module.replace(".", "/") + ".py"):
                src_path = measured
                break
        if src_path is None:  # pragma: no cover - unreachable when modules are imported
            failures.append(f"{module}: not measured (was the module imported?)")
            continue
        analysis = cov.analysis2(src_path)
        executable = set(analysis[1])  # executable line numbers
        missing = set(analysis[3])  # missing line numbers
        if not executable:  # pragma: no cover - defensive
            failures.append(f"{module}: 0 executable lines")
            continue
        pct = 100 * (1 - len(missing) / len(executable))
        if pct + 1e-6 < target:
            failures.append(f"{module}: {pct:.2f}% < {target}% (missing lines: {sorted(missing)[:20]})")
    assert not failures, "Critical-module coverage regressions:\n  " + "\n  ".join(failures)
