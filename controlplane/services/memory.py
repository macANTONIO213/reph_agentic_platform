"""
MemoryService — Phase E Cross-Agent Memory

Provides a governed key-value store for sharing context between agents
inside a workflow run or persistently per agent.

Built-in tools (wired into RegistryToolsMixin):
  memory_read  — retrieve a value by key
  memory_write — store a value by key

Scoping rules:
  - Within a workflow run: scoped to (workflow_run, key)
  - Persistent per agent: scoped to (agent, key)
  - Platform-global reads fall back to agent scope → workflow scope

TTL support: entries older than expires_at are treated as missing.

Usage::
    from controlplane.services.memory import memory_service

    memory_service.write(key="summary", value={"text": "..."}, workflow_run=run)
    entry = memory_service.read(key="summary", workflow_run=run)
"""
from __future__ import annotations

import logging
from typing import Any

from django.utils import timezone

logger = logging.getLogger(__name__)


class MemoryService:

    # ── Write ─────────────────────────────────────────────────────────────────

    def write(
        self,
        *,
        key: str,
        value: Any,
        workflow_run=None,
        agent=None,
        written_by: str = "system",
        ttl_seconds: int | None = None,
    ):
        """
        Store a value.  At least one of workflow_run or agent must be provided.
        If both are provided, the entry is scoped to the workflow_run.
        """
        from controlplane.models import SharedMemory

        if workflow_run is None and agent is None:
            raise ValueError("Either workflow_run or agent must be provided.")

        expires_at = None
        if ttl_seconds:
            from datetime import timedelta
            expires_at = timezone.now() + timedelta(seconds=ttl_seconds)

        # Scope preference: workflow_run > agent
        lookup = {}
        defaults = {"value": value, "written_by": written_by, "expires_at": expires_at}

        if workflow_run is not None:
            lookup = {"workflow_run": workflow_run, "agent": None, "key": key}
        else:
            lookup = {"agent": agent, "workflow_run": None, "key": key}

        obj, created = SharedMemory.objects.update_or_create(defaults=defaults, **lookup)
        logger.debug("Memory write: key=%s scope=%s created=%s", key, lookup, created)
        return obj

    # ── Read ──────────────────────────────────────────────────────────────────

    def read(
        self,
        *,
        key: str,
        workflow_run=None,
        agent=None,
        default=None,
    ) -> Any:
        """
        Retrieve a value.  Checks workflow_run scope first, then agent scope.
        Returns default if not found or expired.
        """
        from controlplane.models import SharedMemory

        # Try workflow_run scope
        if workflow_run is not None:
            entry = SharedMemory.objects.filter(
                workflow_run=workflow_run, key=key
            ).first()
            if entry and not entry.is_expired:
                return entry.value

        # Try agent scope
        if agent is not None:
            entry = SharedMemory.objects.filter(
                agent=agent, workflow_run=None, key=key
            ).first()
            if entry and not entry.is_expired:
                return entry.value

        return default

    # ── List ──────────────────────────────────────────────────────────────────

    def list_keys(self, *, workflow_run=None, agent=None) -> list[str]:
        """List all non-expired keys in the given scope."""
        from controlplane.models import SharedMemory
        from django.db.models import Q

        now = timezone.now()
        q = Q(expires_at__isnull=True) | Q(expires_at__gt=now)
        if workflow_run is not None:
            qs = SharedMemory.objects.filter(q, workflow_run=workflow_run)
        elif agent is not None:
            qs = SharedMemory.objects.filter(q, agent=agent, workflow_run=None)
        else:
            return []
        return list(qs.values_list("key", flat=True))

    # ── Delete ────────────────────────────────────────────────────────────────

    def delete(self, *, key: str, workflow_run=None, agent=None) -> bool:
        from controlplane.models import SharedMemory
        if workflow_run is not None:
            deleted, _ = SharedMemory.objects.filter(workflow_run=workflow_run, key=key).delete()
        elif agent is not None:
            deleted, _ = SharedMemory.objects.filter(agent=agent, workflow_run=None, key=key).delete()
        else:
            return False
        return bool(deleted)

    # ── Cleanup ───────────────────────────────────────────────────────────────

    def purge_expired(self) -> int:
        """Delete all expired entries.  Call from a nightly management command."""
        from controlplane.models import SharedMemory
        deleted, _ = SharedMemory.objects.filter(expires_at__lt=timezone.now()).delete()
        return deleted


# Module-level singleton
memory_service = MemoryService()
