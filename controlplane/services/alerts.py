"""
Operational alert delivery (OE-3) + in-app notifications (UX-2).

One fan-out entrypoint, three sinks — all fail-silent so alerting can never
break the caller (budget computation, baseline drift, dead-letter parking):

  1. In-app ``Notification`` rows for every active platform_admin.
  2. Email to ``ALERT_EMAIL_RECIPIENTS`` (console backend when SMTP is unset).
  3. Webhook POST (Teams/Slack-compatible ``{"text": ...}``) to ``ALERT_WEBHOOK_URL``.
"""
from __future__ import annotations

import json
import logging
import urllib.request

from django.conf import settings

logger = logging.getLogger(__name__)


def send_alert(title: str, body: str, *, category: str = "ops", link: str = "") -> None:
    _notify_admins(title, body, category, link)
    _send_email(title, body)
    _post_webhook(title, body)


def _notify_admins(title: str, body: str, category: str, link: str) -> None:
    try:
        from django.contrib.auth.models import User

        from controlplane.models import Notification

        admins = User.objects.filter(is_active=True).filter(
            models_q_admin()
        ).distinct()
        Notification.objects.bulk_create([
            Notification(user=u, category=category, title=title[:200], body=body, link=link)
            for u in admins
        ])
    except Exception as exc:
        logger.warning("alert: in-app notification failed: %s", exc)


def models_q_admin():
    """Q object matching platform admins via either role path (staff, group, profile)."""
    from django.db.models import Q

    return (
        Q(is_staff=True)
        | Q(is_superuser=True)
        | Q(groups__name="platform_admin")
        | Q(profile__role="platform_admin")
    )


def _send_email(title: str, body: str) -> None:
    recipients = getattr(settings, "ALERT_EMAIL_RECIPIENTS", [])
    if not recipients:
        return
    try:
        from django.core.mail import send_mail

        send_mail(
            subject=f"[Agentic Platform] {title}",
            message=body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=recipients,
            fail_silently=True,
        )
    except Exception as exc:
        logger.warning("alert: email delivery failed: %s", exc)


def _post_webhook(title: str, body: str) -> None:
    url = getattr(settings, "ALERT_WEBHOOK_URL", "")
    if not url:
        return
    try:
        req = urllib.request.Request(
            url,
            data=json.dumps({"text": f"**{title}**\n{body}"}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=10).read()
    except Exception as exc:
        logger.warning("alert: webhook delivery failed: %s", exc)
