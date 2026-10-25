---
description: Explain how to join two or more tables, with cardinality and alternatives
argument-hint: <table1> <table2> [...] [instance=<name>]
---

Use the Poly-QL `find_join_path` tool for these tables: $ARGUMENTS

Report each join hop with its condition and cardinality. Where Poly-QL lists alternative ways to join (for example `order_date_id` versus `ship_date_id`), show all of them and say which one suits which kind of question. Warn about `1:n` and `n:m` hops that multiply rows before an aggregate, and say whether to aggregate first or use `COUNT(DISTINCT ...)`. If tables cannot be connected, say so and suggest an intermediate table from `search_schema` or `list_tables`.

Use `instance=<name>` from the arguments if given, else the plugin's default instance ("${user_config.instance}"), else `default`.
