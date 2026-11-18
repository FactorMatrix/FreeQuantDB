"""Optional upstream inputs; these are separate from BT catalog sources."""

from fqdb import api


def configure(identity, adapter, settings=None, *, authorize_real=False):
    return api.client().call(
        "source_configure",
        {"id": identity, "adapter": adapter, "settings": settings or {}, "authorize_real": authorize_real},
    )


def list():
    return api.client().call("source_list")


def fetch(
    source,
    dataset,
    frequency="1d",
    start_date=None,
    end_date=None,
    *,
    symbols=None,
    version=1,
    dry_run=True,
    background=False,
    authorize_real=False,
):
    return api.client().call(
        "source_fetch",
        {
            "source": source,
            "dataset": dataset,
            "frequency": frequency,
            "start_date": start_date,
            "end_date": end_date,
            "symbols": [symbols] if isinstance(symbols, str) else symbols or [],
            "version": version,
            "dry_run": dry_run,
            "background": background,
            "authorize_real": authorize_real,
        },
    )
