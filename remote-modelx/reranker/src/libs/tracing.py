"""Shared OpenTelemetry setup.

OTEL_EXPORTER_OTLP_ENDPOINT is read here, once. Unset (the default unless a
collector is wired up) means configure_tracing() is a no-op --
tracing/metrics/logs-export are optional observability, not a functional
dependency, so the service must still start fine without a collector to
send to.
"""

from __future__ import annotations

import logging
import os

from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

OTEL_EXPORTER_OTLP_ENDPOINT = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "")

_configured = False


def configure_tracing(service_name: str) -> None:
    """Wires up trace/metric/log export to OTEL_EXPORTER_OTLP_ENDPOINT
    (an otel-collector's OTLP gRPC port) and instruments every httpx client
    in this process so outbound requests carry W3C trace-context headers
    and get their own child spans for free.

    Also attaches an OTLP LoggingHandler to the root logger -- every
    existing logger.info/.error call (via logging_config) then exports to
    Loki too, tagged with the active span's trace_id/span_id automatically,
    with no call-site changes needed.

    A no-op if OTEL_EXPORTER_OTLP_ENDPOINT is unset. Safe to call more than
    once (e.g. accidentally from a reloaded module) -- only the first call
    does anything.
    """
    global _configured
    if _configured or not OTEL_EXPORTER_OTLP_ENDPOINT:
        return
    _configured = True

    resource = Resource.create({"service.name": service_name})

    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=OTEL_EXPORTER_OTLP_ENDPOINT, insecure=True))
    )
    trace.set_tracer_provider(tracer_provider)

    reader = PeriodicExportingMetricReader(
        OTLPMetricExporter(endpoint=OTEL_EXPORTER_OTLP_ENDPOINT, insecure=True)
    )
    metrics.set_meter_provider(MeterProvider(resource=resource, metric_readers=[reader]))

    logger_provider = LoggerProvider(resource=resource)
    logger_provider.add_log_record_processor(
        BatchLogRecordProcessor(OTLPLogExporter(endpoint=OTEL_EXPORTER_OTLP_ENDPOINT, insecure=True))
    )
    logging.getLogger().addHandler(LoggingHandler(logger_provider=logger_provider))

    # Optional: this service has no outbound httpx calls, so it doesn't
    # install opentelemetry-instrumentation-httpx -- this is a no-op here,
    # kept only because it's shared code with services that do.
    try:
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    except ImportError:
        pass
    else:
        HTTPXClientInstrumentor().instrument()


def get_tracer(name: str) -> trace.Tracer:
    """Safe to call even when configure_tracing() was never called (or
    no-opped) -- returns a real no-op tracer whose spans cost effectively
    nothing, so callers don't need to branch on whether tracing is on."""
    return trace.get_tracer(name)


def get_meter(name: str) -> metrics.Meter:
    return metrics.get_meter(name)
