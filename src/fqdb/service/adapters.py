"""Configure a source adapter and publish validated results into the data root."""

import time

import pyarrow as pa
import pyarrow.parquet as pq

from ..adapters import FetchRequest, adapter, describe_adapters
from ..layout import runtime_path
from ..util import FQDBError, day


class UpstreamSources:
    def __init__(self, owner):
        self.owner, self.store, self.config = owner, owner.store, owner.config

    def source_list(self):
        rows = []
        for item in self.config.read().get("source_adapters", []):
            try:
                info = adapter(item["adapter"], item.get("settings", {})).availability()
            except Exception as exc:
                info = {"status": "unavailable", "reason": str(exc)}
            rows.append(item | {"availability": info})
        return {"available": describe_adapters(), "sources": rows}

    def fetch(self, args, progress=None):
        source = next(
            (r for r in self.config.read().get("source_adapters", []) if r["id"] == args["source"]), None
        )
        if source is None:
            raise FQDBError("Unknown upstream source")
        schema = self.store.dataset(args["dataset"])
        if args["frequency"] not in schema.frequencies:
            raise FQDBError("Unsupported source frequency")
        if int(args.get("version", 1)) < 1:
            raise FQDBError("Version must be positive")
        request = FetchRequest(
            args["dataset"],
            args["frequency"],
            day(args["start_date"]),
            day(args["end_date"]),
            tuple(args.get("symbols", [])),
        )
        provider = adapter(source["adapter"], source.get("settings", {}))
        if args.get("dry_run", True):
            return {
                "dry_run": True,
                "source": source["id"],
                "request": request.to_dict(),
                "availability": provider.availability(),
                "network_performed": False,
                "external_publication": False,
            }
        if provider.real_network and not args.get("authorize_real"):
            raise FQDBError("Real upstream access needs explicit authorize_real for this scoped request")
        version = int(args.get("version", 1))
        expected = [
            d
            for d in schema.calendars.get(request.frequency, [])
            if request.start_date <= d <= request.end_date
        ]
        data = provider.fetch(request, schema.for_frequency(request.frequency))
        if progress:
            progress(1, 3, "upstream records fetched; checking explicit source dates")
        if data.empty:
            return {
                "published": [],
                "source_unavailable": expected or [request.start_date, request.end_date],
                "source": source["id"],
                "external_publication": False,
            }
        source_days = data.attrs.get("trading_dates") if request.frequency == "1m" else None
        if source_days is not None:
            if len(source_days) != len(data):
                raise FQDBError("Source trading-date metadata must match every minute record")
            source_days = [day(d) for d in source_days]
        elif "trading_date" in data:
            source_days = data.trading_date.astype(str).tolist()
        else:
            raise FQDBError("Adapter did not provide reliable external trading dates")
        actual_dates = set(source_days)
        if any(d < request.start_date or d > request.end_date for d in actual_dates):
            raise FQDBError("Adapter returned trading dates outside the authorized request")
        identity_column = "instrument"
        if request.symbols and set(data[identity_column]) - set(request.symbols):
            raise FQDBError("Adapter returned symbols outside the authorized request")
        unavailable = sorted(set(expected) - actual_dates)
        directory = runtime_path(self.store.root, "staging")
        path = directory / ("upstream-" + str(time.time_ns()) + ".parquet")
        from ..schema.market import arrow_schema

        table = pa.Table.from_pandas(
            data,
            schema=arrow_schema(schema, request.frequency),
            preserve_index=False,
        )
        try:
            pq.write_table(table, path, compression="zstd")
            with self.owner.storage_lock:
                result = self.owner.publisher.publish(
                    request.dataset,
                    request.frequency,
                    path,
                    version,
                    progress=progress,
                    **({"trading_dates": source_days} if request.frequency == "1m" else {}),
                )
        finally:
            path.unlink(missing_ok=True)
        if progress:
            progress(3, 3, "immutable daily versions validated and registered locally")
        return {
            "published": result["entries"],
            "source_unavailable": unavailable,
            "synthetic": schema.synthetic,
            "source": source["id"],
            "external_publication": False,
        }

    def configure(self, args):
        identity = str(args["id"])
        if not identity or len(identity) > 100:
            raise FQDBError("Adapter id must be 1..100 characters")
        settings = args.get("settings", {})
        forbidden = {
            k
            for k in settings
            if any(s in k.lower() for s in ("password", "secret", "token", "credential", "username"))
            and not k.endswith("_env")
        }
        if forbidden:
            raise FQDBError("Configure credential environment-variable names, never credential values")
        provider = adapter(args["adapter"], settings)
        if provider.real_network and settings.get("allow_real_fetch") and not args.get("authorize_real"):
            raise FQDBError("Enabling a real upstream requires explicit authorize_real=True")
        item = {"id": identity, "adapter": args["adapter"], "settings": settings}
        rows = [r for r in self.config.read().get("source_adapters", []) if r["id"] != identity] + [item]
        self.config.update(source_adapters=rows)
        return {"source": item, "availability": provider.availability()}

    def fetch_action(self, args):
        if args.get("background") and not args.get("dry_run", True):
            return self.owner.jobs.submit("source_fetch", args | {"background": False})
        return self.fetch(args)
