# Recipe specification (format v1 / v2)

Recipes are YAML documents with `version: 1`. They are the durable artifact
CleanFrame is built around.

A recipe stays `version: 1` unless it carries a `read:` section (below), which
promotes it to `version: 2`; the loader reads both. Workbook recipes are a
separate `version: 2` shape — see *Workbook recipes* below.

## Typos fail at load, not at run time

An unknown or misspelled **op parameter is rejected** when the recipe loads, with
the list of valid parameters for that op:

```text
Op 'dedup' got unknown parameter(s) ['case_insensitive']. Valid parameters:
['ignore_case', 'keep', 'subset']. A misspelled parameter would be ignored.
```

Also validated at load time, before any data is touched:

| Rejected | Example message |
|----------|-----------------|
| Unknown op name | `Unknown op 'stip_whitespace'. Known ops: …` |
| Unknown op parameter | see above |
| A `cast` target outside the list below | `Op 'cast' has unknown target 'flaot'.` |
| A `normalize_unit` target outside the units below | `Op 'normalize_unit' has unknown target unit 'furlong'.` |
| `dedup` `keep` other than `first` / `last` / `false` | `Op 'dedup' keep must be 'first', 'last' or false.` |
| A `fill_na` `strategy` outside the list below | `Op 'fill_na' unknown strategy 'average'.` |
| An unknown validation `check` name | `Unknown validation check 'valid_emial'.` |
| An `on_fail` outside the five policies | `Validation on_fail must be one of […]` |
| An unknown key in `read:` | `Unknown key(s) ['bogus'] in the recipe 'read' section.` |
| An unknown top-level key | `Unknown top-level recipe key(s): […]` |
| A duplicate YAML key anywhere in the file | `duplicate key 'version' on line 2 …` |

Duplicate YAML keys are an **error**, not last-wins. `version` must be a whole
number and one of `1` / `2` (anything non-numeric is rejected). `meta` must be a
mapping. `rename_to` must be a non-empty string.

Spell every parameter exactly as the table below gives it — the loader accepts no
near-misses.

## YAML gotchas the loader defends against

CleanFrame reads recipes with YAML 1.1 rules, which have famous traps. What it does about them:

- **Column names.** A plain (unquoted) mapping key is always kept as text: `yes:`, `no:`, `on:`, `null:`, `~:`,
  `010:`, `0x10:` and `12:30:` name columns `yes`, `no`, `on`, `null`, `~`, `010`, `0x10` and `12:30` — not `True`,
  `False`, `None`, `8`, `16` or `750`. (Generated recipes quote them anyway.)
- **Text values.** Values are *not* re-typed for you: `to_na: [no, null]` would hand the op a boolean and `None`, so
  it is refused at load with a "quote it" hint. Write `to_na: ["no", "null"]`. The same applies to `replace:`
  `pattern`/`repl` and `parse_number` `decimal`/`thousands` (each must be text/one character).
- **Everything else fails at load, not at replay:** unknown ops and parameters, duplicate keys, a `version:` that is
  not an integer (`true` is refused), a ReDoS-prone `replace` pattern, a `repl` that refers to a regex group the
  pattern does not have, and unknown option keys in `read:`.
- `ops: strip_whitespace` (a bare name) is a one-op list.

## Top-level fields

```yaml
version: 1                          # required
source_fingerprint: { ... }         # optional; enables drift detection
columns:                            # map of source column name → ColumnRecipe
  "Customer Name":
    rename_to: customer_name
    ops: [strip_whitespace, title_case]
dedup: {subset: [email], keep: first}   # optional; dedup serialises at the top level
frame_ops:                          # optional list (dedup also loads here)
  - drop_columns: [internal_notes]
validate:                           # optional list of rules
  - {column: email, check: valid_email, on_fail: quarantine}
meta:                               # optional free-form
  generated_by: rules
```

`read:` is the one further top-level key (see below). Anything else is rejected.

## Column recipes

| Field | Meaning |
|-------|---------|
| key | **Source** column name as it appears in the input file |
| `rename_to` | Output name after Phase 2 (non-empty string) |
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

An op may also appear as a sibling key next to `rename_to:` instead of inside
`ops:`; both load to the same recipe.

## Column ops (execution order)

The planner emits ops in canonical `OP_ORDER`. When editing by hand, prefer the
same order so transforms compose safely:

1. `normalize_unicode`
2. `strip_whitespace`
3. `collapse_whitespace`
4. `to_na`
5. `extract_currency`
6. `remove_symbols`
7. `normalize_unit`
8. `parse_number`
9. `round`
10. `cast`
11. `parse_date`
12. `normalize_email`
13. `normalize_phone`
14. `replace`
15. `normalize_values`
16. `capitalize` / `title_case` / `lowercase` / `uppercase`
17. `fill_na` *(never auto-proposed — human only)*

