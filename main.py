import logging
import re

import sqlparse
import sqlparse.tokens as T

from APIManager.AllAPICaller import CallLLMApi
from APIManager.PromptBuilder import PromptBuilder
from SQLBuilderComponents import SQLBuilderSupport

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class SQLValidationError(Exception):
    pass


_CODE_FENCE_RE = re.compile(r'```(?:\w+)?\s*(.*?)\s*```', re.DOTALL | re.IGNORECASE)

_VALID_PROVIDERS = {"open_ai", "anthropic", "google", "groq", "codex", "claude_code"}
_VALID_QUERY_TYPES = {"sql", "spark_sql", "dataframe_api", "pandas"}
_CODE_FENCE_PYSPARK_RE = re.compile(r'```(?:python)?\s*(.*?)\s*```', re.DOTALL | re.IGNORECASE)
_MAX_QUERY_LENGTH = 2000


def _strip_code_fence(text: str) -> str:
    """Remove markdown code fences if the LLM wrapped the SQL in them."""
    match = _CODE_FENCE_RE.search(text)
    return match.group(1).strip() if match else text.strip()


def _parse_llm_json(raw: str) -> dict:
    """
    Parse the LLM response as a {type, content} JSON envelope.

    Expected shapes:
      {"type": "sql",     "content": "<SQL>"}
      {"type": "code",    "content": "<PySpark>"}
      {"type": "clarify", "content": "<question>"}

    Falls back to {"type": "sql", "content": <cleaned text>} if the response
    is not valid JSON (backward compatibility with older prompt formats).
    """
    import json as _json

    cleaned = _strip_code_fence(raw)
    m = re.search(r'\{.*\}', cleaned, re.DOTALL)
    if m:
        try:
            parsed = _json.loads(m.group(0))
            if isinstance(parsed.get('type'), str) and isinstance(parsed.get('content'), str):
                return parsed
        except Exception:
            pass
    return {'type': 'sql', 'content': cleaned}


def validate_sql(llm_output: str) -> dict:
    """
    Parse and validate an LLM response for SQL generation.

    Returns a dict:
      {"type": "sql",     "content": "<validated SQL>"}
      {"type": "clarify", "content": "<question for the user>"}

    Raises SQLValidationError if the response claims to be SQL but is not parseable.
    """
    result = _parse_llm_json(llm_output)

    if result['type'] == 'clarify':
        return result

    cleaned = result['content']
    statements = [s for s in sqlparse.parse(cleaned) if str(s).strip()]

    if not statements:
        raise SQLValidationError(
            f"LLM response is empty after stripping. Raw: {llm_output[:200]!r}"
        )

    for stmt in statements:
        for token in stmt.flatten():
            if token.ttype in (T.Keyword.DML, T.Keyword.DDL):
                return result

    raise SQLValidationError(
        f"LLM response does not appear to contain valid SQL.\nRaw: {llm_output[:200]!r}"
    )


def validate_pandas(llm_output: str) -> dict:
    """
    Parse and validate an LLM response for Pandas code generation.

    Returns a dict:
      {"type": "code",    "content": "<validated Pandas code>"}
      {"type": "clarify", "content": "<question for the user>"}

    Raises SQLValidationError if the response claims to be code but contains no
    recognisable Pandas operations.
    """
    result = _parse_llm_json(llm_output)

    if result['type'] == 'clarify':
        return result

    cleaned = result['content']
    if not cleaned:
        raise SQLValidationError(f"LLM response is empty after stripping. Raw: {llm_output[:200]!r}")

    _PANDAS_RE = re.compile(
        r'\.(merge|groupby|agg|sort_values|query|filter|assign|rename|drop|loc|iloc|head|tail|value_counts|pivot|pivot_table|apply|map|fillna|dropna)\s*[(\[]',
        re.IGNORECASE,
    )
    if not _PANDAS_RE.search(cleaned):
        raise SQLValidationError(
            f"Response does not contain valid Pandas code.\nRaw: {llm_output[:200]!r}"
        )
    return result


