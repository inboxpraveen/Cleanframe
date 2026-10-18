# AI agents: MCP server and Agent Skill

Coding agents (Claude Code, Cursor, Codex, Copilot and the rest) are quick to "just fix the
CSV" with a few lines of pandas. That code is never reviewed, cannot be replayed on next
month's file, and does something different without warning when the file changes shape.
CleanFrame gives the agent a better output: a **recipe** that the agent writes, the user
reviews, git keeps, and pure pandas replays with a drift guard.

There are two integrations. Use either one, or both:

| | What it gives the agent | Install |
|--|--|--|
| **MCP server** | Seven tools that return structured JSON and refuse unsafe writes | `pip install "cleanframe-engine[mcp]"` |
| **Agent Skill** | Workflow and rules that teach the agent to drive the `cleanframe` CLI | `npx skills add inboxpraveen/Cleanframe` |

## Why recipes instead of agent-written pandas

| Agent-written pandas | CleanFrame recipe |
|---|---|
| Lives in a chat transcript or a scratch cell | A YAML file in git, reviewed like code |
| Re-generated, slightly differently, every time | Deterministic: the same input gives the same output |
| Silently mis-handles next month's renamed column | Replay **stops** on schema drift and says what changed |
| Drops or coerces bad rows wherever the code happens to | Invalid rows are quarantined with a reason, never deleted |
| "Trust me" | A cell-level diff of every value that changed |
| The model sees and rewrites your data | The planner is deterministic rules; the model only edits the plan |

The agent is still useful. It reads the profile, explains the plan to the user, edits the
YAML (adding a synonym map, a date format, a validation rule) and handles drift with the user.
It just never becomes the thing that executes the cleaning.

## Install the MCP server

```bash
pip install "cleanframe-engine[mcp]"     # adds the MCP Python SDK (mcp 2.3+)
cleanframe-mcp --version                 # or: python -m cleanframe.mcp_server --version
```

The server speaks MCP over **stdio**. Clients start it as a subprocess. The command must run
in the Python environment where you installed the extra. If the client cannot find
`cleanframe-mcp` on its `PATH`, give the absolute path to that environment's Python with
`-m cleanframe.mcp_server`, for example
`/path/to/venv/bin/python -m cleanframe.mcp_server` (`C:\path\to\venv\Scripts\python.exe` on Windows).

### Claude Code

```bash
claude mcp add cleanframe -- cleanframe-mcp
claude mcp add --scope project cleanframe -- cleanframe-mcp     # shared with the repo via .mcp.json
```

### Claude Desktop

