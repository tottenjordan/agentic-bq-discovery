"""Does the configuration a run was submitted with actually reach the tasks?

`ExperimentConfig.from_env` reads seven variables and gives six of them a
default. Nothing set those six in the container: the Dockerfile does not, the
component bodies do not, and `dag.py` did not — so every pipeline run used the
compiled-in defaults no matter what the submitting shell said.

Two consequences, and the second is worse than the first.

**Local and pipeline runs could silently measure different things.** They agree
today only because every value in `.env` happens to equal the code default. That
is not a property anyone maintains; earlier in this project `.env` carried
`RESOURCE_PREFIX=bq_context` while the pipeline used `bigquery_context`, which
would have pointed provisioning and measurement at two different corpora.

**The KFP shard cache could not see the difference.** Shards are keyed on
`code_version` and `corpus_fingerprint`. Changing `AGENT_MODEL` changes neither,
so re-submitting at the same commit against the same corpus returns cells scored
with the *previous* model, green. This is the same failure family that
`code_version` and `corpus_fingerprint` were added to close; the model under test
was the remaining hole.

The fix is to forward the submitter's values as task environment variables, which
land in the executor's container spec and therefore in what Vertex hashes.
"""

from __future__ import annotations

import os

# Must precede the import: `@dsl.pipeline` executes the pipeline body at
# *decoration* time, so anything the body reads from the environment is resolved
# when `dag` is imported, not when `compile_pipeline` is called.
os.environ.setdefault("BQ_CONTEXT_IMAGE", "us-central1-docker.pkg.dev/p/r/runner:testsha")

import pytest

from bq_context.pipeline import components

#: Variables `from_env` reads that are deliberately *not* forwarded, and why.
#: Anything not listed here must be forwarded, or the guard below fails.
NOT_FORWARDED = {
    # Already set per-task by every component body, from the `project` pipeline
    # parameter, so forwarding it would give one value two sources of truth.
    "GOOGLE_CLOUD_PROJECT",
}


def _variables_that_shape_a_run() -> set[str]:
    """Every environment variable a task's behaviour depends on.

    Two sources, not one. `ExperimentConfig.from_env` is the obvious one, and
    `corpus/setup.py` is the one that was missed: it reads RESOURCE_PREFIX,
    CORPUS_PROFILE and BARE_TIER0 at module scope, and a guard that only parsed
    config.py would have waved the last two straight through — the identical bug
    this test exists to prevent, one file over.
    """
    import re
    from pathlib import Path

    root = Path(components.__file__).parent.parent
    config_body = (root / "config.py").read_text().split("def from_env")[1].split("\n    @")[0]
    # Module scope only: an os.getenv inside a function is a runtime lookup in
    # whatever process calls it, not something the task environment must carry.
    setup_body = "\n".join(
        line
        for line in (root / "corpus" / "setup.py").read_text().splitlines()
        if line and not line[0].isspace()
    )
    pattern = r'os\.(?:environ(?:\.get)?|getenv)\(\s*"([A-Z_0-9]+)"'
    return set(re.findall(pattern, config_body)) | set(re.findall(pattern, setup_body))


def test_the_forwarded_set_covers_everything_from_env_reads() -> None:
    """THE guard. Add a variable that changes what a run measures and forget to
    forward it, and the pipeline silently uses its default — which is exactly how
    all six came to be missing."""
    read = _variables_that_shape_a_run()

    assert read, "parsed no variables; the guard would be vacuous"
    assert "CORPUS_PROFILE" in read, "the setup.py half of the parse stopped working"
    missed = read - set(components.CONFIG_ENV_KEYS) - NOT_FORWARDED
    assert not missed, (
        f"{sorted(missed)} shape what the experiment measures but never reach the "
        "tasks, so the pipeline would use the compiled default and the shard cache "
        "could not tell the difference. Forward them, or justify them in NOT_FORWARDED."
    )


def test_the_derived_location_variables_are_never_forwarded() -> None:
    """These are *outputs* of `configure_adk_env`, not user configuration.

    `GOOGLE_CLOUD_LOCATION` is the model endpoint, and the Gemini models here
    resolve only at `global` — they 404 in `us-central1`. The image pins `global`
    deliberately. A developer `.env` carrying `us-central1` (this repo's does)
    forwarded into the tasks would move every Gemini call to an endpoint where it
    404s, turning a correct image into a broken run.
    """
    for derived in ("GOOGLE_CLOUD_LOCATION", "GOOGLE_GENAI_USE_VERTEXAI"):
        assert derived not in components.CONFIG_ENV_KEYS


@pytest.mark.parametrize(
    ("environ", "expected"),
    [
        ({}, {}),
        ({"TOP_K": "9"}, {"TOP_K": "9"}),
        # An empty or whitespace value is not a setting. Forwarding "" would
        # override the container's default with nothing and read as deliberate.
        ({"TOP_K": ""}, {}),
        ({"TOP_K": "   "}, {}),
        ({"NOT_OURS": "x"}, {}),
    ],
)
def test_only_values_the_submitter_actually_set_are_forwarded(
    environ: dict[str, str], expected: dict[str, str]
) -> None:
    assert components.config_env(environ) == expected


def test_a_full_environment_is_forwarded_whole() -> None:
    environ = dict.fromkeys(components.CONFIG_ENV_KEYS, "v")
    assert components.config_env(environ) == environ


def test_every_variable_that_changes_a_run_is_documented() -> None:
    """`.env.example` is the only place a newcomer learns these exist.

    It had already drifted: `SECRET_ID` was absent, so copying the example gave
    you a file that could not enable figures, and the failure was a silent skip
    rather than an error.
    """
    from pathlib import Path

    example = Path(".env.example").read_text()
    documented = {line.split("=", 1)[0] for line in example.splitlines() if "=" in line}
    expected = {*components.CONFIG_ENV_KEYS, "GOOGLE_CLOUD_PROJECT", "SECRET_ID"}
    assert not expected - documented, (
        f"undocumented in .env.example: {sorted(expected - documented)}"
    )