def validate_pyspark(llm_output: str) -> dict:
    """
    Parse and validate an LLM response for PySpark DataFrame API generation.

    Returns a dict:
      {"type": "code",    "content": "<validated PySpark code>"}
      {"type": "clarify", "content": "<question for the user>"}

    Raises SQLValidationError if the response claims to be code but contains no
    recognisable DataFrame API calls.
    """
    result = _parse_llm_json(llm_output)

    if result['type'] == 'clarify':
        return result

    cleaned = result['content']
    if not cleaned:
        raise SQLValidationError(f"LLM response is empty after stripping. Raw: {llm_output[:200]!r}")

    _PYSPARK_RE = re.compile(
        r'\.(select|filter|where|join|groupBy|agg|orderBy|limit|withColumn|alias)\s*\(',
        re.IGNORECASE,
    )
    if not _PYSPARK_RE.search(cleaned):
        raise SQLValidationError(
            f"Response does not contain valid PySpark DataFrame API code.\nRaw: {llm_output[:200]!r}"
        )
    return result


def getRelevantContext(user_query: str, instance_name: str = "default") -> dict:
    """
    Retrieves the schema context needed to build a SQL query for the given question.

    :param user_query: Natural language question from the user.
    :param instance_name: Named database instance to scope metadata lookups.
    :return: Dict containing user_query, table_list, and join_keys.
    """
    from MetadataManager.retrieval_cache import cached_context

    def _retrieve():
        return SQLBuilderSupport(instance_name=instance_name).getBuildComponents(user_query)

    # Callers get their own copy, so adding glossary_hits etc. is safe
    return cached_context(user_query, instance_name, _retrieve)


_PROMPT_MAP = {
    "sql": "generate sql",
    "spark_sql": "generate spark sql",
    "dataframe_api": "generate dataframe api",
    "pandas": "generate pandas",
}


def _get_business_context(query: str, instance_name: str = "default") -> list:
    """
    SL2: Retrieve semantically matched business terms for *query*.

    Delegates to GlossaryStore.get_business_context() and falls back to []
    on any error so that the main query pipeline is never blocked.
    """
    try:
        from MetadataManager.GlossaryStore import get_business_context
        return get_business_context(query, instance_name=instance_name)
    except Exception as exc:
        logger.warning("_get_business_context failed (non-fatal): %s", exc)
        return []


def _is_retrieval_confident(context: dict, min_tables: int) -> bool:
    """Return True when the context contains at least min_tables direct tables."""
    direct = context.get("table_list", {}).get("direct", {})
    return len(direct) >= min_tables


