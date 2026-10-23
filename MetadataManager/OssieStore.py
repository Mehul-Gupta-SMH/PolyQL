"""
MetadataManager/OssieStore.py — Apache Ossie (formerly OSI) import / export

Apache Ossie (incubating) is a vendor-neutral YAML/JSON spec for semantic
models: datasets, fields, relationships and metrics. This module maps it to
PolyQL's metadata:

  Ossie                         PolyQL
  ----------------------------  -------------------------------------------------
  dataset (source, fields,      table in tableDesc / tableColMetadata; PRIMARY KEY
    primary_key, unique_keys)   and UNIQUE constraints feed join cardinality
  relationship (from = many,    fk edge in the Kuzu join graph,
    to = one, from/to_columns)  join keys "from.c1 = to.k1, from.c2 = to.k2"
  metric (expression)           business glossary term (formula, table deps)
  ai_context (synonyms,         folded into descriptions / glossary synonyms;
    instructions)               a model's own ai_context becomes an OKF note

Both the current draft (one model per document, `datasets` at the root) and
the 0.1.x `semantic_model:` list wrapper are accepted on import. Export writes
the current draft (version 0.2.0.dev0). Joins Ossie can't express — a
relationship's `to` side must be the "one" side, so many-to-many joins — are
exported in a `custom_extensions` entry (vendor_name "polyql") rather than
dropped.

Parsing and mapping are pure functions; import_into_polyql() / build_document()
do the storage side.
"""

import json
import logging
import re

import yaml

logger = logging.getLogger(__name__)

EXPORT_VERSION = "0.2.0.dev0"
EXPRESSION_LOGIC = "expression"   # tableColMetadata.type_of_logic for Ossie field expressions
_PREFERRED_DIALECT = "ANSI_SQL"
_PLAIN_IDENT = re.compile(r'^[\w$."`\[\]]+$')


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def load_models(text: str) -> list:
    """
    Parse an Ossie YAML or JSON document into a list of semantic models.

    Raises ValueError when the document isn't an Ossie model.
    """
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ValueError(f"Not valid YAML/JSON: {exc}") from exc
    if not isinstance(doc, dict):
        raise ValueError(
            "An Ossie document must be a mapping with `datasets` (or `semantic_model`)."
        )

    models = doc["semantic_model"] if isinstance(doc.get("semantic_model"), list) else [doc]
    for i, model in enumerate(models):
        datasets = model.get("datasets") if isinstance(model, dict) else None
        if not isinstance(datasets, list) or not datasets:
            raise ValueError(f"Semantic model #{i + 1} has no `datasets` list.")
    return models


def expression_text(expression) -> str:
    """The SQL of an Ossie expression object (ANSI_SQL dialect preferred), or a bare string."""
    if isinstance(expression, str):
        return expression.strip()
    if isinstance(expression, dict):
        dialects = expression.get("dialects") or []
        for entry in dialects:
            dialect = str(entry.get("dialect", "")).upper() if isinstance(entry, dict) else ""
            if dialect == _PREFERRED_DIALECT:
                return str(entry.get("expression", "")).strip()
        for entry in dialects:
            if isinstance(entry, dict) and entry.get("expression"):
                return str(entry["expression"]).strip()
        if expression.get("expression"):
            return str(expression["expression"]).strip()
    return ""


def ai_context(value) -> tuple:
    """(instructions, synonyms) from an ai_context string or object."""
    if isinstance(value, str):
        return value.strip(), []
    if isinstance(value, dict):
        synonyms = value.get("synonyms") or []
        if not isinstance(synonyms, list):
            synonyms = [synonyms]
        return str(value.get("instructions") or "").strip(), [str(s) for s in synonyms]
    return "", []


def _describe(description: str, instructions: str = "", synonyms: list = ()) -> str:
    parts = [description.strip()] if description and description.strip() else []
    if instructions:
        parts.append(instructions)
    if synonyms:
        parts.append(f"Synonyms: {', '.join(synonyms)}.")
    return " ".join(parts)


def _constraint(col: str, primary: list, unique_single: set) -> str:
    if col in primary:
        # Only a single-column key makes the column itself unique
        return "PRIMARY KEY" if len(primary) == 1 else "COMPOSITE KEY PART"
    return "UNIQUE" if col in unique_single else ""


