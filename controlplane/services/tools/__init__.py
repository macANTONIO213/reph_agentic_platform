"""
Tool subsystem — dynamic, per-agent tool registry (Layer 0).

Importing this package registers the built-in tools and exposes the singleton
``tool_registry``.  Adapters use it to expose per-agent tool schemas and to
dispatch tool calls through a single governance gate.
"""
from controlplane.services.tools.registry import (
    ToolContext,
    ToolRegistry,
    ToolSpec,
    tool_registry,
)
from controlplane.services.tools.builtins import register_builtins

# Register built-ins on first import (idempotent).
register_builtins()

__all__ = ["ToolContext", "ToolRegistry", "ToolSpec", "tool_registry", "register_builtins"]
