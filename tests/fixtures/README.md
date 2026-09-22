# Test fixtures

## `upstream_results.json.gz`

The complete 3,000-cell results file from
[`statmike/vertex-ai-mlops`](https://github.com/statmike/vertex-ai-mlops/tree/main/Applied%20ML/AI%20Agents/bigquery-context)
(`examples/results/results.json`, Apache-2.0), gzipped from 5.4 MB to ~125 KB.

It exists so `tests/test_metrics.py` can verify our metrics port against real
data offline. Our scorer must reproduce upstream's published headline table from
these cells exactly; if a formula drifts, that test fails rather than the
divergence surfacing later as an unexplained difference from their numbers.

Do not edit. Refresh only by re-downloading from upstream.
