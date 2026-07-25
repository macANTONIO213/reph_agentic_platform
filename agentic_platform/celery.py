"""
Celery application for the Agentic Platform — Phase 0 durable execution.

Runs WorkflowRun and AsyncAgentTask work off the request thread so long-running,
concurrent agent invocations (including Phase 1 A2A ``message/send``) survive
worker restarts and don't block web workers.

Configuration is namespaced under ``CELERY_*`` in ``settings.py``.  The whole
surface is opt-in: nothing dispatches to Celery unless ``EXECUTION_BACKEND`` is
set to ``"celery"``.  When Celery/Redis are absent the platform falls back to the
legacy DB-backed queue and synchronous execution (see ``workflow_queue`` and
``agent_tasks``), so this module never becomes a hard dependency for running the
control plane or the test suite.
"""
from __future__ import annotations

import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "agentic_platform.settings")

app = Celery("agentic_platform")

# All Celery settings live in Django settings under the CELERY_ namespace.
app.config_from_object("django.conf:settings", namespace="CELERY")

# Discover tasks in every installed app (controlplane/tasks.py).
app.autodiscover_tasks()


@app.task(name="agentic_platform.ping")
def ping() -> str:
    """Liveness probe used by tests and ops to confirm workers are reachable."""
    return "pong"
