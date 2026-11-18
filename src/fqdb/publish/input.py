"""Read and validate trading days from supported local publication inputs."""

from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq

from ..catalog.models import period_bounds
from ..util import FQDBError
from ..validation.quality import validate


def frames_by_day(source, schema, frequency, proof=None, period=None):
    """Stream validated trading days, preserving their explicit source dates."""
    if isinstance(source, pd.DataFrame):
        yield from _dataframe_days(source, schema, frequency, proof, period)
        return
    path = Path(source)
    reader = _csv_days if path.suffix.lower() == ".csv" else _parquet_days
    yield from reader(path, schema, frequency, proof, period)


def _dataframe_days(source, schema, frequency, proof, period):
    frame = source
    proof = proof if proof is not None else frame.attrs.get("trading_dates")
    if frequency == "1m" and proof is None and period and len(period) == 8:
        proof = [period_bounds("day", period)[0]] * len(frame)
    normalized = validate(frame, schema, frequency=frequency, trading_dates=proof)
    dates = (
        list(normalized.trading_date.astype(str)) if frequency == "1d" else normalized.attrs["trading_dates"]
    )
    labels = pd.Series(dates, index=normalized.index)
    for d in sorted(set(dates)):
        part = normalized.loc[labels.eq(d)].copy()
        part.attrs["trading_dates"] = [d] * len(part)
        yield d, part


def _csv_days(source, schema, frequency, proof, period):
    # The CSV reader is bounded too. A period is rescanned by day instead of
    # collecting a year of price rows in a giant DataFrame.
    labels, total = set(), 0
    for chunk in pd.read_csv(source, chunksize=65536, dtype={"instrument": "string"}):
        if frequency == "1d":
            days = pd.to_datetime(chunk.trading_date).dt.date.astype(str).tolist()
        elif isinstance(proof, (list, tuple)):
            days = list(proof[total : total + len(chunk)])
        elif period and len(period) == 8:
            days = [period_bounds("day", period)[0]] * len(chunk)
        else:
            raise FQDBError(
                "Minute CSV requires reliable external per-row trading_dates or an explicit single-day period"
            )
        if len(days) != len(chunk):
            raise FQDBError("Trading-day proof length does not match CSV rows")
        labels.update(str(d) for d in days)
        total += len(chunk)
    if isinstance(proof, (list, tuple)) and len(proof) != total:
        raise FQDBError("Trading-day proof length does not match CSV rows")
    for d in sorted(labels):
        chunks, position = [], 0
        for chunk in pd.read_csv(source, chunksize=65536, dtype={"instrument": "string"}):
            dates = (
                pd.to_datetime(chunk.trading_date).dt.date.astype(str).tolist()
                if frequency == "1d"
                else list(proof[position : position + len(chunk)])
                if isinstance(proof, (list, tuple))
                else [d] * len(chunk)
            )
            position += len(chunk)
            selected = chunk.loc[pd.Series(dates, index=chunk.index).astype(str).eq(d)]
            if not selected.empty:
                chunks.append(selected)
        if chunks:
            part = pd.concat(chunks, ignore_index=True)
            yield d, validate(part, schema, d, frequency=frequency, trading_dates=[d] * len(part))


def _parquet_days(source, schema, frequency, proof, period):
    parquet = pq.ParquetFile(source)
    groups = {}
    masks = {}
    if isinstance(proof, dict):
        flattened = [g for indices in proof.values() for g in indices]
        if sorted(flattened) != list(range(parquet.num_row_groups)):
            raise FQDBError("External trading-day index must cover every input row group exactly once")
        groups = {str(d): list(indices) for d, indices in proof.items()}
    elif frequency == "1d":
        for i in range(parquet.num_row_groups):
            labels = set()
            for batch in parquet.iter_batches(row_groups=[i], columns=["trading_date"], batch_size=65536):
                labels.update(str(v) for v in batch.column(0).to_pylist())
            for d in labels:
                groups.setdefault(d, []).append(i)
    elif isinstance(proof, (list, tuple)):
        position = 0
        for i in range(parquet.num_row_groups):
            count = parquet.metadata.row_group(i).num_rows
            labels = [str(d) for d in proof[position : position + count]]
            if len(labels) != count:
                raise FQDBError("Trading-day proof length does not match Parquet rows")
            for d in set(labels):
                groups.setdefault(d, []).append(i)
                masks[(d, i)] = [v == d for v in labels]
            position += count
        if position != len(proof):
            raise FQDBError("Trading-day proof length does not match Parquet rows")
    elif period and len(period) == 8:
        groups = {period_bounds("day", period)[0]: list(range(parquet.num_row_groups))}
    else:
        raise FQDBError(
            "Minute Parquet requires reliable external trading-day row-group index; natural dates are not inferred"
        )
    for d, indices in sorted(groups.items()):
        chunks = []
        for i in indices:
            position = 0
            for batch in parquet.iter_batches(row_groups=[i], batch_size=65536):
                chunk = batch.to_pandas()
                if (d, i) in masks:
                    chunk = chunk.loc[masks[(d, i)][position : position + len(chunk)]]
                elif frequency == "1d" and not isinstance(proof, dict):
                    chunk = chunk.loc[chunk.trading_date.astype(str).eq(d)]
                position += batch.num_rows
                if not chunk.empty:
                    chunks.append(chunk)
        if not chunks:
            raise FQDBError(f"External trading-day index contains no rows: {d}")
        part = pd.concat(chunks, ignore_index=True) if len(chunks) > 1 else chunks[0]
        yield d, validate(part, schema, d, frequency=frequency, trading_dates=[d] * len(part))
