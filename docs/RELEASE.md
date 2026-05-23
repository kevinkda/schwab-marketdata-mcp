# Release Process — schwab-marketdata-mcp

This document describes the end-to-end release process for the
`schwab-marketdata-mcp` Python package. It is intentionally manual: every step
is explicit so that the human releaser keeps control over what reaches GitHub.

> **Scope:** GitHub repository releases (tags + GitHub Releases UI).
> PyPI publishing is **out of scope** for the 0.1.x line.

---

## 1. Prerequisites

Before cutting a release, confirm **all** of the following:

| Item | Verification command |
|------|----------------------|
| `gh` CLI installed (>= 2.50) | `gh --version` |
| `gh` authenticated to `github.com` | `gh auth status` |
| Working tree clean on `main` | `git status` shows nothing to commit |
| Local `main` is up-to-date with `origin/main` | `git fetch && git status -sb` |
| Local CI passes with zero errors | `bash scripts/local-ci.sh` |
| Tests pass (≥ 247 tests, ≥ 90% coverage) | `uv run pytest --cov` |
| Lint / type-check clean | `uv run ruff check . && uv run mypy src/` |
| Pre-commit clean | `pre-commit run --all-files` |

If any of these fail, **stop** and fix before proceeding.

> **First-time auth on a new machine** (interactive — do **not** run from an
> agent without user consent):
>
> ```bash
> gh auth login --hostname github.com --git-protocol https --web
> ```

---

## 2. Versioning Policy (SemVer)

