"""
Create an API key for /api/v1 access (IN-1).

The plaintext key is printed exactly once; only its SHA-256 hash is stored.

    python manage.py create_api_key <username> --name ci-pipeline
"""
import hashlib
import secrets

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Create an X-API-Key credential for a user (plaintext shown once)."

    def add_arguments(self, parser):
        parser.add_argument("username")
        parser.add_argument("--name", default="default", help="Label for the key.")

    def handle(self, *args, **opts):
        from controlplane.models import ApiKey, AuditLog

        try:
            user = User.objects.get(username=opts["username"])
        except User.DoesNotExist:
            raise CommandError(f"Unknown user {opts['username']!r}")

        raw = "ap_" + secrets.token_urlsafe(32)
        ApiKey.objects.create(
            user=user,
            name=opts["name"],
            key_hash=hashlib.sha256(raw.encode()).hexdigest(),
        )
        AuditLog.objects.create(
            actor=f"system:create_api_key",
            action="api_key.created",
            resource_type="ApiKey",
            resource_id=opts["name"],
            payload={"user": user.username},
        )
        self.stdout.write(self.style.SUCCESS(f"API key for {user.username} ({opts['name']}):"))
        self.stdout.write(raw)
        self.stdout.write("Store it now — it cannot be recovered later.")
