"""Explicit synthetic six-dataset example, requiring a new independent root.

python examples/synthetic_local.py -d /path/to/SYNTHETIC-demo
No real source request, public announce or remote publication.
"""

import argparse
import json
import os
import shlex
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

os.environ["FQDB_AUTOSTART"] = "0"

from fqdb.demo import market_frame, market_schema
from fqdb.layout import runtime_path
from fqdb.schema.market import arrow_schema
from fqdb.service.client import Client

parser = argparse.ArgumentParser()
parser.add_argument("-d", "--data-dir", type=Path, required=True)
args = parser.parse_args()
root = args.data_dir.expanduser().resolve()
if root.exists() and any(root.iterdir()):
    raise SystemExit("Use a new empty SYNTHETIC data root; existing data is never overwritten")
client = Client(root)
client.config.ensure().update(listen="127.0.0.1:0", discovery=False)
days = ["2026-11-13", "2026-11-17"]
outputs = []
for asset in ("stock", "etf", "future"):
    schema = market_schema(asset, days=days)
    client.call("register", schema.to_dict())
    for frequency in ("1d", "1m"):
        data = market_frame(asset, frequency, days=days, night=asset == "future")
        path = runtime_path(root, "imports") / ("SYNTHETIC-" + asset + "-" + frequency + ".parquet")
        pq.write_table(
            pa.Table.from_pandas(data, schema=arrow_schema(schema, frequency), preserve_index=False),
            path,
            compression="zstd",
        )
        try:
            scope = {"dataset": schema.id, "frequency": frequency}
            result = client.call(
                "publish",
                scope
                | {
                    "input_path": str(path),
                    **({"trading_dates": data.attrs["trading_dates"]} if frequency == "1m" else {}),
                },
            )
            outputs.extend(result["entries"])
            for granularity, period in (("month", "202611"), ("year", "2026")):
                outputs.extend(
                    client.call("aggregate", scope | {"granularity": granularity, "period": period})[
                        "entries"
                    ]
                )
        finally:
            path.unlink(missing_ok=True)
print(
    json.dumps(
        {
            "SYNTHETIC_ONLY": True,
            "packages": len(outputs),
            "provider_enabled": False,
            "external_publication": False,
            "gateway_command": shlex.join(["python", "-m", "fqdb", "gateway", "-d", str(root)]),
        },
        indent=2,
    )
)
