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
| [Recipe Specification](Recipe-Specification) | YAML format v1 |
| [Schema Specification](Schema-Specification) | Target schemas |
| [Detectors and Ops](Detectors-and-Ops) | Built-ins + plugins |
| [LLM Planning](LLM-Planning) | Providers, exposure, budgets |
| [Production Guide](Production-Guide) | Scale, safety, CI pipelines |
| [Architecture](Architecture) | Internals and invariants |
| [FAQ](FAQ) | Common questions |

## Quick links

- [Repository](https://github.com/inboxpraveen/Cleanframe)
- [Contributing](https://github.com/inboxpraveen/Cleanframe/blob/main/CONTRIBUTING.md)
- [Security](https://github.com/inboxpraveen/Cleanframe/blob/main/SECURITY.md)
- [Changelog](https://github.com/inboxpraveen/Cleanframe/blob/main/CHANGELOG.md)
- [Examples](https://github.com/inboxpraveen/Cleanframe/tree/main/examples)

```bash
pip install cleanframe
cleanframe report examples/messy_customers.csv
```