def _expression(sql: str) -> dict:
    return {"dialects": [{"dialect": _PREFERRED_DIALECT, "expression": sql}]}


def _table_name(dataset: dict) -> str:
    """Physical table name: `source` when it's a plain identifier, else the dataset name."""
    source = str(dataset.get("source") or "").strip()
    if source and _PLAIN_IDENT.match(source):
        return re.sub(r'[`"\[\]]', "", source)
    return str(dataset["name"])


# ---------------------------------------------------------------------------
# Ossie → PolyQL
# ---------------------------------------------------------------------------

def model_to_polyql(model: dict) -> dict:
    """
    Map one semantic model to PolyQL records.

    Returns {name, tables, relationships, terms, notes, skipped}:
      tables         [{name, desc, columns: [{name, type, constraints, desc}]}]
      relationships  [{source, target, join_keys, kind: "fk"}]
      terms          glossary term dicts (term_name, definition, formula, ...)
      notes          OKF concept dicts (model-level ai_context → one `note` concept
                     attached to the model's tables)
      skipped        human-readable notes for anything not imported
    """
    skipped = []
    tables, by_dataset = [], {}

    for dataset in model["datasets"]:
        if not isinstance(dataset, dict) or not dataset.get("name"):
            skipped.append("dataset without a name")
            continue
        table = _table_name(dataset)
        by_dataset[str(dataset["name"])] = table
        if str(dataset.get("source") or "").strip() and table == str(dataset["name"]):
            skipped.append(
                f"dataset {dataset['name']}: source is a query, imported under its dataset name"
            )

        primary = [str(c) for c in (dataset.get("primary_key") or [])]
        unique_single = {
            str(k[0]) for k in (dataset.get("unique_keys") or [])
            if isinstance(k, list) and len(k) == 1
        }

        columns, seen = [], set()
        for field in dataset.get("fields") or []:
            if not isinstance(field, dict) or not field.get("name"):
                continue
            name = str(field["name"])
            expression = expression_text(field.get("expression"))
            instructions, synonyms = ai_context(field.get("ai_context"))
            trivial = not expression or expression.strip('`"[] ').lower() == name.lower()
            column = {
                "name": name,
                "type": str(field.get("datatype") or ""),
                "constraints": _constraint(name, primary, unique_single),
                "desc": _describe(str(field.get("description") or field.get("label") or ""),
                                  instructions, synonyms),
            }
            if not trivial:
                # Stored as the column's logic so the schema shown to the LLM includes it
                column.update(source_expr=expression, logic_type=EXPRESSION_LOGIC)
            columns.append(column)
            seen.add(name)
        for key_col in primary:
            if key_col not in seen:
                columns.append({"name": key_col, "type": "", "desc": "",
                                "constraints": _constraint(key_col, primary, unique_single)})

        instructions, synonyms = ai_context(dataset.get("ai_context"))
        tables.append({
            "name": table,
            "desc": _describe(str(dataset.get("description") or ""), instructions, synonyms),
            "columns": columns,
        })

    relationships = []
    for rel in model.get("relationships") or []:
        if not isinstance(rel, dict):
            continue
        src, tgt = by_dataset.get(str(rel.get("from"))), by_dataset.get(str(rel.get("to")))
        from_cols, to_cols = rel.get("from_columns") or [], rel.get("to_columns") or []
        if not src or not tgt:
            skipped.append(f"relationship {rel.get('name')}: unknown dataset "
                           f"{rel.get('from')!r} or {rel.get('to')!r}")
            continue
        if not from_cols or len(from_cols) != len(to_cols):
            skipped.append(
                f"relationship {rel.get('name')}: from_columns / to_columns missing or unequal"
            )
            continue
        keys = ", ".join(f"{src}.{f} = {tgt}.{t}" for f, t in zip(from_cols, to_cols, strict=True))
        relationships.append({"source": src, "target": tgt, "join_keys": keys, "kind": "fk"})

    names = sorted(by_dataset.items(), key=lambda kv: -len(kv[0]))
    terms = []
    for metric in model.get("metrics") or []:
        if not isinstance(metric, dict) or not metric.get("name"):
            continue
        formula = expression_text(metric.get("expression"))
        if not formula:
            skipped.append(f"metric {metric['name']}: no expression")
            continue
        instructions, synonyms = ai_context(metric.get("ai_context"))
        deps = [table for dataset, table in names
                if re.search(rf"(?<![\w.]){re.escape(dataset)}\.", formula)
                or re.search(rf"(?<![\w]){re.escape(table)}\.", formula)]
        terms.append({
            "term_name": str(metric["name"]),
            "full_name": "",
            "definition": _describe(str(metric.get("description") or ""), instructions),
            "formula": formula,
            "formula_type": "sql",
            "table_deps": list(dict.fromkeys(deps)),
            "synonyms": synonyms,
            "domain": str(model.get("name") or ""),
        })

    model_name = str(model.get("name") or "")
    notes = []
    instructions, synonyms = ai_context(model.get("ai_context"))
    if instructions or synonyms:
        body = instructions
        if synonyms:
            body = (body + "\n\n" if body else "") + f"Synonyms: {', '.join(synonyms)}."
        notes.append({
            "concept_id": f"ossie/{model_name or 'model'}",
            "type": "note",
            "title": f"{model_name or 'Model'} — guidance",
            "description": "",
            "resource": "",
            "tags": ["ossie"],
            "status": "stable",
            "table_name": "",
            "body": body,
            "links": [],
            # Honoured by OKFStore.concept_table_candidates: the note applies to every table
            "tables": [t["name"] for t in tables],
        })

    return {"name": model_name, "tables": tables, "relationships": relationships,
            "terms": terms, "notes": notes, "skipped": skipped}


