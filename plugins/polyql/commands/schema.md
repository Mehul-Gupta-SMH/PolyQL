---
description: Show Poly-QL's schema for a table, or search the schema for a topic
argument-hint: <table | topic> [instance=<name>]
---

Use the Poly-QL MCP tools to describe: $ARGUMENTS

1. If the argument is a table name, call `get_table_schema`. If that table is unknown, call `search_schema` with the same text and offer the closest matches.
2. Otherwise call `search_schema` and summarise the relevant tables.
3. For a table, also call `get_lineage` and list the tables it connects to and the join keys. Call `get_derivatives` too if the table is built by a pipeline.

Use `instance=<name>` from the arguments if given, else the plugin's default instance ("${user_config.instance}"), else `default`. Present columns as a compact table (name, type, description). Do not write SQL unless asked.
