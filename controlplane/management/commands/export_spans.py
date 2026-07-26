"""
Export unexported OtelSpan rows to an OTLP/HTTP collector (SC-2).

POSTs OTLP JSON to ``$OTEL_EXPORTER_OTLP_ENDPOINT/v1/traces`` and marks rows
exported. No-op (with a note) when the endpoint is unset, so it is safe on the
beat schedule everywhere.
"""
import json
import urllib.request

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

_KIND = {"INTERNAL": 1, "SERVER": 2, "CLIENT": 3, "PRODUCER": 4, "CONSUMER": 5}
_STATUS = {"OK": 1, "ERROR": 2}  # UNSET → 0


def _nanos(dt) -> str:
    return str(int(dt.timestamp() * 1_000_000_000)) if dt else "0"


def _attrs(d: dict) -> list:
    return [{"key": k, "value": {"stringValue": str(v)}} for k, v in (d or {}).items()]


class Command(BaseCommand):
    help = "Export unexported OtelSpans to the configured OTLP/HTTP collector."

    def add_arguments(self, parser):
        parser.add_argument("--batch", type=int, default=500)

    def handle(self, *args, **opts):
        from controlplane.models import OtelSpan

        endpoint = getattr(settings, "OTEL_EXPORTER_OTLP_ENDPOINT", "")
        if not endpoint:
            self.stdout.write("OTEL_EXPORTER_OTLP_ENDPOINT unset — nothing to do.")
            return

        spans = list(OtelSpan.objects.filter(exported=False).order_by("created_at")[: opts["batch"]])
        if not spans:
            self.stdout.write("No unexported spans.")
            return

        payload = {
            "resourceSpans": [{
                "resource": {"attributes": _attrs({"service.name": settings.OTEL_SERVICE_NAME})},
                "scopeSpans": [{
                    "scope": {"name": "controlplane.telemetry"},
                    "spans": [{
                        "traceId": s.trace_id,
                        "spanId": s.span_id,
                        "parentSpanId": s.parent_span_id or "",
                        "name": s.name,
                        "kind": _KIND.get(s.kind, 1),
                        "startTimeUnixNano": _nanos(s.start_time),
                        "endTimeUnixNano": _nanos(s.end_time or s.start_time),
                        "status": {"code": _STATUS.get(s.status_code, 0), "message": s.status_message or ""},
                        "attributes": _attrs(s.attributes),
                    } for s in spans],
                }],
            }]
        }

        headers = {"Content-Type": "application/json"}
        for pair in (settings.OTEL_EXPORTER_OTLP_HEADERS or "").split(","):
            if "=" in pair:
                k, v = pair.split("=", 1)
                headers[k.strip()] = v.strip()

        req = urllib.request.Request(
            endpoint.rstrip("/") + "/v1/traces",
            data=json.dumps(payload).encode(),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            if resp.status >= 300:
                raise RuntimeError(f"OTLP collector returned HTTP {resp.status}")

        OtelSpan.objects.filter(id__in=[s.id for s in spans]).update(exported=True)
        self.stdout.write(self.style.SUCCESS(f"Exported {len(spans)} spans at {timezone.now().isoformat()}"))
