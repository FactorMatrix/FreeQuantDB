from __future__ import annotations

from .config import Config
from .service.client import Client
from .service.subscriptions import DEFAULT_INTERVAL
from .startup import autostart_enabled, select_client
from .storage.metadata import Store
from .storage.query import get_price as query_price


def init(data_dir=None):
    connection, outgoing = select_client(data_dir, persist=True, activate=autostart_enabled())
    config = connection.config
    result = {"data_dir": str(config.root), "provider": config.read()["provider"], "layout": config.layout}
    if outgoing:
        result["previous_backgrounds"] = outgoing
    return result


def client():
    return Client()


def sync(
    dataset,
    frequency,
    start_date=None,
    end_date=None,
    *,
    year=None,
    month=None,
    granularity=None,
    allow_daily_fallback=False,
    background=False,
    timeout=300,
):
    connection = client()
    result = connection.call(
        "sync",
        {
            "dataset": dataset,
            "frequency": frequency,
            "start_date": start_date,
            "end_date": end_date,
            "year": year,
            "month": month,
            "granularity": granularity,
            "allow_daily_fallback": allow_daily_fallback,
        },
    )
    return result if background else connection.wait(result, timeout)


def update(source=None):
    from .catalog import refresh

    return refresh(source)


def get_price(instruments=None, start_date=None, end_date=None, **options):
    # Query local SQLite and Parquet independently of the background service.
    config = Config().ensure()
    with config.pin():
        return query_price(
            Store(config.root, check_available=config.check_available),
            instruments=instruments,
            start_date=start_date,
            end_date=end_date,
            **options,
        )


class Report(dict):
    @property
    def summary(self):
        return self["summary"]


def check(dataset, frequency, **options):
    return Report(client().call("check", {"dataset": dataset, "frequency": frequency, **options}))


def repair(report, dry_run=True, background=False, timeout=300):
    connection = client()
    result = connection.call("repair", {"report": report, "dry_run": dry_run})
    return result if dry_run or background else connection.wait(result, timeout)


def register_dataset(descriptor):
    return client().call("register", descriptor.to_dict() if hasattr(descriptor, "to_dict") else descriptor)


def add_source(source):
    return client().call("source", {"source": str(source)})


def subscribe(dataset=None, frequency=None, *, interval=DEFAULT_INTERVAL, download=False, **options):
    """Save a catalog check or opt into downloads in the shared background."""
    return client().call(
        "subscribe",
        {
            "dataset": dataset,
            "frequency": frequency or ("1d" if dataset else None),
            "interval": interval,
            "download": download,
            **options,
        },
    )


def datasets():
    return client().get("/api/status")["datasets"]


def tasks():
    return client().get("/api/status")["tasks"]
