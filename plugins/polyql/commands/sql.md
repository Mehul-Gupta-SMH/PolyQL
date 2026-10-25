---
description: Answer a data question with SQL using Poly-QL schema context
argument-hint: <question> [instance=<name>]
---

Use the `polyql-sql` skill and the Poly-QL MCP tools (`search_schema`, `get_table_schema`, `find_join_path`, `get_business_terms`, `validate_sql`, and `execute_sql` if it is available) to answer this question with SQL:

$ARGUMENTS

If the arguments contain `instance=<name>`, use that instance. Otherwise use the default instance configured for the plugin ("${user_config.instance}"), or `list_instances` and then `default` if that is empty.

Follow the skill's workflow: get context, fill gaps, write the SQL with only tables and columns Poly-QL returned, validate it, and answer with the final SQL plus one line naming the tables, joins and business definitions used.
