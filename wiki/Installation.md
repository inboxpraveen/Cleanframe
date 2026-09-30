# Installation

## Requirements

- Python **3.10**, 3.11, 3.12, or 3.13
- pandas ≥ 1.5, numpy ≥ 1.23, PyYAML ≥ 6, Jinja2 ≥ 3, python-dateutil ≥ 2.8

## From PyPI

```bash
pip install cleanframe-engine
```

The distribution name on PyPI is `cleanframe-engine`; the import package is
`cleanframe` (`import cleanframe as cf`), and the CLI is `cleanframe` — or
`python -m cleanframe`, an equivalent alias for every invocation.

Package page: [https://pypi.org/project/cleanframe-engine/](https://pypi.org/project/cleanframe-engine/)

Pin a version in a requirements file the usual way:

```
cleanframe-engine==0.4.0
```

Check what you got:

```bash
cleanframe --version
```

### Extras

| Extra | Installs | Needed for |
|-------|----------|------------|
| `excel` | `openpyxl` | `.xlsx` / `.xlsm` |
| `parquet` | `pyarrow` | `.parquet` |
| `llm` | `anthropic`, `openai` | LLM-assisted planning |
| `dev` | pytest, pytest-cov, openpyxl, ruff, mypy | contributing / CI |
| `all` | excel + parquet + llm | full feature set |

```bash
pip install "cleanframe-engine[excel,parquet]"
pip install "cleanframe-engine[llm]"
pip install "cleanframe-engine[all]"
pip install "cleanframe-engine[dev]"
```

Straight from git, if you need a change that is not released yet:

```bash
pip install "cleanframe-engine @ git+https://github.com/inboxpraveen/Cleanframe"
```

### Legacy `.xls`

The `excel` extra (openpyxl) covers `.xlsx` and `.xlsm`. Reading a legacy `.xls`
workbook needs a different engine:

```bash
pip install xlrd
```

Writing `.xls` is refused: pandas emits `.xlsx` bytes, which Excel rejects under
an `.xls` name. Write `.xlsx` instead.

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
and installed with
`pip install --no-index --find-links=./wheels cleanframe-engine`.

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
| `OPENAI_API_KEY` | OpenAI, Azure and generic `openai-compatible` **only** |
| `OPENROUTER_API_KEY` | OpenRouter |
| `GROQ_API_KEY` | Groq |
| `TOGETHER_API_KEY` | Together |
| `FIREWORKS_API_KEY` | Fireworks |
| `DEEPSEEK_API_KEY` | DeepSeek |
| `MISTRAL_API_KEY` | Mistral |
| `GOOGLE_API_KEY` (or `GEMINI_API_KEY`) | Google Gemini |
| `XAI_API_KEY` | xAI |
| `PERPLEXITY_API_KEY` | Perplexity |
| `COHERE_API_KEY` | Cohere |
| `OLLAMA_API_KEY`, `LMSTUDIO_API_KEY` | Optional — local servers need no key |
| `OPENAI_BASE_URL` | Override base URL for `openai` / `openai-compatible` / Azure only |
| `CLEANFRAME_DEBUG` | `1` prints a traceback on an internal error (same as `--debug`) |
| `NO_COLOR` | Disable ANSI colours in diff rendering |

Each provider reads **only its own** variable: a key set for one vendor is never sent
to another. In particular `OPENAI_API_KEY` is not a fallback for Groq, OpenRouter,
Together, DeepSeek, Gemini or Ollama — an unset key raises an `LLMError` naming the
variable that provider needs.

CleanFrame never stores or logs API keys.
