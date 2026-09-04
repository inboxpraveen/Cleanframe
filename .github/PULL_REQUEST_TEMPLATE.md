## What this changes

<!-- The user-visible behaviour change, in a sentence or two. -->

## Invariants

Which of the five invariants in CONTRIBUTING.md did you have to think about, and why is
each still intact?

- [ ] Determinism (same input, same recipe, same diff)
- [ ] The LLM never sees raw data
- [ ] Recipes round-trip losslessly and idempotently
- [ ] Nothing is silently imputed, dropped, or coerced
- [ ] Every changed cell is tracked

## Load-bearing walls

- [ ] This touches the recipe format, `executor.py`, or `planner.OP_ORDER` (say so here)

## Checks

- [ ] `pytest` passes, and new behaviour has a test
- [ ] `ruff check cleanframe tests` passes
- [ ] `mypy` passes
- [ ] Docs updated in **both** `docs/` and `wiki/` if user-facing
- [ ] `CHANGELOG.md` updated under `[Unreleased]`
