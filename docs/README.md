<img src="assets/Logo.svg" alt="CleanFrame logo" width="72" />

# CleanFrame documentation

Welcome. This folder is the **in-repo** documentation for CleanFrame.
The same content is mirrored for the [GitHub Wiki](https://github.com/inboxpraveen/Cleanframe/wiki)
under [`../wiki/`](../wiki/).

| Guide | What you'll learn |
|-------|-------------------|
| [Playground](https://inboxpraveen.github.io/Cleanframe/) | Try CleanFrame in your browser: Mess Score, recipe, diff; nothing uploaded |
| [Getting started](getting-started.md) | Install, 5-minute tour, first recipe |
| [Installation](installation.md) | pip extras, offline / air-gapped, from source |
| [Concepts](concepts.md) | Recipes, modes, quarantine, drift, lineage |
| [CLI reference](cli.md) | Every `cleanframe` subcommand |
| [Python API](api-reference.md) | `clean`, `report`, `apply_recipe`, … |
| [Recipe specification](recipe-spec.md) | YAML format v1/v2, every op |
| [Schema specification](schema-spec.md) | Target schemas and constraints |
| [Detectors & ops](detectors-and-ops.md) | Built-in inventory, how numbers are parsed |
| [Plugins](plugins.md) | Write and ship custom ops/detectors that work in CI, code export and streaming |
| [LLM planning](llm.md) | Providers, exposure modes, budgets |
| [Decision models](decisions.md) | `decide=`: Jev-style typed decisions for aliases, typo merges, null tokens, schema matches |
| [AI agents](agents.md) | MCP server and Agent Skill: make Claude Code / Cursor / Codex write recipes |
| [Benchmark](benchmark.md) | CleanBench: measuring decision models on data-cleaning decisions |
| [Production guide](production.md) | Scale, safety, pipelines, CI |
| [GitHub Action](github-action.md) | Data CI: replay recipes on every PR, fail on drift or validation errors |
| [Architecture](architecture.md) | Pipeline internals and invariants |
| [FAQ](faq.md) | Common questions |

**Contributing:** see [`../CONTRIBUTING.md`](../CONTRIBUTING.md).  
**Security:** see [`../SECURITY.md`](../SECURITY.md).  
**Changelog:** see [`../CHANGELOG.md`](../CHANGELOG.md).