def _adaptive_retrieval(
    initial_query: str,
    LLMservice: str,
    model: str = None,
    instance_name: str = "default",
) -> dict:
    """
    Adaptive re-retrieval loop (R1).

    Runs getRelevantContext up to re_retrieval.max_rounds times.  On each round
    where the confidence check fails, the LLM is asked to rewrite the search
    query using the table directory and the tables found so far.  Results from
    all rounds are merged (union on direct + intermediate tables) before return.

    Falls back gracefully on any error; always returns a context dict.
    """
    from Utilities.base_utils import get_config_val

    try:
        _max_rounds = int(get_config_val("retrieval_config", ["re_retrieval", "max_rounds"]))
    except (KeyError, AttributeError, TypeError, ValueError):
        _max_rounds = 3

    try:
        _min_tables = int(get_config_val("retrieval_config", ["re_retrieval", "min_direct_tables"]))
    except (KeyError, AttributeError, TypeError, ValueError):
        _min_tables = 2

    try:
        _rewrite_provider = get_config_val("retrieval_config", ["re_retrieval", "rewrite_provider"]) or LLMservice
    except (KeyError, AttributeError, TypeError, ValueError):
        _rewrite_provider = LLMservice

    # Skip loop when table directory is empty — nothing for the LLM to rewrite against
    table_dir = _get_table_directory(instance_name=instance_name)
    if not table_dir.strip() or "(no tables" in table_dir or "(table directory unavailable)" in table_dir:
        logger.warning("R1: table directory is empty — skipping adaptive retrieval")
        return getRelevantContext(initial_query, instance_name=instance_name)

    best_context: dict = {}
    best_direct_count = -1
    current_query = initial_query
    prev_direct_names: set = set()

    for round_num in range(1, _max_rounds + 1):
        context = getRelevantContext(current_query, instance_name=instance_name)

        direct = context.get("table_list", {}).get("direct", {})
        direct_names = set(direct.keys())

        # Track best result seen so far
        if len(direct_names) > best_direct_count:
            best_direct_count = len(direct_names)
            best_context = context

        if _is_retrieval_confident(context, _min_tables):
            logger.info("R1: confident after round %d (%d direct tables found)", round_num, len(direct_names))
            return context

        if round_num == _max_rounds:
            logger.warning("R1: max_rounds (%d) exhausted — returning best context (%d direct tables)", _max_rounds, best_direct_count)
            break

        # Stagnation check — stop if no new tables found vs last round
        if round_num > 1 and direct_names == prev_direct_names:
            logger.info("R1: stagnation detected on round %d — stopping early", round_num)
            break

        prev_direct_names = direct_names

        # Build rewriter prompt
        found_list = ", ".join(sorted(direct_names)) if direct_names else "none"
        rewrite_prompt = (
            "You are a database search assistant. Your job is to rewrite a search query "
            "to find relevant database tables.\n\n"
            f'Original user question:\n"{initial_query}"\n\n'
            f'Previous search query (did not find enough tables):\n"{current_query}"\n\n'
            f"Tables found so far: {found_list}\n\n"
            f"Available tables in the database:\n{table_dir}\n\n"
            "Write ONE alternative search query (1-2 sentences) that uses different wording, "
            "synonyms, or more general/specific terms to find the tables needed to answer "
            "the original question. Output ONLY the rewritten query text — no explanation, "
            "no JSON, no quotes."
        )

        try:
            rewritten = CallLLMApi(_rewrite_provider, model=model).CallService(rewrite_prompt).strip()
        except Exception as exc:
            logger.warning("R1: rewrite LLM call failed on round %d: %s — using best context so far", round_num, exc)
            break

        if not rewritten:
            logger.warning("R1: LLM returned empty rewrite on round %d — stopping", round_num)
            break

        if rewritten == current_query:
            logger.info("R1: LLM returned identical query on round %d — stopping", round_num)
            break

        logger.info("R1: round %d rewrite: %r", round_num + 1, rewritten[:120])
        current_query = rewritten

    return best_context if best_context else getRelevantContext(initial_query, instance_name=instance_name)


def _format_conversation(conversation: list) -> str:
    """Format a list of {role, content} message dicts into a readable dialogue string."""
    if not conversation:
        return "(no prior conversation)"
    lines = [
        f"{'User' if m.get('role') == 'user' else 'Assistant'}: {m.get('content', '')}"
        for m in conversation
    ]
    return "\n".join(lines)


