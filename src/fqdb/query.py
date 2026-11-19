"""Offline dataset-based queries, with explicit alias and unchanged raw-price semantics."""

from __future__ import annotations

from .layout import runtime_path

import json
from fqdb.config import Config
from fqdb.storage.market import get_market_price, local_sparse_dates
from fqdb.storage.metadata import Store
from fqdb.storage.query import MissingDataError, get_price
from fqdb.util import FQDBError, atomic_json, day


def _store(data_dir=None):
    config = Config(data_dir).ensure()
    return Store(config.root, check_available=config.check_available)


def register_alias(name, dataset, frequency, *, data_dir=None):
    if not name or "/" in name:
        raise FQDBError("Alias must be a nonempty name without slashes")
    store = _store(data_dir)
    schema = store.dataset(dataset)
    if frequency not in schema.frequencies:
        raise FQDBError("Alias frequency is not declared by its dataset")
    path = runtime_path(store.root, "state") / "query-aliases.json"
    aliases = _aliases(store)
    aliases[name] = {"dataset": dataset, "frequency": frequency}
    atomic_json(path, aliases)
    return aliases[name]


def list_datasets(*, data_dir=None):
    return [d["id"] for d in _store(data_dir).datasets()]


def dataset_schema(dataset, *, data_dir=None):
    store = _store(data_dir)
    aliases = _aliases(store)
    identity = aliases.get(dataset, {}).get("dataset", dataset)
    return store.dataset(identity).to_dict()


def _aliases(store):
    path = runtime_path(store.root, "state") / "query-aliases.json"
    return json.loads(path.read_text()) if path.exists() else {}


def load(
    dataset,
    *,
    symbols=None,
    start=None,
    end=None,
    fields=None,
    frequency=None,
    data_dir=None,
    adjust="none",
    allow_partial=False,
    start_time=None,
    end_time=None,
):
    """Return the exact ordinary columns of the current market contract.

    Bounds include both trading dates. Symbols are used exactly as provided;
    unknown/missing values are never converted, filled or downloaded implicitly.
    """
    store = _store(data_dir)
    alias = _aliases(store).get(dataset)
    if alias:
        dataset = alias["dataset"]
        if frequency is not None and frequency != alias["frequency"]:
            raise FQDBError("Requested frequency conflicts with registered alias")
        frequency = alias["frequency"]
    schema = store.dataset(dataset)
    frequency = frequency or (
        "1d"
        if "1d" in schema.frequencies
        else schema.frequencies[0]
        if len(schema.frequencies) == 1
        else None
    )
    if frequency is None:
        raise FQDBError("Specify frequency for a dataset with multiple non-daily frequencies")
    active = store.active(dataset, frequency)
    if not active:
        raise MissingDataError("No verified local data; query never downloads automatically")
    if not start or not end:
        available = {d for e, _ in active for d in e.days}
        available.update(local_sparse_dates(store, schema, frequency, active))
        if not available:
            raise MissingDataError("No verified local trading dates; inspect the package explicitly")
        start = day(start) if start else min(available)
        end = day(end) if end else max(available)
    else:
        start, end = day(start), day(end)
    if symbols is None and any(e.sparse and e.start <= end and start <= e.end for e, _ in active):
        return get_market_price(
            store,
            dataset,
            None,
            start,
            end,
            frequency=frequency,
            fields=fields,
            start_time=start_time,
            end_time=end_time,
            adjust_type=adjust,
            allow_partial=allow_partial,
            all_instruments=True,
        )
    if symbols is None:
        symbols = sorted(
            {s for e, _ in active if any(start <= d <= end for d in e.days) for s in e.symbols or ()}
        )
        if not symbols:
            raise MissingDataError("Local metadata declares no symbols; specify symbols explicitly")
    return get_price(
        store,
        symbols,
        start,
        end,
        frequency=frequency,
        fields=fields,
        dataset=dataset,
        start_time=start_time,
        end_time=end_time,
        adjust_type=adjust,
        allow_partial=allow_partial,
    )