def import_into_polyql(text: str, instance_name: str = "default", db_type: str = "generic",
                       vdb=None, glossary_index=None, okf_index=None) -> dict:
    """
    Import an Ossie document into PolyQL's stores.

    Tables are re-imported in place (existing rows for the same table and
    instance are replaced); relationships are added as fk join edges; metrics
    are upserted into the glossary by name and, when *glossary_index*
    (term → None, e.g. a closure over index_term) is given, embedded for search.

    A model's top-level ai_context becomes an OKF knowledge note. When
    *okf_index* (callable(notes, bundle_name), e.g. a closure over
    OKFStore.index_concepts) is given it is called with bundle `ossie:<model>`
    (re-importing replaces it); otherwise the note is reported in `skipped`.

    Returns {models, tables, relationships, metrics, notes, skipped}.
    """
    from backend.ingestion import store_table
    from MetadataManager import GlossaryStore
    from MetadataManager.MetadataStore.ManageRelations import Relations

    summary = {"models": [], "tables": 0, "relationships": 0, "metrics": 0, "notes": 0,
               "skipped": []}
    for model in load_models(text):
        mapped = model_to_polyql(model)
        summary["models"].append(mapped["name"])
        summary["skipped"] += mapped["skipped"]

        for table in mapped["tables"]:
            store_table(table["name"], table["desc"], table["columns"], [],
                        instance_name=instance_name, db_type=db_type, vdb=vdb, replace=True)
        summary["tables"] += len(mapped["tables"])

        if mapped["relationships"]:
            Relations(instance_name=instance_name).addRelation([
                [r["source"].lower(), r["target"].lower(), r["join_keys"], "fk"]
                for r in mapped["relationships"]
            ])
        summary["relationships"] += len(mapped["relationships"])

        for term in mapped["terms"]:
            term["instance_name"] = instance_name
            existing = GlossaryStore.get_term_by_name(term["term_name"], instance_name)
            if existing:
                updates = {k: v for k, v in term.items() if k != "term_name"}
                GlossaryStore.update_term(existing["term_id"], updates)
                term_id = existing["term_id"]
            else:
                term_id = GlossaryStore.add_term(term)
            if glossary_index is not None:
                glossary_index(GlossaryStore.get_term(term_id))
        summary["metrics"] += len(mapped["terms"])

        if okf_index is not None:
            # Called even without notes, so a re-import drops a removed ai_context
            okf_index(mapped["notes"], f"ossie:{mapped['name']}")
            summary["notes"] += len(mapped["notes"])
        elif mapped["notes"]:
            summary["skipped"].append(
                f"model {mapped['name']}: ai_context not imported (no OKF index available)"
            )

    logger.info("Ossie import into %r: %d tables, %d relationships, %d metrics", instance_name,
                summary["tables"], summary["relationships"], summary["metrics"])
    return summary


