from django.urls import path
from . import views

urlpatterns = [
    # Catalog
    path("agents/options/",                          views.agent_options,           name="api_agent_options"),
    path("agents/",                                  views.agents_list,             name="api_agents_list"),
    path("agents/<uuid:agent_id>/",                  views.agent_detail,            name="api_agent_detail"),
    path("agents/<uuid:agent_id>/metrics/",          views.agent_metrics,           name="api_agent_metrics"),
    # Monitoring
    path("monitoring/summary/",                      views.monitoring_summary_view, name="api_monitoring_summary"),
    path("monitoring/timeseries/",                   views.monitoring_timeseries,   name="api_monitoring_timeseries"),
    path("monitoring/breakdowns/",                   views.monitoring_breakdowns,   name="api_monitoring_breakdowns"),
    # Org
    path("org/tree/",                                views.org_tree,                name="api_org_tree"),
    # Feedback
    path("feedback/low-rated/",                      views.feedback_low_rated,      name="api_feedback_low_rated"),
    # Governance decisions
    path("governance/<uuid:review_id>/decide/",        views.governance_decide,       name="api_governance_decide"),
    # Agent transitions
    path("agents/<uuid:agent_id>/transition/",         views.agent_transition,        name="api_agent_transition"),
    # Approvals (Phase A governance)
    path("agents/<uuid:agent_id>/approvals/",          views.agent_approvals,         name="api_agent_approvals"),
    # Registration
    path("agents/register/",                           views.agent_register,          name="api_agent_register"),
    # Org cascading selects (for registration form)
    path("org/divisions/",                             views.org_divisions,           name="api_org_divisions"),
    path("org/work-streams/",                          views.org_work_streams,        name="api_org_work_streams"),
    path("org/processes/",                             views.org_processes,           name="api_org_processes"),
    # B3: Eval suite endpoints
    path("agents/<uuid:agent_id>/evals/",              views.eval_suites,             name="api_eval_suites"),
    path("evals/<uuid:suite_id>/run/",                 views.eval_run_suite,          name="api_eval_run_suite"),
    path("evals/runs/<uuid:run_id>/",                  views.eval_run_detail,         name="api_eval_run_detail"),
    # C1: Semantic search
    path("agents/search/",                             views.semantic_search,         name="api_semantic_search"),
    # C2: Knowledge base
    path("knowledge/",                                 views.knowledge_documents,     name="api_knowledge_list"),
    path("knowledge/retrieve/",                        views.knowledge_retrieve,      name="api_knowledge_retrieve"),
    path("knowledge/ingest/",                          views.knowledge_ingest,        name="api_knowledge_ingest"),
    # C3: Data connectors
    path("connectors/",                                views.connectors_list,         name="api_connectors_list"),
    # D1: Prometheus metrics
    path("metrics/",                                   views.prometheus_metrics,      name="api_metrics"),
    # D2: OTel spans
    path("spans/",                                     views.otel_spans,              name="api_otel_spans"),
    # D3: Budget alerts
    path("budget-alerts/",                             views.budget_alerts,           name="api_budget_alerts"),
    # E1: Workflows
    path("workflows/",                                 views.workflows_list,          name="api_workflows_list"),
    path("workflows/<uuid:workflow_id>/",              views.workflow_detail,         name="api_workflow_detail"),
    path("workflows/<uuid:workflow_id>/run/",          views.workflow_trigger,        name="api_workflow_trigger"),
    # E2: Workflow runs
    path("workflow-runs/<uuid:run_id>/",               views.workflow_run_detail,     name="api_workflow_run_detail"),
    path("workflow-runs/<uuid:run_id>/tasks/",         views.workflow_run_tasks,      name="api_workflow_run_tasks"),
    path("workflow-runs/<uuid:run_id>/memory/",        views.shared_memory,           name="api_shared_memory"),
    # E3: Model router explain
    path("agents/<uuid:agent_id>/model-route/",        views.model_route_explain,     name="api_model_route"),
]
