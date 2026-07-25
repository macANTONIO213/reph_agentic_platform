"""A2A + MCP-server routes — included at /a2a/ (separate from the /api/v1/ contract)."""
from django.urls import path

from . import a2a_views, mcp_server_views

urlpatterns = [
    path("registry/",                  a2a_views.registry,    name="a2a_registry"),
    path("agents/",                    a2a_views.discovery,   name="a2a_discovery"),
    path("agents/<slug:slug>/card/",   a2a_views.agent_card,  name="a2a_agent_card"),
    path("agents/<slug:slug>/rpc/",    a2a_views.rpc,         name="a2a_rpc"),
    # Stretch: expose our governed builtins as an MCP server.
    path("mcp/",                       mcp_server_views.mcp_endpoint, name="mcp_server"),
]
