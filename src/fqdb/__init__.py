"""FQDB: one package for data, BitTorrent and the local workbench."""

__version__ = "0.1.0"
from fqdb.api import (
    add_source,
    check,
    datasets,
    get_price,
    init,
    register_dataset,
    repair,
    subscribe,
    sync,
    tasks,
    update,
)
from . import catalog, jobs, peers, provider, query, sources, web
from .query import load
from .startup import start_on_import as _start_on_import


def download(
    dataset,
    frequency="1d",
    start_date=None,
    end_date=None,
    *,
    date=None,
    year=None,
    month=None,
    granularity=None,
    allow_daily_fallback=False,
    background=False,
    timeout=300,
):
    if date is not None:
        if start_date is not None or end_date is not None:
            raise ValueError("Use date or start_date/end_date, not both")
        start_date = end_date = date
    return sync(
        dataset,
        frequency,
        start_date,
        end_date,
        year=year,
        month=month,
        granularity=granularity,
        allow_daily_fallback=allow_daily_fallback,
        background=background,
        timeout=timeout,
    )


__all__ = [
    "__version__",
    "init",
    "download",
    "sync",
    "update",
    "load",
    "query",
    "get_price",
    "check",
    "repair",
    "catalog",
    "sources",
    "jobs",
    "peers",
    "provider",
    "web",
    "subscribe",
    "register_dataset",
    "add_source",
    "datasets",
    "tasks",
]

_start_on_import()
del _start_on_import
