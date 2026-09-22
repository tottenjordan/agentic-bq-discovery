"""Callback: read cached Knowledge Context + rerank.

Runs the entire Approach 3 workflow deterministically in
``before_agent_callback`` so no LLM agent calls are needed. Its distinctive step
is sourcing all metadata from the pre-loaded context cache (zero per-query
discovery API calls); question extraction, reranking, and emit are shared helpers
in ``discovery_common``.
"""

from google.adk.agents.callback_context import CallbackContext

from bq_context.discovery_common import (
    emit,
    get_question,
    plain_content,
    rerank_and_store,
    store_empty,
)
from bq_context.runtime import current_tier

LABEL = "Approach 3: Knowledge Catalog Context"
METHOD = "kc_context"


async def discover_and_rerank(callback_context: CallbackContext):
    """Pre-fetch context + rerank in a single callback — no LLM needed."""
    question = get_question(callback_context)
    if not question:
        return None  # No question — fall back to LLM + tools

    context = current_tier().cache.all_detailed()
    callback_context.state["nominated_tables_kc_context"] = current_tier().cache.table_ids()

    if not context:
        store_empty(callback_context, METHOD, question, "No knowledge context available.")
        return plain_content(LABEL, "No knowledge context available for tables in scope.")

    result = await rerank_and_store(callback_context, question, context, METHOD)
    return emit(LABEL, result)
