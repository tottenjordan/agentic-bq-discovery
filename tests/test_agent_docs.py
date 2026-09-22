"""`CLAUDE.md` and `GEMINI.md` must not drift apart.

Two agent instruction files is two places for the same rule to be written
differently, and the failure is silent: whichever agent reads the weaker copy
works to the weaker standard. That already happened in a narrower form — the
attribution rule was summarised here as "never add Co-Authored-By trailers"
while `CODE_STANDARDS.md` said "no tool attribution anywhere", and the narrow
reading produced a `Generated with ...` line in PR #1.

So these files are kept identical in substance. The only licensed differences
are the H1 and the sentence in which each names the other.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
AGENT_DOCS = ("CLAUDE.md", "GEMINI.md")
STANDARDS = "CODE_STANDARDS.md"


def _body(name: str) -> str:
    """The file with its licensed differences normalised away."""
    text = (ROOT / name).read_text()
    text = text.split("\n", 1)[1]  # drop the H1, which is the filename
    # Each file names the other; fold both to a placeholder.
    return re.sub(r"\b(CLAUDE|GEMINI)\.md\b", "AGENT.md", text).strip()


@pytest.mark.parametrize("name", AGENT_DOCS)
def test_the_file_exists(name: str) -> None:
    assert (ROOT / name).is_file()


def test_the_two_agent_docs_say_the_same_thing() -> None:
    """Substance must match; only the H1 and the cross-reference may differ."""
    claude, gemini = (_body(n) for n in AGENT_DOCS)
    assert claude == gemini, "CLAUDE.md and GEMINI.md have drifted; edit both, or neither"


@pytest.mark.parametrize("name", AGENT_DOCS)
def test_it_points_at_the_standards_with_a_working_link(name: str) -> None:
    """A hyperlink, per the project convention — and one that actually resolves."""
    text = (ROOT / name).read_text()
    assert f"[{STANDARDS}](./{STANDARDS})" in text, "must link, not just mention"
    assert (ROOT / STANDARDS).is_file()


@pytest.mark.parametrize("name", AGENT_DOCS)
def test_every_relative_link_resolves(name: str) -> None:
    """Catches a pointer that survives a rename as a dead link."""
    text = (ROOT / name).read_text()
    targets = re.findall(r"\]\(\./([^)]+)\)", text)
    assert targets, "expected at least one relative link"
    missing = [t for t in targets if not (ROOT / t).exists()]
    assert not missing, f"{name} links to nonexistent {missing}"


@pytest.mark.parametrize("name", AGENT_DOCS)
def test_the_attribution_rule_is_stated_broadly(name: str) -> None:
    """Regression: a summary narrower than the standard is how PR #1 went wrong.

    Naming only `Co-Authored-By` invites the reading that other footers are
    permitted. The rule is that there is no attribution of any kind.
    """
    text = (ROOT / name).read_text()
    assert "No tool attribution anywhere" in text
    assert "Generated with" in text, "the other common form must be named too"


@pytest.mark.parametrize("name", AGENT_DOCS)
def test_it_directs_durable_findings_to_the_notes(name: str) -> None:
    text = (ROOT / name).read_text()
    assert "docs/notes/" in text


# ---------------------------------------------------------------------------
# The claims the docs make must stay true
# ---------------------------------------------------------------------------
# A stale agent doc is worse than none: it is confidently wrong, and an agent
# has no way to tell. These pin the specific, checkable assertions.
@pytest.mark.parametrize("name", AGENT_DOCS)
def test_the_make_targets_it_names_exist(name: str) -> None:
    makefile = (ROOT / "Makefile").read_text()
    targets = set(re.findall(r"^([a-z][a-z-]*):", makefile, re.MULTILINE))
    cited = set(re.findall(r"\bmake ([a-z][a-z-]*)", (ROOT / name).read_text()))
    assert cited, "expected the doc to cite at least one make target"
    assert cited <= targets, f"{name} cites missing target(s): {sorted(cited - targets)}"


@pytest.mark.parametrize("name", AGENT_DOCS)
def test_the_subcommand_count_is_current(name: str) -> None:
    """If this fails, a subcommand was added — update the docs, do not delete this."""
    import typer

    from bq_context.cli import app

    actual = len(typer.main.get_command(app).commands)  # type: ignore[attr-defined]
    claimed = re.search(r"(\d+) subcommands", (ROOT / name).read_text())
    assert claimed, "the doc should state how many subcommands there are"
    assert int(claimed.group(1)) == actual


@pytest.mark.parametrize("name", AGENT_DOCS)
def test_the_files_it_points_at_as_examples_exist(name: str) -> None:
    """The doc names specific modules as patterns to follow or avoid."""
    text = (ROOT / name).read_text()
    for path in re.findall(r"`((?:src/|tests/|docs/)[\w/.]+\.(?:py|md))`", text):
        assert (ROOT / path).exists(), f"{name} names nonexistent {path}"
    for module in re.findall(r"`(pipeline/[\w.]+\.py|corpus/[\w.]+\.py|config\.py)`", text):
        assert (ROOT / "src" / "bq_context" / module).exists(), f"{name} names missing {module}"


def test_the_gemini_endpoint_claim_is_true() -> None:
    """The docs say Gemini lives at `global`; Locations is the source of truth."""
    from bq_context.config import Locations

    assert Locations().gemini == "global"
    for name in AGENT_DOCS:
        assert "`global`" in (ROOT / name).read_text()
