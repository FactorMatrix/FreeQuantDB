"""Write indexed trading-day groups with bounded publication memory."""

from contextlib import closing
from dataclasses import dataclass, field
import sqlite3

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from ..schema.digest import logical_digest
from ..schema.market import arrow_schema
from ..schema.trading_days import MINUTE_DAY_INDEX, encode_minute_day_groups
from ..util import FQDBError


_SPOOL_SCHEMA = """
    PRAGMA journal_mode=OFF;
    PRAGMA synchronous=OFF;
    PRAGMA temp_store=FILE;
    PRAGMA cache_size=-8192;
    CREATE TABLE parts(instrument TEXT, source_day TEXT, first_key INTEGER,
        last_key INTEGER, payload BLOB NOT NULL, nrows INTEGER NOT NULL,
        PRIMARY KEY(instrument,source_day)) WITHOUT ROWID;
    CREATE INDEX parts_order ON parts(instrument,first_key,source_day);
    CREATE TABLE unique_keys(instrument TEXT, stamp INTEGER, source_day TEXT,
        PRIMARY KEY(instrument,stamp)) WITHOUT ROWID;
"""
_ROW_GROUP_SIZE = 1024 * 1024


@dataclass
class PackageContent:
    groups: dict[str, list[int]] = field(default_factory=dict)
    digests: dict[str, str] = field(default_factory=dict)
    counts: dict[str, dict[str, int]] = field(default_factory=dict)
    symbols: set[str] = field(default_factory=set)
    rows: int = 0


class ParquetSpool:
    def __init__(self, schema):
        self.schema = schema
        self.physical = arrow_schema(schema)
        self.time_column = schema.primary_key[1]
        self.daily = self.time_column == "trading_date"

    def write(self, path, frames, progress=None):
        content = PackageContent()
        if self.daily:
            self._write_daily(path, frames, content, progress)
        else:
            self._write_minute(path, frames, content, progress)
        if not content.groups:
            raise FQDBError("No market records to publish")
        if not pq.read_schema(path).remove_metadata().equals(self.physical):
            raise FQDBError("Written Parquet does not match the exact market contract")
        return content

    def _record_day(self, content, trading_day, data):
        content.digests[trading_day] = logical_digest(data, self.schema)
        content.counts[trading_day] = data.instrument.value_counts().to_dict()
        content.symbols.update(content.counts[trading_day])
        content.rows += len(data)

    def _write_daily(self, path, frames, content, progress):
        index = 0
        previous_day = None
        with pq.ParquetWriter(path, self.physical, compression="zstd") as writer:
            for trading_day, data in frames:
                if previous_day is not None and trading_day <= previous_day:
                    raise FQDBError("Daily input must contain each trading day once in date order")
                table = self._table(data)
                writer.write_table(table, row_group_size=_ROW_GROUP_SIZE)
                group_count = (len(data) + _ROW_GROUP_SIZE - 1) // _ROW_GROUP_SIZE
                content.groups[trading_day] = list(range(index, index + group_count))
                self._record_day(content, trading_day, data)
                index += group_count
                previous_day = trading_day
                if progress:
                    progress(len(content.groups), None, f"validated {trading_day}")

    def _write_minute(self, path, frames, content, progress):
        with closing(sqlite3.connect(path.with_name("market-order.sqlite3"))) as spool:
            spool.executescript(_SPOOL_SCHEMA)
            for trading_day, data in frames:
                if trading_day in content.groups:
                    raise FQDBError("Trading-day input repeated; merge batches before publishing")
                content.groups[trading_day] = []
                self._record_day(content, trading_day, data)
                for instrument, part in data.groupby("instrument", sort=True):
                    self._append(spool, str(instrument), trading_day, part)
                spool.commit()
                if progress:
                    progress(len(content.groups), None, f"validated {trading_day}")
            self._write_sorted(spool, path, content.groups)

    def _table(self, data):
        return pa.Table.from_pandas(data, schema=self.physical, preserve_index=False).replace_schema_metadata(
            None
        )

    def _append(self, spool, instrument, trading_day, part):
        table = self._table(part)
        keys = table.column(self.time_column).cast(pa.int64()).to_pylist()
        try:
            spool.executemany(
                "INSERT INTO unique_keys VALUES(?,?,?)",
                ((instrument, value, trading_day) for value in keys),
            )
        except sqlite3.IntegrityError:
            conflict = next(
                (
                    value
                    for value in keys
                    if spool.execute(
                        "SELECT source_day FROM unique_keys WHERE instrument=? AND stamp=? AND source_day!=?",
                        (instrument, value, trading_day),
                    ).fetchone()
                ),
                keys[0],
            )
            label = pd.Timestamp(conflict, unit="us", tz="UTC").tz_convert(self.schema.timezone).isoformat()
            raise FQDBError(
                f"Conflicting trading-date proof for duplicate primary key: {instrument}, "
                f"{self.time_column}={label}, trading_date={trading_day}"
            ) from None
        payload = pa.BufferOutputStream()
        with pa.ipc.new_stream(payload, table.schema) as ipc:
            ipc.write_table(table)
        spool.execute(
            "INSERT INTO parts VALUES(?,?,?,?,?,?)",
            (instrument, trading_day, keys[0], keys[-1], payload.getvalue().to_pybytes(), len(part)),
        )

    def _write_sorted(self, spool, path, groups):
        index = 0
        previous_instrument, previous_last = None, None
        cursor = spool.execute(
            "SELECT instrument,source_day,first_key,last_key,payload,nrows "
            "FROM parts ORDER BY instrument,first_key,source_day"
        )
        with pq.ParquetWriter(path, self.physical, compression="zstd") as writer:
            for instrument, trading_day, first_key, last_key, payload, count in cursor:
                if instrument == previous_instrument and first_key <= previous_last:
                    raise FQDBError(
                        f"Interleaving trading-day time ranges for instrument {instrument}; "
                        "verify source trading-date assignments"
                    )
                table = pa.ipc.open_stream(pa.BufferReader(payload)).read_all()
                group_count = (count + _ROW_GROUP_SIZE - 1) // _ROW_GROUP_SIZE
                groups[trading_day].extend(range(index, index + group_count))
                index += group_count
                writer.write_table(table, row_group_size=_ROW_GROUP_SIZE)
                previous_instrument, previous_last = instrument, last_key
            if groups:
                writer.add_key_value_metadata({MINUTE_DAY_INDEX: encode_minute_day_groups(groups)})