# ---------------------------------------------------------------------------
# PolyQL → Ossie
# ---------------------------------------------------------------------------

_DATATYPES = [
    (r"timestamp\s*with\s*time\s*zone|timestamptz", "DateTimeTz"),
    (r"timestamp|datetime", "DateTime"),
    (r"^date$|^date\b", "Date"),
    (r"^time\b", "Time"),
    (r"bool", "Boolean"),
    (r"int", "Integer"),
    (r"decimal|numeric|number|money", "Decimal"),
    (r"float|double|real", "Float"),
    (r"char|text|string|uuid|enum", "String"),
]


def ossie_datatype(sql_type: str) -> str:
    """Map a SQL type name to an Ossie datatype enum value ("" when blank, Opaque when unknown)."""
    value = (sql_type or "").strip().lower()
    if not value:
        return ""
    if value in ("string", "integer", "decimal", "float", "boolean", "date", "time",
                 "datetime", "datetimetz", "opaque"):
        return {"datetimetz": "DateTimeTz", "datetime": "DateTime"}.get(value, value.capitalize())
    for pattern, name in _DATATYPES:
        if re.search(pattern, value):
            return name
    return "Opaque"


def _dataset_name(table: str) -> str:
    return re.sub(r"[^\w]", "_", table)


def _pairs(join_keys: str, a: str, b: str) -> list | None:
    """[(col_on_a, col_on_b), ...] from "a.x = b.y, ..."; None when it isn't a plain equi-join."""
    from MetadataManager.MetadataStore.relationdb.joinpath import _side

    pairs = []
    for expr in (p for p in join_keys.split(",") if p.strip()):
        if "=" not in expr:
            return None
        (lt, lc), (rt, rc) = (_side(x) for x in expr.split("=", 1))
        if (lt, rt) == (a, b):
            pairs.append((lc, rc))
        elif (lt, rt) == (b, a):
            pairs.append((rc, lc))
        else:
            return None
    return pairs or None


def build_model(name: str, tables: dict, edges: list, unique_columns: set, terms: list) -> dict:
    """
    Build an Ossie semantic model (pure).

    tables  {table: {"desc": str, "columns": [(name, type, constraints, desc[, expression])]}}
    edges   [(table_a, table_b, join_keys)] — fk join edges, either direction
    terms   glossary term dicts
    """
    from MetadataManager.MetadataStore.relationdb.joinpath import join_type

    dataset_of = {t.lower(): _dataset_name(t) for t in tables}
    datasets = []
    for table, info in sorted(tables.items()):
        columns = info["columns"]
        primary = [c[0] for c in columns
                   if any(k in (c[2] or "").upper() for k in ("PRIMARY KEY", "COMPOSITE KEY PART"))]
        unique = [[c[0]] for c in columns if "UNIQUE" in (c[2] or "").upper()]
        dataset = {"name": dataset_of[table.lower()], "source": table}
        if primary:
            dataset["primary_key"] = primary
        if unique:
            dataset["unique_keys"] = unique
        if info.get("desc"):
            dataset["description"] = info["desc"]
        dataset["fields"] = []
        for col_name, col_type, _constraints, col_desc, *logic in columns:
            field = {
                "name": col_name,
                "expression": _expression((logic[0] if logic and logic[0] else "") or col_name),
            }
            if ossie_datatype(col_type):
                field["datatype"] = ossie_datatype(col_type)
            if col_desc:
                field["description"] = col_desc
            dataset["fields"].append(field)
        datasets.append(dataset)

    relationships, unexpressible, seen = [], [], set()
    for a, b, keys in edges:
        a, b = a.lower(), b.lower()
        key = (frozenset((a, b)), keys)
        if key in seen or a not in dataset_of or b not in dataset_of:
            continue
        seen.add(key)
        kind = join_type(keys, a, b, unique_columns)
        if kind == "1:n":
            a, b = b, a           # Ossie: `from` is the many side
        elif kind == "n:m":
            unexpressible.append({"tables": [a, b], "join_keys": keys})
            continue
        pairs = _pairs(keys, a, b)
        if not pairs:
            unexpressible.append({"tables": [a, b], "join_keys": keys})
            continue
        rel_name = f"{dataset_of[a]}_to_{dataset_of[b]}"
        n = sum(1 for r in relationships if r["name"].startswith(rel_name))
        relationships.append({
            "name": rel_name if n == 0 else f"{rel_name}_{n + 1}",
            "from": dataset_of[a],
            "to": dataset_of[b],
            "from_columns": [p[0] for p in pairs],
            "to_columns": [p[1] for p in pairs],
        })

    metrics = []
    for term in terms:
        if not term.get("formula"):
            continue
        metric = {
            "name": term["term_name"],
            "expression": _expression(term["formula"]),
        }
        if term.get("definition") or term.get("full_name"):
            metric["description"] = term.get("definition") or term.get("full_name")
        synonyms = term.get("synonyms") or []
        if isinstance(synonyms, str):
            try:
                synonyms = json.loads(synonyms)
            except ValueError:
                synonyms = [synonyms]
        if synonyms:
            metric["ai_context"] = {"synonyms": list(synonyms)}
        metrics.append(metric)

    model = {"version": EXPORT_VERSION, "name": name, "datasets": datasets}
    if relationships:
        model["relationships"] = relationships
    if metrics:
        model["metrics"] = metrics
    if unexpressible:
        model["custom_extensions"] = [{
            "vendor_name": "polyql",
            "data": json.dumps({"joins_not_expressible_as_relationships": unexpressible}),
        }]
    return model


