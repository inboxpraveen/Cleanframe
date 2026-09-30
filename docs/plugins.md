# Writing and shipping plugins

CleanFrame's long tail — IBANs, national ID formats, vendor quirks — is meant to be
owned by plugins: **detectors** that spot a problem, **ops** that fix it, and
**validators** that guard it. This page covers the part that trips people up: making
a plugin work **outside the session that defined it** — in `cleanframe apply` in CI,
in generated standalone code, and in streaming.

## The three pieces

```python
# my_pkg/cleanframe_plugin.py
import re
import cleanframe as cf

_SEP = re.compile(r"[^A-Za-z0-9]")


def _codegen(params, column):
    # unindented statements that read/write df[column] in exported standalone pandas
    return [
        f"df[{column!r}] = df[{column!r}].map("
        f"lambda v: re.sub('[^A-Za-z0-9]', '', v).upper() if isinstance(v, str) else v)"
    ]


@cf.register_op("normalize_iban", streamable=True, codegen=_codegen)
def normalize_iban(series):
    """Strip separators and upper-case an IBAN."""
    return series.map(lambda v: _SEP.sub("", v).upper() if isinstance(v, str) else v)


@cf.detector("iban")
def detect_iban(series, ctx):
    issues = cf.Issues()
    values = series.dropna().astype(str)
    if len(values) and values.str.fullmatch(r"[A-Za-z]{2}\d{2}[ A-Za-z0-9]{10,30}").all():
        issues.add(
            "iban_format", "IBANs with separators or lower case", confidence=0.95,
            ops=[cf.Op("normalize_iban", {})],
        )
    return issues


@cf.validator("valid_iban")
def valid_iban(series):
    return series.isna() | series.astype(str).str.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]{10,30}")
```

Importing the module registers everything. `cf.clean(df)` then plans with your detector,
and the recipe it writes names `normalize_iban`.

## Making an op travel

An op that declares nothing works in the session that imported it and is **refused
everywhere else** rather than silently mishandled:

| You declare | What it unlocks | If you don't |
|---|---|---|
| *(nothing)* | `cf.clean` / `cf.apply_recipe` in a process that imported the module | `result.code` raises naming the op; `stream_apply` refuses; `cleanframe apply` says "Unknown op … pass `--plugin`" |
| `codegen=fn` | `result.code` / `generate_code` exports it as standalone pandas | export raises (or, with `allow_partial=True`, leaves a `# NOTE` gap) |
| `helpers="…source…"` | module-level imports/functions the generated code needs (emitted once) | — |
| `streamable=True` | `stream_apply` / `apply --chunksize` may run it chunk by chunk | streaming refuses (default-deny) |

Only set `streamable=True` if the op is **row-independent**: applying it to each chunk must equal
applying it to the whole frame. Anything that looks at other rows (deduplication, ranking, mean
imputation, inferring a format from the whole column) must not.

`codegen(params, column)` returns unindented lines that operate on `df` — `df[column]` for a column
op, `df` for a frame op (`column` is `""`). The generated module already imports `re`, `numpy as np` and
`pandas as pd`. **Test parity**: run `exec` on the generated code and compare with `apply_recipe` on
messy inputs (the `tests/test_plugins.py` file in this repository shows the pattern), ideally with
[Hypothesis](https://hypothesis.readthedocs.io/).

## Making the plugin discoverable

`cleanframe apply` runs in a fresh interpreter that has never imported your module. There are three ways
to load it:

```toml
# pyproject.toml of your package — loaded whenever the CLI starts (or cf.load_plugins() runs)
[project.entry-points."cleanframe.plugins"]
iban = "my_pkg.cleanframe_plugin"
```

```bash
cleanframe --plugin my_pkg.cleanframe_plugin apply in.csv --recipe r.yaml   # explicit, repeatable
CLEANFRAME_PLUGINS=my_pkg.cleanframe_plugin,other_mod cleanframe apply ...   # environment
```

```python
cf.load_plugins(["my_pkg.cleanframe_plugin"])   # from Python; idempotent
```

If a recipe names an op nobody has registered, CleanFrame does one lazy discovery pass (entry points +
`CLEANFRAME_PLUGINS`) before failing, so a plugin package that is merely **installed** in the CI
environment is enough. A plugin that raises on import is reported by name:
`Plugin 'my_pkg.cleanframe_plugin' (from entry point 'iban') failed to load: RuntimeError: …`.

**Trust.** Loading a plugin runs its code, exactly like installing any Python package. Install only
plugins you trust. Set `CLEANFRAME_NO_PLUGINS=1` (or pass `--no-plugins`) to switch off automatic discovery;
explicit `--plugin` / `load_plugins([...])` still work.

## Rules of the road

- **Pure and deterministic.** No clock, randomness, network or hidden state — the whole
  point of a recipe is that it replays byte-for-byte.
- **Parameters are validated at load.** An op's keyword parameters are read from its function signature
  (plus `aliases=`); anything else in a recipe is refused. If your op takes a compact form (`normalize_iban: DE`),
  provide `coerce=` (compact → canonical params) and `compact=` (canonical → minimal YAML) and keep
  `coerce(compact(p)) == p`.
- **Missing values pass through.** Ops should leave NaN/None alone unless clearing them is the point.
- **Nothing silently disappears.** If your op cannot parse a cell it should return NaN — the executor counts
  and warns about values that *became* missing — never a guess.
- **Naming.** Prefix custom ops and detectors with your organisation or domain to avoid collisions
  (`acme_normalize_sku`); a duplicate registration raises.

See [Detectors & ops](detectors-and-ops.md) for the built-in inventory and
[`CONTRIBUTING.md`](../CONTRIBUTING.md) for the invariants a contribution to CleanFrame itself must keep.
