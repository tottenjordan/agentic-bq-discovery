"""Optional-dependency specs, in a module a KFP component body can import.

One constant, in its own module, for a specific reason. `finalize` installs the
figures extra at runtime, and a KFP component body is extracted into a standalone
file — so a module-level constant in `components.py` is simply not in scope there:

    NameError: name 'FIGURES_EXTRA' is not defined

Importing it back from `components.py` does not work either: that module resolves
`RUNNER_IMAGE = os.environ["BQ_CONTEXT_IMAGE"]` at import and raises `KeyError`
inside the container, where nothing sets it.

So the value lives somewhere with no imports and no environment reads, which a
component body can import safely. `config.py` would be the other candidate, but a
pip extra is packaging, not experiment configuration.
"""

from __future__ import annotations

__all__ = ["FIGURES_EXTRA"]

#: Installed by `finalize` at runtime, and only when figures are requested.
#: Must stay in step with the `figures` extra in pyproject.toml; a test asserts it.
FIGURES_EXTRA = "paperbanana>=0.1"
