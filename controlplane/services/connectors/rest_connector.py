"""
RestConnector — governed HTTP GET/POST calls to registered REST DataConnectors.

Features:
  - Base URL enforced from connector config (no arbitrary URL overrides)
  - Bearer token / API key injected from config (never from caller)
  - Response size limit (1 MB)
  - 15-second timeout
  - Full audit trail

Usage::
    from controlplane.services.connectors.rest_connector import RestConnector

    connector = DataConnector.objects.get(name="LN API")
    result = RestConnector(connector).get(
        path="/cases/search",
        params={"q": "GDPR", "jurisdiction": "EU"},
        actor="agent:case-researcher",
    )
    # result: {"status_code": 200, "data": {...}}
"""
import logging
import urllib.parse

import urllib.request
import json

logger = logging.getLogger(__name__)

_MAX_RESPONSE_BYTES = 1_048_576  # 1 MB
_TIMEOUT_SECONDS    = 15


class RestConnectorError(RuntimeError):
    pass


class RestConnector:
    def __init__(self, connector):
        self.connector = connector
        self._base_url = connector.config.get("base_url", "").rstrip("/")
        self._auth_header = connector.config.get("auth_header", "")

    def get(self, path: str, params: dict | None = None, actor: str = "unknown") -> dict:
        url = self._build_url(path, params)
        return self._request("GET", url, body=None, actor=actor)

    def post(self, path: str, body: dict, actor: str = "unknown") -> dict:
        url = self._build_url(path)
        return self._request("POST", url, body=body, actor=actor)

    # ── Internal ─────────────────────────────────────────────────────────────

    def _build_url(self, path: str, params: dict | None = None) -> str:
        if not self._base_url:
            raise RestConnectorError(
                f"Connector '{self.connector.name}' has no 'base_url' in config."
            )
        url = self._base_url + "/" + path.lstrip("/")
        if params:
            url += "?" + urllib.parse.urlencode(params)
        return url

    def _request(self, method: str, url: str, body, actor: str) -> dict:
        headers = {"Accept": "application/json", "User-Agent": "RELX-AgentPlatform/1.0"}
        if self._auth_header:
            # auth_header value like "Bearer {token}" or "ApiKey {key}"
            key, _, value = self._auth_header.partition(" ")
            headers["Authorization"] = f"{key} {value}"

        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"

        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=_TIMEOUT_SECONDS) as resp:
                raw = resp.read(_MAX_RESPONSE_BYTES)
                status_code = resp.status
                try:
                    response_data = json.loads(raw)
                except json.JSONDecodeError:
                    response_data = {"raw": raw.decode("utf-8", errors="replace")}
        except Exception as exc:
            self._audit(method, url, actor, success=False, error=str(exc))
            raise RestConnectorError(f"Request failed: {exc}") from exc

        self._audit(method, url, actor, success=True, status_code=status_code)
        return {"status_code": status_code, "data": response_data}

    def _audit(self, method: str, url: str, actor: str,
               success: bool, error: str = "", status_code: int = 0):
        try:
            from controlplane.models import AuditLog
            AuditLog.objects.create(
                actor=actor,
                action="connector.rest_call",
                resource_type="DataConnector",
                resource_id=str(self.connector.id),
                payload={
                    "connector":   self.connector.name,
                    "method":      method,
                    "url_preview": url[:200],
                    "success":     success,
                    "status_code": status_code,
                    "error":       error,
                },
            )
        except Exception:
            pass
