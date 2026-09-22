"""Guard the walkthrough notebook against package drift.

A notebook is the one artifact that rots silently: nothing imports it, so a
rename in `bq_context` leaves it broken until someone opens it. These tests are
static — no kernel, no GCP — and only assert that what the notebook *says it
imports* still exists.
"""

from __future__ import annotations

import ast
import importlib
import json
import re
from pathlib import Path

import pytest

NOTEBOOK = Path(__file__).parent.parent / "notebooks" / "walkthrough.ipynb"


def _cells() -> list[tuple[int, str]]:
    nb = json.loads(NOTEBOOK.read_text())
    return [
        (i, "".join(c["source"])) for i, c in enumerate(nb["cells"]) if c["cell_type"] == "code"
    ]


def test_notebook_exists_and_is_valid_json() -> None:
    nb = json.loads(NOTEBOOK.read_text())
    assert nb["nbformat"] == 4
    assert any(c["cell_type"] == "code" for c in nb["cells"])


_CODE_CELLS = _cells()


@pytest.mark.parametrize(
    "source",
    [source for _, source in _CODE_CELLS],
    ids=[f"cell{index}" for index, _ in _CODE_CELLS],
)
def test_every_code_cell_parses(source: str) -> None:
    """Catch syntax errors without starting a kernel.

    Top-level `await` is legal in Jupyter but not in a plain module, so the
    cell that awaits the ADK executor is compiled in async context.
    """
    try:
        ast.parse(source)
    except SyntaxError:
        ast.parse("async def _cell():\n" + "\n".join(f"    {line}" for line in source.splitlines()))


@pytest.mark.parametrize(
    ("module", "name"),
    sorted(
        {
            (m, n.strip())
            for _, src in _cells()
            for m, names in re.findall(r"from (bq_context[\w.]*) import ([\w, ]+)", src)
            for n in names.split(",")
        }
    ),
)
def test_imported_symbols_still_exist(module: str, name: str) -> None:
    """The notebook's `from bq_context... import X` must still resolve.

    This is the test that fires when someone renames a helper: the notebook is
    the last place a rename gets propagated, because nothing else imports it.
    """
    assert hasattr(importlib.import_module(module), name), f"{module}.{name} is gone"
