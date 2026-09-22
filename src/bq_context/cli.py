"""The single entrypoint every other surface wraps.

Each KFP pipeline component is a thin wrapper over one of these subcommands, so
the pipeline and a local shell run exactly the same code path. That is
deliberate: it keeps the debug loop in seconds rather than container builds, and
it means a pipeline failure can always be reproduced locally with one command.
"""

from __future__ import annotations

from importlib.metadata import version as pkg_version

import typer

app = typer.Typer(
    name="bq-context",
    help="Evaluate six BigQuery table-discovery approaches.",
    no_args_is_help=True,
)


@app.callback()
def _root() -> None:
    """Keep this a multi-command app.

    Typer collapses an app with exactly one command into a bare single-command
    CLI, which would make `bq-context version` an "unexpected extra argument".
    An empty callback pins the subcommand form regardless of how many commands
    happen to be registered.
    """


@app.command()
def version() -> None:
    """Print the installed package version."""
    typer.echo(pkg_version("bq-context"))


if __name__ == "__main__":
    app()