def generateQuery(
    userQuery: str,
    LLMservice: str,
    query_type: str = "sql",
    conversation: list = None,
    model: str = None,
    instance_name: str = "default",
) -> dict:
    """
    Generates a SQL, Spark SQL, or PySpark DataFrame API query from a natural language question,
    or returns a clarifying question if the schema is insufficient.

    :param userQuery: Natural language question (or requirements summary from gathering agent).
    :param LLMservice: LLM provider to use.
    :param query_type: Output type — 'sql', 'spark_sql', or 'dataframe_api'.
    :param conversation: Full session conversation history [{role, content}, ...] for context.
    :param instance_name: Named database instance to scope metadata lookups.
    :return: Dict with keys 'type' ('sql'|'code'|'clarify') and 'content' (str).
    """
    if not isinstance(userQuery, str) or not userQuery.strip():
        raise ValueError("userQuery must be a non-empty string.")
    if len(userQuery) > _MAX_QUERY_LENGTH:
        raise ValueError(
            f"userQuery exceeds maximum length of {_MAX_QUERY_LENGTH} characters "
            f"(got {len(userQuery)})."
        )
    if not isinstance(LLMservice, str) or LLMservice.lower() not in _VALID_PROVIDERS:
        raise ValueError(
            f"LLMservice must be one of {sorted(_VALID_PROVIDERS)}, got {LLMservice!r}."
        )
    if query_type not in _VALID_QUERY_TYPES:
        raise ValueError(
            f"query_type must be one of {sorted(_VALID_QUERY_TYPES)}, got {query_type!r}."
        )

    # Use raw user messages from conversation for RAG search — natural-language
    # questions match ChromaDB table-description embeddings far better than the
    # structured requirements summary that gatherRequirements() produces.
    rag_query = userQuery
    if conversation:
        user_texts = [m["content"] for m in conversation if m.get("role") == "user"]
        if user_texts:
            rag_query = " ".join(user_texts[-3:])

    context = _adaptive_retrieval(rag_query, LLMservice, model=model, instance_name=instance_name)

    no_tables = not any(context["table_list"].get(k) for k in ("direct", "intermediate"))
    if no_tables:
        logger.error("R1: all retrieval rounds exhausted with no tables found for query %r — schema section will be empty", rag_query)

    # SL2: attach business glossary hits so format_schema renders ## Business Definitions
    context["glossary_hits"] = _get_business_context(rag_query, instance_name=instance_name)

    schema_str = PromptBuilder.format_schema(context)
    conversation_str = _format_conversation(conversation)

    prompt = PromptBuilder(_PROMPT_MAP[query_type]).build({
        'CONVERSATION': conversation_str,
        'SCHEMA': schema_str,
    })

    logger.debug("Prompt sent to LLM:\n%s", prompt)

    LLMObj = CallLLMApi(LLMservice, model=model)
    raw = LLMObj.CallService(prompt)

    logger.debug("Raw LLM response:\n%s", raw)

    if query_type == "dataframe_api":
        return validate_pyspark(raw)
    if query_type == "pandas":
        return validate_pandas(raw)
    return validate_sql(raw)


def generateQueryStream(
    userQuery: str,
    LLMservice: str,
    query_type: str = "sql",
    conversation: list = None,
    model: str = None,
    instance_name: str = "default",
):
    """
    Generator version of generateQuery.

    Yields a sequence of dicts:
      {"event": "token", "data": "<chunk>"}  — raw LLM token strings
      {"event": "done",  "type": "sql"|"code"|"clarify", "content": "<full text>"}

    The "done" event carries the fully-accumulated and validated response.
    Consumers should display tokens incrementally and use "done" for final rendering.
    """
    if not isinstance(userQuery, str) or not userQuery.strip():
        raise ValueError("userQuery must be a non-empty string.")
    if len(userQuery) > _MAX_QUERY_LENGTH:
        raise ValueError(
            f"userQuery exceeds maximum length of {_MAX_QUERY_LENGTH} characters "
            f"(got {len(userQuery)})."
        )
    if not isinstance(LLMservice, str) or LLMservice.lower() not in _VALID_PROVIDERS:
        raise ValueError(
            f"LLMservice must be one of {sorted(_VALID_PROVIDERS)}, got {LLMservice!r}."
        )
    if query_type not in _VALID_QUERY_TYPES:
        raise ValueError(
            f"query_type must be one of {sorted(_VALID_QUERY_TYPES)}, got {query_type!r}."
        )

    rag_query = userQuery
    if conversation:
        user_texts = [m["content"] for m in conversation if m.get("role") == "user"]
        if user_texts:
            rag_query = " ".join(user_texts[-3:])

    context = _adaptive_retrieval(rag_query, LLMservice, model=model, instance_name=instance_name)

    no_tables = not any(context["table_list"].get(k) for k in ("direct", "intermediate"))
    if no_tables:
        logger.error("R1: all retrieval rounds exhausted with no tables found for query %r — schema section will be empty", rag_query)

    # SL2: attach business glossary hits so format_schema renders ## Business Definitions
    context["glossary_hits"] = _get_business_context(rag_query, instance_name=instance_name)

    schema_str = PromptBuilder.format_schema(context)
    conversation_str = _format_conversation(conversation)

    prompt = PromptBuilder(_PROMPT_MAP[query_type]).build({
        'CONVERSATION': conversation_str,
        'SCHEMA': schema_str,
    })

    LLMObj = CallLLMApi(LLMservice, model=model)

    accumulated = []
    upstream = LLMObj.CallServiceStream(prompt)
    try:
        for token in upstream:
            accumulated.append(token)
            yield {"event": "token", "data": token}
    finally:
        # Consumer stopped early (client Stop) -> close the LLM stream now
        # instead of waiting for garbage collection.
        close = getattr(upstream, "close", None)
        if close:
            close()

    full_response = "".join(accumulated)

    if query_type == "dataframe_api":
        result = validate_pyspark(full_response)
    elif query_type == "pandas":
        result = validate_pandas(full_response)
    else:
        result = validate_sql(full_response)

    yield {"event": "done", "type": result["type"], "content": result["content"]}


