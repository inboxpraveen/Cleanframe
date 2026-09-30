# Security Policy

## Supported versions

| Version | Supported |
|---------|-----------|
| 0.4.x   | Yes       |
| 0.3.x   | Security fixes only |
| < 0.3   | No (never published under the `cleanframe-engine` name) |

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
- Recipes and schemas are loaded with a `SafeLoader` subclass (no arbitrary Python object construction).
- HTML reports use Jinja2 autoescaping.
- The LLM planner never receives raw cell values in the default `metadata` exposure, and each
  provider reads only its own API-key environment variable (an `OPENAI_API_KEY` is never sent to another provider).
- An LLM-authored recipe can never contain `fill_na` or `drop_columns`; they are stripped with a warning.
- CSV/Excel exports sanitise formula-like cells **and header labels** (`=`, `@`, and
  `+`/`-` followed by anything that is not a plain number) by default. A signed
  number is left alone so normalised phone numbers and negative amounts survive.
- Generated standalone pandas escapes column names and validation labels, so a
  crafted CSV header cannot inject code into the exported module.
- User-supplied regex patterns in recipes are length- and complexity-bounded, and refused at load time.
- Recipes and schemas reject duplicate YAML keys instead of silently keeping the last.
- Cleaned output never overwrites its own input file without an explicit opt-in.

## What CleanFrame is not

CleanFrame is a **data-cleaning library**, not a sandbox. Callers who pass untrusted
file paths, recipes, or schemas still control the host filesystem and process.
Treat recipe YAML from untrusted sources like any other untrusted config: review it
before applying it to production data.

**Plugins run code.** `cleanframe.plugins` entry points, `CLEANFRAME_PLUGINS` and `--plugin MODULE` import Python
modules, exactly like installing a package. Only install plugins you trust; set `CLEANFRAME_NO_PLUGINS=1` (or
pass `--no-plugins`) to disable automatic discovery. A recipe cannot load a plugin by itself: it can only *name* an
op, which is resolved against the plugins already installed or explicitly requested.
