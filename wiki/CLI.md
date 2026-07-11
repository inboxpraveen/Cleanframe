# CLI reference

Entry point: `cleanframe` (console script from `cleanframe.cli:main`).

```bash
cleanframe --help
cleanframe <subcommand> --help
```

## `report`

Profile a file and write an HTML report (no transforms applied).

```bash
cleanframe report examples/messy_customers.csv
cleanframe report data.csv --schema examples/customer.schema.yaml -o report.html
```

## `clean`

Full pipeline: profile → detect → plan → execute → save artifacts.

```bash
cleanframe clean examples/messy_customers.csv \
  --schema examples/customer.schema.yaml \
  --mode auto \
  --out-dir out/
```

Common flags:

| Flag | Meaning |
|------|---------|
| `--schema PATH` | Target schema YAML |
| `--mode review\|auto\|strict` | Confidence / failure policy |
| `--llm provider/model` | Optional LLM planner |
| `--out-dir DIR` | Write cleaned data, recipe, code, report |
| `--out PATH` | Cleaned data path only |

## `apply`

Replay a saved recipe (deterministic, no LLM).

```bash
cleanframe apply new.csv --recipe customer.recipe.yaml --out clean.csv
cleanframe apply new.csv --recipe customer.recipe.yaml --force   # ignore drift (dangerous)
```

| Flag | Meaning |
|------|---------|
| `--recipe PATH` | Required recipe YAML |
| `--mode` | Execution mode |
| `--force` | Set `on_drift=ignore` |
| `--out PATH` | Output path |

## `suggest`

Detect drift and optionally write a patched recipe.

```bash
cleanframe suggest new.csv --recipe customer.recipe.yaml
cleanframe suggest new.csv --recipe customer.recipe.yaml --update -o patched.recipe.yaml
```

## `infer-schema`

Draft a target schema from data.

```bash
cleanframe infer-schema examples/messy_customers.csv -o examples/customer.schema.yaml
```

## `detectors` / `ops`

List registered plugins (built-ins + any you imported).

```bash
cleanframe detectors
cleanframe ops
```

## Exit behaviour

- Drift with default apply → non-zero exit / raised error
- Missing input file → error
- Validation `on_fail: error` or `strict` mode → error