def _preload_schemas_bulk(instance_name: str = "default") -> dict:
    """
    Preload all table schemas from SQLite in two queries (one for descriptions,
    one for all column metadata), returning a dict of table_name -> markdown string.

    This is the P4 optimisation: instead of N×2 SQLite round-trips during the
    gather loop, we pay 2 queries upfront and serve every get_schema call from
    an in-memory dict.  Falls back to an empty dict on any error so the loop
    can still call _get_full_table_schema() per-table as before.
    """
    from collections import defaultdict
    try:
        from Utilities.base_utils import accessDB, get_config_val

        tmddb = get_config_val("retrieval_config", ["tableMDdb"], True)
        db = accessDB(tmddb["info_type"], tmddb["dbName"])

        inst_filter = {} if instance_name == "default" else {"instance_name": instance_name}

        # Query 1 — all table descriptions
        desc_rows = db.get_data(
            tmddb["tableDescName"], inst_filter, ["tableName", "Desc"], fetchtype="All"
        ) or []
        desc_map = {row[0]: row[1] for row in desc_rows if row[0]}

        # Query 2 — all column metadata
        col_rows = db.get_data(
            tmddb["tableColName"], inst_filter,
            ["TableName", "ColumnName", "DataType", "Constraints", "Desc",
             "logic", "type_of_logic", "base_table"],
            fetchtype="All",
        ) or []

        cols_map: dict = defaultdict(list)
        for row in col_rows:
            if row[0]:
                cols_map[row[0]].append(row[1:])  # drop TableName prefix

        # Build formatted markdown per table
        schema_cache: dict = {}
        for table_name in set(desc_map) | set(cols_map):
            lines = [f"### {table_name}"]
            if table_name in desc_map:
                lines.append(f"> {desc_map[table_name]}")
            lines.append("")

            cols = cols_map.get(table_name, [])
            if cols:
                has_lineage = any(col[3] or col[4] or col[5] for col in cols)
                if has_lineage:
                    lines.append("| Column | Type | Constraints | Description | Source Expression | Logic Type | Base Table |")
                    lines.append("|--------|------|-------------|-------------|-------------------|------------|------------|")
                    for col in cols:
                        lines.append(
                            f"| {col[0] or ''} | {col[1] or ''} | {col[2] or ''} | {col[3] or ''}"
                            f" | {col[4] or ''} | {col[5] or ''} | {col[6] or ''} |"
                        )
                else:
                    lines.append("| Column | Type | Constraints | Description |")
                    lines.append("|--------|------|-------------|-------------|")
                    for col in cols:
                        lines.append(f"| {col[0] or ''} | {col[1] or ''} | {col[2] or ''} | {col[3] or ''} |")
            else:
                lines.append("_(no columns found for this table)_")

            schema_cache[table_name] = "\n".join(lines)

        logger.debug("Bulk schema preload: %d tables loaded in 2 queries", len(schema_cache))
        return schema_cache

    except Exception as exc:
        logger.warning("Bulk schema preload failed, will fall back to per-table fetches: %s", exc)
        return {}


