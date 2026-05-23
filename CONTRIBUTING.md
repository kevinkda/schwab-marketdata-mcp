# Contributing to schwab-marketdata-mcp

Thanks for considering a contribution! This is a personal-scale project with
strict quality standards inherited from its parent plan
([`docs/THREAT_MODEL.md`](docs/THREAT_MODEL.md),
[`docs/RELEASE.md`](docs/RELEASE.md)).

## Before you start

1. Read [`docs/REGISTER.md`](docs/REGISTER.md) to understand the OAuth +
   `envFile` setup.
2. Read [`docs/THREAT_MODEL.md`](docs/THREAT_MODEL.md) for the threat
   surface and security boundaries.
3. Check open issues / discussions to avoid duplicate work.

## Development setup

```bash
git clone https://github.com/kevinkda/schwab-marketdata-mcp
cd schwab-marketdata-mcp
uv sync --extra dev
uv run pre-commit install
```

## Quality gates (must pass before PR)

- `uv run pytest --cov` — all tests pass, ≥85% total coverage, 100% on
  critical modules (`errors.py`, `security.py`, `auth_logic.py`,
  `models.py`, `_platform.py`).
- `uv run ruff check src tests` — 0 warnings.
- `uv run ruff format --check src tests` — must be formatted.
- `uv run mypy --strict src` — 0 errors.
- `uv run bandit -r src -lll` — 0 high.
- `uv run pip-audit` — 0 known vulnerabilities.
- `pre-commit run --all-files` — all hooks pass.

Or run the full local CI: `bash scripts/local-ci.sh`.

## Commit message style

Follow [Conventional Commits](https://www.conventionalcommits.org/).
Examples:

- `feat(client): identify outbound traffic with stable User-Agent`
- `fix(server): bootstrap dotenv before importing tool modules`
- `chore(lint): replace inclusive-language violations`
- `docs(register): document envFile vs cwd-dotenv precedence`

Subject ≤ 72 chars. Use English. Body explains *why*, not *what*.

## Branching

- `main` is the integration branch. PRs target `main`.
- For features that may take multiple PRs, use a topic branch:
  `feature/streaming-snapshot`, `fix/oauth-state-mismatch`, etc.
- **Never force-push `main`**. See
  [`schwab-marketdata-skill/schwab-marketdata-ops/references/credentials-rotate-runbook.md`](https://github.com/kevinkda/schwab-marketdata-skill/blob/main/schwab-marketdata-ops/references/credentials-rotate-runbook.md)
  for the rationale.

## What contributions are welcome

- Bug fixes, documentation improvements, additional tests, OWASP coverage
  expansion.
- Cross-platform support improvements (Windows Tier B, etc.).
- New Schwab Market Data Production endpoints (if any are added by Schwab).
- New tools: please open a discussion first to align with plan §1 / §10
  boundaries.
- Trader API integration (writes / orders) — explicitly **out of scope**
  per plan §1, will not be accepted.

## Inclusive language

This project follows
[Amazon's inclusive language guidelines](https://aws.amazon.com/blogs/aws/blogpost-inclusive-language/).
Replace `master` / `blacklist` / `whitelist` etc. with `main` /
`deny list` / `allow list`. The `pre-commit` hook does not auto-enforce
this — please self-audit before submitting.

## Questions?

Open a [discussion](https://github.com/kevinkda/schwab-marketdata-mcp/discussions) —
issues are for bugs.

## Security disclosures

Do **not** open a public issue for vulnerabilities. Use the GitHub
private security advisory flow described in
[`.github/ISSUE_TEMPLATE/security_report.md`](.github/ISSUE_TEMPLATE/security_report.md).

## License

By submitting a PR, you agree your contribution will be licensed under
MIT (see [LICENSE](LICENSE)).
