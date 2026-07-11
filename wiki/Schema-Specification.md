# Schema specification

A **target schema** describes the shape you want after cleaning. It is optional
but recommended for production pipelines.

## Example

See [`examples/customer.schema.yaml`](https://github.com/inboxpraveen/Cleanframe/blob/main/examples/customer.schema.yaml):

```yaml
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
```

## Column fields

| Field | Meaning |
|-------|---------|
| `dtype` | Logical type: `string`, `integer`, `float`, `boolean`, `date`, `datetime`, `email`, `phone`, `url`, `category`, … |
| `required` | Synthesise `not_null` validation |
| `unique` | Synthesise `unique` validation |
| `min` / `max` | Numeric bounds → comparison checks |
| `allowed_values` | Category membership check |
| `date_formats` | Hint preferred parse formats |
| `aliases` | Alternate source names for mapping |

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