def _get_full_table_schema(table_name: str, instance_name: str = "default") -> str:
    """
    Fetch full column metadata for a single table from SQLite.

    Returns a markdown table with all available metadata columns:
      ColumnName, DataType, Constraints, Description, Logic (source expression),
      LogicType, BaseTable — the latter three are populated for pipeline-derived tables.
    """
    try:
        from Utilities.base_utils import accessDB, get_config_val

        tmddb = get_config_val("retrieval_config", ["tableMDdb"], True)
        db = accessDB(tmddb["info_type"], tmddb["dbName"])

        desc_lookup = {"tableName": table_name}
        col_lookup = {"TableName": table_name}
        if instance_name != "default":
            desc_lookup["instance_name"] = instance_name
            col_lookup["instance_name"] = instance_name

        desc_row = db.get_data(tmddb["tableDescName"], desc_lookup, ["Desc"])
        cols = db.get_data(
            tmddb["tableColName"], col_lookup,
            ["ColumnName", "DataType", "Constraints", "Desc", "logic", "type_of_logic", "base_table"],
            fetchtype="All",
        ) or []

        lines = [f"### {table_name}"]
        if desc_row:
            lines.append(f"> {desc_row[0]}")
        lines.append("")

        if cols:
            # Determine whether any lineage columns are populated
            has_lineage = any(col[4] or col[5] or col[6] for col in cols)

            if has_lineage:
                lines.append("| Column | Type | Constraints | Description | Source Expression | Logic Type | Base Table |")
                lines.append("|--------|------|-------------|-------------|-------------------|------------|------------|")
                for col in cols:
                    lines.append(
                        f"| {col[0] or ''} | {col[1] or ''} | {col[2] or ''} | {col[3] or ''}"
                        f" | {col[4] or ''} | {col[5] or ''} | {col[6] or ''} |"
                    )
            else:
                lines.append("| Column | Type | Constraints | Description |")
                lines.append("|--------|------|-------------|-------------|")
                for col in cols:
                    lines.append(f"| {col[0] or ''} | {col[1] or ''} | {col[2] or ''} | {col[3] or ''} |")
        else:
            lines.append("_(no columns found for this table)_")

        return "\n".join(lines)
    except Exception as exc:
        logger.warning("Schema fetch failed for '%s': %s", table_name, exc)
        return f"### {table_name}\n_(schema unavailable: {exc})_"


def _get_table_directory(instance_name: str = "default") -> str:
    """Return a compact list of all tables + descriptions for the requirement gathering prompt."""
    try:
        from MetadataManager.MetadataStore.relationdb import kuzuDB
        from Utilities.base_utils import accessDB, get_config_val

        graph = kuzuDB.getObj(instance_name)
        tmddb = get_config_val("retrieval_config", ["tableMDdb"], True)
        db = accessDB(tmddb["info_type"], tmddb["dbName"])

        lines = []
        for table in sorted(graph.nodes()):
            desc_lookup = {"tableName": table}
            if instance_name != "default":
                desc_lookup["instance_name"] = instance_name
            desc_row = db.get_data(tmddb["tableDescName"], desc_lookup, ["Desc"])
            desc = (desc_row[0] if desc_row else "") or ""
            lines.append(f"- **{table}**: {desc}")

        return "\n".join(lines) if lines else "(no tables in schema yet)"
    except Exception as exc:
        logger.warning("Could not load table directory: %s", exc)
        return "(table directory unavailable)"


