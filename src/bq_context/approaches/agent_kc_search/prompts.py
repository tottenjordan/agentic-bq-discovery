"""Prompts for the Knowledge Catalog Search discovery agent.

The ``before_agent_callback`` (``discover_and_rerank``) runs the entire workflow
deterministically — semantic search, entry lookup, and the shared reranker — and
returns its own content, so these instructions are only a fallback if the callback
declines (no question in context). No LLM tool loop runs on the happy path.
"""

import datetime

from bq_context.runtime import default_config

today_date = datetime.date.today().strftime("%A, %B %d, %Y")
project_id = default_config().project

global_instructions = f"""\
You are a BigQuery table discovery agent that uses Knowledge Catalog semantic
search to find relevant tables. Today's date is {today_date}. Project: {project_id}.
"""

agent_instructions = """\
You discover relevant BigQuery tables using Knowledge Catalog semantic search,
entry lookup, and the shared reranker.

This approach runs entirely in a callback — no LLM reasoning is needed.
If you see this prompt, the callback did not return Content; respond with
the results from state if available.
"""
