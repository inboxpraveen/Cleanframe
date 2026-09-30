# Detectors & ops

## Built-in detectors

| Name | Scope | Priority | Proposes fix? | Notes |
|------|-------|----------|---------------|-------|
| `schema_mapping` | frame | 5 | Renames | Requires target schema |
| `unicode` | column | 8 | `normalize_unicode` | Zero-width characters / BOMs, non-breaking spaces, decomposed accents (`cafe` + U+0301 vs `café`) |
| `whitespace` | column | 10 | Yes | Strip / collapse |
| `nulls` | column | 20 | `to_na` for disguised nulls | Real nulls reported only |
| `dates` | column | 40 | `parse_date` | Mixed formats → ISO |
| `emails` | column | 45 | `normalize_email` | |
| `phones` | column | 45 | `normalize_phone` | |
| `currency` | column | 45 | parse + optional currency split | `1.234`-shaped values with no other evidence are reported as `ambiguous_number_format` and **not** parsed |
| `units` | column | 46 | `normalize_unit` | |
| `categories` | column | 50 | `normalize_values` | Low cardinality only |
| `text_case` | column | 60 | casing ops | Name-like columns |
| `outliers` | column | 70 | **No** | Flag only |
| `dedup` | frame | 80 | `dedup` for exact | Fuzzy reported |

On large columns, detectors sample up to **50,000** non-null values for pattern
inference. Execution still transforms every row.

List at runtime: `cleanframe detectors` / `cf.list_detectors()`.

## Built-in ops

**Column:** `strip_whitespace`, `collapse_whitespace`, `lowercase`, `uppercase`,
`title_case`, `capitalize`, `remove_symbols`, `replace`, `to_na`, `fill_na`,
`normalize_email`, `normalize_phone`, `parse_number`, `cast`, `round`,
`parse_date`, `normalize_values`, `extract_currency`, `normalize_unit`

**Frame:** `dedup`, `drop_columns`

List at runtime: `cleanframe ops` / `cf.list_ops()`.

## Writing a detector

```python
import cleanframe as cf
import pandas as pd
from cleanframe.types import Op, Severity

@cf.detector("iban", priority=45)
def detect_iban(series: pd.Series, ctx: cf.DetectorContext) -> cf.Issues:
    issues = cf.Issues()
    # early-out on irrelevant semantic types…
    issues.add(
        "invalid_iban",
        "…",
        severity=Severity.WARNING,
        confidence=0.9,
        ops=[Op("remove_symbols", {"symbols": [" "]})],
    )
    return issues
```

Import the module from `cleanframe/detectors/__init__.py` (or import it yourself
before calling `clean`). A plugin that lives outside this repository is loaded with
`cf.load_plugins`, `--plugin`, `CLEANFRAME_PLUGINS` or a `cleanframe.plugins` entry point — see the
[plugin guide](plugins.md).

## Writing an op

Must be pure and deterministic. Parameterised ops need `coerce` / `compact`.
Add the name to `OP_ORDER` in `planner.py` if the planner should emit it.

To let a custom op leave your session, pass `codegen=` (export as standalone pandas),
`helpers=` (module-level code the export needs) and `streamable=True` (row-independent, so
`stream_apply` may chunk it) to `register_op`. Each is opt-in; an op that declares none is
refused by the exporter and by streaming instead of being silently skipped.

## How numbers are parsed

`parse_number` (and the planner's currency/number fixes) use one strict, locale-aware parser that the
generated standalone code embeds verbatim, so both agree:

- **Decoration is ignored:** currency symbols, unit text (`1200 INR`), NBSP and zero-width characters, full-width digits (`１２３`).
- **Sign is found wherever it is written:** `-5`, `-$5`, `$-5`, `5-`, `(5)`, `$(5)`, `($5)`, and the unicode minus.
- **Structure is validated for the convention.** With `thousands=","`, grouping must be 3-digit (`1,234,567`) or Indian
  (`1,20,00,000`); `2,5` is *not* 25 and `1.234,56` is not `1.23456` — they become a **counted, warned null**
  instead of a wrong number. Set `decimal`/`thousands` (`","`/`"."`) for European data; the currency detector infers that
  only from unambiguous evidence (`1.200,50`, `12,5`) and keeps the default when a column mixes both conventions.
- **Ambiguity is refused, not guessed:** in `normalize_unit`, `1,500 g` could be 1500 g or 1.5 g, so it becomes null with
  a warning (`1,5 kg` and `0.500 kg` are unambiguous and parse).
- **`cast: int` is exact** for integer text (no float64 round-trip), so 19-digit IDs are not rounded.

Categories are never merged across a numeric difference: `-300`/`300`, `12.00`/`1,200` and `A-1`/`A1` differ in sign, decimal
or grouping and stay separate values.

## Writing a validator

```python
@cf.validator("valid_iban")
def _(series):
    return series.isna() | series.astype(str).str.match(IBAN_RE)
```

Return a boolean pass-mask (`True` = ok). NaN usually passes unless the check is
`not_null`.

Full contributor guide: [`CONTRIBUTING.md`](../CONTRIBUTING.md).
