---
name: polyql-sql
description: Answer data questions with SQL using the PolyQL MCP server (schema search, join paths, business definitions, knowledge notes, validation, optional read-only execution). Use when the user asks a question about data in a database PolyQL has metadata for, or asks for a SQL query against it.
---

# Writing SQL with PolyQL

PolyQL's MCP tools (`mcp__polyql__*`) know the schema, the join graph, the
business glossary and the team's knowledge notes. Use them instead of guessing
table or column names.

## Workflow

1. **Pick the instance.** If the user didn't name one, call `list_instances`.
   Use `default` to search all instances.
2. **Get context.** Call `search_schema(question, instance)` and read every section:
   - **Business Definitions**: use these formulas exactly. Don't derive your own.
   - **Knowledge Notes**: team guidance (filters, history rules, which table
     to join through). Follow it where it applies.
   - **Tables**: `direct` tables matched the question; `intermediate` ones are
     needed only to join.
   - **Join Paths**: the join conditions to use.
3. **Fill gaps.** If a table you need is missing, call `get_table_schema`, or
   `list_tables` / `search_schema` with other wording. For several tables, call
   `find_join_path(tables)`:
   - Each hop shows its cardinality.
   - Where it lists alternatives (e.g. `order_date_id` vs `ship_date_id`),
     pick the one the question means.
4. **Write the SQL.**
   - Use only tables and columns PolyQL returned.
   - Qualify columns (`o.total`) so validation can check them.
   - Watch `n:m` and `1:n` joins before aggregating: they multiply rows.
     Aggregate first or use `COUNT(DISTINCT …)`.
5. **Validate.** Call `validate_sql(sql, instance)` and fix every error
   (unknown tables or columns, non-read-only statements). Re-validate.
6. **Run (optional).** If `execute_sql` is configured, run the query and sanity
   check the result: row count, nulls, totals that look plausible. If it
   errors, fix and re-validate. Pass the user's question as `question` so the
   run is recorded with it.
7. **Answer** with the final SQL and one line naming the tables, joins and
   business definitions used. Mention any assumption you made.

## Don'ts

- Don't invent tables, columns or metric formulas. Ask PolyQL, or ask the user.
- Don't run anything except a single `SELECT`; `execute_sql` rejects the rest.
- Don't call `generate_sql` unless the user asks for PolyQL's own generation.
  It calls another LLM and costs tokens.
