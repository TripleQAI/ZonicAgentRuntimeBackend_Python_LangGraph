"""``GatewayTracing``: the production tracer (design 12; plan P2-08).

``LangfuseTracer`` exporting through ``GatewaySpanExporter`` to ``sp_obs_ingest``, when
``observability.enabled`` and ``observability.exporters`` names ``gateway``; with ``langfuse`` too,
the same spans also go to a Langfuse server (``langfuse_exporter``, P3-11, case O-08). ``start`` binds the
exporter's gateway calls to the app's event loop; ``aclose`` flushes what is queued (within
``export.shutdownFlushTimeoutMilliseconds``) from a worker thread, before the gateway closes.
"""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass

from opentelemetry.sdk.trace.export import SpanExporter

from zonic_runtime.config.settings import RuntimeResources
from zonic_runtime.errors import ZonicError
from zonic_runtime.integrations.gateway.repository import GatewayRepository
from zonic_runtime.integrations.langfuse.export import FanOutExporter
from zonic_runtime.integrations.langfuse.gateway_exporter import GatewayIngest, GatewaySpanExporter
from zonic_runtime.integrations.langfuse.provider import LangfuseTracer
from zonic_runtime.integrations.langfuse.record_mapper import LangfuseRecordMapper
from zonic_runtime.ports.gateway import GatewayPort

GATEWAY_EXPORTER, LANGFUSE_EXPORTER = "gateway", "langfuse"
_OTLP_PATH = "/api/public/otel/v1/traces"
_INGESTION_VERSION = "x-langfuse-ingestion-version"


def langfuse_exporter(base_url: str, public_key: str, secret_key: str) -> SpanExporter:
    """The Langfuse server's OTLP endpoint, authenticated with the project's key pair. The
    ingestion-version header makes Langfuse 4 show the spans at once instead of minutes later."""
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    token = base64.b64encode(f"{public_key}:{secret_key}".encode()).decode()
    return OTLPSpanExporter(
        endpoint=base_url.rstrip("/") + _OTLP_PATH,
        headers={"Authorization": f"Basic {token}", _INGESTION_VERSION: "4"},
    )


@dataclass
class GatewayTracing:
    tracer: LangfuseTracer
    exporter: GatewaySpanExporter
    ingest: GatewayIngest

    @classmethod
    def create(
        cls,
        resources: RuntimeResources,
        gateway: GatewayPort,
        *,
        release: str = "dev",
        langfuse: SpanExporter | None = None,
    ) -> GatewayTracing | None:
        """``langfuse``: the Langfuse server exporter, required when the exporters name it."""
        settings = resources.settings
        observability = settings.observability
        if not observability.enabled:
            return None
        exporters = set(observability.exporters)
        if exporters - {GATEWAY_EXPORTER, LANGFUSE_EXPORTER}:
            raise ZonicError("RESOURCE_INVALID", reason="observability.exporters: gateway and langfuse only")
        if LANGFUSE_EXPORTER in exporters and (GATEWAY_EXPORTER not in exporters or langfuse is None):
            raise ZonicError(
                "RESOURCE_INVALID",
                reason="observability.exporters: langfuse runs beside gateway, with LANGFUSE_* configured",
            )
        if GATEWAY_EXPORTER not in exporters:
            return None
        call = settings.gateway
        timeout = call.timeout_milliseconds / 1000 * (call.read_retries + 1)
        ingest = GatewayIngest(GatewayRepository(gateway), timeout)
        exporter = GatewaySpanExporter(
            ingest, LangfuseRecordMapper(resources.observability), observability.export
        )
        both: SpanExporter = FanOutExporter(exporter, langfuse) if langfuse is not None else exporter
        tracer = LangfuseTracer(
            observability, resources.observability, both, release=release, prices=resources.models
        )
        return cls(tracer=tracer, exporter=exporter, ingest=ingest)

    def start(self) -> None:
        """In the running loop."""
        self.ingest.bind(asyncio.get_running_loop())

    async def flush(self) -> None:
        await asyncio.to_thread(self.tracer.flush)

    async def aclose(self) -> None:
        await asyncio.to_thread(self.tracer.shutdown)

    @property
    def healthy(self) -> bool:
        return self.exporter.healthy
