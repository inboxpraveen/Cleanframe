# Recipe specification (format v1 / v2)

Recipes are YAML documents with `version: 1`. They are the durable artifact
CleanFrame is built around.

A recipe stays `version: 1` unless it carries a `read:` section (below), which
promotes it to `version: 2`; the loader reads both. Workbook recipes are a
separate `version: 2` shape — see *Workbook recipes* below.

## Top-level fields

```yaml
version: 1                          # required
source_fingerprint: { ... }         # optional; enables drift detection
columns:                            # map of source column name → ColumnRecipe
  "Customer Name":
    rename_to: customer_name
    ops: [strip_whitespace, title_case]
frame_ops:                          # optional list
  - dedup: {subset: [email], keep: first}
validate:                           # optional list of rules
  - {column: email, check: valid_email, on_fail: quarantine}
meta:                               # optional free-form
  generated_by: rules
```

## Column recipes

| Field | Meaning |
|-------|---------|
| key | **Source** column name as it appears in the input file |
| `rename_to` | Output name after Phase 2 |
| `ops` | Ordered list of column ops (see below) |

Ops may be bare names or mappings with parameters:

```yaml
ops:
  - strip_whitespace
  - parse_date:
      formats: ["%d/%m/%Y", "%Y-%m-%d"]
      dayfirst: true
  - normalize_values:
      Bengaluru: Bangalore
      BLR: Bangalore
```

## Column ops (execution order)

The planner emits ops in canonical `OP_ORDER`. When editing by hand, prefer the
same order so transforms compose safely:

1. `strip_whitespace`
2. `collapse_whitespace`
3. `to_na`
4. `extract_currency`
5. `remove_symbols`
6. `normalize_unit`
7. `parse_number`
8. `round`
9. `cast`
10. `parse_date`
11. `normalize_email`
12. `normalize_phone`
13. `replace`
14. `normalize_values`
15. `capitalize` / `title_case` / `lowercase` / `uppercase`
16. `fill_na` *(never auto-proposed — human only)*

### Notable parameters

| Op | Params |
|----|--------|
| `to_na` | `tokens`, `case_insensitive` |
| `parse_date` | `formats`, `dayfirst`, `output` (`iso` default) |
| `parse_number` | decimal/thousands separators, strip symbols |
| `cast` | `to`: `float` \| `int` \| `string` \| `bool` \| `datetime` \| `category` — **int rounds floats** |
| `normalize_phone` | `country_code` |
| `normalize_values` | mapping `{variant: canonical}` |
| `extract_currency` | emits `<col>_currency` |
| `normalize_unit` | `to` target unit |
| `replace` | `pattern`, `repl`, `regex` — patterns length/complexity limited |
| `fill_na` | `value` or `strategy` (`mean`/`median`/`mode`) |
| `round` | `ndigits` |

## Frame ops

| Op | Params |
|----|--------|
| `dedup` | `subset`, `keep`, `case_insensitive` |
| `drop_columns` | list of names |

## Validation rules

```yaml
validate:
  - column: amount_inr
    check: ">= 0"
    on_fail: quarantine
  - column: email
    check: valid_email
    on_fail: quarantine
  - column: code
    check: "matches: ^[A-Z]{3}$"
    on_fail: warn
```

### Named checks

`not_null`, `unique`, `valid_email`, `valid_url`, `valid_phone`

### Expressions

- Comparisons: `>= 0`, `<= 100`, `== 1`, `!= 0`, `>`, `<`
- Membership: `in [a, b, c]`
- Regex: `matches: <pattern>` or `regex: <pattern>`

### `on_fail` policies

| Policy | Behaviour |
|--------|-----------|
| `quarantine` | Move row to quarantine frame (default) |
| `error` | Raise `ValidationFailure` |
| `warn` | Log only |
| `drop` | Discard row (explicit) |
| `null` | Blank the offending cell |

`strict` mode promotes every policy to `error`.

## Fingerprint

Stored so `apply_recipe` can detect drift. Includes column names, dtypes, row
count, and a sample hash. Do not hand-edit unless you know why.

## `read:` section (v2)

Optional top-level block recording how the source slice was read, so
`apply_recipe` re-reads the same slice. Its presence promotes the recipe to
`version: 2`.

```yaml
version: 2
read:
  sheet: "Q3"            # Excel sheet name or 0-based index
  columns: [id, email]   # usecols subset — a filter, not a reorder
  nrows: 10000
  skiprows: 2
  encoding: utf-8        # pinned by read-time format correction
  sep: ","               # pinned delimiter
columns:
  ...
```

`clean`/`report` record `sheet`/`columns`/`nrows`/`skiprows` (and, from format
auto-correction, `encoding`/`sep`); `apply_recipe` replays them. Under
`skiprows`/`nrows` the diff `row_id` is relative to the loaded slice.

## Workbook recipes

A multi-sheet Excel workbook produces a **separate** shape: `version: 2` with a
top-level `sheets:` mapping (sheet name → a normal recipe). A per-sheet recipe
never carries its own `read.sheet` — the dict key is the sheet.

```yaml
version: 2
sheets:
  Customers:
    columns:
      "Customer Name": {rename_to: customer_name, ops: [strip_whitespace]}
  Orders:
    columns:
      amount: {ops: [parse_number]}
```

Load with `load_recipe(path)` (auto-detects the `sheets:` block) or
`WorkbookRecipe.load(path)`. `Recipe.from_dict` rejects a `sheets:` doc and
points to `WorkbookRecipe`/`load_recipe`.

## Round-trip contract

```python
assert Recipe.from_yaml(recipe.to_yaml()) == recipe
assert recipe.to_yaml() == Recipe.from_yaml(recipe.to_yaml()).to_yaml()
```

Ops with parameters must implement `coerce` / `compact` so YAML stays minimal
and lossless — see [`CONTRIBUTING.md`](../CONTRIBUTING.md).