An op the planner does not order keeps its insertion position after all ordered
ones. Ops inside a hand-written `ops:` list run exactly as written.

## Op parameters

`strip_whitespace`, `collapse_whitespace`, `lowercase`, `uppercase`,
`title_case`, `normalize_email` and `capitalize` take no parameters.

| Op | Parameters | Notes |
|----|------------|-------|
| `normalize_unicode` | `form` | `NFC` (default), `NFD`, `NFKC` or `NFKD`. Also removes zero-width spaces, word joiners and BOMs and turns a non-breaking space into a plain one. Zero-width *joiners* are kept (emoji sequences, several scripts). `normalize_unicode: NFKC` is the bare form |
| `to_na` | `tokens`, `case_insensitive` | `tokens` is a string or list; omitted uses the default token list below. `case_insensitive` defaults `true`. A bare list or scalar is the token list: `to_na: ["-", "?"]` |
| `parse_date` | `formats`, `dayfirst`, `yearfirst`, `output` | `formats` is a **list** of strftime patterns (a bare string is rejected); `allowed` is accepted as an alias. `dayfirst`/`yearfirst` default `false`. `output` defaults `%Y-%m-%d`; `iso`/`date` mean the same; `datetime`/`raw`/`none` keep datetime dtype; anything else is used as a strftime pattern |
| `parse_number` | `decimal`, `thousands`, `symbols` | Defaults `"."`, `","`, `[]`. Set `decimal: ","` / `thousands: "."` for European formats; `symbols` is a list of substrings to strip first |
| `round` | `decimals` | `round: 2` or `round: {decimals: 2}` |
| `cast` | `to` | `cast: float` shorthand or `cast: {to: float}`. Targets: `float`, `float64`, `number`, `int`, `integer`, `int64`, `string`, `str`, `text`, `bool`, `boolean`, `datetime`, `date`, `category` — **int rounds floats** (banker's rounding, to nullable `Int64`) |
| `remove_symbols` | `symbols` | A string or a list of strings: `remove_symbols: ","` or `remove_symbols: ["₹", ","]`. A number is rejected — quote it |
| `replace` | `pattern`, `repl`, `regex` | `pattern` is required; `repl` defaults `""`, `regex` defaults `true`. Patterns are length/complexity limited |
| `normalize_phone` | `default_country_code` | `country_code` and `region` are accepted as aliases. A bare string is the code: `normalize_phone: "+91"` |
| `normalize_values` | `map`, `case_insensitive` | A bare mapping **is** the map: `normalize_values: {BLR: Bangalore}`. Use the `map:` key when you also want `case_insensitive: true` |
| `extract_currency` | `to`, `default` | Emits a new ISO-code column named `to` (default `<col>_currency`) and leaves the source column unchanged. `default` is the code for cells with no symbol. A bare string is `to` |
| `normalize_unit` | `to`, `emit_unit_column` | `to` is a unit from one family: mass `mg` `g` `kg` `oz` `lb`; length `mm` `cm` `m` `km` `in` `ft` `yd`; volume `ml` `l` `gal` (plural and long spellings such as `lbs`, `pounds`, `kgs`, `inches` are recognised). A bare string is `to`. Cross-family and unparseable cells become NaN; `emit_unit_column` records the original unit |
| `fill_na` | `value`, `strategy` | `strategy` is one of `mean`, `median`, `mode`, `ffill`, `pad`, `bfill`, `backfill`, `zero`, `empty`; otherwise `value` is a constant. A bare scalar is `value`: `fill_na: 0`. Never auto-proposed |

Default `to_na` tokens (matched case-insensitively after trimming): empty string,
`na`, `n/a`, `n.a.`, `null`, `none`, `nil`, `nan`, `-`, `--`, `?`, `unknown`,
`not available`, `not applicable`.

## Frame ops

| Op | Parameters | Notes |
|----|------------|-------|
| `dedup` | `subset`, `keep`, `ignore_case` | `subset` is a column name or list (omitted = all columns); `keep` is `first` (default), `last` or `false`; `ignore_case` defaults `false` and also ignores surrounding whitespace |
| `drop_columns` | `columns` | A name or a list: `drop_columns: [a, b]`. Absent columns are ignored |

`dedup` is serialised at the **top level** — that is what `Recipe.to_dict` emits,
with default parameters pruned:

```yaml
dedup: {subset: [email]}     # keep: first is the default, so it is omitted
```

```yaml
dedup: true                  # dedup on every column, all defaults
```

The equivalent `frame_ops: [{dedup: {subset: [email]}}]` also loads, as does the
shorthand `dedup: [email, phone]` (a bare subset list). Every other frame op
serialises under `frame_ops:`.

## Validation rules

```yaml
validate:
  - column: amount_inr
    check: ">= 0"
    on_fail: quarantine
  - column: email
    check: valid_email
    on_fail: quarantine
  - column: city
    check: in
    values: [Bangalore, Bengaluru, Mumbai, Delhi]
    on_fail: quarantine
  - column: code
    check: "matches: ^[A-Z]{3}$"
    on_fail: warn
```

`column` and `check` are required; `on_fail` defaults to `quarantine`.

### Named checks

`not_null`, `unique`, `valid_email`, `valid_url`, `valid_phone`

Register your own with `@cleanframe.validator("name")` **before** loading the
recipe — an unrecognised `check` is rejected at load.

### Expressions

- Comparisons: `>= 0`, `<= 100`, `== 1`, `!= 0`, `>`, `<`
- Membership: `in [a, b, c]`, or `check: in` with a sibling `values:` list
- Regex: `matches: <pattern>` or `regex: <pattern>`

Both membership forms are equivalent. `check: in` plus `values:` is what a
generated recipe emits (it is how a schema's `allowed_values` is expressed):

```yaml
- {column: city, check: in, values: [Bangalore, Mumbai]}
- {column: city, check: "in [Bangalore, Mumbai]"}
```

Membership values are compared **as text**, so `in [Yes, No]` matches the
literal strings `"Yes"` / `"No"`. YAML reads a bare `Yes`/`No`/`On`/`Off` as a
boolean; every spelling of that boolean is matched, so both forms below behave
the same. Quoting is still clearer about what the data holds:

```yaml
- {column: consent, check: in, values: ["Yes", "No"]}
- {column: consent, check: in, values: [Yes, No]}     # same effect
```

Null cells always pass a membership, comparison or regex check; use `not_null`
to require a value.

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
`version: 2`; a `version: 2` recipe without a `read:` section loads and
serialises back as `version: 1`.

```yaml
version: 2
read:
  sheet: "Q3"            # Excel sheet name or 0-based index
  columns: [id, email]   # usecols subset — a filter, not a reorder
  nrows: 10000
  skiprows: 2
  encoding: utf-8        # pinned by read-time format correction
  sep: ","               # pinned delimiter
  blank_lines: 0         # empty lines above the header
  text: true             # every field was read verbatim
columns:
  ...
```

| Key | Meaning |
|-----|---------|
| `sheet` | Excel sheet name, or a 0-based index |
| `columns` | Column subset (`usecols`) — a filter, not a reorder; output keeps file order |
| `nrows` | Read only the first N data rows |
| `skiprows` | Skip N leading **data** rows (the header row is kept), or a list of 1-based data-row numbers |
| `header_row` | 0-based line (CSV) / row (Excel) holding the column names; everything above it is skipped. Recorded together with `blank_lines` (the same number) so a streamed replay skips the same lines |
| `encoding` | File encoding, pinned by read-time format correction |
| `sep` | Field delimiter, pinned by read-time format correction |
| `blank_lines` | Empty lines above the header row, found by the format corrector |
| `text` | `true` means every field was read verbatim — no numeric coercion, no invented nulls, so leading zeros, literal `NA`/`None` and `1e5` survive |

Any other key is rejected.

`clean`/`report` record `sheet`/`columns`/`nrows`/`skiprows`/`header_row` and `text` (and,
from format auto-correction, `encoding`/`sep`/`blank_lines`); `apply_recipe`
replays them. Under `skiprows`/`nrows` the diff `row_id` is relative to the
loaded slice.

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

## Number-format drift

A column the recipe parses with `parse_number` is checked on replay: if 5% or more of its digit-bearing values no longer
fit the recipe's `decimal`/`thousands` convention (a European `€1.200,50` against a `.`-decimal recipe), replay stops with a
`number_format_drift` finding (exit `3`). A smaller share is reported as INFO — a stray `12ab34` is noise, not a new format.
Values with no digit (`N/A`, blanks) never count.

## Round-trip contract

```python
assert Recipe.from_yaml(recipe.to_yaml()) == recipe
assert recipe.to_yaml() == Recipe.from_yaml(recipe.to_yaml()).to_yaml()
```

Ops with parameters must implement `coerce` / `compact` so YAML stays minimal
and lossless — see [`CONTRIBUTING.md`](../CONTRIBUTING.md).
