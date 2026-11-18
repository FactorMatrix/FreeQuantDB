from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

import pandas as pd

from ..util import FQDBError, day


@dataclass(frozen=True)
class FetchRequest:
    dataset: str
    frequency: str
    start_date: str
    end_date: str
    symbols: tuple[str, ...] = ()

    def __post_init__(self):
        if day(self.start_date) > day(self.end_date):
            raise FQDBError("Start date must not exceed end date")

    def to_dict(self):
        return asdict(self)


class Adapter(Protocol):
    real_network: bool

    def availability(self) -> dict: ...
    def fetch(self, request: FetchRequest, schema) -> pd.DataFrame: ...


_REGISTRY = {}


def register_adapter(name, factory):
    """Register installed trusted code in the service process, never execute JSON code."""
    if not name or not callable(factory):
        raise FQDBError("Adapter needs a name and callable factory")
    _REGISTRY[name] = factory


def adapter(name, settings=None):
    from .local import LocalFiles, Synthetic

    factories = {"local": LocalFiles, "synthetic": Synthetic} | _REGISTRY
    if name not in factories:
        raise FQDBError(f"Unknown upstream adapter: {name}")
    return factories[name](settings or {})


def describe_adapters():
    return [
        {"name": name, "real_network": False, "optional": False}
        for name in ("local", "synthetic", *_REGISTRY)
    ]


def source_path(settings):
    path = Path(settings.get("path", "")).expanduser().resolve()
    if not settings.get("path") or not path.is_file():
        raise FQDBError("Local adapter needs an existing CSV or Parquet file")
    return path
