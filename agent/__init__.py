"""AI Trader agent side.

This package is the trading agent that connects to the independent simulator through
the fixed JSON contract. It never imports the simulator (`sim`) or its server; the
runner is the only module that wires the two together.

Pieces:
  config      pinned model/query settings and released-feed table descriptions
  contract    build and validate the fixed JSON action envelope (no sim import)
  model       Nemotron OpenAI-compatible chat adapter
  query_tool  the `zetaris_query` tool (SQLite / Zetaris JDBC backends)
  memory      append-only, run-scoped, as_of-gated experience log
  policy      the Nemotron trading policy loop
"""
