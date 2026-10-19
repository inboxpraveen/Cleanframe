<img src="https://raw.githubusercontent.com/inboxpraveen/Cleanframe/main/docs/assets/Logo.svg" alt="CleanFrame logo" width="72" />

# CleanFrame

**The reproducible data-cleaning engine for Python.**

AI writes the cleaning recipe once. The recipe runs forever — deterministic, diffable, reviewable.

> The LLM never touches your data. It only writes the plan.

## Documentation index

| Guide | Description |
|-------|-------------|
| [Getting Started](Getting-Started) | Install, 5-minute tour, first recipe |
| [Installation](Installation) | pip extras, offline, from source |
| [Concepts](Concepts) | Recipes, modes, quarantine, drift, lineage |
| [CLI](CLI) | Every cleanframe subcommand |
| [API Reference](API-Reference) | Python API |
| [Recipe Specification](Recipe-Specification) | YAML format v1 / v2 |
| [Schema Specification](Schema-Specification) | Target schemas |
| [Detectors and Ops](Detectors-and-Ops) | Built-ins, how numbers are parsed |
| [Plugins](Plugins) | Write and ship custom ops/detectors that work in CI, code export and streaming |
| [LLM Planning](LLM-Planning) | Providers, exposure, budgets |
| [Decision Models](Decision-Models) | Jev-style typed decisions: aliases, typo checks, null tokens, schema matches |
| [AI Agents](AI-Agents) | MCP server and Agent Skill for Claude Code, Cursor, Codex |
| [Benchmark](Benchmark) | CleanBench: how well decision models clean data |
| [Production Guide](Production-Guide) | Scale, safety, CI pipelines |
| [GitHub Action](GitHub-Action) | Data CI on pull requests and schedules |
| [Architecture](Architecture) | Internals and invariants |
| [FAQ](FAQ) | Common questions |

## Quick links

- [Repository](https://github.com/inboxpraveen/Cleanframe)
- [Contributing](https://github.com/inboxpraveen/Cleanframe/blob/main/CONTRIBUTING.md)
- [Security](https://github.com/inboxpraveen/Cleanframe/blob/main/SECURITY.md)
- [Changelog](https://github.com/inboxpraveen/Cleanframe/blob/main/CHANGELOG.md)
- [Examples](https://github.com/inboxpraveen/Cleanframe/tree/main/examples)

```bash
pip install cleanframe-engine
cleanframe demo          # bundled sample data: profile, clean, drift stop, patch
```

Or try it in your browser, with nothing installed and no data uploaded:
[inboxpraveen.github.io/Cleanframe](https://inboxpraveen.github.io/Cleanframe/)