def read_instance(instance_name: str = "default") -> tuple:
    """
    Read an instance's metadata for export.

    Returns (tables, edges, unique_columns, terms):
      tables          {table: {"desc", "columns": [(name, type, constraints, desc, expression)]}}
      edges           [(table_a, table_b, join_keys)] — fk join edges with keys
      unique_columns  {(table, column)} PRIMARY KEY / UNIQUE
      terms           glossary term dicts
    """
    from MetadataManager import GlossaryStore
    from MetadataManager.MetadataStore.relationdb import kuzuDB
    from MetadataManager.MetadataStore.relationdb.joinpath import is_join_edge, load_unique_columns
    from Utilities.base_utils import accessDB, get_config_val

    tmddb = get_config_val("retrieval_config", ["tableMDdb"], True)
    db = accessDB(tmddb["info_type"], tmddb["dbName"])
    inst_filter = {} if instance_name == "default" else {"instance_name": instance_name}
    desc_rows = db.get_data(tmddb["tableDescName"], inst_filter, ["tableName", "Desc"],
                            fetchtype="All") or []
    tables = {row[0]: {"desc": row[1] or "", "columns": []} for row in desc_rows if row[0]}
    lower = {t.lower(): t for t in tables}
    col_rows = db.get_data(tmddb["tableColName"], inst_filter,
                           ["TableName", "ColumnName", "DataType", "Constraints", "Desc",
                            "logic", "type_of_logic"],
                           fetchtype="All") or []
    for row in col_rows:
        table = lower.get((row[0] or "").lower())
        if table and row[1]:
            expression = row[5] if row[6] == EXPRESSION_LOGIC else ""
            tables[table]["columns"].append(
                (row[1], row[2] or "", row[3] or "", row[4] or "", expression or "")
            )

    graph = kuzuDB.getObj(instance_name)
    edges = []
    for a, b, data in graph.edges(data=True):
        if is_join_edge(data):
            for keys in data.get("JoinKeysAlternatives") or [data.get("JoinKeys", "")]:
                if keys:
                    edges.append((a, b, keys))

    terms = GlossaryStore.list_terms(
        instance_name=None if instance_name == "default" else instance_name
    )
    return tables, edges, load_unique_columns(instance_name), terms


def build_document(instance_name: str = "default", name: str | None = None) -> str:
    """Export an instance's tables, join graph and glossary as an Ossie YAML document."""
    tables, edges, unique_columns, terms = read_instance(instance_name)
    model = build_model(name or f"polyql_{instance_name}", tables, edges, unique_columns, terms)
    return yaml.safe_dump(model, sort_keys=False, allow_unicode=True)
