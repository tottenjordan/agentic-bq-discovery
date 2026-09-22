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
