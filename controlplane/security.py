from __future__ import annotations

from django.http import JsonResponse


def is_cross_tenant(user) -> bool:
    if user.is_staff or user.is_superuser:
        return True
    profile = getattr(user, "profile", None)
    return bool(profile and profile.is_cross_tenant)


def user_business_unit_id(user):
    profile = getattr(user, "profile", None)
    return profile.business_unit_id if profile is not None else None


def has_role(user, *roles: str) -> bool:
    role_set = set(roles)
    if user.is_staff or user.is_superuser:
        return True
    if user.groups.filter(name__in=role_set).exists():
        return True
    profile = getattr(user, "profile", None)
    return bool(profile and profile.role in role_set)


def require_role_json(user, *roles: str):
    if has_role(user, *roles):
        return None
    return JsonResponse(
        {"error": f"Action requires one of: {', '.join(sorted(set(roles)))}."},
        status=403,
    )


def can_access_agent(user, agent) -> bool:
    if user.is_staff or user.is_superuser:
        return True
    profile = getattr(user, "profile", None)
    return bool(profile and profile.can_access_agent(agent))


def can_access_business_unit(user, business_unit_id) -> bool:
    if is_cross_tenant(user):
        return True
    bu_id = user_business_unit_id(user)
    if bu_id is None or business_unit_id is None:
        return False
    return str(bu_id) == str(business_unit_id)


def can_access_workflow_run(user, workflow_run) -> bool:
    if is_cross_tenant(user):
        return True
    if workflow_run.triggered_by == user.username:
        return True
    return can_access_business_unit(user, workflow_run.workflow.business_unit_id)


def blueprint_business_unit_id(blueprint):
    if blueprint.insight_id and blueprint.insight is not None:
        return blueprint.insight.business_unit_id
    if blueprint.built_agent_id and blueprint.built_agent is not None:
        return blueprint.built_agent.org_unit_id
    return None


def package_business_unit_id(package):
    if package.insight_id and package.insight is not None:
        return package.insight.business_unit_id
    if package.blueprint_id and package.blueprint is not None:
        return blueprint_business_unit_id(package.blueprint)
    if package.sandbox_agent_id and package.sandbox_agent is not None:
        return package.sandbox_agent.org_unit_id
    return None
