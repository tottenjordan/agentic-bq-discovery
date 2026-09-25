"""`scripts/identity-watch.sh` must keep calling the CLI it wraps.

The script deploys a Cloud Run job that runs `bq-context preflight` daily and
reads the result back out of Cloud Logging. Both ends are strings in a shell
file, so a renamed flag or a reworded warning would break the job silently: a
job that exits 2 on a bad option, or a log query that matches nothing, looks
the same as a week with nothing to report.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import typer

from bq_context.cli import _probe_summary, app, assess_search_identity
from bq_context.pipeline.components import CONFIG_ENV_KEYS

ROOT = Path(__file__).parent.parent
SCRIPT = ROOT / "scripts" / "identity-watch.sh"
TEXT = SCRIPT.read_text()


def _job_args() -> list[str]:
    (line,) = re.findall(r'^JOB_ARGS="([^"]+)"$', TEXT, re.MULTILINE)
    return line.split(",")


def test_the_script_is_executable_and_parses() -> None:
    assert SCRIPT.stat().st_mode & 0o111
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)  # noqa: S603, S607


def test_the_job_runs_a_real_subcommand_with_real_options() -> None:
    subcommand, *rest = _job_args()
    command = typer.main.get_command(app).commands[subcommand]  # type: ignore[attr-defined]
    options = {opt for param in command.params for opt in param.opts}
    flags = [a for a in rest if a.startswith("--")]
    assert flags, "expected the job to pass options"
    assert set(flags) <= options, f"not preflight options: {set(flags) - options}"


def test_the_job_searches_as_the_pipeline_sa() -> None:
    """Without --impersonate there is nothing to compare, and preflight
    reports no gap every day."""
    args = _job_args()
    assert args[args.index("--impersonate") + 1] == "${PIPELINE_SA}"


def test_the_default_question_set_ships_in_the_image() -> None:
    """The Dockerfile copies experiments/, so a repo-relative path resolves."""
    (default,) = re.findall(r'QUESTIONS="\$\{QUESTIONS:-([^}]+)\}"', TEXT)
    assert (ROOT / default).is_file()
    assert "COPY experiments/" in (ROOT / "Dockerfile").read_text()


def test_the_log_query_matches_what_preflight_prints() -> None:
    needles = re.findall(r'textPayload:\\"([^\\]+)\\"', TEXT)
    assert len(needles) == 2, needles
    printed = _probe_summary({"tier0/q": ("t",)}, [0]) + "\n".join(
        assess_search_identity({"tier0/q": ("a",)}, {"tier0/q": ("b",)}, "sa@p.iam")
    )
    missing = [n for n in needles if n not in printed]
    assert not missing, f"the log query looks for text preflight never prints: {missing}"


def test_the_job_gets_the_config_the_pipeline_forwards() -> None:
    (keys,) = re.findall(r"for key in ([A-Z_ ]+); do", TEXT)
    assert set(keys.split()) == set(CONFIG_ENV_KEYS)