The package follows [Semantic Versioning 2.0.0](https://semver.org/).

| Change type | Bump | Example |
|------|------|---------|
| Bug fix, doc-only, internal refactor (no public-API change) | **patch** | `0.1.0 → 0.1.1` |
| Backward-compatible new feature, new MCP tool, new optional config | **minor** | `0.1.0 → 0.2.0` |
| Breaking change to MCP tool schema, env-var rename, removed feature | **major** | `0.1.0 → 1.0.0` |
| Pre-1.0 breaking change | **minor** (allowed under SemVer 0.x) | `0.1.0 → 0.2.0` |

**Current version:** `0.1.0` (in `pyproject.toml`). This is the initial public
release line.

**Tag format:** `vX.Y.Z` (e.g. `v0.1.0`). Always prefixed with `v`.

---

## 3. Release Checklist

The full sequence to release **`v0.1.0`** (substitute the actual version):

### 3.1 Pre-flight

```bash
# 1. Confirm clean state
cd /opt/workspace/code/kevinkda/schwab-marketdata-mcp
git checkout main
git pull --ff-only origin main
git status                    # must be clean

# 2. Run local CI end-to-end
bash scripts/local-ci.sh      # must exit 0
```

### 3.2 Bump version

Edit **`pyproject.toml`**:

```toml
[project]
name = "schwab-marketdata-mcp"
version = "0.1.0"             # ← bump here
```

Verify the change is the only modification:

```bash
git diff pyproject.toml
```

### 3.3 Update CHANGELOG.md

If `CHANGELOG.md` does not yet exist, create it from the template in
[Section 5](#5-changelogmd-template).

Add a new entry **at the top** of `CHANGELOG.md`:

```markdown
## [0.1.0] — 2026-MM-DD

### Added
- Initial public release of the Schwab Market Data MCP server.
- 12 MCP tools: …

### Security
- …
```

### 3.4 Commit + tag

```bash
git add pyproject.toml CHANGELOG.md
git commit -m "chore(release): v0.1.0"
git tag -a v0.1.0 -m "Release v0.1.0"
```

### 3.5 Push

```bash
git push origin main
git push origin v0.1.0
```

### 3.6 Create the GitHub Release

Extract the `[0.1.0]` section of `CHANGELOG.md` into a temporary file
`/tmp/release-notes-v0.1.0.md`, then:

```bash
gh release create v0.1.0 \
  --title "v0.1.0 — Initial public release" \
  --notes-file /tmp/release-notes-v0.1.0.md \
  --verify-tag
```

> Add `--draft` if you want to review the page before publishing, or
> `--prerelease` for non-stable lines (e.g. `v0.2.0-rc.1`).

### 3.7 Verify

```bash
gh release view v0.1.0 --web      # opens in browser
gh release list                   # confirms it appears
```

Manually confirm on GitHub:

- Tag `v0.1.0` is present.
- Release notes render correctly (headings, lists, links).
- Source tarballs (`.tar.gz`, `.zip`) are auto-attached by GitHub.

---

## 4. Release Notes Template

Save as the `--notes-file` argument to `gh release create`.

```markdown
## What's Changed

### Added
- <user-visible new features, in past tense>

### Changed
- <behavior changes that don't break compatibility>

### Fixed
- <bug fixes>

### Security
- <security-relevant fixes>

### Deprecated
- <APIs/flags scheduled for removal>

### Removed
- <removed APIs/flags>

## Migration

<For minor/major releases: explicit before/after snippets if any user-visible
behavior shifts. Omit the section for pure-patch releases.>

## Acknowledgements

Thanks to <contributor handles> for issues, reviews, and patches.

## Full Changelog

https://github.com/kevinkda/schwab-marketdata-mcp/compare/<prev-tag>...v0.1.0
```

---

## 5. CHANGELOG.md Template

If `CHANGELOG.md` does not yet exist at the repo root, create it with this
shell. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

```markdown
# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
### Changed
### Fixed
### Security

## [0.1.0] — 2026-MM-DD

### Added
- Initial public release.
- 12 MCP tools covering quotes, price history, option chains, market hours,
  movers, search, and instrument lookup against the Charles Schwab Market
  Data Production API.
- OAuth2 PKCE login flow with 7-day refresh-token rotation.
- Local-only token storage with 0600 permissions.
- Bilingual documentation (English + 简体中文).

### Security
- All outbound traffic identified by stable `User-Agent`.
- No third-party telemetry; no token leaves the local machine.

[Unreleased]: https://github.com/kevinkda/schwab-marketdata-mcp/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/kevinkda/schwab-marketdata-mcp/releases/tag/v0.1.0
```

---

## 6. Rollback / Recovery

If a release was published in error (wrong notes, wrong commit, broken build):

```bash
# Delete the GitHub Release (keeps the tag by default)
gh release delete v0.1.0 --yes

# Delete the tag locally and on the remote
git tag -d v0.1.0
git push --delete origin v0.1.0
```

After rollback, fix the underlying issue, **bump the patch version**
(`v0.1.0 → v0.1.1`), and run the full release checklist again. Never re-use
a tag name that has been published, even if deleted.

---

## 7. Current Readiness Assessment (as of doc creation)

| Item | Status |
|------|--------|
| Tests pass (247+) | ✅ |
| Coverage ≥ 90% | ✅ (~93%) |
| `ruff` / `mypy` clean | ✅ |
| Pre-commit clean | ✅ |
| LICENSE present (MIT) | ✅ |
| README (EN + ZH) | ✅ |
| `pyproject.toml` version | ✅ (`0.1.0`, suitable for initial release) |
| `main` pushed to `origin` | ✅ (in sync with `origin/main`) |
| `CHANGELOG.md` | ✅ added in 6a9aca2; `[0.1.0]` section frozen at release time |
| Windows native support | ❌ Not supported. Documented as `experimental` in `docs/WINDOWS_PORTING.md`. WSL2 is the supported path. |
| Uncommitted edits in working tree | ✅ clean as of 2026-05-23 |

**Verdict:** ready for `v0.1.0` once `CHANGELOG.md` is added and the working
tree is clean.

---

## 8. Notes for Future Releases

- Keep `CHANGELOG.md` updated **as part of each PR**, not only at release time.
- Add a `## [Unreleased]` heading at the top so contributors know where to add
  entries.
- Consider automating Steps 3.4–3.6 with a `scripts/release.sh` once the
  process has run cleanly two or three times manually.
- If/when the project moves to PyPI, add a `Publish to PyPI` section below
  Section 3.7.

---

## 9. Repository metadata

After release, update GitHub repo description and topics via `gh`:

```bash
gh repo edit kevinkda/schwab-marketdata-mcp \
  --description "Read-only MCP server for Charles Schwab Market Data Production API (12 tools, 274 tests, OWASP-tested)." \
  --add-topic mcp \
  --add-topic schwab \
  --add-topic market-data \
  --add-topic finance \
  --add-topic python \
  --add-topic cursor \
  --add-topic claude
```

For the companion skill repo:

```bash
gh repo edit kevinkda/schwab-marketdata-skill \
  --description "Cursor / Claude Code skill pack for schwab-marketdata-mcp (60+ references, 4 playbooks, EN+zh-CN)." \
  --add-topic mcp \
  --add-topic schwab \
  --add-topic cursor-skill \
  --add-topic claude-skill \
  --add-topic finance
```

> Run these once after the first release; topics persist across future
> releases.
