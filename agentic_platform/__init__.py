"""
Agentic Platform project package.

Loads the Celery application at Django startup so ``@shared_task``-decorated
tasks register against it.  The import is guarded: if Celery is not installed
(e.g. a minimal control-plane deployment that never enables the ``celery``
execution backend), the platform still imports and runs normally.
"""
try:
    from .celery import app as celery_app

    __all__ = ("celery_app",)
except ImportError:  # Celery not installed — DB/synchronous execution only.
    celery_app = None
    __all__ = ("celery_app",)
