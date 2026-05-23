---
name: Security report
about: Report a security vulnerability or concern (please read carefully).
title: "security: <short summary>"
labels: ["security"]
---

> **STOP — read this before filing a public issue.**
>
> If you have found a security vulnerability that could expose user
> credentials, OAuth tokens, Schwab Market Data, or allow remote code
> execution, **do not file a public issue**. Use GitHub's
> [private security advisory flow](https://github.com/kevinkda/schwab-marketdata-mcp/security/advisories/new)
> instead — that channel notifies maintainers privately and lets us
> coordinate a fix and disclosure timeline.
>
> Only continue with this public issue template if **all** of the
> following are true:
>
> - The report is about a **non-sensitive** security concern (e.g.
>   missing best-practice hardening, request for an additional
>   pre-commit hook, request for documentation of a known limitation).
> - No exploit, PoC, or sensitive data is included in the description.
> - You have read [`docs/THREAT_MODEL.md`](../../docs/THREAT_MODEL.md)
>   and confirmed your concern is not already a documented out-of-scope
>   item.

## Category

- [ ] Hardening request (missing best-practice, no known exploit)
- [ ] Documentation request (clarify a known limitation)
- [ ] Tooling request (additional lint / pre-commit / CI check)

## Description

Describe the concern at a high level. **Do not** include exploit code,
PoC, or any data that should not be public.

## Suggested remediation

If applicable, what would the fix look like? (e.g. "add `bandit -lll`
to pre-commit", "document that `SCHWAB_TOKEN_PATH` is intentionally
unsupported in the README").

## Additional context

Link to relevant CVEs, OWASP categories, or upstream advisories.
