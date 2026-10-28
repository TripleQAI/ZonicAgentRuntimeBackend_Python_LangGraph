"""Gauges for autoscaling (design 11.2, "Metrics for scaling"; served on the internal port).

``RuntimeMetrics`` reads its gauges when Prometheus scrapes ``/metrics``, so nothing is counted
on the run path. Each gauge is a reader: a number, or numbers by one label (the governor queue
depth per model). The composition root (``api/deps.py``) names the readers, so this module
depends on nothing it measures.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass

from prometheus_client import CollectorRegistry, generate_latest
from prometheus_client.core import GaugeMetricFamily, Metric
from prometheus_client.exposition import CONTENT_TYPE_LATEST
from prometheus_client.registry import Collector

CONTENT_TYPE = CONTENT_TYPE_LATEST
PREFIX = "zonic_"


@dataclass(frozen=True)
class Gauge:
    name: str
    help: str
    read: Callable[[], float | Mapping[str, float]]
    #: The label of a gauge whose reader returns numbers by label value.
    label: str | None = None


class _Collector(Collector):
    def __init__(self, gauges: Sequence[Gauge]) -> None:
        self._gauges = tuple(gauges)

    def collect(self) -> Iterator[Metric]:
        for gauge in self._gauges:
            value = gauge.read()
            if isinstance(value, Mapping):
                family = GaugeMetricFamily(PREFIX + gauge.name, gauge.help, labels=[gauge.label or "key"])
                for key, number in sorted(value.items()):
                    family.add_metric([key], float(number))
            else:
                family = GaugeMetricFamily(PREFIX + gauge.name, gauge.help, value=float(value))
            yield family


class RuntimeMetrics:
    def __init__(self, gauges: Sequence[Gauge]) -> None:
        self._registry = CollectorRegistry(auto_describe=False)
        self._registry.register(_Collector(gauges))

    def render(self) -> bytes:
        return generate_latest(self._registry)
