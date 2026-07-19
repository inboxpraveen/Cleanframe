# Security Policy

## Supported versions

| Version | Supported |
|---------|-----------|
| 0.2.x   | Yes       |
| 0.1.x   | Best-effort |

## Reporting a vulnerability

Please **do not** open a public GitHub issue for security vulnerabilities.

Email the maintainer via the address on the [GitHub profile](https://github.com/inboxpraveen),
or open a [private security advisory](https://github.com/inboxpraveen/Cleanframe/security/advisories/new)
on the repository.

Include:

1. A clear description of the issue and impact
2. Steps to reproduce (minimal CSV / recipe if possible)
3. Affected CleanFrame version and Python version

You should receive an acknowledgement within a few days. We will coordinate a fix
and disclosure timeline with you.

## Design guarantees relevant to security

- **No `eval` / `exec`** in the library path. Recipes are data, not code.
- Recipes and schemas are loaded with `yaml.safe_load`.
- HTML reports use Jinja2 autoescaping.
- The LLM planner never receives raw cell values in the default `metadata` exposure.
- CSV exports sanitise formula-like cells (`=`, `+`, `-`, `@`, …) by default.
- User-supplied regex patterns in recipes are length- and complexity-bounded.

## What CleanFrame is not

CleanFrame is a **data-cleaning library**, not a sandbox. Callers who pass untrusted
file paths, recipes, or schemas still control the host filesystem and process.
Treat recipe YAML from untrusted sources like any other untrusted config: review it
before applying it to production data.
