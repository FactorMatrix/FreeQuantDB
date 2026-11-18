"""Small synthetic scale measurement; no production performance promise."""

import argparse
from datetime import date, timedelta
import json
import platform
import resource
import time

import libtorrent as lt

from fqdb.config import Config
from test_contracts import sample_frame, sample_schema
from fqdb.publish.publisher import Publisher
from fqdb.storage.metadata import Store
from fqdb.storage.query import get_price


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--days", type=int, default=200)
    args = parser.parse_args()
    root = Config(args.data_dir).ensure().root
    store = Store(root)
    if store.entries():
        raise ValueError("Use a new isolated synthetic root")
    days = [(date(2022, 1, 1) + timedelta(days=n)).isoformat() for n in range(args.days)]
    schema = sample_schema(days=days)
    store.register(schema)
    source = root / ".state/imports/SYNTHETIC-scale.csv"
    sample_frame(days=days).to_csv(source, index=False)
    begin = time.perf_counter()
    result = Publisher(store).publish(schema.id, "1d", source)
    publish_seconds = time.perf_counter() - begin
    begin = time.perf_counter()
    query = get_price(
        store, "000100.SZ", days[-30] if len(days) >= 30 else days[0], days[-1], fields=["close", "volume"]
    )
    query_seconds = time.perf_counter() - begin
    report = {
        "synthetic": True,
        "platform": platform.platform(),
        "python": platform.python_version(),
        "libtorrent": lt.__version__,
        "daily_torrents": len(result["entries"]),
        "rows": sum(e["rows"] for e in result["entries"]),
        "data_bytes": sum(e["size"] for e in result["entries"]),
        "publish_seconds": publish_seconds,
        "query_rows": len(query),
        "query_fields": ["close", "volume"],
        "query_seconds": query_seconds,
        "peak_process_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        "note": "One offline process; includes Python/Arrow/libtorrent imports; OS cache uncontrolled; no throughput guarantee or production multi-market claim.",
    }
    (root / "SYNTHETIC-performance.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report), flush=True)


if __name__ == "__main__":
    main()