def gatherRequirements(messages: list, provider: str, model: str = None, instance_name: str = "default") -> dict:
    """
    Agentic requirement gathering loop.

    The LLM can call a get_schema tool (up to gather_requirements.max_tool_calls
    times, configurable in retrieval_config.YAML) to inspect individual table
    schemas before deciding to ask a question or declare ready.

    Each iteration re-builds the full prompt with all previously fetched schemas
    appended, so the LLM always sees the complete accumulated context.

    :param instance_name: Named database instance to scope all metadata lookups.

    Returns:
      {"ready": False, "question": str}  — needs more info from the user
      {"ready": True,  "summary": str}   — enough info; passed to generateQuery
    """
    import json as _json
    from Utilities.base_utils import get_config_val

    if not isinstance(provider, str) or provider.lower() not in _VALID_PROVIDERS:
        raise ValueError(
            f"LLMservice must be one of {sorted(_VALID_PROVIDERS)}, got {provider!r}."
        )

    try:
        _max_tool_calls = int(get_config_val("retrieval_config", ["gather_requirements", "max_tool_calls"]))
    except (KeyError, AttributeError, TypeError, ValueError):
        _max_tool_calls = 5

    table_dir = _get_table_directory(instance_name=instance_name)

    # P4: preload all schemas upfront in 2 SQLite queries so get_schema tool
    # calls are served from memory rather than hitting the DB per table.
    schema_cache = _preload_schemas_bulk(instance_name=instance_name)

    # Best-effort RAG schema retrieval from the recent conversation
    user_texts = [m["content"] for m in messages if m.get("role") == "user"]
    search_query = " ".join(user_texts[-3:]) if user_texts else ""
    try:
        context = getRelevantContext(search_query, instance_name=instance_name)
        # SL2: attach glossary hits so the gather agent sees business term formulas
        context["glossary_hits"] = _get_business_context(search_query, instance_name=instance_name)
        rag_schema_str = PromptBuilder.format_schema(context)
    except Exception as exc:
        logger.warning("RAG schema retrieval failed: %s", exc)
        rag_schema_str = "(schema retrieval failed)"

    conversation_str = "\n".join(
        f"{'User' if m.get('role') == 'user' else 'Assistant'}: {m['content']}"
        for m in messages
    )

    fetched_schemas: dict[str, str] = {}   # table_name -> markdown schema string

    for attempt in range(_max_tool_calls + 1):
        # Build the FETCHED_SCHEMAS section from any tool results so far
        if fetched_schemas:
            fetched_section = (
                "## Schemas Fetched via get_schema Tool\n\n"
                + "\n\n".join(fetched_schemas.values())
            )
        else:
            fetched_section = ""

        prompt = PromptBuilder('gather requirements').build({
            'TABLE_DIRECTORY': table_dir,
            'SCHEMA': rag_schema_str,
            'FETCHED_SCHEMAS': fetched_section,
            'CONVERSATION': conversation_str,
        })

        logger.debug("Gathering agent prompt (attempt %d):\n%s", attempt + 1, prompt)

        raw = CallLLMApi(provider, model=model).CallService(prompt)

        logger.debug("Gathering agent response (attempt %d):\n%s", attempt + 1, raw)

        cleaned = _strip_code_fence(raw)
        json_match = re.search(r'\{.*\}', cleaned, re.DOTALL)
        if not json_match:
            # LLM returned plain text instead of JSON — re-prompt once with a strict reminder
            logger.warning("Gathering agent returned non-JSON (attempt %d); re-prompting for JSON", attempt + 1)
            correction_prompt = (
                prompt
                + "\n\n[SYSTEM] Your last response was not valid JSON. "
                "You MUST respond with ONLY a JSON object — no explanation, no plain text. "
                "If you need schema details, use {\"action\": \"get_schema\", \"table\": \"...\"}. "
                "Do NOT ask the user for schema or column information."
            )
            try:
                raw = CallLLMApi(provider, model=model).CallService(correction_prompt)
                cleaned = _strip_code_fence(raw)
                json_match = re.search(r'\{.*\}', cleaned, re.DOTALL)
            except Exception:
                pass
            if not json_match:
                return {"ready": False, "question": "I need a bit more context to build this query. Could you describe what you're looking for — which entities, what filters, and what the output should show?"}

        try:
            parsed = _json.loads(json_match.group(0))
        except Exception:
            return {"ready": False, "question": raw.strip()}

        # --- Tool call: fetch a table's schema ---
        if parsed.get("action") == "get_schema":
            table_name = str(parsed.get("table", "")).strip()
            if not table_name:
                logger.warning("get_schema called with empty table name")
                continue
            if table_name in fetched_schemas:
                logger.debug("Schema for '%s' already fetched; skipping duplicate", table_name)
                continue
            logger.info("Gathering agent fetching schema for table: %s", table_name)
            # Serve from bulk preload cache; fall back to per-table DB query if missing
            fetched_schemas[table_name] = (
                schema_cache.get(table_name)
                or _get_full_table_schema(table_name, instance_name=instance_name)
            )
            continue   # re-prompt with the new schema appended

        # --- Final answer: question or ready ---
        if isinstance(parsed.get("ready"), bool):
            result = {"ready": parsed["ready"]}
            if parsed["ready"]:
                result["summary"] = parsed.get("summary", "")
            else:
                result["question"] = parsed.get("question", "")
                options = parsed.get("options")
                if isinstance(options, list) and options:
                    result["options"] = [str(o) for o in options]
            return result

        # Unrecognised JSON shape — treat as a question
        return {"ready": False, "question": raw.strip()}

    # Exhausted tool-call budget — ask a generic fallback question
    logger.warning("Gathering agent exhausted %d tool calls without a final answer", _max_tool_calls)
    return {
        "ready": False,
        "question": (
            "I've reviewed the available tables. Could you describe in more detail "
            "what you'd like to query — which entities, what filters, and what the "
            "output should look like?"
        ),
    }


