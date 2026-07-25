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
from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

logger = logging.getLogger(__name__)

_FORBIDDEN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|TRUNCATE|ALTER|CREATE|EXEC|EXECUTE|GRANT|REVOKE)\b",
    re.IGNORECASE,
)
_MAX_ROWS = 200
_TABLE_REF = re.compile(r"\b(?:FROM|JOIN)\s+([A-Za-z_][\w.]*|\"[^\"]+\")", re.IGNORECASE)


class SqlConnectorError(RuntimeError):
    pass


class SqlConnector:
    def __init__(self, connector):
        self.connector = connector

    def query(self, sql: str, actor: str = "unknown", max_rows: int = _MAX_ROWS) -> dict:
        self._assert_circuit_closed()
        allowed_schema = (self.connector.config or {}).get("schema", "")
        self._validate(sql, allowed_schema=allowed_schema)
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
            self._register_failure()
            self._audit(sql, actor, success=False, error=str(exc))
            raise SqlConnectorError(f"Query failed: {exc}") from exc

        self._clear_failures()
        self._audit(sql, actor, success=True, row_count=len(rows))
        return {"columns": columns, "rows": rows, "row_count": len(rows)}

    @staticmethod
    def _validate(sql: str, *, allowed_schema: str = ""):
        sql_stripped = sql.strip()
        if _FORBIDDEN.search(sql_stripped):
            raise SqlConnectorError(
                "Only SELECT statements are permitted. "
                "Mutation or DDL statements are blocked."
            )
        if not sql_stripped.upper().startswith("SELECT"):
            raise SqlConnectorError("Query must start with SELECT.")
        if not allowed_schema:
            return
        schema = str(allowed_schema).strip().strip('"').lower()
        if not schema:
            return
        table_refs = _TABLE_REF.findall(sql_stripped)
        if not table_refs:
            return
        invalid_refs = []
        for ref in table_refs:
            clean = ref.strip().strip('"')
            if clean.startswith("("):
                continue
            if "." not in clean:
                invalid_refs.append(clean)
                continue
            ref_schema = clean.split(".", 1)[0].strip('"').lower()
            if ref_schema != schema:
                invalid_refs.append(clean)
        if invalid_refs:
            raise SqlConnectorError(
                f"Query may only access schema '{schema}'. "
                f"Found disallowed table refs: {', '.join(invalid_refs[:5])}."
            )

    def _failure_key(self) -> str:
        return f"cb:sql:failures:{self.connector.id}"

    def _open_until_key(self) -> str:
        return f"cb:sql:open_until:{self.connector.id}"

    def _assert_circuit_closed(self) -> None:
        open_until = cache.get(self._open_until_key())
        if open_until and timezone.now() < open_until:
            raise SqlConnectorError(
                f"Circuit breaker open for connector '{self.connector.name}'. "
                "Retry after cooldown."
            )

    def _register_failure(self) -> None:
        threshold = int(getattr(settings, "CONNECTOR_CIRCUIT_BREAKER_FAILURE_THRESHOLD", 5))
        cooldown = int(getattr(settings, "CONNECTOR_CIRCUIT_BREAKER_COOLDOWN_SECONDS", 60))
        key = self._failure_key()
        failures = cache.get(key, 0) + 1
        cache.set(key, failures, timeout=max(cooldown, 60))
        if failures >= threshold:
            cache.set(self._open_until_key(), timezone.now() + timedelta(seconds=cooldown), timeout=cooldown)

    def _clear_failures(self) -> None:
        cache.delete(self._failure_key())
        cache.delete(self._open_until_key())

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
