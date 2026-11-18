"""Local adoption must retain valid existing BT identities and reject conflicts."""

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from fqdb.bt.torrents import create_metadata, torrent_cache_path
from fqdb.catalog.magnet import MagnetCatalog
from fqdb.catalog.models import Entry
from fqdb.config import Config
from fqdb.demo import market_frame, market_schema
from fqdb.layout import runtime_path
from fqdb.schema.market import arrow_schema
from fqdb.service.core import Owner


DAY = "2026-11-17"


def local_daily_without_registration(root):
    owner = Owner(Config(root))
    schema = market_schema(days=[DAY], symbols=["000100.SZ"])
    owner.store.register(schema)
    relative = "cn/stock/base/1d/2026/11/20261117"
    data = root / "data" / (relative + ".parquet")
    data.parent.mkdir(parents=True, exist_ok=True)
    frame = market_frame(days=[DAY], symbols=["000100.SZ"])
    pq.write_table(
        pa.Table.from_pandas(frame, schema=arrow_schema(schema, "1d"), preserve_index=False),
        data,
        compression="zstd",
    )
    identity, metadata = create_metadata(data, piece_size=16 * 1024)
    magnet = f"magnet:?xt=urn:btih:{identity}"
    entry = Entry.from_magnet(schema.id, "1d", "20261117", magnet)
    torrent = torrent_cache_path(root, entry)
    torrent.parent.mkdir(parents=True, exist_ok=True)
    torrent.write_bytes(metadata)
    MagnetCatalog(root).write_publication(
        f"{schema.id}/1d",
        {
            "schema_version": 5,
            "name": "Synthetic stock",
            "fields": list(schema.fields_for("1d")),
            "instrument_column": "instrument",
            "time_column": "trading_date",
        },
        "20261117",
        {
            "start_date": DAY,
            "end_date": DAY,
            "rows": 1,
            "instruments": 1,
            "trading_days": 1,
            "size_bytes": data.stat().st_size,
            "magnet": magnet,
        },
    )
    assert owner.store.entries() == []
    return owner, schema, data, torrent, identity


def test_provider_adopts_described_local_file_without_changing_its_swarm(data_root):
    owner, _, data, torrent, identity = local_daily_without_registration(data_root)
    original_torrent, original_data = torrent.read_bytes(), data.read_bytes()
    try:
        result = owner.dispatch("provider_start", {})
        assert result["provider"] is True
        assert result["provision"]["eligible"] == 1
        entry = owner.store.active()[0][0]
        assert entry.info_hash == identity
        assert torrent.read_bytes() == original_torrent
        assert torrent_cache_path(data_root, entry).read_bytes() == original_torrent
        assert data.read_bytes() == original_data
    finally:
        owner.close()


@pytest.mark.parametrize("fault", ["malformed", "conflicting_payload"])
def test_provider_does_not_replace_invalid_or_conflicting_existing_seed(data_root, fault):
    owner, schema, data, torrent, _ = local_daily_without_registration(data_root)
    if fault == "malformed":
        torrent.write_bytes(b"SYNTHETIC invalid torrent metadata")
    else:
        alternate = runtime_path(data_root, "imports") / data.name
        frame = market_frame(days=[DAY], symbols=["000100.SZ"], correction=3)
        pq.write_table(
            pa.Table.from_pandas(frame, schema=arrow_schema(schema, "1d"), preserve_index=False),
            alternate,
            compression="zstd",
        )
        torrent.write_bytes(create_metadata(alternate, piece_size=16 * 1024)[1])
    original_torrent, original_data = torrent.read_bytes(), data.read_bytes()
    try:
        result = owner.dispatch("provider_start", {})
        assert result["provider"] is False
        assert result["provision"]["eligible"] == 0
        assert result["provision"]["status"] == "invalid_data"
        assert owner.store.active() == []
        assert torrent.read_bytes() == original_torrent
        assert data.read_bytes() == original_data
    finally:
        owner.close()
