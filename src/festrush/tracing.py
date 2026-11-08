"""OpenTelemetry tracing, off unless ``FESTRUSH_OTLP_ENDPOINT`` is set.

With it on, every web request, every SQL statement, every call to the payments service and every
outbox event becomes a span, and the trace context travels across the HTTP call to the payments
service, so one trace shows a student's payment from the browser to the provider and back.
Spans go to a collector you run yourself (Jaeger in the observability profile); nothing leaves
the machine and no account or key is involved.
"""

from typing import Any

from opentelemetry import trace

from festrush.config import Settings

tracer = trace.get_tracer("festrush")  # follows the real provider once setup() installs one


def setup(settings: Settings, service_name: str, app: Any | None = None) -> bool:
    """Install tracing for this process. Returns False (and does nothing) when it is not configured."""
    if not settings.otlp_endpoint:
        return False
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.instrumentation.psycopg import PsycopgInstrumentor
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    endpoint = settings.otlp_endpoint.rstrip("/") + "/v1/traces"
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    trace.set_tracer_provider(provider)
    PsycopgInstrumentor().instrument()
    if app is not None:
        FastAPIInstrumentor.instrument_app(app, excluded_urls="healthz,readyz,metrics,static")
    return True
