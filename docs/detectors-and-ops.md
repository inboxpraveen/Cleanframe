# Detectors & ops

## Built-in detectors

| Name | Scope | Priority | Proposes fix? | Notes |
|------|-------|----------|---------------|-------|
| `schema_mapping` | frame | 5 | Renames | Requires target schema |
| `whitespace` | column | 10 | Yes | Strip / collapse |
| `nulls` | column | 20 | `to_na` for disguised nulls | Real nulls reported only |
| `dates` | column | 40 | `parse_date` | Mixed formats → ISO |
| `emails` | column | 45 | `normalize_email` | |
| `phones` | column | 45 | `normalize_phone` | |
| `currency` | column | 45 | parse + optional currency split | |
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
before calling `clean`).

## Writing an op

Must be pure and deterministic. Parameterised ops need `coerce` / `compact`.
Add the name to `OP_ORDER` in `planner.py` if the planner should emit it.

## Writing a validator

```python
@cf.validator("valid_iban")
def _(series):
    return series.isna() | series.astype(str).str.match(IBAN_RE)
```

Return a boolean pass-mask (`True` = ok). NaN usually passes unless the check is
`not_null`.

Full contributor guide: [`CONTRIBUTING.md`](../CONTRIBUTING.md).
