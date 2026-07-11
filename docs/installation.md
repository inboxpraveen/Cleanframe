# Installation

## Requirements

- Python **3.10**, 3.11, 3.12, or 3.13
- pandas ≥ 1.5, numpy ≥ 1.23, PyYAML ≥ 6, Jinja2 ≥ 3, python-dateutil ≥ 2.8

## From PyPI

```bash
pip install cleanframe
```

### Extras

| Extra | Installs | Needed for |
|-------|----------|------------|
| `excel` | `openpyxl` | `.xlsx` / `.xls` / `.xlsm` |
| `parquet` | `pyarrow` | `.parquet` |
| `llm` | `anthropic`, `openai` | LLM-assisted planning |
| `dev` | pytest, pytest-cov, openpyxl, ruff | contributing / CI |
| `all` | excel + parquet + llm | full feature set |

```bash
pip install "cleanframe[excel,parquet]"
pip install "cleanframe[llm]"
pip install "cleanframe[dev]"
```

## From source (editable)

```bash
git clone https://github.com/inboxpraveen/Cleanframe.git
cd Cleanframe
pip install -e ".[dev]"
pytest
```

Windows consoles that mangle `₹` / `€`: set `PYTHONUTF8=1`. The CLI forces UTF-8
stdout itself.

## Offline / air-gapped

Rules-only mode needs **no network** and **no API keys**:

```python
import cleanframe as cf
result = cf.clean(df, mode="auto")   # llm=None by default
```

Wheel + dependencies can be vendored with `pip download` on a connected machine
and installed with `pip install --no-index --find-links=./wheels cleanframe`.

LLM mode requires outbound HTTPS to your chosen provider (or a local Ollama /
LM Studio endpoint).

## Cross-platform notes

CleanFrame is tested on **Windows, macOS, and Linux**:

- All paths use `pathlib` (forward or backslash both work on Windows).
- Text artifacts (recipes, schemas, reports, codegen) are written as **UTF-8 with LF**
  newlines on every OS — no Windows CRLF drift in git.
- CSV/TSV reads accept a UTF-8 **BOM** (common from Excel on Windows).
- Parent directories are created automatically when saving outputs.
- The CLI forces UTF-8 stdout/stderr so `₹` / diff glyphs render on Windows consoles.

Set `PYTHONUTF8=1` if a legacy Windows console still mis-decodes Unicode outside the CLI.

## Optional environment variables (LLM only)

| Variable | Used by |
|----------|---------|
| `ANTHROPIC_API_KEY` | Anthropic |
| `OPENAI_API_KEY` | OpenAI (+ fallback for some providers) |
| `OPENROUTER_API_KEY` | OpenRouter |
| `GROQ_API_KEY` | Groq |
| `OPENAI_BASE_URL` | Override base URL for OpenAI-compatible APIs |
| `NO_COLOR` | Disable ANSI colours in diff rendering |

CleanFrame never stores or logs API keys.