def generate_pipeline_dict(
    pipeline_sql: str,
    source_schemas_str: str,
    column_mappings_str: str,
    LLMservice: str,
    model: str = None,
) -> dict:
    """
    Use an LLM to generate a data dictionary for the OUTPUT table of a SQL pipeline.

    :param pipeline_sql: The full INSERT...SELECT or CREATE TABLE AS SELECT statement.
    :param source_schemas_str: Pre-formatted string describing source table schemas.
    :param column_mappings_str: Pre-formatted string listing target ← source mappings.
    :param LLMservice: LLM provider to use.
    :return: Parsed JSON dict: {tableDesc: str, columns: [{name, desc}]}.
    """
    import json as _json

    if not isinstance(LLMservice, str) or LLMservice.lower() not in _VALID_PROVIDERS:
        raise ValueError(
            f"LLMservice must be one of {sorted(_VALID_PROVIDERS)}, got {LLMservice!r}."
        )
    if not pipeline_sql or not pipeline_sql.strip():
        raise ValueError("pipeline_sql must be a non-empty string.")

    prompt = PromptBuilder('ingest pipeline').build({
        'SQL': pipeline_sql.strip(),
        'SOURCE_SCHEMAS': source_schemas_str or "(source schemas not available)",
        'COLUMN_MAPPINGS': column_mappings_str or "(no mappings detected)",
    })

    logger.debug("Pipeline dict prompt:\n%s", prompt)

    LLMObj = CallLLMApi(LLMservice, model=model)
    raw = LLMObj.CallService(prompt)

    logger.debug("Pipeline dict raw response:\n%s", raw)

    cleaned = _strip_code_fence(raw)
    json_match = re.search(r'\{.*\}', cleaned, re.DOTALL)
    if not json_match:
        raise SQLValidationError(f"LLM did not return valid JSON. Raw: {raw[:200]!r}")

    return _json.loads(json_match.group(0))


if __name__ == "__main__":
    query = """
    Which products have the highest sales revenue and which products have the lowest sales revenue over the past year?
    """
    print(generateQuery(query, "open_ai"))
