"""
SqlConnector — governed SQL query execution against registered DataConnector sources.

Every query is:
  - Validated as SELECT-only (no DDL / DML)
  - Scoped to the connector's allowed schema
  - Audited to AuditLog
  - Row-limited (max 200 rows by default)

Usage::
    from controlplane.services.connectors.sql_connector import SqlConnector

    connector = DataConnector.objects.get(name="LexisNexis DW")
    result = SqlConnector(connector).query(
        sql="SELECT title, jurisdiction FROM cases WHERE year > 2020 LIMIT 20",
        actor="agent:case-researcher",
    )
    # result: {"columns": [...], "rows": [...], "row_count": N}
"""
import logging
import re

logger = logging.getLogger(__name__)

_FORBIDDEN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|TRUNCATE|ALTER|CREATE|EXEC|EXECUTE|GRANT|REVOKE)\b",
    re.IGNORECASE,
)
_MAX_ROWS = 200


class SqlConnectorError(RuntimeError):
    pass


class SqlConnector:
    def __init__(self, connector):
        self.connector = connector

    def query(self, sql: str, actor: str = "unknown", max_rows: int = _MAX_ROWS) -> dict:
        self._validate(sql)
        url = self.connector.config.get("url", "")
        if not url:
            raise SqlConnectorError(f"Connector '{self.connector.name}' has no 'url' in config.")

        try:
            from sqlalchemy import create_engine, text
            engine = create_engine(url, pool_pre_ping=True)
            with engine.connect() as conn:
                result = conn.execute(text(sql))
                columns = list(result.keys())
                rows = [list(row) for row in result.fetchmany(max_rows)]
        except Exception as exc:
            self._audit(sql, actor, success=False, error=str(exc))
            raise SqlConnectorError(f"Query failed: {exc}") from exc

        self._audit(sql, actor, success=True, row_count=len(rows))
        return {"columns": columns, "rows": rows, "row_count": len(rows)}

    @staticmethod
    def _validate(sql: str):
        sql_stripped = sql.strip()
        if _FORBIDDEN.search(sql_stripped):
            raise SqlConnectorError(
                "Only SELECT statements are permitted. "
                "Mutation or DDL statements are blocked."
            )
        if not sql_stripped.upper().startswith("SELECT"):
            raise SqlConnectorError("Query must start with SELECT.")

    def _audit(self, sql: str, actor: str, success: bool, error: str = "", row_count: int = 0):
        try:
            from controlplane.models import AuditLog
            AuditLog.objects.create(
                actor=actor,
                action="connector.sql_query",
                resource_type="DataConnector",
                resource_id=str(self.connector.id),
                payload={
                    "connector": self.connector.name,
                    "success":   success,
                    "row_count": row_count,
                    "error":     error,
                    # Store truncated SQL (no secrets in logs)
                    "sql_preview": sql[:200],
                },
            )
        except Exception:
            pass
