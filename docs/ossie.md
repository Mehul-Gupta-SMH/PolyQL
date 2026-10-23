# Apache Ossie (OSI) import / export

[Apache Ossie](https://ossie.apache.org/) (incubating, formerly the Open Semantic Interchange) is a vendor-neutral YAML/JSON format for semantic models. Poly-QL can import Ossie models and export its own metadata as Ossie, so it can share definitions with dbt, Snowflake semantic views and other tools that speak the format.

## Endpoints

Both endpoints require login.

```bash
# Import (YAML or JSON in `content`)
curl -X POST http://localhost:8000/api/semantic/import/ossie \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d "$(jq -n --rawfile c tpcds_semantic_model.yaml '{content: $c, instance_name: "tpcds", db_type: "snowflake"}')"
# → {"models": ["tpcds_retail_model"], "tables": 5, "relationships": 4, "metrics": 8, "notes": 1, "skipped": [...]}

# Export
curl -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8000/api/semantic/export/ossie?instance_name=tpcds" -o tpcds.ossie.yaml
```

## Mapping

| Ossie | Poly-QL |
|---|---|
| `dataset` | A table. The name comes from `source` when it's a plain identifier (`tpcds.public.store_sales`), otherwise from the dataset `name` (for example when `source` is a query; this is reported in `skipped`) |
| `fields` | Columns, with `datatype` as the type and `description` (or `label`). Synonyms from `ai_context` are added to the description. A field whose expression isn't just its own name keeps the expression as the column's logic, which Poly-QL shows to the LLM in the schema |
| `primary_key` / `unique_keys` | A single-column primary key becomes `PRIMARY KEY`, and a single-column unique key becomes `UNIQUE`. These drive join cardinality. Parts of a composite key are marked `COMPOSITE KEY PART`, because no one of them is unique alone |
| `relationship` | An FK edge in the join graph, `from.c1 = to.k1, from.c2 = to.k2`. `from` is the many side |
| `metric` | A glossary term with the formula, the tables it references, and synonyms from `ai_context`. Existing terms with the same name are updated |
| Model `ai_context` | An OKF knowledge note (`ossie/<model>`, type `note`) attached to every table of the model, so it reaches table search and the prompt's Knowledge Notes. It is stored as OKF bundle `ossie:<model>`, listed in the Knowledge tab; re-importing replaces it. Not written back on export |

Re-importing replaces the same tables' metadata for that instance. The current draft (`version: 0.2.0.dev0`, one model per document) and the 0.1.x `semantic_model:` list wrapper are both accepted.

## Export details

- Writes the current draft (`0.2.0.dev0`). Dataset names are the table names with non-word characters replaced by `_`, and `source` is the table name.
- SQL types are mapped to Ossie datatypes (`varchar` → `String`, `numeric` → `Decimal`, `timestamptz` → `DateTimeTz`, …). Unknown types become `Opaque`, and blank types are left out.
- Relationships are oriented so `from` is the many side, using the key columns.
- A many-to-many join can't be an Ossie relationship, since `to` must be the "one" side. These go into a `custom_extensions` entry (`vendor_name: polyql`) instead of being dropped.
- Glossary terms with a formula become metrics.

The import and export were checked against the official `examples/tpcds_semantic_model.yaml` from the Ossie repository: 5 tables, 4 relationships, 8 metrics. The exported documents validate against `core-spec/ossie-schema.json`, and a round trip (import → export → import) is lossless.
