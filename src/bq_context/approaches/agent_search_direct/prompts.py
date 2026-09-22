"""Prompts for the Search Direct discovery agent.

The ``before_agent_callback`` handles the entire workflow deterministically and
returns its own content, so these instructions are only a fallback if the
callback declines (no question in context). No LLM call is made on the happy path.
"""

import datetime

from bq_context.runtime import default_config, get_datasets

today_date = datetime.date.today().strftime("%A, %B %d, %Y")
project_id = default_config().project

global_instructions = f"""\
You are a BigQuery table discovery agent that uses Knowledge Catalog semantic
search results directly as the ranking. Today's date is {today_date}.
Project: {project_id}.
"""


def agent_instructions(_ctx=None) -> str:
    """ADK InstructionProvider: rebuilt per request to read the live tier scope.

    Upstream built this string at import time. That was latent rather than
    broken there, because this prompt is dead code on the happy path — the
    callback never returns None, so the LLM never sees it. It breaks here for a
    different reason: there is no ambient TierContext at import, so reading the
    scope eagerly would raise before any agent could be constructed.

    Either way the fix is the same one approaches 1 and 4 already use, and it is
    the correct shape regardless: a frozen dataset list would leak whichever
    tier happened to be active at import into every tier's run.
    """
    dataset_list = ", ".join(get_datasets())
    return f"""\
You discover relevant BigQuery tables using Knowledge Catalog semantic search,
using the search's own relevance order as the final ranking (no reranking step).

## Your scope
Search within these datasets: {dataset_list}

## Output format
Begin your response with: **[Approach 6: Search Direct]**
List the tables semantic search returned, in order.
"""
