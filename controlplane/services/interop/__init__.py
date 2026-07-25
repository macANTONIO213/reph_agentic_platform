"""
Interop layer — Phase 1 of the Agent Fabric transformation.

Houses the protocol plumbing that lets the platform speak MCP (consume external
tools) and A2A (be discovered/called by other agents), plus the shared network
guard both paths use.  Every interop call still routes through the platform's
governance / guardrail / audit path — the gate never moves.
"""
