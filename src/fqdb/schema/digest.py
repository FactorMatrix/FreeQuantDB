"""Stable identities for dataset contracts and canonical trading-day content."""

import hashlib
import json

import pyarrow as pa

from .market import arrow_schema


def contract_hash(schema):
    return hashlib.sha256(
        json.dumps(
            {k: v for k, v in schema.to_dict().items() if k not in ("calendars", "instruments", "sessions")},
            sort_keys=True,
        ).encode()
    ).hexdigest()


def logical_digest(frame, schema):
    """A compression-independent digest of one canonical trading day's records."""
    table = (
        pa.Table.from_pandas(frame, schema=arrow_schema(schema), preserve_index=False)
        .replace_schema_metadata(None)
        .combine_chunks()
    )
    stream = pa.BufferOutputStream()
    with pa.ipc.new_stream(stream, table.schema) as writer:
        writer.write_table(table)
    return hashlib.sha256(stream.getvalue().to_pybytes()).hexdigest()
