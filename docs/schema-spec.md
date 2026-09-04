# Schema specification

A **target schema** describes the shape you want after cleaning. It is optional
but recommended for production pipelines.

## Example

The block below is illustrative; the shipped schema for the bundled sample data
is [`examples/customer.schema.yaml`](../examples/customer.schema.yaml).

```yaml
version: 1
name: customer
columns:
  customer_name:
    dtype: string
    required: true
    aliases: ["Customer Name", "Cust Name"]
  signup_date:
    dtype: date
    required: true
    date_formats: ["%d/%m/%Y", "%Y-%m-%d"]
  amount_inr:
    dtype: float
    required: true
    min: 0
  email:
    dtype: email
    required: true
  phone:
    dtype: phone
  city: category          # dtype-only shorthand
```

`version` is optional on load; `Schema.to_dict` / `save` always emit
`version: 1`, then `name` (if set), then `columns`.

## Column fields

| Field | Meaning |
|-------|---------|
| `dtype` | Logical type (see below); defaults to `string` |
| `required` | Synthesise `not_null` validation |
| `unique` | Synthesise `unique` validation |
| `min` / `max` | Numeric bounds → comparison checks |
| `allowed_values` | Category membership check |
| `date_formats` | Hint preferred parse formats |
| `aliases` | Alternate source names for mapping |

A column may be written as `name: dtype` instead of a mapping (`city: category`),
and an empty column spec means `dtype: string`.

`aliases` must be a list; a single string is accepted and wrapped in a list.
Anything else (a mapping, a number) is rejected.

## Accepted dtypes

| dtype | Normalises to |
|-------|---------------|
| `string` | `string` |
| `str` | `string` |
| `text` | `string` |
| `id` | `string` |
| `integer` | `integer` |
| `int` | `integer` |
| `float` | `float` |
| `number` | `float` |
| `boolean` | `boolean` |
| `bool` | `boolean` |
| `date` | `date` |
| `datetime` | `datetime` |
| `email` | `email` |
| `phone` | `phone` |
| `url` | `url` |
| `category` | `category` |

Spelling is matched case-insensitively. The aliases `int`, `number`, `text`,
`bool` and `id` are normalised at load to `integer`, `float`, `string`,
`boolean` and `string`, so they drive exactly the same planning as their
canonical spelling — `int` and `number` now produce a `cast` op just as
`integer` and `float` do.

An unknown dtype is **rejected** at load time, naming the valid list:

```text
Schema column 'amount' has unknown dtype 'flaot'. Valid dtypes: bool, boolean,
category, date, datetime, email, float, id, int, integer, number, phone,
string, text, url.
```

A duplicate YAML key in a schema is an error, not last-wins.

## Inferring a draft

```bash
cleanframe infer-schema data.csv -o schema.yaml
```

```python
schema = cf.infer_schema(df, name="customer")
schema.save("customer.schema.yaml")
```

Always review inferred schemas before committing — especially `allowed_values`
and date formats.

## How schemas affect planning

1. **schema_mapping** detector proposes renames from messy → canonical names
2. Planner synthesises validations from constraints / semantic types
3. Confidence still gates inclusion by `mode`

`allowed_values` becomes a membership rule in the recipe (`check: in` with a
sibling `values:` list) — see
[Recipe specification](recipe-spec.md).
