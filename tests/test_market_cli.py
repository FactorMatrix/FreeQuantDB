"""Actual fqdb subprocesses exercise strict market publishing and explicit period downloads."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import time

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from fqdb.demo import market_frame, market_schema
from fqdb.query import load
from fqdb.schema.market import arrow_schema, market_fields
from fqdb.service.client import Client
from fqdb.storage.metadata import Store


def cli(root, *args, expected=0):
    result = subprocess.run(
        [sys.executable, "-m", "fqdb", "--data-dir", str(root), *map(str, args)],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == expected, result.stdout + result.stderr
    return json.loads(result.stdout) if expected == 0 else result.stderr


def stop(root):
    client = Client(root)
    if client.endpoint():
        cli(root, "service", "stop")
    deadline = time.monotonic() + 20
    while client.endpoint() and time.monotonic() < deadline:
        time.sleep(0.1)
    assert client.endpoint() is None


def test_cli_market_minute_publish_sidecar_period_fallback_check_and_offline_sdk(data_root):
    root = data_root / "seed"
    days = ["2026-11-13", "2026-11-17"]
    symbols = ["RB2701.SHFE", "I2701.DCE"]
    schema = market_schema("future", days=days, symbols=symbols)
    descriptor = data_root / "SYNTHETIC-market-schema.json"
    descriptor.write_text(json.dumps(schema.to_dict()))
    data = market_frame("future", "1m", days=days, symbols=symbols, night=True)
    source = data_root / "SYNTHETIC-minute.parquet"
    pq.write_table(
        pa.Table.from_pandas(
            data, schema=arrow_schema(schema, "1m"), preserve_index=False
        ).replace_schema_metadata(None),
        source,
        compression="zstd",
    )
    proof = data_root / "SYNTHETIC-source-trading-days.json"
    proof.write_text(json.dumps(data.attrs["trading_dates"]))
    try:
        assert cli(root, "init")["data_dir"] == str(root)
        assert cli(root, "register", descriptor)["id"] == "cn/future/base"
        failure = cli(root, "publish", schema.id, source, "--frequency", "1m", expected=1)
        assert "trading" in failure.lower()
        assert not Store(root).entries(schema.id)
        published = cli(
            root, "publish", schema.id, source, "--frequency", "1m", "--trading-dates-file", proof
        )
        assert len(published["entries"]) == 2
        scope = [schema.id, "--frequency", "1m", "--year", "2026", "--month", "11"]
        missing = cli(root, "plan", *scope, "--granularity", "month")
        assert missing["fallback_required"] and not missing["items"]
        assert cli(root, "sync", *scope, "--granularity", "month")["status"] == "fallback_required"
        automatic = cli(root, "plan", *scope, "--granularity", "auto")
        assert [item["entry"]["period_kind"] for item in automatic["items"]] == ["day", "day"]
        fallback = cli(root, "sync", *scope, "--daily-fallback")
        assert fallback["status"] == "complete" and not fallback["task_ids"]
        assert fallback["plan"]["daily_fallback_selected"]
        month = cli(
            root,
            "publish",
            schema.id,
            "--frequency",
            "1m",
            "--aggregate",
            "--granularity",
            "month",
            "--period",
            "202611",
        )["entries"][0]
        assert month["file"].endswith("/2026/11/202611.parquet")
        planned = cli(root, "plan", *scope)
        assert len(planned["items"]) == 1 and planned["items"][0]["entry"]["period_kind"] == "month"
        assert month["info_hash"] not in {entry["info_hash"] for entry in published["entries"]}
        report = cli(
            root,
            "check",
            schema.id,
            "--frequency",
            "1m",
            "--start-date",
            days[0],
            "--end-date",
            days[-1],
            "--deep",
        )
        assert len(report["files"]) == 3 and all(row["file"] == "verified" for row in report["files"])
        assert all(row["quality"] == "verified" and row["format"] == "verified" for row in report["files"])
        assert report["coverage"]["numerator"] == 2
        stop(root)
        result = load(schema.id, frequency="1m", symbols=symbols, start=days[0], end=days[-1], data_dir=root)
        assert list(result) == list(market_fields("1m")) and len(result) == 4
        assert isinstance(result.index, pd.RangeIndex)
        assert str(result.trading_time.dt.tz) == "Asia/Shanghai"
        assert str(result.trading_time.max().date()) == "2026-11-16"
        assert not Client(root).endpoint()
    finally:
        stop(root)
