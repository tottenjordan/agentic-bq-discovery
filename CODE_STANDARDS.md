# Code Standards

Standards that apply to all code and environment changes in this project.
Read this before writing code, adding dependencies, or changing tooling.

For the full rationale and command reference behind the Python tooling choices,
see the `modern-python` skill (`~/.claude/skills/modern-python/SKILL.md`) and
its `references/` directory.

---

## Git and version control

- **Never add `Co-Authored-By` trailers** to commit messages or pull requests.
  This applies to every commit, regardless of who or what authored the change.
- Commit messages describe the change and why it was made. No tool attribution
  footers of any kind.

## Python: package management

**Use `uv` for everything.** Never invoke bare `pip`, `python`, or
`virtualenv`, and never activate a venv manually.

| Do | Don't |
|---|---|
| `uv add <pkg>` | `pip install <pkg>`, `uv pip install <pkg>` |
| `uv add --group dev <pkg>` | editing `pyproject.toml` deps by hand |
| `uv remove <pkg>` | `pip uninstall <pkg>` |
| `uv sync --all-groups` | `pip install -r requirements.txt` |
| `uv run <cmd>` | `source .venv/bin/activate && <cmd>` |
| `uv run python script.py` | `python script.py` |
| `uv run --with <pkg> <cmd>` | installing a one-off dep into the project |

Additional rules:

- Dev/test/docs dependencies go in `[dependency-groups]` (PEP 735), **not**
  `[project.optional-dependencies]`.
- Commit `uv.lock` to version control.
- No `requirements.txt`. Standalone scripts use PEP 723 inline metadata;
  projects use `pyproject.toml`.
- Use the `src/` layout and `requires-python = ">=3.11"`.

> **Deviation, 2026-09-22:** this repo pins `requires-python = ">=3.13"`. The
> vendored upstream code (see `NOTICE`) declares `>=3.13.3`, and the container
> base is `python:3.13-slim` so prod matches dev. uv provisions 3.13
> automatically, so this costs nothing.

## Python: lint and format

**Use `ruff` for both linting and formatting.** Never add or run `black`,
`flake8`, `isort`, `pyupgrade`, or `pydocstyle` — ruff replaces all of them.

```bash
uv run ruff check .        # lint
uv run ruff check --fix .  # lint with autofix
uv run ruff format .       # format
uv run ruff format --check # verify formatting in CI
```

Configure ruff with `select = ["ALL"]` and an explicit, justified `ignore`
list rather than an opt-in allowlist.

## Testing and type checking

- **Tests: `pytest`.** Not `unittest`.
- **Type checking: `ty`** (Astral). Never add `mypy` or `pyright`.

```bash
uv run pytest
uv run ty check src/
```

Notes:

- Enforce a coverage minimum (80%+) via `[tool.pytest.ini_options]`.
- `ty` config lives under `[tool.ty.environment]`, `[tool.ty.rules]`, and
  `[tool.ty.terminal]`. `python-version` belongs in `[tool.ty.environment]`,
  **not** a bare `[tool.ty]` table.

## Environment changes

Any change to tooling, dependencies, or CI must stay consistent with the
choices above. If a change would introduce a tool from a "Don't" column,
raise it before making the change rather than adding it silently.
