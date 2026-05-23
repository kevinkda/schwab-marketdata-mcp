---
name: Feature request
about: Suggest a new tool, behavior, or improvement.
title: "feat: <short summary>"
labels: ["enhancement"]
---

## Motivation

What problem are you trying to solve? What is the current workflow that
this feature would simplify or unblock?

## Proposed change

Describe the proposed API. If this is a new tool, sketch the input /
output schema:

```text
new_tool_name(arg1: str, arg2: list[str], ...) -> {...}
```

## Plan / scope alignment

Please answer all three before submitting — these gate whether the
proposal can be accepted:

- [ ] **Is this a Schwab Market Data Production API endpoint?**
      Provide the official Schwab API doc URL if yes. (Trader API and
      streaming-only endpoints are out of scope; see plan §1.)
- [ ] **Is this covered by plan §1 / §10 (read-only Market Data,
      personal-scale, single-tenant)?**
- [ ] **Does it require new credentials, secrets, or external services
      beyond what `schwab-marketdata-mcp` already uses?** If yes, list
      them.

## Alternatives considered

Did you consider doing this via the companion `schwab-marketdata-skill`
playbooks instead of adding a server-side tool? If so, what's the gap
that requires server-side work?

## Additional context

Link to upstream Schwab API release notes, related issues, or example
agent transcripts demonstrating the gap.
