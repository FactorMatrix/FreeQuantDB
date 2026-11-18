"""Six market formats through actual loopback BT, with independent period packages."""

from __future__ import annotations

import time

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from fqdb.bt.torrents import torrent_cache_path
from fqdb.catalog.models import Entry
from fqdb.demo import market_frame, market_schema
from fqdb.query import load
from fqdb.schema.market import arrow_schema, market_fields
from fqdb.service.client import Client
from fqdb.storage.metadata import Store
from fqdb.util import sha256
from magnet_helpers import mirror


def until(fn, timeout=45):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = fn()
        if result:
            return result
        time.sleep(0.1)
    raise AssertionError("SYNTHETIC market BT state did not converge")


def test_six_formats_independent_day_month_year_bt_and_offline_queries(data_root):
    source, daily_receiver, period_receiver = [
        Client(data_root / name) for name in ("source", "daily", "period")
    ]
    clients = (source, daily_receiver, period_receiver)
    days = ["2026-11-13", "2026-11-17"]
    scopes = []
    originals = {}
    try:
        for client in clients:
            client.config.update(peer_timeout=15, max_retries=0, max_active=6)
            client.ensure()
        for asset, target in (
            ("stock", "000100.SZ"),
            ("etf", "510300.SH"),
            ("future", "RB2701.SHFE"),
        ):
            schema = market_schema(asset, days=days, symbols=[target])
            source.call("register", schema.to_dict())
            for frequency in ("1d", "1m"):
                scope = {"dataset": schema.id, "frequency": frequency}
                scopes.append(scope | {"instrument": target})
                synthetic = market_frame(
                    asset, frequency, days=days, symbols=[target], night=asset == "future"
                )
                path = source.config.root / ".state/imports" / f"SYNTHETIC-{asset}-{frequency}.parquet"
                pq.write_table(
                    pa.Table.from_pandas(
                        synthetic, schema=arrow_schema(schema, frequency), preserve_index=False
                    ),
                    path,
                    compression="zstd",
                )
                published = source.call(
                    "publish",
                    scope | {"input_path": str(path), "trading_dates": synthetic.attrs["trading_dates"]},
                )
                assert len(published["entries"]) == 2
                month = source.call("aggregate", scope | {"granularity": "month", "period": "202611"})[
                    "entries"
                ][0]
                year = source.call("aggregate", scope | {"granularity": "year", "period": "2026"})["entries"][
                    0
                ]
                first = published["entries"][0]
                assert len({first["info_hash"], month["info_hash"], year["info_hash"]}) == 3
                assert first["file"].endswith("/2026/11/20261113.parquet")
                assert month["file"].endswith("/2026/11/202611.parquet")
                assert year["file"].endswith("/2026/2026.parquet")
                for entry in [*published["entries"], month, year]:
                    physical = source.config.root / entry["file"]
                    assert pq.read_schema(physical).remove_metadata() == arrow_schema(schema, frequency)
                    assert list(pq.read_table(physical).column_names) == list(market_fields(frequency))
                    cached = torrent_cache_path(source.config.root, Entry(**entry))
                    assert cached.is_file() and cached.suffix == ".btmeta"
                originals[(schema.id, frequency)] = {"day": first, "month": month, "year": year}
        source.call("provider_start", {"datasets": [s["dataset"] + "/" + s["frequency"] for s in scopes]})
        until(lambda: len(source.get("/api/status")["transfers"]) == 24)
        source_port = source.get("/api/status")["listen_port"]
        for receiver in (daily_receiver, period_receiver):
            receiver.call("source", {"source": str(mirror(source.config.root))})
            receiver.call("peers_add", {"ip": "127.0.0.1", "port": source_port})
        daily_ids = []
        for scope in scopes:
            selected = {k: scope[k] for k in ("dataset", "frequency")}
            planned = daily_receiver.call(
                "plan", selected | {"start_date": days[0], "end_date": days[0], "granularity": "day"}
            )
            assert len(planned["items"]) == 1 and planned["items"][0]["entry"]["period_kind"] == "day"
            daily_ids.extend(
                daily_receiver.call(
                    "sync", selected | {"start_date": days[0], "end_date": days[0], "granularity": "day"}
                )["task_ids"]
            )
        received = daily_receiver.wait({"task_ids": daily_ids}, timeout=60)
        assert received["status"] == "complete"
        assert len(received["tasks"]) == 6 and all(t["metrics"]["downloaded"] > 0 for t in received["tasks"])
        for scope in scopes:
            expected = originals[(scope["dataset"], scope["frequency"])]["day"]
            assert sha256(daily_receiver.config.root / expected["file"]) == expected["sha256"]
        period_ids = []
        future_scope = {"dataset": "cn/future/base", "frequency": "1m"}
        for kind, extra in (("month", {"year": 2026, "month": 11}), ("year", {"year": 2026})):
            plan = period_receiver.call("plan", future_scope | extra | {"granularity": kind})
            assert len(plan["items"]) == 1 and plan["items"][0]["entry"]["period_kind"] == kind
            period_ids.extend(
                period_receiver.call("sync", future_scope | extra | {"granularity": kind})["task_ids"]
            )
        got_periods = period_receiver.wait({"task_ids": period_ids}, timeout=60)
        assert got_periods["status"] == "complete" and len(got_periods["tasks"]) == 2
        assert all(t["metrics"]["downloaded"] > 0 for t in got_periods["tasks"])
        local = Store(period_receiver.config.root).active()
        assert {e.period_kind for e, _ in local} == {"month", "year"}
        assert not list((period_receiver.config.root / "data/cn/future/base/1m").rglob("20261113.parquet"))
        # Query after all services are stopped to verify offline behavior.
        for client in clients:
            client.call("shutdown")
        for client in clients:
            until(lambda c=client: c.endpoint() is None, 20)
        for scope in scopes:
            frame = load(
                scope["dataset"],
                frequency=scope["frequency"],
                symbols=[scope["instrument"]],
                start=days[0],
                end=days[0],
                data_dir=daily_receiver.config.root,
            )
            assert list(frame) == list(market_fields(scope["frequency"]))
            assert isinstance(frame.index, pd.RangeIndex) and len(frame) == 1
        night = load(
            "cn/future/base",
            frequency="1m",
            symbols=["RB2701.SHFE"],
            start=days[-1],
            end=days[-1],
            data_dir=period_receiver.config.root,
        )
        assert len(night) == 1 and str(night.trading_time.iloc[0].date()) == "2026-11-16"
        assert "trading_date" not in night and "prev_close" not in night
        assert set(night.attrs["selected_sources"]) == {days[-1]}
        overlap = load(
            "cn/future/base",
            frequency="1m",
            symbols=["RB2701.SHFE"],
            start=days[0],
            end=days[-1],
            data_dir=period_receiver.config.root,
        )
        assert len(overlap) == 2 and not overlap.duplicated(["instrument", "trading_time"]).any()
        assert not any(client.endpoint() for client in clients)
    finally:
        for client in clients:
            if client.endpoint():
                client.call("shutdown")
        for client in clients:
            until(lambda c=client: c.endpoint() is None, 20)
