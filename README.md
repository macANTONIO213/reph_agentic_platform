# REPH Agentic Platform

This is a Django-based production-style demo for an in-house Agentic Platform.

The platform is a control plane for agents built in any environment: Django Runtime, Microsoft Copilot Studio, Azure AI Foundry, custom APIs, vendor platforms, or embedded internal applications.

## What Is Implemented

- Django project with persistent SQLite database.
- Agent registry model with owner, platform, lifecycle status, risk tier, tools, data sources, and telemetry settings.
- Live deployed agent: `Agent Deployment Advisor`.
- Real-time agent execution through streaming Server-Sent Events.
- Backend tool calls for registry search, risk classification, and deployment gate generation.
- Agent runs, tool calls, and telemetry events written to the database.
- Django admin for operational inspection.
- Seed command for repeatable demo data.

## Run Locally

```powershell
python manage.py migrate
python manage.py seed_demo
python manage.py runserver 127.0.0.1:8765
```

Open:

```text
http://127.0.0.1:8765/
```

## Demo Flow

1. Show the dashboard metrics and registry.
2. Open the Live Agent section.
3. Run the prefilled prompt or enter a new agent idea.
4. Watch the agent stream its answer in real time.
5. Show tool calls appearing during execution.
6. Refresh telemetry and show that the run, tools, latency, and events are persisted.
7. Open Django admin for deeper inspection of agents, runs, tool calls, and telemetry.

## Next Production Steps

- Replace the local runtime with provider-backed models where needed.
- Add SSO, RBAC, and business-unit-scoped access.
- Add formal approval workflow and review assignments.
- Add OpenTelemetry or event pipeline export.
- Add agent manifest upload/API for Copilot, custom API, and vendor agents.
- Add evaluation datasets and deployment release gates.
