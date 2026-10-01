"""Which ingesters exist, and how to build one.

Keeping construction here means a runner never imports a source module
directly, so adding a source touches this file and nothing else.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

from cairn.ingest.base import Fetcher, Ingester
from cairn.ingest.merges import MergesIngester
from cairn.ingest.migration import MigrationIngester

IngesterFactory = Callable[[Fetcher, str], Ingester]

_REGISTRY: dict[str, IngesterFactory] = {}


def register(name: str, factory: IngesterFactory) -> None:
    if name in _REGISTRY:
        raise ValueError(f"duplicate ingester {name}")
    _REGISTRY[name] = factory


def available() -> Mapping[str, IngesterFactory]:
    return dict(_REGISTRY)


def build(name: str, fetcher: Fetcher, series: str) -> Ingester:
    try:
        factory = _REGISTRY[name]
    except KeyError:
        known = ", ".join(sorted(_REGISTRY)) or "none"
        raise KeyError(f"unknown ingester {name!r}; known: {known}") from None
    return factory(fetcher, series)


register(
    MergesIngester.name,
    lambda fetcher, series: MergesIngester(fetcher, series=series),
)
register(
    MigrationIngester.name,
    lambda fetcher, series: MigrationIngester(fetcher, series=series),
)
