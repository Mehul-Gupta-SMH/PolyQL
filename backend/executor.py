from sqlalchemy import create_engine, text


def execute_query(sql: str, query_type: str, connection_string: str):
    """
    Execute a SQL or Spark SQL query via SQLAlchemy.

    Args:
        sql: The query string to execute.
        query_type: "sql" or "spark_sql" (dataframe_api is not supported).
        connection_string: SQLAlchemy-compatible connection string.

    Returns:
        Tuple of (columns: list[str], rows: list[list]).

    Raises:
        ValueError: For empty inputs.
        SQLAlchemy errors: Re-raised for the endpoint to handle.
    """
    if not sql or not sql.strip():
        raise ValueError("sql must be a non-empty string.")
    if not connection_string or not connection_string.strip():
        raise ValueError("connection_string must be a non-empty string.")

    engine = create_engine(connection_string, pool_pre_ping=True)

    with engine.connect() as conn:
        result = conn.execute(text(sql))
        columns = list(result.keys())
        rows = [list(row) for row in result.fetchmany(500)]

    return columns, rows


# ---------------------------------------------------------------------------
# Read-only execution (MCP execute_sql)
# ---------------------------------------------------------------------------

def _json_safe(value):
    """Convert DB values (Decimal, datetime, bytes, ...) to JSON-friendly types."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<{len(bytes(value))} bytes>"
    return str(value)


def execute_read_only(sql: str, connection_string: str, max_rows: int = 100, timeout_s: float = 30):
    """
    Run one read-only statement and roll back.

    The caller must already have checked that *sql* is a single SELECT. On top
    of that, the transaction is always rolled back, and where the dialect
    supports it the session is made read-only and given a statement timeout:
      postgresql — SET TRANSACTION READ ONLY + SET LOCAL statement_timeout
      mysql      — SET SESSION max_execution_time (SELECT only)
      sqlite     — PRAGMA query_only + a progress-handler deadline
    The real guarantee is a database user with read-only grants; use one.

    Returns:
        (columns, rows, truncated, enforced) — at most *max_rows* rows;
        truncated is True when more rows existed; enforced lists the
        protections that were applied.
    """
    import time

    if not sql or not sql.strip():
        raise ValueError("sql must be a non-empty string.")
    if not connection_string or not connection_string.strip():
        raise ValueError("connection_string must be a non-empty string.")

    engine = create_engine(connection_string, pool_pre_ping=True)
    enforced = ["rollback"]
    try:
        with engine.connect() as conn:
            dialect = engine.dialect.name
            timeout_ms = max(1, int(timeout_s * 1000))
            raw = None
            if dialect == "postgresql":
                conn.execute(text("SET TRANSACTION READ ONLY"))
                conn.execute(text(f"SET LOCAL statement_timeout = {timeout_ms}"))
                enforced += ["read_only_transaction", "statement_timeout"]
            elif dialect in ("mysql", "mariadb"):
                conn.execute(text(f"SET SESSION max_execution_time = {timeout_ms}"))
                enforced.append("statement_timeout")
            elif dialect == "sqlite":
                conn.execute(text("PRAGMA query_only = ON"))
                raw = conn.connection.driver_connection
                deadline = time.monotonic() + timeout_s
                raw.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10_000)
                enforced += ["query_only", "statement_timeout"]
            try:
                result = conn.execute(text(sql))
                columns = list(result.keys())
                fetched = result.fetchmany(max_rows + 1)
            finally:
                if raw is not None:
                    raw.set_progress_handler(None, 0)
                    conn.execute(text("PRAGMA query_only = OFF"))
                conn.rollback()
    finally:
        engine.dispose()

    truncated = len(fetched) > max_rows
    rows = [[_json_safe(v) for v in row] for row in fetched[:max_rows]]
    return columns, rows, truncated, enforced
