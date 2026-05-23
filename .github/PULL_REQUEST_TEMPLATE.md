# Pull request

## Summary

<!-- 1–3 sentences describing what changed and why. Link the issue this
closes. -->

## Type of change

- [ ] Bug fix (non-breaking change which fixes an issue)
- [ ] New feature (non-breaking change which adds functionality)
- [ ] Documentation only
- [ ] Refactor / chore (no behavior change)
- [ ] Breaking change (semver-major)

## Checklist

- [ ] Tests pass locally — `uv run pytest --cov` (≥ 85% overall, 100%
      on critical modules).
- [ ] `uv run ruff check src tests` is clean.
- [ ] `uv run mypy --strict src` is clean.
- [ ] Pre-commit hooks pass — `pre-commit run --all-files`.
- [ ] Conventional commit message — `feat(...)`, `fix(...)`,
      `docs(...)`, `chore(...)`, etc.
- [ ] [`CHANGELOG.md`](../CHANGELOG.md) updated under `## [Unreleased]`
      (if applicable).
- [ ] Documentation updated — README, `docs/REGISTER.md`,
      `docs/THREAT_MODEL.md`, etc. (if applicable).
- [ ] Inclusive-language audit — no `master` / `blacklist` /
      `whitelist` / `kill` / `abort`. Use `main` / `deny list` /
      `allow list` / `stop` instead.
- [ ] No secrets, `.env`, `token.json`, or Bearer tokens committed.
      Verified with `git diff --staged | grep -iE 'access_token|refresh_token|bearer'`.

## Test plan

<!-- How did you verify this change? Include exact commands and
relevant output. -->

## Screenshots / logs (if applicable)

<!-- Trim to ≤ 30 lines, redact tokens. -->
