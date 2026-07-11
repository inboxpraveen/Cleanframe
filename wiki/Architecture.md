# Architecture

```
CSV / Excel / DataFrame
   │
   ▼
profile.py ──► detectors/ ──► planner.py ──► Recipe (recipe.py)
                  │              │                │
                  │         llm.py (optional)     │
                  ▼                               ▼
               Issues                      executor.py
                                                  │
                     ┌────────────────────────────┼────────────────┐
                     ▼              ▼              ▼                ▼
                validate.py      diff.py       drift.py         report.py
```

## Module map

| Module | Responsibility |
|--------|----------------|
| `api.py` | High-level `clean` / `report` / `apply_recipe` / `suggest_update` |
| `profile.py` | Semantic typing + column stats |
| `detectors/` | Issue discovery + op proposals (plugins) |
| `planner.py` | Confidence gating, rename resolution, `OP_ORDER` |
| `llm.py` | Optional plan writer (metadata only) |
| `recipe.py` | YAML model |
| `executor.py` | Deterministic 4-phase replay + lineage |
| `ops.py` | Pure pandas transforms |
| `validate.py` | Post-clean checks + quarantine |
| `diff.py` | Cell-level before/after |
| `drift.py` | Fingerprint comparison on replay |
| `schema.py` | Target schema model |
| `codegen.py` | Recipe → standalone pandas script |
| `dataio.py` | Path ↔ DataFrame |
| `cli.py` | Console entry |

## Executor phases

1. **Column ops** — apply each column's op list in order
2. **Renames** — atomic rename map (clash → `ExecutionError`)
3. **Frame ops** — dedup / drop_columns (preserve surviving row index)
4. **Validation** — policies → quarantine / error / …

Then `compute_diff(original, cleaned, lineage)`.

## Dependency direction

Lower layers must not import higher ones at module load time. Local imports inside
functions are used where cycles would otherwise appear (e.g. profile ↔ detectors).

## Determinism tactics

- Sort before iterating when order is observable
- No `random` / clock / network in the core path (LLM sample shuffle is seeded by
  column name and only affects the prompt, not the recipe executor)
- Detector registry runs in `(priority, name)` order; columns in frame order

## Extension points

- `@detector` / `@register_op` / `@validator`
- Custom `LLMClient`
- Custom `Planner` passed to `cf.clean(..., planner=...)`