Edit `claude_desktop_config.json` (macOS: `~/Library/Application Support/Claude/`, Windows:
`%APPDATA%\Claude\`), then restart Claude Desktop:

```json
{
  "mcpServers": {
    "cleanframe": {
      "command": "cleanframe-mcp"
    }
  }
}
```

### Cursor

`.cursor/mcp.json` in the project (or `~/.cursor/mcp.json` for every project):

```json
{
  "mcpServers": {
    "cleanframe": {
      "command": "cleanframe-mcp",
      "args": []
    }
  }
}
```

### VS Code

`.vscode/mcp.json`. VS Code uses a top-level `servers` key, not `mcpServers`:

```json
{
  "servers": {
    "cleanframe": {
      "type": "stdio",
      "command": "cleanframe-mcp",
      "args": []
    }
  }
}
```

### Codex

```bash
codex mcp add cleanframe -- cleanframe-mcp
```

or in `~/.codex/config.toml`:

```toml
[mcp_servers.cleanframe]
command = "cleanframe-mcp"
args = []
```

### Server options

| Option | Effect |
|--------|--------|
| `--plugin MODULE` (repeatable) | Import your custom ops/detectors first (see [Plugins](Plugins)). Installed `cleanframe.plugins` entry points and `CLEANFRAME_PLUGINS` load automatically, as in the CLI |
| `--no-plugins` | Skip automatic plugin discovery |
| `CLEANFRAME_AGENT_HIDE_VALUES=1` (environment) | Replace the cell values quoted in tool results with `"<hidden>"` (see [Privacy](#privacy)) |

In a JSON config, pass these as `"args": ["--plugin", "my_pkg.cleanframe_plugin"]` and
`"env": {"CLEANFRAME_AGENT_HIDE_VALUES": "1"}`.

## Tools

| Tool | What it does | Writes |
|------|--------------|--------|
| `profile_data(path, sheet?, nrows?, header_row?, text?)` | Quality score, per-column semantic types, detected issues (kind, column, severity, message, confidence, whether an automatic fix exists and which modes plan it) | nothing |
| `suggest_recipe(path, recipe_out, schema?, mode?, sheet?, header_row?, text?, options?, overwrite?)` | Plans a recipe with the deterministic rules planner and saves the YAML. Returns the YAML, planning notes, a diff summary, per-column change counts, a preview of up to 20 changed cells and the rows validation would quarantine | the recipe |
| `apply_recipe(path, recipe, out?, mode?, on_drift?, quarantine_out?, overwrite?)` | Replays a recipe behind the drift guard. Without `out` it is a dry run | `out`, `quarantine_out` |
| `check_drift(path, recipe, patched_recipe_out?)` | Drift findings only; optionally drafts a patched recipe for review | the draft, if asked |
| `explain_recipe(recipe)` | A plain-English, step-by-step explanation of every op, rename, frame op and validation rule | nothing |
| `list_capabilities()` | Every op (with parameters), detector, validation check, mode and planner option, including plugins | nothing |
| `export_code(recipe, out, func_name?, allow_partial?, overwrite?)` | Writes the recipe as a standalone pandas script | the `.py` |

At initialisation the server sends the workflow as MCP `instructions`: profile, suggest,
review, apply, then check drift on the next file.

Every result is a JSON object with `"ok"`. A deliberate failure comes back as
`{"ok": false, "kind": "<exception class>", "error": "<message>"}` and is flagged `isError`.
Examples are a missing file (`CleanFrameError`), a bad recipe (`RecipeError`), a refused write
(`OutputError`), an argument the tool cannot use (`InvalidArgument`) or a drift stop
(`DriftError`, with structured `drift.findings` and a `next_step` telling the agent to stop and
ask the user). Warnings CleanFrame raised during the call are listed in `warnings`.

Results are bounded for a context window: 40 issues, 60 column profiles, 3 examples per
issue and column, 20 preview cells, 20 quarantined rows, 50 drift findings, 50 entries per
per-column map (renames, change counts, reasons), 40 explained columns and rules per sheet,
12,000 characters of recipe YAML or code preview (the full file is on disk), and values cut at
80 characters. Every cap that applies is reported with a `*_truncated` field or a total count.
Nothing is dropped silently. On a 300-column file the largest result is about 28,000
characters of JSON.

## Safety rules the tools enforce

- **The input file is never written.** An `out`, `quarantine_out`, `recipe_out` or
  `patched_recipe_out` that resolves to the input data, the recipe or the schema is refused,
  even with `overwrite=true`.
- **Existing outputs are kept.** An existing output file is replaced only with
  `overwrite=true`. The one exception is a recipe or script whose new bytes are identical,
  so repeating a call is idempotent. `check_drift` has no `overwrite` and never replaces a
  different file.
- **Extensions decide formats.** A recipe must be `.yaml`/`.yml`, code must be `.py`, and data
  must be `.csv`, `.tsv`, `.txt`, `.xlsx`, `.xlsm`, `.parquet` or `.json`. A typo cannot write a
  CSV into a file named `recipe.yaml`.
- **Writes are atomic.** Each output is written to a temporary file and moved into place.
- **Drift stops by default.** `on_drift="warn"` or `"ignore"` must be passed explicitly. The
  tool descriptions tell the agent to do that only when the user said so.
- **Paths** are taken as given and resolved against the server's working directory, which
  depends on the client. Use absolute paths. Results always report absolute paths.
- **No LLM and no network.** The server never calls a model or the network. The calling agent
  is the only LLM involved.

## Privacy

The built-in LLM planner sends only metadata by default (see [LLM planning](LLM-Planning)). The MCP
tools are different: their results go to **your agent's model**, and they quote a few capped
cell values (issue and column examples, before/after cells in diff previews) because an agent
needs them to judge a fix.

With `CLEANFRAME_AGENT_HIDE_VALUES=1` in the server's environment, those value fields
(`examples`, and `before`/`after` in `preview`) become `"<hidden>"`. Missing values still show
as `null`. The switch does **not** scrub free text, which can still contain data:

| Where | What it can contain |
|-------|---------------------|
| The recipe itself: `recipe_yaml`, the `explain_recipe` text, the `export_code` preview | Category mappings (`normalize_values`) and allowed values (`check: in`) |
| Issue `message` | The value of a column that holds a single value throughout |
| `read_notes` and `warnings` | One example value that type inference changed while reading (a lost leading zero, for example) |
| Drift `findings[].message`, `warnings`, `log` | One example of a new date or number format |

So the switch limits exposure; it does not anonymise. For data that must not reach the agent's
provider at all, do not connect the agent to it.

## Agent Skill

```bash
npx skills add inboxpraveen/Cleanframe
```

This installs [`skills/cleanframe/SKILL.md`](https://github.com/inboxpraveen/Cleanframe/blob/main/skills/cleanframe/SKILL.md)
into your agents (Claude Code, Codex, Cursor and others detected by the
[skills CLI](https://github.com/vercel-labs/skills); `-a claude-code` picks one, `-g` installs for your
user instead of the project). The agent loads it when asked to clean, fix, standardise or
dedupe a CSV or Excel file, or to set up a cleaning pipeline. It teaches the workflow
(report, recipe, review, apply, drift), the hard rules (never edit the data file, no
`--overwrite` unless asked, exit code 3 means stop, keep the recipe in git), the `--json` CLI
and the plugin route for custom ops. The skill drives the CLI, so it needs only
`pip install cleanframe-engine`. If the MCP server is also connected, the skill tells the agent
to prefer the tools.

## Without MCP: the same tools from Python

The tools are plain functions in `cleanframe.agent_tools` (no `mcp` needed), for any other agent
framework or a test:

```python
from cleanframe import agent_tools

plan = agent_tools.suggest_recipe("data.csv", "data.recipe.yaml")
if plan["ok"]:
    print(plan["recipe_yaml"], plan["summary"])
```

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| Client shows the server failed to start | Run the configured command in a terminal. `cleanframe-mcp --version` must print a version. Without the extra it prints the `pip install "cleanframe-engine[mcp]"` hint and exits `1` |
| `Input file not found` with a relative path | The server's working directory is not your project. Pass absolute paths |
| `Unknown op '...'` for a custom op | Add `"args": ["--plugin", "your.module"]`, or install the plugin package into the server's environment |
| A tool returns `kind: "DriftError"` | Expected: the file changed shape. Show the findings to the user; `check_drift(..., patched_recipe_out=...)` drafts a patch |
